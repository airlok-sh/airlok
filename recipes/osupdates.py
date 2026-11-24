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


def lock_pid(locks, dev, inode):
    """PID того, кто держит блокировку файла, по тексту /proc/locks. Ждущих (->) пропускаем."""
    key = "%02x:%02x:%d" % (os.major(dev), os.minor(dev), inode)
    for line in locks.splitlines():
        f = line.split()
        if len(f) >= 6 and f[1] == "POSIX" and f[5] == key and f[4].isdigit():
            return int(f[4])
    return None


# Юнит systemd надёжнее имени процесса: unattended-upgrades под apt-daily-upgrade.service
# называется по-разному в разных версиях Ubuntu.
UNITS = (
    (re.compile(r"apt-daily-upgrade\.service|unattended-upgrades\.service"), "ставятся автообновления Ubuntu"),
    (re.compile(r"apt-daily\.service"), "Ubuntu проверяет обновления"),
    (re.compile(r"cloud-(init|config|final)\.service"), "хостер ещё донастраивает сервер (cloud-init)"),
    (re.compile(r"packagekit\.service"), "PackageKit проверяет обновления"),
    (re.compile(r"airlok"), "идёт другая задача Airlok"),
    (re.compile(r"session-\d+\.scope"), "кто-то ставит пакеты вручную по SSH"),
)
COMMS = {"unattended-upgr": "ставятся автообновления Ubuntu", "cloud-init": "хостер ещё донастраивает сервер (cloud-init)"}


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


def describe(pid, proc="/proc"):
    """Что за процесс держит apt, простыми словами."""
    comm = read(os.path.join(proc, str(pid), "comm")).strip()
    cgroup = read(os.path.join(proc, str(pid), "cgroup"))
    what = next((text for unit, text in UNITS if unit.search(cgroup)), None) or COMMS.get(comm)
    name = f"{comm}, PID {pid}" if comm else f"PID {pid}"
    return f"{what} ({name})" if what else name


def holder(paths, proc="/proc"):
    """Кто держит первую занятую блокировку: описание или None, если узнать не вышло."""
    locks = read(os.path.join(proc, "locks"))
    for path in paths:
        try:
            st = os.stat(path)
        except OSError:
            continue
        pid = lock_pid(locks, st.st_dev, st.st_ino)
        if pid:
            return describe(pid, proc)
    return None


def wait_lock(seconds, paths=LOCKS, poll=LOCK_POLL, proc="/proc"):
    deadline = time.monotonic() + seconds
    told = False
    while held := busy(paths):
        if time.monotonic() >= deadline:
            return False
        # Очередь обычно сменяется: за apt-daily сразу идёт apt-daily-upgrade.
        who = holder(held, proc)
        if not told or (who and who != told):
            print(f"  система занята: {who}, ждём…" if who else "  система занята другой установкой (часто это автообновления), ждём…", flush=True)
            told = who or True
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
