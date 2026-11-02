#!/usr/bin/env python3
"""Обновления системы: сколько их, сколько из них исправлений безопасности, нужна ли перезагрузка.

  osupdates.py report КАТАЛОГ    — сводка в JSON для отметки агента, из кэша в каталоге
  osupdates.py count             — сколько обновлений поставит os-upgrade.sh
  osupdates.py wait-lock СЕКУНД  — дождаться, пока apt освободится

Подсчёт занимает секунды процессора и на сервере с 1 ГБ памяти заметен, а отметка идёт каждую
минуту: поэтому считаем заново, только когда сменились списки пакетов или установленные пакеты.
"""
import fcntl
import json
import os
import re
import subprocess
import sys
import time

CACHE = "os-updates.json"
MAX_AGE = 24 * 60 * 60
REBOOT_FLAG = "/var/run/reboot-required"
# Что меняется после apt update и после установки пакетов.
SOURCES = ("/var/lib/apt/lists", "/var/lib/apt/periodic/update-success-stamp", "/var/lib/dpkg/status")
# apt и dpkg берут их через fcntl, поэтому flock(1) занятую блокировку не увидит.
LOCKS = ("/var/lib/dpkg/lock-frontend", "/var/lib/dpkg/lock", "/var/lib/apt/lists/lock", "/var/cache/apt/archives/lock")
EXIT_BUSY = 3
LOCK_POLL = 5

# Inst пакет [старая] (новая Ubuntu:26.04/resolute-updates, Ubuntu:26.04/resolute-security [amd64]) [зависимый:amd64 ]
# У нового пакета, например ядра, старой версии в квадратных скобках нет.
INST = re.compile(r"^Inst \S+(?: \[[^\]]*\])? \(\S+ ([^)]*)\)")


def parse_simulation(text):
    """Вывод apt-get -s: всего пакетов к установке и сколько из них из репозитория безопасности."""
    total = security = 0
    for line in text.splitlines():
        m = INST.match(line)
        if not m:
            continue
        total += 1
        if "-security" in m.group(1):
            security += 1
    return {"total": total, "security": security}


# Тот же набор, что ставит os-upgrade.sh: отложенные фазами обновления сюда не попадают, иначе
# предложение висело бы вечно и после установки.
def simulate():
    out = subprocess.run(["apt-get", "-s", "-o", "Debug::NoLocking=1", "--with-new-pkgs", "upgrade"], capture_output=True, text=True,
                         timeout=90, check=True, env={**os.environ, "LC_ALL": "C"})
    return parse_simulation(out.stdout)


def mtime(path):
    try:
        return os.stat(path).st_mtime
    except OSError:
        return 0


def stale(cache, sources, now):
    at = mtime(cache)
    return at == 0 or now - at > MAX_AGE or any(mtime(s) >= at for s in sources)


def counts(state_dir, count=simulate, sources=SOURCES, now=None):
    """Сводка из кэша, а если он устарел — заново. Нет ни того, ни другого — исключение."""
    cache = os.path.join(state_dir, CACHE)
    if not stale(cache, sources, time.time() if now is None else now):
        with open(cache, encoding="utf-8") as f:
            return json.load(f)
    # Время подсчёта берём до него: изменившееся за эти секунды посчитается в следующий раз.
    started = time.time()
    value = count()
    tmp = cache + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(value, f)
    os.utime(tmp, (started, started))
    os.replace(tmp, cache)
    return value


def report(state_dir):
    # Перезагрузку смотрим всегда: её флаг ничего не стоит и снимается без смены пакетов.
    try:
        return {**counts(state_dir), "reboot_required": os.path.exists(REBOOT_FLAG)}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}


def busy(paths):
    """Какие блокировки сейчас заняты. Свои сразу отпускаем: мы только смотрим."""
    held = []
    for path in paths:
        try:
            fd = os.open(path, os.O_RDWR)
        except OSError:
            continue
        try:
            fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            held.append(path)
        finally:
            os.close(fd)
    return held


def wait_lock(seconds, paths=LOCKS, poll=LOCK_POLL):
    deadline = time.monotonic() + seconds
    told = False
    while busy(paths):
        if time.monotonic() >= deadline:
            return False
        if not told:
            print("  система занята другой установкой (часто это автообновления), ждём…", flush=True)
            told = True
        time.sleep(poll)
    return True


def main(argv):
    if len(argv) == 3 and argv[1] == "report":
        print(json.dumps(report(argv[2])))
        return 0
    if len(argv) == 2 and argv[1] == "count":
        print(simulate()["total"])
        return 0
    if len(argv) == 3 and argv[1] == "wait-lock":
        return 0 if wait_lock(int(argv[2])) else EXIT_BUSY
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
