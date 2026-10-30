#!/usr/bin/env python3
"""SSH-ключи устройств из дашборда — в authorized_keys пользователя сервера (root, если не выбран другой).

    sshkeys.py < ключи.json
    sshkeys.py wait-login НАЧАЛО СЕКУНД  — дождаться входа по ключу устройства
    sshkeys.py report КАТАЛОГ           — опись ключей и новые входы для отметки в дашборд
    sshkeys.py report-sent КАТАЛОГ      — отметка дошла: запомнить, что уже отправлено

Агент передаёт поле ssh_keys своей команды: {"user", "keys": [{"id", "key"}]} — пользователя и полный
список ключей устройств, которым открыт этот сервер. У остальных пользователей строки устройств
убираются: так ключи переезжают, когда в дашборде выбирают другого. Свои строки агент помечает комментарием
airlok-device:<id> и заменяет только их: ключи, положенные на сервер иначе, остаются. Строка
без пометки с тем же ключом, что у устройства, уходит: иначе, отключив устройство в дашборде,
человек думал бы, что закрыл ему доступ.

Последний ключ пользователя агент не уберёт, если войти им по паролю нельзя: так сервер остался
бы без входа, и вернуть его можно было бы только через консоль хостера. В чужой домашний каталог
агент пишет от root, поэтому ссылки там не проходит: иначе ~/.ssh, указывающий на /root/.ssh,
подсунул бы ему чужой файл.

wait-login нужен, когда вход по паролю закрывают из дашборда: настройки закрепляются, только если
после закрытия на сервер вошли новым соединением по ключу одного из устройств. Вход любым ключом
не годится — на сервер ходят и скрипты, а проверить нужно ключ человека. Код выхода 0 — вход
был, 2 — не дождались, 1 — проверить нечем.

report собирает для отметки в дашборд пользователей, которым можно класть ключи, опись их
authorized_keys (ключ, комментарий, устройство)
и входы по SSH из журнала sshd с места, где остановился прошлый раз. Опись уходит, когда она
изменилась, и раз в час на всякий случай; прошлые входы при первом запуске не досылаются.
Где остановились, report пишет в КАТАЛОГ/ssh-report.next, а report-sent переносит это в
состояние, только когда отметка дошла: иначе входы из неотправленной отметки потерялись бы.
"""
import base64
import datetime
import hashlib
import json
import os
import pwd
import re
import struct
import subprocess
import sys
import tempfile
import time
from typing import NamedTuple

ROOT_HOME = "/root"
MAX_KEYS = 50
MAX_KEY_CHARS = 16 << 10
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TYPES = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "ssh-rsa",
         "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com")
KEY = re.compile(r"^(" + "|".join(re.escape(t) for t in TYPES) + r") ([A-Za-z0-9+/]+={0,2})$")
MARK = re.compile(r" airlok-device:[0-9a-f-]{36}$")
ACCEPTED = re.compile(r"^Accepted publickey for (\S+) from \S+ port \d+ ssh2: \S+ (SHA256:[A-Za-z0-9+/]+)$")
USER = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
MAX_ACCOUNTS = 20
LOGIN = re.compile(r"^Accepted (publickey|password|keyboard-interactive/pam) for (\S+) from (\S+) port (\d+) ssh2"
                   r"(?:: \S+ (SHA256:[A-Za-z0-9+/]+))?$")
MAX_INVENTORY = 100
MAX_LOGINS = 500
MAX_COMMENT = 100
# Опись уходит и без изменений раз в час: дашборд мог обновиться и ещё её не знать.
INVENTORY_EVERY = 3600
# Ключ в строке authorized_keys: перед ним бывают опции (from=…, command="…").
LINE_KEY = re.compile(r"(?:^|\s)(" + "|".join(re.escape(t) for t in TYPES) + r"|ssh-dss)\s+([A-Za-z0-9+/]+={0,2})(?=\s|$)")


class Failed(Exception):
    pass


class Account(NamedTuple):
    name: str
    uid: int
    gid: int
    home: str


ROOT = Account("root", 0, 0, ROOT_HOME)


def accounts():
    """Кому можно класть ключи: root и люди — uid от 1000, оболочка для входа и домашний каталог."""
    out = []
    for p in pwd.getpwall():
        if (p.pw_uid != 0 and not 1000 <= p.pw_uid < 60000) or p.pw_shell.endswith(("nologin", "false")):
            continue
        if USER.fullmatch(p.pw_name) and os.path.isdir(p.pw_dir):
            out.append(Account(p.pw_name, p.pw_uid, p.pw_gid, p.pw_dir))
    out.sort(key=lambda a: (a.uid != 0, a.name))
    return out[:MAX_ACCOUNTS] or [ROOT]


