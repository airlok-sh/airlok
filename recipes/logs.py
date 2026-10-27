#!/usr/bin/env python3
"""Отправляет вывод приложений сервера в дашборд: короткая история и живой поток.

Работает службой airlok-logs. Какие приложения собирать, дашборд отвечает на каждую
отправку: у приложения в настройках есть выключатель, а без него ничего не читается.
Вывод берётся через docker logs каждого контейнера приложения — stdout и stderr
по отдельности, как на хостинге. Пока логи приложения открыты в дашборде, пачки уходят
каждые 200 мс, иначе — раз в 5 секунд.

Приложение, которое пишет больше 200 строк в секунду, прореживается: лишние строки
отбрасываются, а в логи уходит строка о том, сколько пропущено. Если дашборд недоступен,
строки копятся в памяти до 5 МБ, дальше выбрасываются самые старые. Где остановились
в каждом контейнере, запоминается на диске: после перезапуска службы строки не повторяются.
"""
import calendar
import collections
import http.client
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from metrics import conf_value, load_json, save_json  # noqa: E402

DASHBOARD_CONF = "/etc/airlok/dashboard.env"
STATE_DIR = "/var/lib/airlok/logs"
STATE_FILE = os.path.join(STATE_DIR, "state.json")
NAME = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
CONTAINER = re.compile(r"^[0-9a-f]{12,64}$")
TIMESTAMP = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?Z$")
LIVE_EVERY = 0.2
IDLE_EVERY = 5.0
SCAN_EVERY = 5.0
SAVE_EVERY = 10.0
RATE = 200
BURST = 1000
# Дашборд всё равно обрезает строку до 4 КБ: длиннее незачем и отправлять.
MAX_LINE = 4 << 10
MAX_BUFFER_BYTES = 5 << 20
MAX_BATCH = 5000
MAX_BATCH_BYTES = 1 << 20
# Контейнер, которого служба ещё не видела: его начало вывода, а не весь лог за месяцы.
FIRST_TAIL = 200
# Где остановились в контейнере, помним неделю: столько дашборд и хранит историю.
KEEP_POSITION_NS = 7 * 86400 * 10**9
# Старый дашборд ручки логов не знает: спрашивать его чаще незачем.
OLD_DASHBOARD_WAIT = 600


def now_rfc3339():
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".%09dZ" % (int(t * 1e9) % 10**9)


