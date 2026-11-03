#!/usr/bin/env python3
"""Сборка по Dockerfile на своём сервере: что собирать и на каком порту слушает приложение.

Те же правила, что у сборщика хостинга (internal/builder/docker.go): build.dockerfile из
airlok.yml, единственный собираемый сервис compose.yml или Dockerfile в каталоге проекта.

    dockerfile.py guess-port  — порт по настройкам образа: docker image inspect .Config в stdin
"""
import json
import os
import posixpath
import re
import sys

import yaml

COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
# Службы, которых Airlok не запускает: приложению придётся без них или с внешними.
SERVICE_IMAGES = ("mysql", "mariadb", "mongo", "redis", "valkey", "rabbitmq", "elasticsearch", "clickhouse", "kafka")
DEFAULT_PORT = 8080
ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
REQUIRED_VAR = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*:?\?")
OWN_DEFAULT = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*):?-([^}$]*)\}$")
TRAEFIK_PORT = re.compile(r"^traefik\.http\.services\.[^.]+\.loadbalancer\.server\.port$")
LOCALHOST_PORT = re.compile(r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0):(\d{2,5})\b")
PORT_FLAG = re.compile(r"(?:--port|-p|--server\.port|--listen)[ =](\d{2,5})\b")
BIND_FLAG = re.compile(r"(?:--bind|-b|--host|--listen)[ =](?:[\w.\[\]:-]*):(\d{2,5})\b")
ASGI_CLI = re.compile(r"(^|[\s/\"])(fastapi|uvicorn|gunicorn|hypercorn|granian)\b")


class DockerfileError(Exception):
    pass


def is_file(root, name):
    return os.path.isfile(os.path.join(root, *name.split("/")))


def valid_port(value):
    try:
        port = int(str(value).strip())
    except ValueError:
        return 0
    return port if 1 <= port <= 65535 else 0


def pairs(value):
    """Словарь compose.yml, записанный и как map, и как список «KEY=value»."""
    if isinstance(value, dict):
        return {str(k): "" if v is None else str(v) for k, v in value.items()}
    if isinstance(value, list):
        return dict((str(item).split("=", 1) + [""])[:2] for item in value)
    return {}


def compose_env(service):
    """Обязательные переменные (${X:?…}) и значения по умолчанию сервиса. DATABASE_URL задаёт Airlok."""
    required, defaults = [], {}
    for key, value in pairs(service.get("environment")).items():
        if not ENV_NAME.match(key) or key == "DATABASE_URL":
            continue
        if REQUIRED_VAR.search(value):
            required.append(key)
        elif "$" not in value:
            defaults[key] = value
        else:
            own = OWN_DEFAULT.match(value)
            if own and own.group(1) == key:
                defaults[key] = own.group(2)
    return sorted(required), defaults


def compose_port(service):
    """Порт сервиса: из меток traefik, ports, expose или проверки здоровья."""
    for key, value in pairs(service.get("labels")).items():
        if TRAEFIK_PORT.match(key) and valid_port(value):
            return valid_port(value)
    for item in service.get("ports") or []:
        spec = str(item.get("target", "")) if isinstance(item, dict) else str(item)
        spec, _, proto = spec.partition("/")
        if proto in ("", "tcp") and valid_port(spec.rsplit(":", 1)[-1]):
            return valid_port(spec.rsplit(":", 1)[-1])
    for item in service.get("expose") or []:
        if valid_port(str(item).removesuffix("/tcp")):
            return valid_port(str(item).removesuffix("/tcp"))
    test = (service.get("healthcheck") or {}).get("test")
    found = LOCALHOST_PORT.search(" ".join(test) if isinstance(test, list) else str(test or ""))
    return valid_port(found.group(1)) if found else 0


