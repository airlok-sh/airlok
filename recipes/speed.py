"""Замер скорости у посетителей сайтов сервера — как на хостинге (internal/speed и
internal/edge/speed.go в backend).

Когда замер приложения включён в дашборде, nginx его сайта дописывает в страницы скрипт
speed.js (копия internal/edge/speed.js), сам его отдаёт и сам принимает маячки /_airlok/v —
приложение их не видит. Маячок пишется в лог airlok-<имя>.speed.log без IP: время, путь
страницы, показатели и браузер, по которому отличаются телефоны и боты. metrics.py раз в
минуту складывает лог в гистограммы с теми же границами, что у edge, отправляет их вместе
с минутой и стирает прочитанное.

Какие приложения замерять, дашборд отвечает на отправку метрик; apply() приводит к этому
сниппеты nginx, а если nginx не принял конфиг — возвращает файлы как были.
"""
import bisect
import glob
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import urllib.parse

NGINX_DIR = "/etc/nginx"
SHARE_DIR = "/usr/share/airlok"
LOG_DIR = "/var/log/nginx"
SCRIPT_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "speed.js")
SITE_NAME = re.compile(r"^airlok-([a-z][a-z0-9-]{1,30})\.conf$")
SNIPPET_NAME = re.compile(r"^airlok-speed\.([a-z][a-z0-9-]{1,30})\.conf$")

TIME_BOUNDS = [50, 100, 150, 200, 250, 300, 400, 500, 600, 800, 1000, 1200, 1500, 1800, 2000,
               2500, 3000, 3500, 4000, 5000, 6000, 8000, 10000, 15000, 20000]
CLS_BOUNDS = [10, 25, 50, 75, 100, 150, 200, 250, 300, 400, 500, 750, 1000]
# Как internal/speed.Metrics: имя, параметр маячка, больше чего значение — мусор, границы корзин.
METRICS = (
    ("ttfb", "t", 120_000, TIME_BOUNDS),
    ("fcp", "f", 120_000, TIME_BOUNDS),
    ("lcp", "l", 120_000, TIME_BOUNDS),
    ("inp", "i", 120_000, TIME_BOUNDS),
    ("cls", "c", 100_000, CLS_BOUNDS),
)
MAX_PAGE_LEN = 200
# За минуту у одного приложения, как у edge: бот со случайными путями не раздует ни состояние, ни базу.
MAX_PAGES = 50
MAX_SAMPLES = 10_000
OTHER = "<other>"
BEACON_METHODS = ("GET", "POST")


def parse_beacon(query):
    """Путь страницы и показатели из запроса скрипта, как speed.ParseBeacon; None — ни одного
    годного показателя. Показателя нет — -1."""
    q = urllib.parse.parse_qs(query, keep_blank_values=True)
    values, ok = [], False
    for _, key, limit, _ in METRICS:
        try:
            n = float(q.get(key, [""])[0])
        except ValueError:
            n = math.nan
        if math.isnan(n) or n < 0 or n > limit:
            values.append(-1)
            continue
        # Округление половины — от нуля, как math.Round в Go.
        values.append(int(math.floor(n + 0.5)))
        ok = True
    if not ok:
        return None
    page = q.get("p", [""])[0]
    if not page.startswith("/"):
        page = "/"
    page = re.split(r"[?#]", page, maxsplit=1)[0]
    raw = page.encode()
    if len(raw) > MAX_PAGE_LEN:
        page = raw[:MAX_PAGE_LEN].decode("utf-8", "ignore")
    return page, values


def device_of(ua):
    """Как speed.DeviceOf: планшеты — к телефонам, сеть и процессор у них ближе к телефону."""
    if any(s in ua for s in ("Mobi", "Android", "iPhone", "iPad", "Tablet")):
        return "mobile"
    return "desktop"


def parse_line(line, is_bot):
    """Строка лога маячков (формат airlok_speed): (время, страница, устройство, показатели) или None."""
    try:
        v = json.loads(line)
        ts, status = float(v["t"]), int(v["s"])
    except (ValueError, KeyError, TypeError):
        return None
    ua = v.get("ua", "")
    if status != 204 or v.get("m") not in BEACON_METHODS or not ua or is_bot(ua):
        return None
    parsed = parse_beacon(v.get("q", ""))
    if parsed is None:
        return None
    page, values = parsed
    return ts, page, device_of(ua), values


def new_bucket(page, device):
    b = {"page": page, "device": device, "samples": 0}
    for name, _, _, bounds in METRICS:
        b[name] = [0] * (len(bounds) + 1)
    return b


def add(sp, page, device, values):
    """Добавляет замер в минуту приложения; False — лимит минуты исчерпан."""
    if sp["samples"] >= MAX_SAMPLES:
        return False
    buckets = sp["buckets"]
    key = page + "\t" + device
    if key not in buckets and len(buckets) >= MAX_PAGES:
        page, key = OTHER, OTHER + "\t" + device
    b = buckets.get(key)
    if b is None:
        b = buckets[key] = new_bucket(page, device)
    b["samples"] += 1
    for (name, _, _, bounds), v in zip(METRICS, values):
        if v >= 0:
            b[name][bisect.bisect_left(bounds, v)] += 1
    sp["samples"] += 1
    return True


def new_minute():
    return {"samples": 0, "buckets": {}}


def buckets(sp):
    return list(sp["buckets"].values())


