#!/usr/bin/env python3
"""Проверяет airlok.yml и печатает настройки приложения как присваивания для bash.

    manifest.py КАТАЛОГ_ПРОЕКТА

airlok.yml необязателен: без него стек определяется по файлам проекта (detect.py), имя
приходит от дашборда, а адрес выдаёт рецепт. Все значения проходят строгую проверку
формата и экранируются: манифест пишет пользователь или его ИИ, а результат исполняется
через eval от root.
"""
import json
import os
import re
import shlex
import sys

import yaml

from cron import CronError, parse_jobs
from detect import STACKS, DetectError, detect, subprojects

NAME = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
EXT = re.compile(r"^[a-z0-9]{1,8}$")
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
RESERVED_ENV = {"DATABASE_URL", "UPLOAD_DIR", "UPLOAD_MAX_BYTES", "PORT", "HOSTNAME", "NODE_ENV", "HOME"}
# Переменные, которые проект описывает сам. Правила те же, что у сборщика хостинга airlok
# (internal/builder/env.go): дашборд и рецепт должны спрашивать одно и то же.
EXAMPLE_FILES = (".env.example", ".env.sample", ".env.template")
EXAMPLE_LINE = re.compile(r"^(#\s*)?(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
# Значение-заглушка: его всё равно нужно заменить своим.
PLACEHOLDER = re.compile(r"^(<.*>|your[-_ ].*|change[-_ ]?me|replace[-_ ]?me|x{3,}|\.\.\.|todo)$", re.I)
# Комментарий говорит, что без значения можно обойтись.
OPTIONAL_HINT = re.compile(r"необязательн|по умолчанию|можно не |optional|default", re.I)
EXAMPLE_RESERVED = {"PORT", "HOST", "HOSTNAME", "HOME", "NODE_ENV"}
# Каталог готового сайта: путь внутри проекта без выходов наверх и скрытых каталогов
# (кроме .vitepress/dist — так его кладёт VitePress).
# Путь проверки: от корня сайта, без пробелов и фрагмента — так же, как на хостинге.
HEALTH = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%/?-]{0,199}$")
OUTPUT = re.compile(r"^(\.|(\.vitepress/)?[A-Za-z0-9_][A-Za-z0-9._-]*(/[A-Za-z0-9_][A-Za-z0-9._-]*)*)$")


class ManifestError(Exception):
    pass


def section(data, key):
    value = data.get(key) or {}
    if not isinstance(value, dict):
        raise ManifestError(f"раздел {key} должен быть словарём")
    return value


def flag(data, key, default=False):
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise ManifestError(f"{key} должен быть true или false")
    return value


def number(data, key, default, low, high):
    value = data.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        raise ManifestError(f"{key} должен быть целым числом от {low} до {high}")
    return value


def command(data, key, label):
    value = data.get(key, "")
    if not isinstance(value, str) or len(value) > 1000 or any(c in value for c in "\n\r\x00"):
        raise ManifestError(f"{label}: одна строка до 1000 символов")
    return value.strip()


def choice(data, key, allowed, label):
    value = data.get(key, "")
    if value in ("", None):
        return ""
    if not isinstance(value, str) or value not in allowed:
        raise ManifestError(f"{label}: одно из значений {', '.join(allowed)}")
    return value


def env_spec(value):
    """env из airlok.yml: список имён (все обязательные) или словарь «ИМЯ: описание» /
    «ИМЯ: {required, secret, description, default, build}»; без default переменная обязательна.
    build — явно указанное «нужна ли сборке», None — решает имя (build_by_default)."""
    if value is None:
        return []
    if isinstance(value, list):
        if not all(isinstance(n, str) for n in value):
            raise ManifestError("env: список имён переменных вида OPENAI_API_KEY")
        return [{"name": n, "required": True, "build": None} for n in value]
    if not isinstance(value, dict):
        raise ManifestError("env: список имён переменных или словарь «ИМЯ: описание»")
    out = []
    for name, spec in value.items():
        if spec is None or isinstance(spec, str):
            out.append({"name": name, "required": True, "build": None})
            continue
        if not isinstance(spec, dict) or set(spec) - {"required", "secret", "description", "default", "build"}:
            raise ManifestError(f"env.{name}: описание строкой или поля required, secret, description, default, build")
        default = spec.get("default", "")
        required = spec.get("required", default in ("", None))
        if not isinstance(required, bool):
            raise ManifestError(f"env.{name}.required должен быть true или false")
        build = spec.get("build")
        if build is not None and not isinstance(build, bool):
            raise ManifestError(f"env.{name}.build должен быть true или false")
        out.append({"name": name, "required": required, "build": build})
    return out


