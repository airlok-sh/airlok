#!/usr/bin/env python3
"""Проверяет airlok.yml и печатает его как присваивания для bash.

Все значения проходят строгую проверку формата и экранируются: манифест пишет
пользователь или его ИИ, а результат исполняется через eval от root.
"""
import re
import shlex
import sys

import yaml

NAME = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
EXT = re.compile(r"^[a-z0-9]{1,8}$")
ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")
RESERVED_ENV = {"DATABASE_URL", "UPLOAD_DIR", "UPLOAD_MAX_BYTES", "PORT", "HOSTNAME", "NODE_ENV", "HOME"}
STACKS = {"nextjs"}


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


def parse(data):
    if not isinstance(data, dict) or data.get("version") != 1:
        raise ManifestError("ожидается version: 1")

    app = section(data, "app")
    name = str(app.get("name", ""))
    if not NAME.match(name):
        raise ManifestError("app.name: латиница в нижнем регистре, цифры и дефис, от 2 до 31 символа")
    stack = app.get("stack")
    if stack not in STACKS:
        raise ManifestError(f"app.stack: пока поддерживается только {', '.join(sorted(STACKS))}")
    domain = str(app.get("domain", "")).lower()
    if not DOMAIN.match(domain):
        raise ManifestError("app.domain: укажите домен, например app.example.ru")

    caps = section(data, "capabilities")
    db = section(caps, "database")
    uploads = section(caps, "uploads")
    exts = uploads.get("allowed", [])
    if not isinstance(exts, list) or not all(isinstance(e, str) and EXT.match(e) for e in exts):
        raise ManifestError("capabilities.uploads.allowed: список расширений вида [jpg, png, pdf]")
    uploads_enabled = flag(uploads, "enabled")
    if uploads_enabled and not exts:
        raise ManifestError("capabilities.uploads.allowed: перечислите разрешённые расширения")

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
        "APP_STACK": stack,
        "APP_DOMAIN": domain,
        "DB_ENABLED": int(flag(db, "enabled")),
        "UPLOADS_ENABLED": int(uploads_enabled),
        "UPLOADS_PUBLIC": int(flag(uploads, "public", True)),
        "UPLOAD_MAX_MB": number(uploads, "max_size_mb", 10, 1, 512),
        "UPLOAD_EXTS": " ".join(sorted(set(exts))),
        "ENV_NAMES": " ".join(env),
        "MEMORY_MB": number(resources, "memory_mb", 512, 128, 8192),
    }


def main():
    if len(sys.argv) != 2:
        sys.exit("использование: manifest.py airlok.yml")
    try:
        with open(sys.argv[1], encoding="utf-8") as f:
            values = parse(yaml.safe_load(f))
    except (OSError, yaml.YAMLError) as e:
        sys.exit(f"Ошибка: не удалось прочитать манифест: {e}")
    except ManifestError as e:
        sys.exit(f"Ошибка в airlok.yml: {e}")
    for key, value in values.items():
        print(f"{key}={shlex.quote(str(value))}")


if __name__ == "__main__":
    main()