def script_tag():
    """По отпечатку в адресе браузер берёт новую версию скрипта сразу после обновления агента."""
    with open(SCRIPT_SRC, "rb") as f:
        tag = hashlib.sha256(f.read()).hexdigest()[:8]
    return '<script src="/_airlok/speed.js?v=%s" async></script>' % tag


def http_conf():
    return """# Замер скорости у посетителей (recipes/speed.py). Страницу приложение отдаёт nginx несжатой:
# в сжатую скрипт не дописать, сжимает уже сам nginx.
map $http_accept $airlok_speed_ae {
    "~text/html" "";
    default $http_accept_encoding;
}
# На одну открытую страницу посетитель шлёт один маячок, поток с одного адреса — бот или
# подделка: такие маячки в лог не пишутся.
limit_req_zone $binary_remote_addr zone=airlok_speed:1m rate=5r/s;
map $limit_req_status $airlok_speed_log {
    "~^REJECTED" 0;
    default 1;
}
log_format airlok_speed escape=json '{"t":$msec,"s":$status,"m":"$request_method","q":"$args","ua":"$http_user_agent"}';
"""


def snippet_conf(app, tag):
    return """# Замер скорости у посетителей: включается в дашборде, файл ведёт recipes/speed.py.
proxy_set_header Accept-Encoding $airlok_speed_ae;
gzip on;
sub_filter '</head>' '%(tag)s</head>';
sub_filter_once on;
location = /_airlok/speed.js {
    alias %(share)s/speed.js;
    types { }
    default_type application/javascript;
    charset utf-8;
    gzip_types application/javascript;
    access_log off;
    include snippets/airlok-security-headers.conf;
    add_header Cache-Control "public, max-age=31536000, immutable" always;
}
location = /_airlok/v {
    limit_req zone=airlok_speed burst=20 nodelay;
    limit_req_status 429;
    access_log %(log)s/airlok-%(app)s.speed.log airlok_speed if=$airlok_speed_log;
    include snippets/airlok-security-headers.conf;
    add_header Cache-Control no-store always;
    # Не return: он выполняется раньше limit_req, и предел бы не действовал.
    root /nonexistent;
    try_files /_ =204;
}
""" % {"tag": tag, "share": SHARE_DIR, "log": LOG_DIR, "app": app}


def include_line(app):
    # [f] делает путь шаблоном: без файла nginx такой include пропускает, а не падает.
    return "        include snippets/airlok-speed.%s.con[f];\n" % app


SITE_LOCATION = "\n    location / {\n"


def with_include(text, app):
    """Конфиг сайта со строкой include внутри location / — как в шаблоне app-deploy.sh. Сайты,
    опубликованные старым агентом, получают её здесь, без новой публикации. None — конфиг
    не похож на шаблон Airlok, его не трогаем."""
    line = include_line(app)
    if line in text:
        return text
    if text.count(SITE_LOCATION) != 1:
        return None
    return text.replace(SITE_LOCATION, SITE_LOCATION + line)


def read_file(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return None


class Changes:
    """Записанные файлы и их прежнее содержимое: если nginx не примет конфиг, всё возвращается."""

    def __init__(self):
        self.done = []

    def write(self, path, data, mode=0o644):
        old = read_file(path)
        if old == data:
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        self.done.append((path, old))

    def remove(self, path):
        old = read_file(path)
        if old is None:
            return
        os.remove(path)
        self.done.append((path, old))

    def revert(self):
        for path, old in reversed(self.done):
            if old is None:
                os.remove(path)
            else:
                with open(path, "wb") as f:
                    f.write(old)
        self.done = []


def apply(apps, run=subprocess.run):
    """Включает замер у приложений apps и выключает у остальных. True — nginx перечитал
    конфиг, False — изменений нет или nginx их не принял (тогда всё возвращено)."""
    sites = {}
    for path in glob.glob(os.path.join(NGINX_DIR, "sites-available", "airlok-*.conf")):
        m = SITE_NAME.match(os.path.basename(path))
        if m:
            sites[m.group(1)] = path
    want = {a for a in apps if a in sites}
    snippets = os.path.join(NGINX_DIR, "snippets")
    ch = Changes()
    if want:
        ch.write(os.path.join(NGINX_DIR, "conf.d", "airlok-speed.conf"), http_conf().encode())
        with open(SCRIPT_SRC, "rb") as f:
            ch.write(os.path.join(SHARE_DIR, "speed.js"), f.read())
        tag = script_tag()
    for app in sorted(want):
        ch.write(os.path.join(snippets, "airlok-speed.%s.conf" % app), snippet_conf(app, tag).encode())
        try:
            patched = with_include((read_file(sites[app]) or b"").decode(), app)
        except UnicodeDecodeError:
            patched = None
        if patched is None:
            print("speed: конфиг сайта %s не похож на шаблон Airlok, замер заработает после публикации" % app, file=sys.stderr)
        else:
            ch.write(sites[app], patched.encode())
    for path in glob.glob(os.path.join(snippets, "airlok-speed.*.conf")):
        m = SNIPPET_NAME.match(os.path.basename(path))
        if m and m.group(1) not in want:
            ch.remove(path)
    if not ch.done:
        return False
    test = run(["nginx", "-t", "-q"], capture_output=True, text=True)
    if test.returncode != 0:
        ch.revert()
        print("speed: nginx не принял конфиг замера, файлы возвращены: %s" % test.stderr.strip()[:500], file=sys.stderr)
        return False
    run(["systemctl", "reload", "nginx"], capture_output=True, text=True)
    return True
