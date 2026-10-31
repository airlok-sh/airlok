#!/usr/bin/env python3
"""Считает запросы к сайтам по минутам из логов nginx и отправляет их в дашборд.

Запускается из healthcheck.sh раз в минуту. Лог каждого сайта — формат airlok из
lib.sh: «$msec $status $request_time $body_bytes_sent $request_method $uri
"$upstream_response_time"», без IP и query-строки. Путь из сервера уходит только шаблоном,
как у edge хостинга: /orders/:id. Время запроса — время ответа приложения, а если nginx
ответил сам — $request_time. Строки старых форматов считаются по $request_time, а самые
старые, без метода и пути, — в итоги без адресов.

Файл читается с места, где остановились в прошлый раз (по inode и смещению), поэтому
каждую строку считаем один раз, а ротацию logrotate замечаем по смене inode. Минута
уходит в дашборд, только когда закончилась; незаконченная копится в состоянии. Если
дашборд недоступен, минуты ждут отправки до 7 дней: повтор минуты дашборд перезаписывает.

Посещения — открытия страниц браузерами без cookies — берутся из второго лога сайта
(формат airlok_visit, JSON): те же правила, что у edge хостинга (internal/edge/visits.go в
backend). IP и браузер превращаются в хэш с солью московских суток, страна — по базе из
дашборда (internal/geoip). Прочитанный лог посещений сразу стирается: IP на сервере не копятся.
"""
import calendar
import fcntl
import glob
import hashlib
import ipaddress
import json
import math
import os
import re
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