def service_source(root, compose, name, service):
    """Сборка сервиса; None — он берёт готовый образ."""
    build = service.get("build")
    if build is None:
        return None
    if isinstance(build, str):
        build = {"context": build}
    if not isinstance(build, dict):
        raise DockerfileError(f"{compose}: build сервиса {name} не разобрать")
    if build.get("dockerfile_inline"):
        raise DockerfileError(f"{compose}: сервис {name} собирается из dockerfile_inline — вынесите его в файл")
    context = str(build.get("context") or "")
    if "://" in context or context.startswith("git@"):
        raise DockerfileError(f"{compose}: сервис {name} собирается из внешнего репозитория — так нельзя, нужен код в этом репозитории")
    context = posixpath.normpath(context) if context else "."
    if context.startswith(("/", "../")) or context == "..":
        raise DockerfileError(f"{compose}: каталог сборки сервиса {name} ({context}) — вне каталога проекта. "
                              "Выберите каталогом проекта корень, от которого собирается сервис")
    context = "" if context == "." else context
    dockerfile = posixpath.normpath(posixpath.join(context, str(build.get("dockerfile") or "Dockerfile")))
    if dockerfile.startswith(("/", "../")) or dockerfile == ".." or not is_file(root, dockerfile):
        raise DockerfileError(f"{compose}: у сервиса {name} нет Dockerfile {dockerfile} в каталоге проекта")
    required, defaults = compose_env(service)
    return {"context": context, "dockerfile": dockerfile, "target": str(build.get("target") or ""), "service": name,
            "args": pairs(build.get("args")), "port": compose_port(service), "env": required, "defaults": defaults,
            "database": False, "warnings": []}


def from_compose(root):
    compose = next((f for f in COMPOSE_FILES if is_file(root, f)), "")
    if not compose:
        return None
    try:
        with open(os.path.join(root, compose), encoding="utf-8") as f:
            services = (yaml.safe_load(f) or {}).get("services") or {}
    except (OSError, UnicodeDecodeError, yaml.YAMLError, AttributeError):
        # Сломанный compose.yml — не повод не собрать проект по Dockerfile или по стеку.
        return None
    if not isinstance(services, dict):
        return None
    chosen, builds = None, {}
    for name in sorted(services):
        service = services[name] if isinstance(services[name], dict) else {}
        src = service_source(root, compose, name, service)
        if src is None:
            continue
        key = (src["context"], src["dockerfile"], src["target"])
        builds.setdefault(key, []).append(name)
        # Из одинаковых сборок (например, сервис миграций на том же образе) берём ту, где виден
        # порт: это и есть веб-приложение.
        same = chosen and key == (chosen["context"], chosen["dockerfile"], chosen["target"])
        if chosen is None or (same and not chosen["port"] and src["port"]):
            chosen = src
    if not builds:
        return None
    if len(builds) > 1:
        names = ", ".join(sorted(v[0] for v in builds.values()))
        raise DockerfileError(f"в {compose} несколько собираемых сервисов ({names}). Выберите каталог одного из них "
                              "как каталог проекта или укажите build.dockerfile в airlok.yml")
    for name in sorted(services):
        image = str((services[name] if isinstance(services[name], dict) else {}).get("image") or "")
        if "postgres" in image.lower() or "postgis" in image.lower():
            chosen["database"] = True
        elif any(s in image.lower() for s in SERVICE_IMAGES):
            chosen["warnings"].append(f"сервис {name} ({image}) из {compose} Airlok не запускает: приложению придётся "
                                      "обойтись без него или подключиться к внешнему")
    return chosen


def find_source(root, dockerfile=""):
    """Что собирать; None — собирать по стеку."""
    if dockerfile:
        if not is_file(root, dockerfile):
            raise DockerfileError(f"airlok.yml: нет файла {dockerfile} из build.dockerfile")
        return {"context": "", "dockerfile": dockerfile, "target": "", "service": "", "args": {}, "port": 0,
                "env": [], "defaults": {}, "database": False, "warnings": []}
    src = from_compose(root)
    if src is not None:
        return src
    return find_source(root, "Dockerfile") if is_file(root, "Dockerfile") else None


def guess_port(config):
    """Порт приложения: из EXPOSE, из флагов команды запуска, по серверу (uvicorn и fastapi по
    умолчанию слушают 8000), иначе 8080 — его же Airlok передаёт в PORT."""
    specs = (spec.partition("/") for spec in config.get("ExposedPorts") or {})
    exposed = sorted(p for p in (valid_port(num) for num, _, proto in specs if proto in ("", "tcp")) if p)
    if len(exposed) == 1:
        return exposed[0]
    command = " ".join((config.get("Entrypoint") or []) + (config.get("Cmd") or []))
    for pattern in (PORT_FLAG, BIND_FLAG):
        found = pattern.search(command)
        if found:
            return valid_port(found.group(1)) or DEFAULT_PORT
    if ASGI_CLI.search(command):
        return 8000
    return exposed[0] if exposed else DEFAULT_PORT


def main():
    if sys.argv[1:] == ["guess-port"]:
        print(guess_port(json.load(sys.stdin)))
        return
    sys.exit("использование: dockerfile.py guess-port")


if __name__ == "__main__":
    main()
