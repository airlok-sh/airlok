#!/usr/bin/env python3
"""Задачи по расписанию (capabilities.cron) и команда перед запуском (run.release) на своём сервере.

    cron.py sync ИМЯ              таймеры systemd по shared/cron.json приложения
    cron.py run ИМЯ_НОМЕР         задача по расписанию; её запускает служба airlok-cron@
    cron.py release ИМЯ КОМАНДА   команда перед запуском новой версии; её запускает app-deploy.sh

Задача и команда идут разовым контейнером приложения (docker compose run): с той же
изоляцией, переменными и базой, что у него, но со своим таймаутом. Расписание — как на
хостинге airlok: пять полей cron, время московское. Вывод уходит в журнал службы и, если
сервер подключён к дашборду, в логи приложения с пометкой задачи.
"""
import collections
import datetime
import fcntl
import glob
import json
import os
import re
import subprocess
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from logs import DASHBOARD_CONF, MAX_LINE, now_rfc3339, spool  # noqa: E402
from metrics import conf_value  # noqa: E402

APPS_ROOT = "/srv/apps"
UNIT_DIR = "/etc/systemd/system"
NAME = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
NUMBER = re.compile(r"^[0-9]+$")
MAX_JOBS = 10
RELEASE_TIMEOUT = 600
# Строк одного запуска в логах дашборда: причина ошибки обычно в последних.
MAX_LINES = 1000
TZ = "Europe/Moscow"
# В России нет перехода на летнее время, поэтому хватает постоянного сдвига, как на хостинге.
MSK = datetime.timezone(datetime.timedelta(hours=3))
# Таймер запускает задачу в первую секунду минуты. Позже — значит, systemd догоняет минуту,
# пропущенную, пока шёл прошлый запуск: хостинг такой запуск пропускает.
LATE_SEC = 30

ALIASES = {"@hourly": "0 * * * *", "@daily": "0 0 * * *", "@midnight": "0 0 * * *",
           "@weekly": "0 0 * * 0", "@monthly": "0 0 1 * *"}
BOUNDS = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
FIELDS = ("минута", "час", "день месяца", "месяц", "день недели")
WEEKDAYS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")

Schedule = collections.namedtuple("Schedule", "minute hour dom month dow dom_star dow_star")

SERVICE = """[Unit]
Description=Задача по расписанию airlok %i
After=docker.service

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 {script} run %i
# Задачу по timeout_min останавливает сам cron.py; это запас на случай, если завис docker.
TimeoutStartSec=3h
ExecStopPost=-/bin/sh -c 'docker rm -f airlok-cron-%i >/dev/null 2>&1'
"""


class CronError(Exception):
    pass


def parse_field(field, lo, hi):
    values = set()
    for part in field.split(","):
        rng, has_step, step = part.partition("/")
        if has_step and (not NUMBER.match(step) or int(step) < 1):
            raise CronError(f"шаг «{step}»")
        if rng == "*":
            start, end = lo, hi
        elif "-" in rng:
            a, _, b = rng.partition("-")
            if not NUMBER.match(a) or not NUMBER.match(b) or int(a) > int(b):
                raise CronError(f"диапазон «{rng}»")
            start, end = int(a), int(b)
        else:
            if not NUMBER.match(rng):
                raise CronError(f"значение «{rng}»")
            start = int(rng)
            end = hi if has_step else start
        if start < lo or end > hi:
            raise CronError(f"«{part}» вне {lo}–{hi}")
        values.update(range(start, end + 1, int(step) if has_step else 1))
    return values


def parse_schedule(expr):
    """Расписание из пяти полей с теми же правилами, что у хостинга (internal/cronexpr):
    *, списки, диапазоны, шаг и @hourly, @daily, @weekly, @monthly."""
    expr = expr.strip() if isinstance(expr, str) else ""
    expr = ALIASES.get(expr, expr)
    fields = expr.split()
    if len(fields) != 5:
        raise CronError(f"расписание «{expr}»: нужно пять полей — минута, час, день месяца, месяц, день недели")
    sets = []
    for i, field in enumerate(fields):
        try:
            sets.append(parse_field(field, *BOUNDS[i]))
        except CronError as e:
            raise CronError(f"расписание «{expr}», {FIELDS[i]}: {e}") from None
    # 7 в дне недели — тоже воскресенье.
    if 7 in sets[4]:
        sets[4] = (sets[4] - {7}) | {0}
    return Schedule(*sets, dom_star=fields[2] == "*", dow_star=fields[4] == "*")