def example_value(raw):
    """Значение из .env.example без кавычек и без комментария в конце строки."""
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] in "\"'":
        end = raw.find(raw[0], 1)
        if end > 0:
            return raw[1:end]
    if " #" in raw:
        raw = raw[:raw.index(" #")]
    return raw.strip()


def parse_env_example(text):
    """Пустое значение или заглушка — переменная обязательна, заполненное или закомментированная
    строка — необязательна. «По умолчанию» или «необязательно» в комментарии над строкой тоже
    делают её необязательной."""
    out, notes = [], []
    for line in text.splitlines():
        line = line.strip()
        m = EXAMPLE_LINE.match(line)
        if not line:
            notes = []
            continue
        if m is None:
            notes = notes + [line.lstrip("#").strip()] if line.startswith("#") else []
            continue
        commented, name, value = m.group(1), m.group(2), example_value(m.group(3))
        desc, notes = " ".join(notes), []
        if name in EXAMPLE_RESERVED or any(v["name"] == name for v in out):
            continue
        required = not commented and (value == "" or PLACEHOLDER.match(value) is not None) and not OPTIONAL_HINT.search(desc)
        out.append({"name": name, "required": required})
    return out


def merge_env(*sources):
    """Первый источник важнее: airlok.yml, потом .env.example."""
    out = []
    for src in sources:
        for v in src:
            if not any(o["name"] == v["name"] for o in out):
                out.append(v)
    return out


def read_example(root):
    for name in EXAMPLE_FILES:
        try:
            with open(os.path.join(root, name), encoding="utf-8", errors="replace") as f:
                return f.read()
        except OSError:
            continue
    return ""


