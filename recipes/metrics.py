#!/usr/bin/env python3
"""Считает запросы к сайтам по минутам из логов nginx и отправляет их в дашборд.

Запускается из healthcheck.sh раз в минуту. Лог каждого сайта — формат airlok из
lib.sh: «$msec $status $request_time $body_bytes_sent», без IP и адресов страниц.

Файл читается с места, где остановились в прошлый раз (по inode и смещению), поэтому
каждую строку считаем один раз, а ротацию logrotate замечаем по смене inode. Минута
уходит в дашборд, только когда закончилась; незаконченная копится в состоянии. Если
дашборд недоступен, минуты ждут отправки до 7 дней: повтор минуты дашборд перезаписывает.
"""
import calendar
import fcntl
import glob
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.request

LOG_GLOB = "/var/log/nginx/airlok-*.access.log"
LOG_NAME = re.compile(r"^airlok-([a-z][a-z0-9-]{1,30})\.access\.log$")
STATE_DIR = "/var/lib/airlok/metrics"
STATE_FILE = os.path.join(STATE_DIR, "state.json")
PENDING_FILE = os.path.join(STATE_DIR, "pending.json")
DASHBOARD_CONF = "/etc/airlok/dashboard.env"
MAX_BATCH = 500
# Дашборд принимает минуты не старше 7 дней; час запаса — чтобы минута на границе не
# оказалась старше уже у него и не отправила в отказ всю пачку.
MAX_AGE = 7 * 86400 - 3600
# За минуту у небольшого сайта столько запросов не бывает; больше — прореживаем для перцентилей.
MAX_SAMPLES = 5000
# Первый запуск не должен вычитывать многомесячный лог: дашборду нужны последние дни.
MAX_FIRST_READ = 8 << 20


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
    return {"requests": 0, "status_4xx": 0, "status_5xx": 0, "bytes_sent": 0, "ms": []}


def add_line(minutes, line):
    parts = line.split()
    if len(parts) != 4:
        return
    try:
        ts, status, took, sent = float(parts[0]), int(parts[1]), float(parts[2]), int(parts[3])
    except ValueError:
        return
    m = minutes.setdefault(str(int(ts) // 60 * 60), new_minute())
    m["requests"] += 1
    if 400 <= status < 500:
        m["status_4xx"] += 1
    elif status >= 500:
        m["status_5xx"] += 1
    m["bytes_sent"] += sent
    if len(m["ms"]) < MAX_SAMPLES:
        m["ms"].append(round(took * 1000))


def read_from(path, offset, minutes):
    """Дочитывает файл с offset до последнего перевода строки, возвращает новое смещение."""
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read()
    end = data.rfind(b"\n") + 1
    for line in data[:end].decode("utf-8", "replace").splitlines():
        add_line(minutes, line)
    return offset + end


def percentile(sorted_ms, p):
    if not sorted_ms:
        return 0
    k = max(0, min(len(sorted_ms) - 1, math.ceil(p / 100 * len(sorted_ms)) - 1))
    return sorted_ms[k]


def collect(state, now):
    """Читает новые строки всех логов, возвращает законченные минуты."""
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
        last = s.get("last_sent", 0)
        still_open = {}
        for key, m in minutes.items():
            minute = int(key)
            if minute >= current:
                still_open[key] = m
            elif minute > last and m["requests"] > 0:
                ms = sorted(m["ms"])
                done.append({
                    "app": app,
                    "minute": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(minute)),
                    "requests": m["requests"],
                    "status_4xx": m["status_4xx"],
                    "status_5xx": m["status_5xx"],
                    "bytes_sent": m["bytes_sent"],
                    "p50_ms": percentile(ms, 50),
                    "p95_ms": percentile(ms, 95),
                })
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
        state = load_json(STATE_FILE, {})
        pending = load_json(PENDING_FILE, [])
        pending.extend(collect(state, now))
        pending = [b for b in pending if now - minute_ts(b) <= MAX_AGE]
        # Сначала очередь, потом смещения: при сбое между ними строки прочитаются ещё раз,
        # а повтор той же минуты в дашборде безвреден. Наоборот — минуты бы потерялись.
        save_json(PENDING_FILE, pending)
        save_json(STATE_FILE, state)
        if not os.path.exists(DASHBOARD_CONF):
            return
        while pending:
            batch = pending[:MAX_BATCH]
            code = send(batch)
            if code is None:
                break
            if 200 <= code < 300 or code in (400, 413):
                # 400/413 — дашборд отверг сами данные: повтор не поможет, а очередь вставать не должна.
                if code in (400, 413):
                    print("дашборд не принял метрики (код %d), пачка отброшена" % code, file=sys.stderr)
                pending = pending[MAX_BATCH:]
                save_json(PENDING_FILE, pending)
                continue
            break


if __name__ == "__main__":
    main()