def matches(s, t):
    """Подходит ли минута t под расписание — как Schedule.Match у хостинга."""
    if t.minute not in s.minute or t.hour not in s.hour or t.month not in s.month:
        return False
    dom, dow = t.day in s.dom, (t.weekday() + 1) % 7 in s.dow
    if s.dom_star and s.dow_star:
        return True
    if s.dom_star:
        return dow
    if s.dow_star:
        return dom
    return dom or dow


def on_calendar(s):
    """Строки OnCalendar= для таймера systemd. Если ограничены и день месяца, и день недели,
    в cron подходит любой из них: у таймера тогда две строки, и он срабатывает по каждой."""
    def part(values, lo, hi):
        return "*" if values == set(range(lo, hi + 1)) else ",".join(map(str, sorted(values)))

    when = f"{part(s.hour, 0, 23)}:{part(s.minute, 0, 59)}:00 {TZ}"
    month = part(s.month, 1, 12)
    by_dom = f"*-{month}-{part(s.dom, 1, 31)} {when}"
    by_dow = f"{','.join(WEEKDAYS[d] for d in sorted(s.dow))} *-{month}-* {when}"
    if s.dom_star and s.dow_star:
        return [f"*-{month}-* {when}"]
    if s.dow_star:
        return [by_dom]
    if s.dom_star:
        return [by_dow]
    return [by_dom, by_dow]


def one_line(value, limit):
    return isinstance(value, str) and 0 < len(value.strip()) <= limit and not any(c in value for c in "\r\n\x00")