def parse(data, root):
    if data is None:
        data = {"version": 1}
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ManifestError("ожидается version: 1")

    app = section(data, "app")
    name = str(app.get("name", ""))
    if name and not NAME.match(name):
        raise ManifestError("app.name: латиница в нижнем регистре, цифры и дефис, от 2 до 31 символа")
    domain = str(app.get("domain", "")).lower()
    if domain and not DOMAIN.match(domain):
        raise ManifestError("app.domain: домен вида app.example.ru")
    stack = choice(app, "stack", STACKS, "app.stack")
    kind = choice(app, "type", ("web", "worker"), "app.type")

    build = section(data, "build")
    output = command(build, "output", "build.output")
    if output and not OUTPUT.match(output):
        raise ManifestError("build.output: каталог внутри проекта, например dist")
    run = section(data, "run")
    release = command(run, "release", "run.release")
    health = command(run, "health", "run.health")
    if health and (not HEALTH.match(health) or health.startswith("//")):
        raise ManifestError("run.health: путь на сайте приложения, например /healthz")
    runtime = section(data, "runtime")
    versions = {k: str(runtime.get(k, "")) for k in ("node", "python", "go")}
    try:
        plan = detect(root, stack=stack, kind=kind, build_command=command(build, "command", "build.command"),
                      build_output=output, run_command=command(run, "command", "run.command"), **versions)
    except DetectError as e:
        found = subprojects(root)
        if found and not stack:
            raise ManifestError(f"{e}. Похоже, приложения лежат в подкаталогах: {', '.join(found[:5])} — "
                                "укажите нужный как каталог проекта в настройках приложения в дашборде")
        raise ManifestError(str(e))

    caps = section(data, "capabilities")
    db = section(caps, "database")
    uploads = section(caps, "uploads")
    exts = uploads.get("allowed", [])
    if not isinstance(exts, list) or not all(isinstance(e, str) and EXT.match(e) for e in exts):
        raise ManifestError("capabilities.uploads.allowed: список расширений вида [jpg, png, pdf]")
    uploads_enabled = flag(uploads, "enabled")
    if uploads_enabled and not exts:
        raise ManifestError("capabilities.uploads.allowed: перечислите разрешённые расширения")
    if plan["stack"] == "static" and (uploads_enabled or flag(db, "enabled")):
        raise ManifestError("у статического сайта нет своего кода на сервере: база и загрузки ему недоступны")
    if plan["kind"] == "worker" and uploads_enabled and flag(uploads, "public", True):
        raise ManifestError("у фонового приложения нет сайта: загрузки только с public: false")
    try:
        cron = parse_jobs(caps.get("cron"))
    except CronError as e:
        raise ManifestError(str(e)) from None
    if plan["stack"] == "static" and (cron or release):
        raise ManifestError("у статического сайта нет своего кода на сервере: run.release и задачам по расписанию нечего запускать")

    # Эта возможность описана в формате, но рецепт её ещё не умеет. Лучше честно
    # остановиться, чем молча развернуть приложение не так, как просили.
    if not flag(caps, "outbound_network", True):
        raise ManifestError("outbound_network: false пока не поддерживается, приложение получит доступ в интернет")

    declared = env_spec(data.get("env"))
    names = [v["name"] for v in declared]
    if len(names) > 100 or not all(isinstance(n, str) and ENV_NAME.match(n) for n in names):
        raise ManifestError("env: до 100 имён переменных вида OPENAI_API_KEY")
    clash = RESERVED_ENV.intersection(names)
    if clash:
        raise ManifestError(f"env: {', '.join(sorted(clash))} задаёт сам рецепт, уберите из списка")
    # Что задаёт рецепт, спрашивать незачем; DATABASE_URL — только когда базу заводит он сам.
    provided = RESERVED_ENV - ({"DATABASE_URL"} if not flag(db, "enabled") else set())
    env = [v["name"] for v in merge_env(declared, parse_env_example(read_example(root)))
           if v["required"] and v["name"] not in provided]

    resources = section(data, "resources")
    return {
        "APP_NAME": name,
        "APP_STACK": plan["stack"],
        "APP_KIND": plan["kind"],
        "APP_FRAMEWORK": plan["framework"],
        "APP_DOMAIN": domain,
        "NODE_VERSION": plan["node"],
        "PYTHON_VERSION": plan["python"],
        "GO_VERSION": plan["go"],
        "BUILD_INSTALL": plan["install"],
        "BUILD_CMD": plan["build"],
        "BUILD_OUTPUT": plan["output"],
        "RUN_CMD": plan["start"],
        "RELEASE_CMD": release,
        "CRON_JOBS": json.dumps(cron, ensure_ascii=False),
        "HEALTH_PATH": health,
        "DB_ENABLED": int(flag(db, "enabled")),
        "UPLOADS_ENABLED": int(uploads_enabled),
        "UPLOADS_PUBLIC": int(flag(uploads, "public", True)),
        "UPLOAD_MAX_MB": number(uploads, "max_size_mb", 10, 1, 512),
        "UPLOAD_EXTS": " ".join(sorted(set(exts))),
        "ENV_NAMES": " ".join(env),
        # Явное build из airlok.yml; остальное решает имя (envkey.py build-env).
        "ENV_BUILD": " ".join(v["name"] for v in declared if v["build"] is True),
        "ENV_NO_BUILD": " ".join(v["name"] for v in declared if v["build"] is False),
        "MEMORY_MB": number(resources, "memory_mb", 512, 128, 8192),
    }


def main():
    if len(sys.argv) != 2 or not os.path.isdir(sys.argv[1]):
        sys.exit("использование: manifest.py КАТАЛОГ_ПРОЕКТА")
    root = sys.argv[1]
    path = os.path.join(root, "airlok.yml")
    try:
        data = None
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f)
        values = parse(data, root)
    except (OSError, yaml.YAMLError) as e:
        sys.exit(f"Ошибка: не удалось прочитать манифест: {e}")
    except ManifestError as e:
        sys.exit(f"Ошибка в airlok.yml: {e}" if os.path.exists(path) else f"Ошибка: {e}")
    for key, value in values.items():
        print(f"{key}={shlex.quote(str(value))}")


if __name__ == "__main__":
    main()
