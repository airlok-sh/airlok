#!/usr/bin/env python3
"""SSH-ключи устройств из дашборда — в authorized_keys root.

    sshkeys.py < ключи.json
    sshkeys.py wait-login НАЧАЛО СЕКУНД  — дождаться входа root по ключу устройства


Агент передаёт поле ssh_keys своей команды: {"keys": [{"id", "key"}]} — полный список ключей
устройств, которым открыт этот сервер. Свои строки агент помечает комментарием
airlok-device:<id> и заменяет только их: ключи, положенные на сервер иначе, остаются. Строка
без пометки с тем же ключом, что у устройства, уходит: иначе, отключив устройство в дашборде,
человек думал бы, что закрыл ему доступ.

Последний ключ root агент не уберёт, если войти root'ом по паролю нельзя: так сервер остался
бы без входа, и вернуть его можно было бы только через консоль хостера.

wait-login нужен, когда вход по паролю закрывают из дашборда: настройки закрепляются, только если
после закрытия на сервер вошли новым соединением по ключу одного из устройств. Вход любым ключом
не годится — на сервер ходят и скрипты, а проверить нужно ключ человека. Код выхода 0 — вход
был, 2 — не дождались, 1 — проверить нечем.
"""
import base64
import hashlib
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import time

ROOT_HOME = "/root"
MAX_KEYS = 50
MAX_KEY_CHARS = 16 << 10
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TYPES = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "ssh-rsa",
         "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com")
KEY = re.compile(r"^(" + "|".join(re.escape(t) for t in TYPES) + r") ([A-Za-z0-9+/]+={0,2})$")
MARK = re.compile(r" airlok-device:[0-9a-f-]{36}$")
# Ключ в строке authorized_keys: перед ним бывают опции (from=…, command="…").
ACCEPTED = re.compile(r"^Accepted publickey for root from \S+ port \d+ ssh2: \S+ (SHA256:[A-Za-z0-9+/]+)$")
LINE_KEY = re.compile(r"(?:^|\s)(" + "|".join(re.escape(t) for t in TYPES) + r"|ssh-dss)\s+([A-Za-z0-9+/]+={0,2})(?=\s|$)")


class Failed(Exception):
    pass


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


def keys_path(conf):
    first = ((conf or {}).get("authorizedkeysfile") or ".ssh/authorized_keys").split()[0]
    if first == "none":
        raise Failed("в настройках sshd файлы ключей отключены (AuthorizedKeysFile none)")

    def token(m):
        value = {"%%": "%", "%h": ROOT_HOME, "%u": "root"}.get(m.group())
        if value is None:
            raise Failed(f"AuthorizedKeysFile {first}: агент понимает в пути только %h и %u")
        return value

    path = re.sub(r"%.", token, first)
    return path if path.startswith("/") else os.path.join(ROOT_HOME, path)


def root_password_login(conf):
    if conf is None:
        return False
    password = "yes" in (conf.get("passwordauthentication"), conf.get("kbdinteractiveauthentication"))
    return password and conf.get("permitrootlogin") == "yes"


def apply(path, keys, password_login):
    """Переписывает файл, если что-то поменялось; возвращает True, если поменялось."""
    if os.path.islink(path):
        raise Failed(f"{path} — ссылка на другой файл, агент такой не правит")
    try:
        with open(path, encoding="utf-8", errors="surrogateescape") as f:
            old = f.read()
    except FileNotFoundError:
        old = ""
    new = render(old, keys)
    if new == old:
        return False
    if has_keys(old) and not has_keys(new) and not password_login:
        raise Failed("это последний ключ root, а войти по паролю нельзя — сервер остался бы без входа. "
                     "Оставьте доступ хотя бы одному устройству")
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".authorized_keys.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", errors="surrogateescape") as f:
            f.write(new)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
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


def device_login(journal, fps):
    """Отпечаток устройства из строки журнала sshd о входе root по ключу; None — такого входа не было."""
    for line in journal.splitlines():
        m = ACCEPTED.match(line.strip())
        if m and m.group(1) in fps:
            return m.group(1)
    return None


def wait_login(since, seconds):
    with open(keys_path(sshd_conf()), encoding="utf-8", errors="surrogateescape") as f:
        fps = device_fingerprints(f.read())
    if not fps:
        raise Failed("на сервере нет ключей устройств — вход по ключу проверить нечем")
    deadline = time.monotonic() + seconds
    while True:
        r = subprocess.run(["journalctl", "--since", f"@{since}", "-t", "sshd", "-t", "sshd-session", "-o", "cat", "--no-pager"],
                           capture_output=True, text=True, timeout=30)
        fp = device_login(r.stdout, fps)
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


def main():
    if sys.argv[1:2] == ["wait-login"]:
        main_wait(sys.argv[2:])
        return
    try:
        keys = parse_request(json.load(sys.stdin))
        conf = sshd_conf()
        path = keys_path(conf)
        changed = apply(path, keys, root_password_login(conf))
    except (Failed, ValueError, OSError) as e:
        print(f"Ошибка: {e}")
        sys.exit(1)
    state = "обновлены" if changed else "уже на месте"
    print(f"Готово: ключи устройств ({len(keys)}) {state} в {path}")


if __name__ == "__main__":
    main()
