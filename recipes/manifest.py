#!/usr/bin/env python3
"""Проверяет airlok.yml и печатает настройки приложения как присваивания для bash.

    manifest.py КАТАЛОГ_ПРОЕКТА

airlok.yml необязателен: без него стек определяется по файлам проекта (detect.py), имя
приходит от дашборда, а адрес выдаёт рецепт. Все значения проходят строгую проверку
формата и экранируются: манифест пишет пользователь или его ИИ, а результат исполняется
через eval от root.
"""
import os
import re
import shlex
import sys

import yaml

from detect import STACKS, DetectError, detect, subprojects

NAME = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
EXT = re.compile(r"^[a-z0-9]{1,8}$")
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
RESERVED_ENV = {"DATABASE_URL", "UPLOAD_DIR", "UPLOAD_MAX_BYTES", "PORT", "HOSTNAME", "NODE_ENV", "HOME"}
# Каталог готового сайта: путь внутри проекта без выходов наверх и скрытых каталогов
# (кроме .vitepress/dist — так его кладёт VitePress).
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
    runtime = section(data, "runtime")
    versions = {k: str(runtime.get(k, "")) for k in ("node", "python")}
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

    # Эти возможности описаны в формате, но рецепт v1 их ещё не умеет. Лучше честно
    # остановиться, чем молча развернуть приложение не так, как просили.
    if not flag(caps, "outbound_network", True):
        raise ManifestError("outbound_network: false пока не поддерживается, приложение получит доступ в интернет")
    if caps.get("cron"):
        raise ManifestError("cron пока не поддерживается, уберите задачи или оставьте cron: []")

    env = data.get("env", [])
    if not isinstance(env, list) or not all(isinstance(e, str) and ENV_NAME.match(e) for e in env):
        raise ManifestError("env: список имён переменных вида OPENAI_API_KEY")
    clash = RESERVED_ENV.intersection(env)
    if clash:
        raise ManifestError(f"env: {', '.join(sorted(clash))} задаёт сам рецепт, уберите из списка")

    resources = section(data, "resources")
    return {
        "APP_NAME": name,
        "APP_STACK": plan["stack"],
        "APP_KIND": plan["kind"],
        "APP_FRAMEWORK": plan["framework"],
        "APP_DOMAIN": domain,
        "NODE_VERSION": plan["node"],
        "PYTHON_VERSION": plan["python"],
        "BUILD_INSTALL": plan["install"],
        "BUILD_CMD": plan["build"],
        "BUILD_OUTPUT": plan["output"],
        "RUN_CMD": plan["start"],
        "DB_ENABLED": int(flag(db, "enabled")),
        "UPLOADS_ENABLED": int(uploads_enabled),
        "UPLOADS_PUBLIC": int(flag(uploads, "public", True)),
        "UPLOAD_MAX_MB": number(uploads, "max_size_mb", 10, 1, 512),
        "UPLOAD_EXTS": " ".join(sorted(set(exts))),
        "ENV_NAMES": " ".join(env),
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