def parse_ts(ts):
    """Время docker logs --timestamps в наносекундах от эпохи; None — не время."""
    m = TIMESTAMP.match(ts)
    if not m:
        return None
    sec = calendar.timegm(time.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S"))
    return sec * 10**9 + int((m.group(2) or "").ljust(9, "0"))


def parse_line(raw):
    """Строка docker logs --timestamps: (время строкой, наносекунды, текст) или None."""
    ts, _, text = raw.rstrip("\r\n").partition(" ")
    ns = parse_ts(ts)
    if ns is None:
        return None
    return ts, ns, text


def docker_since(ns):
    return "%d.%09d" % (ns // 10**9, ns % 10**9)


class Buffer:
    """Строки, ждущие отправки: общий лимит по размеру и ограничение строк в секунду на приложение."""

    def __init__(self, clock=time.monotonic):
        self.lock = threading.Lock()
        self.items = collections.deque()
        self.size = 0
        self.clock = clock
        self.tokens = {}
        self.skipped = collections.Counter()
        self.lost = collections.Counter()

    @staticmethod
    def weight(e):
        return len(e["line"].encode()) + 64

    def _take(self, app):
        now = self.clock()
        tokens, at = self.tokens.get(app, (BURST, now))
        tokens = min(BURST, tokens + (now - at) * RATE)
        if tokens < 1:
            self.tokens[app] = (tokens, now)
            return False
        self.tokens[app] = (tokens - 1, now)
        return True

    def _push(self, e):
        self.items.append(e)
        self.size += self.weight(e)
        while self.size > MAX_BUFFER_BYTES:
            old = self.items.popleft()
            self.size -= self.weight(old)
            self.lost[old["app"]] += 1

    def add(self, app, ts, stream, line):
        with self.lock:
            if not self._take(app):
                self.skipped[app] += 1
                return
            self._push({"app": app, "ts": ts, "stream": stream, "line": line[:MAX_LINE]})

    def __len__(self):
        with self.lock:
            return len(self.items)

    def take_batch(self):
        with self.lock:
            ts = now_rfc3339()
            for app, n in self.skipped.items():
                self._push({"app": app, "ts": ts, "stream": "system",
                            "line": "пропущено строк вывода: %d — приложение пишет больше %d строк в секунду" % (n, RATE)})
            for app, n in self.lost.items():
                self._push({"app": app, "ts": ts, "stream": "system",
                            "line": "дашборд был недоступен, не сохранилось строк вывода: %d" % n})
            self.skipped.clear()
            self.lost.clear()
            batch, size = [], 0
            while self.items and len(batch) < MAX_BATCH:
                w = self.weight(self.items[0])
                if batch and size + w > MAX_BATCH_BYTES:
                    break
                e = self.items.popleft()
                self.size -= w
                size += w
                batch.append(e)
            return batch

    def requeue(self, batch):
        with self.lock:
            for e in reversed(batch):
                self.items.appendleft(e)
                self.size += self.weight(e)
            while self.size > MAX_BUFFER_BYTES:
                old = self.items.pop()
                self.size -= self.weight(old)
                self.lost[old["app"]] += 1


class Follower:
    """docker logs -f одного контейнера. after — наносекунды последней уже прочитанной строки.

    stdout и stderr читаются параллельно и обгоняют друг друга, поэтому граница у каждого своя:
    общая отбросила бы строку stderr, которую прочитали позже более новой строки stdout."""

    def __init__(self, app, cid, service, after, buf):
        self.app, self.cid, self.service, self.buf = app, cid, service, buf
        self.after = {"stdout": after, "stderr": after}
        self.lock = threading.Lock()
        args = ["docker", "logs", "-f", "--timestamps"]
        args += ["--since", docker_since(after)] if after else ["--tail", str(FIRST_TAIL)]
        self.proc = subprocess.Popen(args + [cid], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.threads = [threading.Thread(target=self.read, args=(pipe, stream), daemon=True)
                        for pipe, stream in ((self.proc.stdout, "stdout"), (self.proc.stderr, "stderr"))]
        for t in self.threads:
            t.start()

    def read(self, pipe, stream):
        for raw in iter(lambda: pipe.readline(MAX_LINE * 4), b""):
            parsed = parse_line(raw.decode("utf-8", "replace"))
            if parsed is None:
                continue
            ts, ns, text = parsed
            with self.lock:
                # --since включает строку на самой границе: её уже отправили.
                if ns <= self.after[stream]:
                    continue
                self.after[stream] = ns
            if self.service != "app":
                text = "[%s] %s" % (self.service, text)
            self.buf.add(self.app, ts, stream, text)

    def position(self):
        """С какого места читать после перезапуска. Строки отстающего потока между его границей
        и этой при остановке теряются; так бывает только при спаме сразу в оба потока."""
        with self.lock:
            return max(self.after.values())

    def alive(self):
        return self.proc.poll() is None

    def stop(self):
        if self.alive():
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def running_containers():
    """Контейнеры compose-проектов: {id: (приложение, сервис)}. Проект compose — имя приложения."""
    try:
        out = subprocess.run(
            ["docker", "ps", "--no-trunc", "--filter", "label=com.docker.compose.project",
             "--format", '{{.ID}}\t{{.Label "com.docker.compose.project"}}\t{{.Label "com.docker.compose.service"}}'],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    found = {}
    for row in out.splitlines():
        parts = row.split("\t")
        if len(parts) == 3 and CONTAINER.match(parts[0]) and NAME.match(parts[1]):
            found[parts[0]] = (parts[1], parts[2] or "app")
    return found


def reconcile(apps, running, followers, positions, start, off_since=None):
    """Следит ровно за работающими контейнерами приложений из apps: лишних останавливает,
    новых и упавших (контейнер перезапустился) запускает с места, где остановились.

    off_since — текущее время, когда дашборд уже ответил, какие приложения собирать: у
    контейнеров с выключенными логами место сдвигается на него, чтобы после включения не
    досылать вывод за время, когда сбор был выключен. До первого ответа место не трогаем:
    иначе перезапуск службы терял бы вывод за время перезапуска."""
    for cid in list(followers):
        f = followers[cid]
        if cid not in running or running[cid][0] not in apps or not f.alive():
            f.stop()
            positions[cid] = f.position() or positions.get(cid, 0)
            del followers[cid]
    for cid, (app, service) in running.items():
        if app not in apps:
            if off_since is not None:
                positions[cid] = off_since
        elif cid not in followers:
            followers[cid] = start(app, cid, service, positions.get(cid, 0))


def prune_positions(positions, now_ns):
    return {cid: ns for cid, ns in positions.items() if now_ns - ns < KEEP_POSITION_NS}


class Dashboard:
    """Одно соединение на все отправки: при живом потоке их пять в секунду."""

    def __init__(self, url, token):
        u = urllib.parse.urlsplit(url)
        self.https = u.scheme == "https"
        self.host = u.netloc
        self.token = token
        self.conn = None

    def post(self, entries):
        """Код ответа и тело; 0 — дашборд недоступен."""
        body = json.dumps({"entries": entries}, ensure_ascii=False).encode()
        headers = {"Authorization": "Bearer " + self.token, "Content-Type": "application/json"}
        for attempt in range(2):
            if self.conn is None:
                cls = http.client.HTTPSConnection if self.https else http.client.HTTPConnection
                self.conn = cls(self.host, timeout=15)
            try:
                self.conn.request("POST", "/agent/v1/logs", body, headers)
                resp = self.conn.getresponse()
                return resp.status, resp.read()
            except (OSError, http.client.HTTPException):
                # Соединение могло закрыться, пока простаивало: один повтор на новом.
                self.conn.close()
                self.conn = None
        return 0, b""


def parse_response(data):
    """Приложения с включёнными логами и те из них, что сейчас открыты в дашборде."""
    try:
        resp = json.loads(data)
        apps = {a for a in resp.get("apps", []) if isinstance(a, str) and NAME.match(a)}
        live = {a for a in resp.get("live", []) if a in apps}
    except (ValueError, AttributeError, TypeError):
        return None
    return apps, live


def main():
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    positions = load_json(STATE_FILE, {})
    buf = Buffer()
    followers = {}
    apps, live = set(), set()
    # Дашборд ещё не ответил, какие приложения собирать: пустой apps значит «не знаем».
    apps_known = False
    dash = None
    next_scan = next_send = next_save = 0.0
    backoff = 0.0

    def start(app, cid, service, after):
        return Follower(app, cid, service, after, buf)

    while not stop.is_set():
        now = time.monotonic()
        if dash is None:
            url, token = conf_value(DASHBOARD_CONF, "AIRLOK_API_URL"), conf_value(DASHBOARD_CONF, "AIRLOK_AGENT_TOKEN")
            if not url or not token:
                stop.wait(60)
                continue
            dash = Dashboard(url.rstrip("/"), token)
        if now >= next_scan:
            running = running_containers()
            if running is not None:
                reconcile(apps, running, followers, positions, start, time.time_ns() if apps_known else None)
            next_scan = now + SCAN_EVERY
        if now >= next_save:
            for cid, f in followers.items():
                positions[cid] = f.position() or positions.get(cid, 0)
            positions = prune_positions(positions, time.time_ns())
            save_json(STATE_FILE, positions)
            next_save = now + SAVE_EVERY
        if now >= next_send:
            batch = buf.take_batch()
            code, data = dash.post(batch)
            parsed = parse_response(data) if 200 <= code < 300 else None
            if parsed is not None:
                if parsed[0] != apps:
                    next_scan = 0.0
                apps, live = parsed
                apps_known = True
                backoff = 0.0
                every = LIVE_EVERY if live else IDLE_EVERY
                # Накопилось больше пачки — досылаем сразу, не ждём следующего тика.
                next_send = now + (0 if len(buf) >= MAX_BATCH else every)
            elif code == 404:
                apps, live = set(), set()
                apps_known = True
                next_scan = 0.0
                next_send = now + OLD_DASHBOARD_WAIT
            elif code in (400, 413):
                print("дашборд не принял логи (код %d), пачка отброшена" % code, file=sys.stderr)
                next_send = now + IDLE_EVERY
            else:
                if code == 401:
                    # Токен сменили в /etc/airlok/dashboard.env: перечитаем его.
                    dash = None
                buf.requeue(batch)
                backoff = min(max(backoff * 2, IDLE_EVERY), 60.0)
                next_send = now + backoff
        stop.wait(max(0.0, min(next_send, next_scan) - time.monotonic()))

    for cid, f in followers.items():
        f.stop()
        positions[cid] = f.position() or positions.get(cid, 0)
    save_json(STATE_FILE, positions)
    # Перезапуск службы при обновлении не должен терять то, что уже прочитано.
    if dash is not None and len(buf):
        dash.post(buf.take_batch())


if __name__ == "__main__":
    main()