LOG_GLOB = "/var/log/nginx/airlok-*.access.log"
LOG_NAME = re.compile(r"^airlok-([a-z][a-z0-9-]{1,30})\.access\.log$")
STATE_DIR = "/var/lib/airlok/metrics"
STATE_FILE = os.path.join(STATE_DIR, "state.json")
PENDING_FILE = os.path.join(STATE_DIR, "pending.json")
SALT_FILE = os.path.join(STATE_DIR, "salt.json")
GEOIP_FILE = os.path.join(STATE_DIR, "geoip.bin")
GEOIP_STATE = os.path.join(STATE_DIR, "geoip.json")
DASHBOARD_CONF = "/etc/airlok/dashboard.env"
MAX_BATCH = 500
# Дашборд принимает тело до 256 КБ, а минута с адресами весит до нескольких килобайт.
MAX_BATCH_BYTES = 200 << 10
# Дашборд принимает минуты не старше 7 дней; час запаса — чтобы минута на границе не
# оказалась старше уже у него и не отправила в отказ всю пачку.
MAX_AGE = 7 * 86400 - 3600
# За минуту у небольшого сайта столько запросов не бывает; больше — прореживаем для перцентилей.
MAX_SAMPLES = 5000
# Первый запуск не должен вычитывать многомесячный лог: дашборду нужны последние дни.
MAX_FIRST_READ = 8 << 20
# Адреса — как в edge хостинга (internal/edge/routes.go): статика и сканеры — служебными
# строками, идентификаторы в пути — :id, больше 50 адресов за минуту — в «прочее».
ROUTE_STATIC, ROUTE_UNKNOWN, ROUTE_OTHER = "<static>", "<unknown>", "<other>"
MAX_APP_ROUTES = 50
ROUTE_SAMPLES = 256
MAX_ROUTE_SEGMENTS = 6
MAX_ROUTE_LEN = 120
STATIC_PREFIXES = ("/_next/static/", "/_nuxt/", "/assets/", "/static/", "/build/")
STATIC_EXTS = {
    ".js", ".mjs", ".css", ".map", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".avif", ".ico",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp4", ".webm", ".mp3", ".wav",
}
SCANNER_PREFIXES = (
    "/wp-", "/wordpress", "/phpmyadmin", "/pma", "/myadmin", "/cgi-bin/", "/actuator", "/vendor/phpunit",
    "/boaform", "/hnap1", "/owa/", "/autodiscover", "/solr/", "/manager/html", "/server-status",
)
SCANNER_SUFFIXES = (".php", ".asp", ".aspx", ".jsp", ".cgi", ".sql", ".bak")
KNOWN_METHODS = {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
# nginx экранирует в логе байты вне ASCII как \xD0: возвращаем их, чтобы кириллица в пути осталась читаемой.
NGINX_ESCAPE = re.compile(rb"\\x([0-9A-Fa-f]{2})")
# Элемент $upstream_response_time: «0.004» или «-», если до приложения не дошло.
UPSTREAM_TIME = re.compile(r"\d+\.\d+")
# Посещения: лимиты за минуту — как у edge; сверх лимита — VISIT_OTHER.
MAX_VISITORS, MAX_VISIT_PAGES, MAX_VISIT_SOURCES = 5000, 100, 50
MAX_PAGE_LEN, MAX_SOURCE_LEN = 200, 100
VISIT_OTHER = "<other>"
MSK = 3 * 3600
GEOIP_EVERY = 86400
BOT = re.compile(
    r"bot|crawl|spider|slurp|preview|scan|monitor|uptime|lighthouse|headless|phantom|facebookexternalhit|whatsapp|"
    r"vkshare|yandex|curl|wget|python|go-http|java/|okhttp|axios|node-fetch|httpclient|libwww|ahrefs|semrush",
    re.IGNORECASE,
)


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def conf_value(path, key):
    value = ""
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.startswith(key + "="):
                    value = line[len(key) + 1:].strip()
    except OSError:
        pass
    return value


def new_minute():
    return {"requests": 0, "status_4xx": 0, "status_5xx": 0, "bytes_sent": 0, "ms": [], "routes": {}}


def id_like(seg):
    """Сегмент похож на идентификатор: число, дата, UUID, хеш, токен."""
    size = len(seg.encode())
    if len(seg) > 40:
        return True
    run = max_run = digits = 0
    hex_only = True
    for c in seg:
        if "0" <= c <= "9":
            digits += 1
            run += 1
            max_run = max(max_run, run)
        else:
            run = 0
            if not ("a" <= c <= "f" or "A" <= c <= "F" or c == "-"):
                hex_only = False
    return digits == size or max_run >= 3 or (hex_only and len(seg) >= 16 and digits > 0) or (len(seg) >= 20 and digits > 0)


def route_template(path):
    out = []
    for seg in path.split("/"):
        if not seg:
            continue
        if len(out) == MAX_ROUTE_SEGMENTS:
            out.append("…")
            break
        out.append(":id" if id_like(seg) else seg)
    if not out:
        return "/"
    route = "/" + "/".join(out)
    raw = route.encode()
    if len(raw) > MAX_ROUTE_LEN:
        route = raw[:MAX_ROUTE_LEN].decode("utf-8", "ignore") + "…"
    return route


def route_of(method, path):
    lower = path.lower()
    # Неразобранный запрос nginx пишет с путём «-».
    if not lower.startswith("/") or ("/." in lower and not lower.startswith("/.well-known/")) or ".php/" in lower \
            or lower.endswith(SCANNER_SUFFIXES) or lower.startswith(SCANNER_PREFIXES):
        return "*", ROUTE_UNKNOWN
    if lower.startswith(STATIC_PREFIXES) or os.path.splitext(lower)[1] in STATIC_EXTS:
        return "*", ROUTE_STATIC
    return (method if method in KNOWN_METHODS else "OTHER"), route_template(path)


def route_key(routes, method, path, status):
    """Адрес запроса в минуте. 404 по адресу, который в эту минуту ещё ничего другого не
    отвечал, — почти всегда перебор путей, а не ручка сайта."""
    m, r = route_of(method, path)
    key = m + " " + r
    if key in routes or m == "*":
        return key
    if status == 404:
        return "* " + ROUTE_UNKNOWN
    if len(routes) >= MAX_APP_ROUTES:
        return "* " + ROUTE_OTHER
    return key


def count(m, status, took, limit):
    m["requests"] += 1
    if 400 <= status < 500:
        m["status_4xx"] += 1
    elif status >= 500:
        m["status_5xx"] += 1
    if len(m["ms"]) < limit:
        m["ms"].append(round(took * 1000))


def unescape(path):
    return NGINX_ESCAPE.sub(lambda g: bytes([int(g.group(1), 16)]), path.encode()).decode("utf-8", "replace")


def upstream_time(value):
    """Сколько отвечало приложение, по $upstream_response_time. После повтора на другой апстрим
    или внутреннего редиректа там список через «, » и « : » — время складываем. None — nginx
    ответил сам."""
    times = UPSTREAM_TIME.findall(value)
    return sum(map(float, times)) if times else None


def add_line(minutes, line):
    # В пути бывают пробелы, но не кавычки — их nginx пишет как \x22: первая кавычка
    # открывает время приложения.
    head, quote, upstream = line.partition(' "')
    if quote and not upstream.endswith('"'):
        return
    parts = head.split(" ", 5)
    if len(parts) not in (4, 6):
        return
    try:
        ts, status, took, sent = float(parts[0]), int(parts[1]), float(parts[2]), int(parts[3])
    except ValueError:
        return
    app_took = upstream_time(upstream[:-1]) if quote else None
    if app_took is not None:
        took = app_took
    m = minutes.setdefault(str(int(ts) // 60 * 60), new_minute())
    count(m, status, took, MAX_SAMPLES)
    m["bytes_sent"] += sent
    if len(parts) == 6:
        routes = m.setdefault("routes", {})
        key = route_key(routes, parts[4], unescape(parts[5]), status)
        r = routes.setdefault(key, {"requests": 0, "status_4xx": 0, "status_5xx": 0, "ms": []})
        count(r, status, took, ROUTE_SAMPLES)


def read_from(path, offset, minutes, add=add_line):
    """Дочитывает файл с offset до последнего перевода строки, возвращает новое смещение."""
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n") + 1
    for line in data[:end].decode("utf-8", "replace").splitlines():
        add(minutes, line)
    return offset + end


class GeoIP:
    """База стран из дашборда (формат internal/geoip): начала диапазонов по возрастанию и
    коды стран; диапазон длится до начала следующего, "--" — неизвестно."""

    def __init__(self, data):
        if not data.startswith(b"AIRGEO1\n"):
            raise ValueError("not a geoip file")
        self.data, pos, self.tables = data, 8, []
        for size in (4, 16):
            if len(data) < pos + 4:
                raise ValueError("geoip file is damaged")
            (n,) = struct.unpack_from(">I", data, pos)
            pos += 4
            self.tables.append((pos, n, size, pos + n * size))
            pos += n * (size + 2)
        if pos != len(data):
            raise ValueError("geoip file is damaged")

    def country(self, ip):
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return ""
        if addr.version == 6 and addr.ipv4_mapped:
            addr = addr.ipv4_mapped
        starts, n, size, codes = self.tables[0 if addr.version == 4 else 1]
        key, lo, hi = addr.packed, 0, n
        # Первый диапазон, который начинается после адреса; нужный — перед ним.
        while lo < hi:
            mid = (lo + hi) // 2
            if self.data[starts + mid * size:starts + (mid + 1) * size] > key:
                hi = mid
            else:
                lo = mid + 1
        if lo == 0:
            return ""
        code = self.data[codes + (lo - 1) * 2:codes + lo * 2].decode()
        return "" if code == "--" else code


def load_geoip():
    try:
        with open(GEOIP_FILE, "rb") as f:
            return GeoIP(f.read())
    except (OSError, ValueError):
        return None


def refresh_geoip(now):
    """Раз в сутки спрашивает у дашборда, не обновилась ли база стран (меняется раз в месяц)."""
    st = load_json(GEOIP_STATE, {})
    if now - st.get("checked", 0) < GEOIP_EVERY:
        return
    url = conf_value(DASHBOARD_CONF, "AIRLOK_API_URL").rstrip("/")
    token = conf_value(DASHBOARD_CONF, "AIRLOK_AGENT_TOKEN")
    if not url or not token:
        return
    headers = {"Authorization": "Bearer " + token}
    if st.get("etag") and os.path.exists(GEOIP_FILE):
        headers["If-None-Match"] = st["etag"]
    # Неудачу повторим через час, а не через сутки.
    st["checked"] = now - GEOIP_EVERY + 3600
    try:
        with urllib.request.urlopen(urllib.request.Request(url + "/agent/v1/geoip", headers=headers), timeout=30) as resp:
            data = resp.read(64 << 20)
            GeoIP(data)
            tmp = GEOIP_FILE + ".tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, GEOIP_FILE)
            st["etag"] = resp.headers.get("ETag", "")
        st["checked"] = now
    except urllib.error.HTTPError as e:
        if e.code in (304, 404):
            st["checked"] = now
    except (urllib.error.URLError, OSError, ValueError):
        pass
    save_json(GEOIP_STATE, st)


def msk_day(ts):
    return time.strftime("%Y-%m-%d", time.gmtime(ts + MSK))


def salt_for(salts, day):
    """Соль хэшей посетителей на московские сутки: живёт только на этом сервере."""
    if day not in salts:
        salts[day] = os.urandom(16).hex()
    return bytes.fromhex(salts[day])


def visitor_id(salt, app, ip, ua):
    try:
        packed = ipaddress.ip_address(ip).packed
    except ValueError:
        packed = b""
    h = hashlib.sha256(salt + app.encode() + packed + b"\0" + ua.encode("utf-8", "replace"))
    return int.from_bytes(h.digest()[:8], "big", signed=True)


def page_view(v, status, ua):
    """Запрос открыл страницу в браузере человека: как pageView у edge хостинга."""
    if not ua or BOT.search(ua):
        return False
    if "prefetch" in v.get("sp", "") or v.get("pu") == "prefetch" or v.get("nrp") == "1":
        return False
    if v.get("rsc") == "1":
        return status == 200
    if v.get("dest", "") not in ("", "document"):
        return False
    return status == 304 or (200 <= status < 300 and v.get("ct", "").startswith("text/html"))


def clip(s, n):
    raw = s.encode()
    if len(raw) <= n:
        return s
    return raw[:n].decode("utf-8", "ignore") + "…"


def device(ua):
    if "iPad" in ua or "Tablet" in ua or ("Android" in ua and "Mobile" not in ua):
        return "tablet"
    if "Mobi" in ua or "iPhone" in ua or "Android" in ua:
        return "mobile"
    return "desktop"


def visit_source(query, ref, host):
    """Откуда пришли: utm_source или сайт по Referer, "" — прямой заход; None — переход
    внутри самого сайта, это не новый заход."""
    utm = urllib.parse.parse_qs(query).get("utm_source", [""])[0].strip().lower()
    if utm:
        return clip(utm, MAX_SOURCE_LEN)
    try:
        frm = (urllib.parse.urlsplit(ref).hostname or "") if ref else ""
    except ValueError:
        frm = ""
    if not frm:
        return ""
    frm = frm.lower().removeprefix("www.")
    if frm == host.lower().removeprefix("www."):
        return None
    return clip(frm, MAX_SOURCE_LEN)


def bump(counts, key, limit):
    if key not in counts and len(counts) >= limit:
        key = VISIT_OTHER
    counts[key] = counts.get(key, 0) + 1


def add_visit(minutes, line, app, salts, geo):
    try:
        v = json.loads(line)
        ts, status = float(v["t"]), int(v["s"])
    except (ValueError, KeyError, TypeError):
        return
    ua = v.get("ua", "")
    if not page_view(v, status, ua):
        return
    m = minutes.setdefault(str(int(ts) // 60 * 60), new_minute())
    vs = m.setdefault("visits", {"views": 0, "visitors": {}, "pages": {}, "sources": {}, "devices": {}, "countries": {}})
    vs["views"] += 1
    ip = v.get("ip", "")
    if len(vs["visitors"]) < MAX_VISITORS:
        vs["visitors"][str(visitor_id(salt_for(salts, msk_day(ts)), app, ip, ua))] = 1
    path, _, query = v.get("uri", "").partition("?")
    bump(vs["pages"], clip(urllib.parse.unquote(path), MAX_PAGE_LEN), MAX_VISIT_PAGES)
    src = visit_source(query, v.get("ref", ""), v.get("host", ""))
    if src is not None:
        bump(vs["sources"], src, MAX_VISIT_SOURCES)
    dev = device(ua)
    vs["devices"][dev] = vs["devices"].get(dev, 0) + 1
    country = geo.country(ip) if geo else ""
    vs["countries"][country] = vs["countries"].get(country, 0) + 1


def read_visits(path, s, minutes, app, salts, geo):
    """Дочитывает лог посещений и стирает прочитанное, если nginx за это время ничего не дописал."""
    try:
        st = os.stat(path)
    except OSError:
        return
    inode, offset = s.get("visits_inode"), s.get("visits_offset", 0)
    add = lambda mins, line: add_visit(mins, line, app, salts, geo)  # noqa: E731
    try:
        if inode is not None and inode != st.st_ino:
            rotated = path + ".1"
            if os.path.exists(rotated) and os.stat(rotated).st_ino == inode:
                read_from(rotated, offset, minutes, add)
            offset = 0
        elif st.st_size < offset:
            offset = 0
        offset = read_from(path, offset, minutes, add)
        if os.stat(path).st_size == offset:
            os.truncate(path, 0)
            offset = 0
    except OSError:
        return
    s["visits_inode"], s["visits_offset"] = st.st_ino, offset


def visits_bucket(vs):
    return {
        "views": vs["views"], "visitors": [int(k) for k in vs["visitors"]], "pages": vs["pages"],
        "sources": vs["sources"], "devices": vs["devices"], "countries": vs["countries"],
    }


def percentile(sorted_ms, p):
    if not sorted_ms:
        return 0
    k = max(0, min(len(sorted_ms) - 1, math.ceil(p / 100 * len(sorted_ms)) - 1))
    return sorted_ms[k]


def collect(state, now, salts=None, geo=None):
    """Читает новые строки всех логов, возвращает законченные минуты."""
    salts = {} if salts is None else salts
    done = []
    seen = set()
    current = int(now) // 60 * 60
    for path in sorted(glob.glob(LOG_GLOB)):
        match = LOG_NAME.match(os.path.basename(path))
        if not match:
            continue
        app = match.group(1)
        seen.add(app)
        try:
            st = os.stat(path)
        except OSError:
            continue
        s = state.setdefault(app, {})
        minutes = s.get("open", {})
        inode, offset = s.get("inode"), s.get("offset", 0)
        try:
            if inode is None:
                offset = max(0, st.st_size - MAX_FIRST_READ)
                if offset:
                    # Начинаем с границы строки: кусок строки дал бы мусор.
                    with open(path, "rb") as f:
                        f.seek(offset)
                        offset += len(f.readline())
            elif inode != st.st_ino:
                # logrotate переименовал файл: дочитываем старый (.1, сжимается позже), новый — с начала.
                rotated = path + ".1"
                if os.path.exists(rotated) and os.stat(rotated).st_ino == inode:
                    read_from(rotated, offset, minutes)
                offset = 0
            elif st.st_size < offset:
                offset = 0
            offset = read_from(path, offset, minutes)
        except OSError:
            continue
        s["inode"], s["offset"] = st.st_ino, offset
        read_visits(path[:-len(".access.log")] + ".visits.log", s, minutes, app, salts, geo)
        last = s.get("last_sent", 0)
        still_open = {}
        for key, m in minutes.items():
            minute = int(key)
            if minute >= current:
                still_open[key] = m
            elif minute > last and m["requests"] > 0:
                ms = sorted(m["ms"])
                routes = []
                for key, r in m.get("routes", {}).items():
                    method, route = key.split(" ", 1)
                    rms = sorted(r["ms"])
                    routes.append({
                        "method": method, "route": route, "requests": r["requests"],
                        "status_4xx": r["status_4xx"], "status_5xx": r["status_5xx"],
                        "p50_ms": percentile(rms, 50), "p95_ms": percentile(rms, 95),
                    })
                bucket = {
                    "app": app,
                    "minute": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(minute)),
                    "requests": m["requests"],
                    "status_4xx": m["status_4xx"],
                    "status_5xx": m["status_5xx"],
                    "bytes_sent": m["bytes_sent"],
                    "p50_ms": percentile(ms, 50),
                    "p95_ms": percentile(ms, 95),
                    "routes": routes,
                }
                if m.get("visits", {}).get("views"):
                    bucket["visits"] = visits_bucket(m["visits"])
                done.append(bucket)
                s["last_sent"] = max(s.get("last_sent", 0), minute)
            # Запоздавшие строки уже отправленной минуты отбрасываем: повтор минуты
            # перезаписал бы её в дашборде неполными данными.
        s["open"] = still_open
    for app in list(state):
        if app not in seen:
            del state[app]
    return done


def send(buckets):
    url = conf_value(DASHBOARD_CONF, "AIRLOK_API_URL").rstrip("/")
    token = conf_value(DASHBOARD_CONF, "AIRLOK_AGENT_TOKEN")
    if not url or not token:
        return None
    req = urllib.request.Request(
        url + "/agent/v1/metrics",
        data=json.dumps({"buckets": buckets}).encode(),
        method="POST",
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError):
        return 0


def next_batch(pending):
    """Сколько минут из начала очереди уходит одной пачкой: по числу и по размеру тела."""
    size = 0
    for i, b in enumerate(pending[:MAX_BATCH]):
        size += len(json.dumps(b)) + 1
        if size > MAX_BATCH_BYTES and i > 0:
            return i
    return min(len(pending), MAX_BATCH)


def minute_ts(bucket):
    return calendar.timegm(time.strptime(bucket["minute"], "%Y-%m-%dT%H:%M:%SZ"))


def main():
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    with open(os.path.join(STATE_DIR, ".lock"), "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return
        now = time.time()
        refresh_geoip(now)
        state = load_json(STATE_FILE, {})
        pending = load_json(PENDING_FILE, [])
        salts = load_json(SALT_FILE, {})
        pending.extend(collect(state, now, salts, load_geoip()))
        pending = [b for b in pending if now - minute_ts(b) <= MAX_AGE]
        # Соль живёт сутки: вчерашняя нужна для запоздавших строк, старше — стирается, и
        # хэши прошлых дней ни с чем уже не связать.
        keep = {msk_day(now), msk_day(now - 86400)}
        save_json(SALT_FILE, {d: v for d, v in salts.items() if d in keep})
        # Сначала очередь, потом смещения: при сбое между ними строки прочитаются ещё раз —
        # метрики минуты дашборд перезапишет, посещения прибавит повторно, но такое редко.
        # Наоборот — минуты бы потерялись.
        save_json(PENDING_FILE, pending)
        save_json(STATE_FILE, state)
        if not os.path.exists(DASHBOARD_CONF):
            return
        while pending:
            n = next_batch(pending)
            code = send(pending[:n])
            if code is None:
                break
            if 200 <= code < 300 or code in (400, 413):
                # 400/413 — дашборд отверг сами данные: повтор не поможет, а очередь вставать не должна.
                if code in (400, 413):
                    print("дашборд не принял метрики (код %d), пачка отброшена" % code, file=sys.stderr)
                pending = pending[n:]
                save_json(PENDING_FILE, pending)
                continue
            break


if __name__ == "__main__":
    main()