def parse_request(req):
    """Список (id, тип, base64) из команды; тип внутри ключа должен совпадать с заявленным."""
    if not isinstance(req, dict) or not isinstance(req.get("keys"), list):
        raise Failed("в команде нет списка ключей")
    if len(req["keys"]) > MAX_KEYS:
        raise Failed(f"ключей больше {MAX_KEYS}")
    out, seen = [], set()
    for k in req["keys"]:
        if not isinstance(k, dict) or not isinstance(k.get("id"), str) or not UUID.fullmatch(k["id"]):
            raise Failed("у ключа нет корректного id")
        key = k.get("key")
        m = KEY.fullmatch(key) if isinstance(key, str) and len(key) <= MAX_KEY_CHARS else None
        if not m or blob_type(m.group(2)) != m.group(1):
            raise Failed(f"ключ устройства {k['id']} не похож на открытый SSH-ключ")
        if k["id"] in seen:
            raise Failed(f"устройство {k['id']} в списке дважды")
        seen.add(k["id"])
        out.append((k["id"], m.group(1), m.group(2)))
    return out


def blob_type(b64):
    try:
        raw = base64.b64decode(b64, validate=True)
    except ValueError:
        return None
    if len(raw) < 4:
        return None
    (n,) = struct.unpack(">I", raw[:4])
    return raw[4:4 + n].decode("ascii", "replace") if len(raw) >= 4 + n else None


def line_key(line):
    m = LINE_KEY.search(line)
    return (m.group(1), m.group(2)) if m else None


def render(text, keys):
    """Новое содержимое authorized_keys: чужие строки на месте, ключи устройств — в конце."""
    wanted = {(t, b) for _, t, b in keys}
    kept = []
    for line in text.split("\n"):
        if MARK.search(line.rstrip()) or (not line.lstrip().startswith("#") and line_key(line) in wanted):
            continue
        kept.append(line)
    while kept and not kept[-1].strip():
        kept.pop()
    lines = kept + [f"{t} {b} airlok-device:{i}" for i, t, b in keys]
    return "".join(line + "\n" for line in lines)


def has_keys(text):
    return any(not line.lstrip().startswith("#") and line_key(line) for line in text.split("\n"))


