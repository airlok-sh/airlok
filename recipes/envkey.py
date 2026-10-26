"""Переменные окружения из дашборда, зашифрованные под ключ этого сервера.

Закрытый ключ создаётся здесь и сервер не покидает: дашборд получает только открытый и
хранит шифротексты, которые браузер или CLI сделали под него. Расшифровка — openssl с
ключом из файла, значения и ключи в аргументы команд не попадают.

Значение — "v1:" и base64 блоков RSA-OAEP (SHA-256, MGF1 SHA-256) по 384 байта, в каждом до
318 байт текста. Метка блока — airlok-env/v1/<приложение>/<ИМЯ>/<номер>/<всего>: блок не
подменить блоком другой переменной или приложения, не переставить и не отрезать.

  python3 envkey.py panel КЛЮЧ ПРИЛОЖЕНИЕ < env.json > panel.env
  python3 envkey.py build-env "ИМЕНА" "ИСКЛЮЧЕНИЯ" user.env panel.env   # переменные сборки через NUL
"""
import base64
import json
import re
import subprocess
import sys

BLOCK = 384
NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
APP = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
# Эти фреймворки встраивают переменные в код на сборке: без них в браузере будет undefined.
BUILD_PREFIXES = ("NEXT_PUBLIC_", "VITE_")
# Их задают рецепты: значение из дашборда сломало бы запуск или базу.
RESERVED = {"DATABASE_URL", "UPLOAD_DIR", "UPLOAD_MAX_BYTES", "PORT", "HOST", "HOSTNAME", "HOME", "PATH",
            "NODE_ENV", "NEXT_TELEMETRY_DISABLED"}


class EnvError(Exception):
    pass


def valid_name(name):
    upper = name.upper()
    return bool(NAME.match(name)) and upper not in RESERVED and not upper.startswith(("AIRLOK_", "DOCKER_"))


def decrypt(key, app, name, sealed):
    if not isinstance(sealed, str) or not sealed.startswith("v1:"):
        raise EnvError(f"{name}: значение не зашифровано под ключ сервера")
    try:
        raw = base64.b64decode(sealed[3:], validate=True)
    except ValueError:
        raise EnvError(f"{name}: шифротекст повреждён") from None
    if not raw or len(raw) % BLOCK:
        raise EnvError(f"{name}: шифротекст повреждён")
    n = len(raw) // BLOCK
    out = bytearray()
    for i in range(n):
        label = f"airlok-env/v1/{app}/{name}/{i}/{n}".encode().hex()
        r = subprocess.run(
            ["openssl", "pkeyutl", "-decrypt", "-inkey", key,
             "-pkeyopt", "rsa_padding_mode:oaep", "-pkeyopt", "rsa_oaep_md:sha256",
             "-pkeyopt", "rsa_mgf1_md:sha256", "-pkeyopt", f"rsa_oaep_label:{label}"],
            input=raw[i * BLOCK:(i + 1) * BLOCK], capture_output=True)
        if r.returncode:
            raise EnvError(f"{name}: не расшифровать — значение задано под прежний ключ сервера или для другого приложения; "
                           "задайте его заново на странице приложения")
        out += r.stdout
    try:
        value = out.decode()
    except UnicodeDecodeError:
        raise EnvError(f"{name}: значение — не текст UTF-8") from None
    if "\0" in value:
        raise EnvError(f"{name}: в значении нулевой байт")
    return value


def dotenv_line(name, value):
    """Строка env_file для docker compose: в одинарных кавычках значение берётся как есть,
    в двойных работают \\n и \\", а $ нужно удвоить, иначе compose подставит переменную хоста."""
    if "'" not in value and "\n" not in value and "\r" not in value:
        return f"{name}='{value}'"
    escaped = (value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
               .replace("\r", "\\r").replace("$", "$$"))
    return f'{name}="{escaped}"'


def panel(key, app, data):
    if not APP.match(app):
        raise EnvError("некорректное имя приложения")
    if not isinstance(data, list):
        raise EnvError("переменные из дашборда — не список")
    lines, seen = [], set()
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            raise EnvError("переменная из дашборда без имени")
        name = item["name"]
        if not valid_name(name) or name in seen:
            raise EnvError(f"{name[:64]!r}: недопустимое имя переменной")
        seen.add(name)
        lines.append(dotenv_line(name, decrypt(key, app, name, item.get("value"))))
    return "".join(line + "\n" for line in lines)


def dotenv_value(raw):
    """Значение строки env_file так, как его читает docker compose: в одинарных кавычках — как
    есть, в двойных — с \\n, \\t, \\r, \\", \\\\ и $$, без кавычек — до « #»."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1]
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        out, i, body = [], 0, raw[1:-1]
        while i < len(body):
            c = body[i]
            if c == "\\" and i + 1 < len(body):
                out.append({"n": "\n", "t": "\t", "r": "\r"}.get(body[i + 1], body[i + 1]))
                i += 2
            elif c == "$" and body[i + 1:i + 2] == "$":
                out.append("$")
                i += 2
            else:
                out.append(c)
                i += 1
        return "".join(out)
    return raw.split(" #", 1)[0].strip()


def read_dotenv(path):
    values = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            lines = f.read().splitlines()
    except OSError:
        return values
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, eq, raw = line.partition("=")
        name = name.strip()
        if eq and valid_name(name):
            values[name] = dotenv_value(raw)
    return values


def build_env(build, no_build, paths):
    """Переменные сборки из файлов переменных (последний важнее): отмеченные build: true и
    NEXT_PUBLIC_* / VITE_*, кроме отмеченных build: false. Остальное сборка не видит."""
    values = {}
    for path in paths:
        values.update(read_dotenv(path))
    pick = set(build.split())
    skip = set(no_build.split())
    return {n: v for n, v in sorted(values.items())
            if n not in skip and (n in pick or n.startswith(BUILD_PREFIXES)) and "\0" not in v}


def main(argv):
    try:
        if len(argv) == 4 and argv[1] == "panel":
            sys.stdout.write(panel(argv[2], argv[3], json.load(sys.stdin)))
            return 0
        if len(argv) >= 4 and argv[1] == "build-env":
            env = build_env(argv[2], argv[3], argv[4:])
            sys.stdout.write("".join(f"{n}={v}\0" for n, v in env.items()))
            return 0
    except (EnvError, ValueError) as e:
        print(e, file=sys.stderr)
        return 1
    print("\n".join(line.strip() for line in __doc__.strip().splitlines()[-2:]), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