def parse_jobs(value):
    """capabilities.cron из airlok.yml: задачи с именем, расписанием, командой и таймаутом."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise CronError("capabilities.cron: список задач с полями schedule и command")
    if len(value) > MAX_JOBS:
        raise CronError(f"capabilities.cron: не больше {MAX_JOBS} задач")
    jobs = []
    for i, job in enumerate(value):
        if not isinstance(job, dict) or set(job) - {"name", "schedule", "command", "timeout_min"}:
            raise CronError(f"cron[{i}]: поля name, schedule, command и timeout_min")
        name = job.get("name") or f"задача {i + 1}"
        if not one_line(name, 64):
            raise CronError(f"cron[{i}].name — одна строка до 64 символов")
        if not one_line(job.get("command"), 500):
            raise CronError(f"cron «{name}»: command — одна строка до 500 символов")
        timeout = job.get("timeout_min", 10)
        if not isinstance(timeout, int) or isinstance(timeout, bool) or not 1 <= timeout <= 60:
            raise CronError(f"cron «{name}»: timeout_min — от 1 до 60 минут")
        try:
            parse_schedule(job.get("schedule"))
        except CronError as e:
            raise CronError(f"cron «{name}»: {e}") from None
        jobs.append({"name": name.strip(), "schedule": job["schedule"].strip(),
                     "command": job["command"].strip(), "timeout_min": timeout})
    return jobs


def timer_unit(app, index, job):
    lines = ["[Unit]", f"Description=Задача {index + 1} приложения {app}", "", "[Timer]"]
    lines += [f"OnCalendar={c}" for c in on_calendar(parse_schedule(job["schedule"]))]
    lines += ["AccuracySec=1s", "", "[Install]", "WantedBy=timers.target", ""]
    return "\n".join(lines)


def write_if_changed(path, text):
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == text:
                return False
    except OSError:
        pass
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    return True


def load_jobs(app):
    try:
        with open(os.path.join(APPS_ROOT, app, "shared", "cron.json"), encoding="utf-8") as f:
            return parse_jobs(json.load(f))
    except FileNotFoundError:
        return []
    except ValueError as e:
        raise CronError(f"shared/cron.json приложения {app}: {e}") from None


def systemctl(*args):
    """systemctl без «Created symlink…» в выводе выкатки; причину неудачи — в текст ошибки."""
    r = subprocess.run(["systemctl", *args], stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if r.returncode:
        raise CronError(f"systemctl {' '.join(args)}: {r.stderr.strip() or f'код {r.returncode}'}")


def sync(app):
    """Таймеры задач приложения: новые включаются, изменённые перезапускаются, убранные
    из airlok.yml выключаются. Возвращает число задач."""
    jobs = load_jobs(app)
    reload = write_if_changed(os.path.join(UNIT_DIR, "airlok-cron@.service"),
                              SERVICE.format(script=os.path.abspath(__file__)))
    want = {f"airlok-cron@{app}_{i}.timer": timer_unit(app, i, job) for i, job in enumerate(jobs)}
    # «_» в имени приложения не бывает, поэтому маска не заденет таймеры приложения shop-2 у shop.
    stale = [os.path.basename(p) for p in glob.glob(os.path.join(UNIT_DIR, f"airlok-cron@{app}_*.timer"))
             if os.path.basename(p) not in want]
    if stale:
        subprocess.run(["systemctl", "disable", "--now", *stale], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for unit in stale:
            os.remove(os.path.join(UNIT_DIR, unit))
        reload = True
    changed = [unit for unit, text in want.items() if write_if_changed(os.path.join(UNIT_DIR, unit), text)]
    if reload or changed:
        systemctl("daemon-reload")
    if want:
        systemctl("enable", "--now", *want)
    if changed:
        systemctl("restart", *changed)
    return len(jobs)


def duration(sec):
    sec = round(sec)
    return f"{sec // 60} мин {sec % 60} с" if sec >= 60 else f"{sec} с"


def remove(container):
    try:
        subprocess.run(["docker", "rm", "-f", container], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
    except (OSError, subprocess.SubprocessError):
        pass


def oneoff(app, container, command, timeout):
    """Команда в разовом контейнере приложения. Вывод печатается по ходу; возвращает код
    выхода (None — прервана по таймауту), последние MAX_LINES строк со временем и сколько
    строк до них не вошло."""
    app_dir = os.path.join(APPS_ROOT, app)
    remove(container)  # остаток прерванного запуска
    args = ["docker", "compose", "-f", os.path.join(app_dir, "compose.yml"), "--project-directory", app_dir,
            "run", "--rm", "--no-deps", "-T", "--name", container, "--entrypoint", "sh", "app", "-c", command]
    timed_out = threading.Event()

    def stop():
        timed_out.set()
        remove(container)

    timer = threading.Timer(timeout, stop)
    lines, total = collections.deque(maxlen=MAX_LINES), 0
    with subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as proc:
        timer.start()
        try:
            for raw in proc.stdout:
                text = raw.decode("utf-8", "replace").rstrip("\r\n")
                print(text, flush=True)
                # Пустые строки в логи не идут, как на хостинге.
                if text.strip():
                    lines.append((now_rfc3339(), text[:MAX_LINE]))
                    total += 1
            code = proc.wait()
        finally:
            timer.cancel()
    return (None if timed_out.is_set() and code != 0 else code), list(lines), total - len(lines)


def report(app, prefix, started, first, lines, skipped, summary):
    """Вывод запуска — в логи приложения в дашборде, с пометкой и итогом, как на хостинге."""
    print(summary, flush=True)
    if not conf_value(DASHBOARD_CONF, "AIRLOK_API_URL"):
        return
    entries = [{"ts": started, "stream": "system", "line": first}]
    if skipped:
        entries.append({"ts": started, "stream": "system", "line": f"{prefix}первые строки вывода пропущены: {skipped}"})
    entries += [{"ts": ts, "stream": "stdout", "line": prefix + text} for ts, text in lines]
    entries.append({"ts": now_rfc3339(), "stream": "system", "line": summary})
    spool(app, entries)


def deployed(app):
    app_dir = os.path.join(APPS_ROOT, app)
    return os.path.isfile(os.path.join(app_dir, "compose.yml")) and os.path.islink(os.path.join(app_dir, "current"))


def wait_deploy(app, limit):
    """Дождаться конца идущей выкатки: на её время compose.yml уже указывает на новую версию,
    которая ещё может не подняться и откатиться."""
    deadline = time.monotonic() + limit
    with open(f"/run/airlok-deploy-{app}.lock", "w") as lock:
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(2)


def run(instance, now=None):
    app, _, index = instance.rpartition("_")
    if not NAME.match(app) or not NUMBER.match(index):
        raise CronError(f"некорректная задача {instance}")
    jobs = load_jobs(app)
    if int(index) >= len(jobs):
        return 0  # задачу убрали из airlok.yml, а таймер ещё не выключен
    job = jobs[int(index)]
    label, limit = f"Задача «{job['name']}»", job["timeout_min"] * 60
    # now нет у запуска руками из терминала: он идёт сразу, без сверки с расписанием.
    if now is not None and (now.second >= LATE_SEC or not matches(parse_schedule(job["schedule"]), now)):
        print(f"{label}: запуск пропущен — прошлый шёл дольше, чем до следующего по расписанию", flush=True)
        return 0
    started, at = now_rfc3339(), time.monotonic()
    first = f"{label} запущена ({job['schedule']})"
    if not deployed(app):
        report(app, "", started, first, [], 0, f"{label} не запущена: приложение ещё не выкачено")
        return 1
    if not wait_deploy(app, limit):
        report(app, "", started, first, [], 0, f"{label} не запущена: выкатка идёт дольше {job['timeout_min']} мин")
        return 1
    # Выкатка, которой задача дождалась, могла убрать её из airlok.yml или поменять команду.
    jobs = load_jobs(app)
    if int(index) >= len(jobs) or jobs[int(index)]["name"] != job["name"]:
        return 0
    job = jobs[int(index)]
    code, lines, skipped = oneoff(app, f"airlok-cron-{instance}", job["command"], limit)
    took = duration(time.monotonic() - at)
    if code is None:
        summary = f"{label} прервана: дольше {job['timeout_min']} мин"
    elif code:
        summary = f"{label} завершилась с ошибкой (код {code}) за {took}"
    else:
        summary = f"{label} выполнена за {took}"
    report(app, f"[{job['name']}] ", started, first, lines, skipped, summary)
    return 0 if code == 0 else 1


def release(app, command):
    if not one_line(command, 1000):
        raise CronError("run.release: одна строка до 1000 символов")
    started, at = now_rfc3339(), time.monotonic()
    code, lines, skipped = oneoff(app, f"airlok-release-{app}", command, RELEASE_TIMEOUT)
    if code is None:
        summary = f"Команда перед запуском прервана: дольше {RELEASE_TIMEOUT // 60} мин — новая версия не запущена"
    elif code:
        summary = f"Команда перед запуском завершилась с ошибкой (код {code}) — новая версия не запущена, причина в строках выше"
    else:
        summary = f"Команда перед запуском выполнена за {duration(time.monotonic() - at)}"
    report(app, "[перед запуском] ", started, f"Команда перед запуском новой версии: {command}", lines, skipped, summary)
    return 0 if code == 0 else 1


def main():
    args = sys.argv[1:]
    try:
        if len(args) == 2 and args[0] == "sync" and NAME.match(args[1]):
            print(sync(args[1]))
            return 0
        if len(args) == 2 and args[0] == "run":
            # INVOCATION_ID задаёт systemd: запуск по таймеру, а не руками.
            return run(args[1], datetime.datetime.now(MSK) if os.environ.get("INVOCATION_ID") else None)
        if len(args) == 3 and args[0] == "release" and NAME.match(args[1]):
            return release(args[1], args[2])
    except (OSError, CronError, subprocess.SubprocessError) as e:
        sys.exit(f"Ошибка: {e}")
    sys.exit("использование: cron.py sync ИМЯ | run ИМЯ_НОМЕР | release ИМЯ КОМАНДА")


if __name__ == "__main__":
    sys.exit(main())