def sshd_conf():
    """Действующие настройки sshd: первое значение каждого параметра. None — sshd не ответил."""
    try:
        r = subprocess.run(["sshd", "-T"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode:
        return None
    conf = {}
    for line in r.stdout.splitlines():
        name, _, value = line.partition(" ")
        conf.setdefault(name.lower(), value.strip())
    return conf


def keys_path(conf, account=ROOT):
    first = ((conf or {}).get("authorizedkeysfile") or ".ssh/authorized_keys").split()[0]
    if first == "none":
        raise Failed("в настройках sshd файлы ключей отключены (AuthorizedKeysFile none)")

    def token(m):
        value = {"%%": "%", "%h": account.home, "%u": account.name}.get(m.group())
        if value is None:
            raise Failed(f"AuthorizedKeysFile {first}: агент понимает в пути только %h и %u")
        return value

    path = re.sub(r"%.", token, first)
    return path if path.startswith("/") else os.path.join(account.home, path)


def password_login(conf, user="root"):
    """Можно ли войти этим пользователем по паролю; sshd не ответил — считаем, что нельзя."""
    if conf is None:
        return False
    password = "yes" in (conf.get("passwordauthentication"), conf.get("kbdinteractiveauthentication"))
    return password and (user != "root" or conf.get("permitrootlogin") == "yes")


def check_owned(path, account):
    """Файл чужого пользователя: без ссылок и внутри его домашнего каталога."""
    folder = os.path.dirname(path)
    if os.path.islink(path) or os.path.islink(folder):
        raise Failed(f"{path} — ссылка на другой файл, агент такой не правит")
    if account.uid != 0:
        home = os.path.realpath(account.home)
        if os.path.commonpath([home, os.path.realpath(folder)]) != home:
            raise Failed(f"{path} вне домашнего каталога {account.name} — агент такой файл не правит")


def own(path, account):
    # Агент работает от root; без root (в тестах) файл и так принадлежит тому, кто его пишет.
    if os.geteuid() == 0:
        os.chown(path, account.uid, account.gid)


def apply(path, keys, password_login, account=ROOT):
    """Переписывает файл, если что-то поменялось; возвращает True, если поменялось."""
    check_owned(path, account)
    try:
        with open(path, encoding="utf-8", errors="surrogateescape") as f:
            old = f.read()
    except FileNotFoundError:
        old = ""
    new = render(old, keys)
    if new == old:
        return False
    if has_keys(old) and not has_keys(new) and not password_login:
        raise Failed(f"это последний ключ {account.name}, а войти им по паролю нельзя — сервер остался бы без входа. "
                     "Оставьте доступ хотя бы одному устройству")
    folder = os.path.dirname(path)
    if not os.path.isdir(folder):
        os.makedirs(folder, mode=0o700)
        own(folder, account)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".authorized_keys.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write(new)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        own(tmp, account)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise
    return True


def fingerprint(b64):
    """Отпечаток как у ssh-keygen -l и в журнале sshd: SHA256 и base64 без «=»."""
    digest = hashlib.sha256(base64.b64decode(b64)).digest()
    return "SHA256:" + base64.b64encode(digest).decode().rstrip("=")


def device_fingerprints(text):
    return {fingerprint(k[1]) for line in text.split("\n") if MARK.search(line.rstrip()) and (k := line_key(line))}


def device_login(journal, allowed):
    """Отпечаток устройства из строки журнала sshd о входе по ключу; allowed — пары (пользователь,
    отпечаток) ключей устройств. None — такого входа не было."""
    for line in journal.splitlines():
        m = ACCEPTED.match(line.strip())
        if m and (m.group(1), m.group(2)) in allowed:
            return m.group(2)
    return None


def read_keys(path):
    try:
        with open(path, encoding="utf-8", errors="surrogateescape") as f:
            return f.read()
    except OSError:
        return ""


def wait_login(since, seconds):
    conf = sshd_conf()
    allowed = {(a.name, fp) for a in accounts() for fp in device_fingerprints(read_keys(keys_path(conf, a)))}
    if not allowed:
        raise Failed("на сервере нет ключей устройств — вход по ключу проверить нечем")
    deadline = time.monotonic() + seconds
    while True:
        r = subprocess.run(["journalctl", "--since", f"@{since}", "-t", "sshd", "-t", "sshd-session", "-o", "cat", "--no-pager"],
                           capture_output=True, text=True, timeout=30)
        fp = device_login(r.stdout, allowed)
        if fp or time.monotonic() >= deadline:
            return fp
        time.sleep(3)


def main_wait(args):
    try:
        since, seconds = (int(a) for a in args)
        fp = wait_login(since, seconds)
    except (Failed, ValueError, OSError, subprocess.SubprocessError) as e:
        print(f"Ошибка: {e}")
        sys.exit(1)
    if not fp:
        sys.exit(2)
    print(f"на сервер вошли по ключу устройства {fp}")


def inventory(text, user="root"):
    """Ключи authorized_keys для дашборда: ключ без опций, комментарий и устройство, если строка его."""
    out = []
    for line in text.split("\n"):
        m = None if line.lstrip().startswith("#") else LINE_KEY.search(line)
        if not m:
            continue
        mark = MARK.search(line.rstrip())
        comment = "" if mark else "".join(ch for ch in line[m.end():] if ch.isprintable()).strip()[:MAX_COMMENT]
        out.append({"user": user, "key": f"{m.group(1)} {m.group(2)}", "comment": comment,
                    "device": mark.group(0).rsplit(":", 1)[1] if mark else None})
        if len(out) >= MAX_INVENTORY:
            break
    return out


def parse_logins(lines):
    """Входы из записей journalctl -o json и курсор последней записи."""
    out, cursor = [], None
    for raw in lines:
        try:
            e = json.loads(raw)
        except ValueError:
            continue
        cursor = e.get("__CURSOR") or cursor
        msg, ts = e.get("MESSAGE"), str(e.get("__REALTIME_TIMESTAMP", ""))
        m = LOGIN.match(msg.strip()) if isinstance(msg, str) else None
        if not m or not ts.isdigit() or len(out) >= MAX_LOGINS:
            continue
        at = datetime.datetime.fromtimestamp(int(ts) / 1e6, datetime.timezone.utc)
        out.append({"at": at.isoformat(timespec="microseconds").replace("+00:00", "Z"), "user": m.group(2),
                    "method": "keyboard-interactive" if m.group(1).startswith("keyboard") else m.group(1),
                    "ip": m.group(3), "port": int(m.group(4)), "fingerprint": m.group(5) or ""})
    return out, cursor


def read_journal(cursor):
    args = ["journalctl", "-t", "sshd", "-t", "sshd-session", "-o", "json", "--no-pager"]
    r = subprocess.run(args + (["--after-cursor", cursor] if cursor else ["-n", "1"]), capture_output=True, text=True, timeout=60)
    if r.returncode:
        raise Failed("journalctl: " + r.stderr.strip()[-200:])
    logins, last = parse_logins(r.stdout.splitlines())
    # Первый запуск: прошлые входы не досылаем, только запоминаем, где журнал сейчас.
    return (logins if cursor else []), last or cursor


def read_state(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def write_state(path, value):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(value)
    os.replace(tmp, path)


def report(state_dir, now=None):
    now = time.time() if now is None else now
    conf, accs, keys = sshd_conf(), accounts(), []
    for a in accs:
        try:
            keys += inventory(read_keys(keys_path(conf, a)), a.name)
        except Failed:
            continue
    keys = keys[:MAX_INVENTORY]
    users = [a.name for a in accs]
    digest = hashlib.sha256(json.dumps([keys, users], sort_keys=True).encode()).hexdigest()
    sha_path = os.path.join(state_dir, "ssh-keys.sha")
    out = {}
    try:
        stale = now - os.path.getmtime(sha_path) > INVENTORY_EVERY
    except OSError:
        stale = True
    if stale or read_state(sha_path) != digest:
        out["ssh_keys"], out["ssh_users"] = keys, users
    cursor = read_state(os.path.join(state_dir, "ssh-logins.cursor"))
    try:
        logins, cursor = read_journal(cursor or None)
    except (Failed, OSError, subprocess.SubprocessError):
        logins = []
    if logins:
        out["ssh_logins"] = logins
    write_state(os.path.join(state_dir, "ssh-report.next"), json.dumps({"keys_sha": digest, "cursor": cursor or ""}))
    return out


def report_sent(state_dir):
    try:
        with open(os.path.join(state_dir, "ssh-report.next"), encoding="utf-8") as f:
            nxt = json.load(f)
    except (OSError, ValueError):
        return
    write_state(os.path.join(state_dir, "ssh-keys.sha"), nxt.get("keys_sha", ""))
    if nxt.get("cursor"):
        write_state(os.path.join(state_dir, "ssh-logins.cursor"), nxt["cursor"])


def sync(req, conf, accs):
    """Ключи устройств — выбранному пользователю, у остальных строки устройств убираются. Чужой
    последний ключ не трогаем: об этом — в итоге, а ключи нужному пользователю уже на месте."""
    keys = parse_request(req)
    user = req.get("user", "root")
    target = next((a for a in accs if a.name == user), None) if isinstance(user, str) else None
    if target is None:
        raise Failed(f"на сервере нет пользователя {user!s:.40}, которому можно класть ключи")
    path = keys_path(conf, target)
    changed = apply(path, keys, password_login(conf, user), target)
    left = []
    for a in accs:
        other = keys_path(conf, a)
        if a.name == user or "airlok-device:" not in read_keys(other):
            continue
        try:
            changed = apply(other, [], password_login(conf, a.name), a) or changed
        except Failed:
            left.append(a.name)
    state = "обновлены" if changed else "уже на месте"
    msg = f"Готово: ключи устройств ({len(keys)}) {state} в {path}"
    if left:
        msg += f"; у {', '.join(left)} прежние ключи устройств оставлены — это их последние ключи, а по паролю не войти"
    return msg


def main():
    if sys.argv[1:2] == ["wait-login"]:
        main_wait(sys.argv[2:])
        return
    if sys.argv[1:2] == ["report"] and len(sys.argv) == 3:
        print(json.dumps(report(sys.argv[2]), ensure_ascii=False))
        return
    if sys.argv[1:2] == ["report-sent"] and len(sys.argv) == 3:
        report_sent(sys.argv[2])
        return
    try:
        print(sync(json.load(sys.stdin), sshd_conf(), accounts()))
    except (Failed, ValueError, OSError) as e:
        print(f"Ошибка: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
