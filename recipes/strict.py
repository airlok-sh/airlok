#!/usr/bin/env python3
"""Строгий режим агента: что дашборду нельзя, решает файл на самом сервере.

Даже взломанный бэкенд Airlok не получит root: он не положит SSH-ключ, не обновит код агента, не
прочитает базу и не опубликует чужой репозиторий. Файл меняет только человек на сервере: ни одна
команда дашборда его не трогает, поэтому и выключить режим дашборд не может.

    strict.py show                  — режим и разрешённые репозитории
    strict.py enable [--allow-repo РЕПОЗИТОРИЙ]…
    strict.py disable
    strict.py allow РЕПОЗИТОРИЙ
    strict.py deny РЕПОЗИТОРИЙ
    strict.py check ВИД ЗНАЧЕНИЕ_1 ЗНАЧЕНИЕ_2 — для агента: код 0, если команду можно выполнить

РЕПОЗИТОРИЙ — владелец/имя на GitHub или https://хост/путь.git.
"""
import os
import pwd
import re
import subprocess
import sys

POLICY = "/etc/airlok/policy.env"
REPO_PART = re.compile(r"^[A-Za-z0-9_.-]+$")
# Как в агенте: другого вида он команду публикации не примет.
GIT_URL = re.compile(r"^https://[a-z0-9.-]+(/[A-Za-z0-9_][A-Za-z0-9_.~-]*){2,8}\.git$")
REFUSED = {
    "update": "обновление агента из дашборда",
    "db_query": "запросы к базе из дашборда",
    "ssh_keys": "SSH-ключи из дашборда",
    "ssh_password": "вход по паролю из дашборда",
    "os_upgrade": "обновления системы из дашборда",
    "data_move": "перенос данных через хранилище Airlok",
}
HINT = "Настройки строгого режима меняются только на самом сервере: sudo /opt/airlok/recipes/strict.py show"


class PolicyError(Exception):
    pass


def normalize(repo):
    """Один репозиторий — одна запись: GitHub не различает регистр и пишется по-разному."""
    repo = repo.strip()
    if repo.lower().startswith("https://github.com/"):
        repo = repo[len("https://github.com/"):]
        repo = repo[:-4] if repo.endswith(".git") else repo
    if repo.startswith("https://"):
        if not GIT_URL.fullmatch(repo) or ".." in repo:
            raise PolicyError(f"некорректная ссылка на репозиторий: {repo}")
        return repo.lower()
    parts = repo.split("/")
    if len(parts) != 2 or not all(REPO_PART.match(p) and p not in (".", "..") for p in parts):
        raise PolicyError(f"некорректный репозиторий: {repo}. Нужно владелец/имя или https://хост/путь.git")
    return repo.lower()


def read_policy(path=POLICY):
    strict, repos = False, []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                key, _, value = line.strip().partition("=")
                if key == "AIRLOK_MODE":
                    strict = value == "strict"
                elif key == "ALLOW_REPO" and value:
                    repos.append(value)
    except FileNotFoundError:
        pass
    return strict, repos


def write_policy(strict, repos, path=POLICY):
    os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
    lines = ["# Строгий режим агента Airlok. Менять через recipes/strict.py, дашборд этот файл не трогает.",
             f"AIRLOK_MODE={'strict' if strict else 'normal'}"]
    lines += [f"ALLOW_REPO={r}" for r in sorted(set(repos))]
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def refusal(strict, repos, kind, first="", second=""):
    """Почему команду выполнять нельзя; None — можно. Значения те же, что разобрал агент: у публикации
    это приложение и репозиторий, у удаления — приложение и признак удаления с данными."""
    if not strict:
        return None
    if kind in REFUSED:
        return f"строгий режим запрещает {REFUSED[kind]} на этом сервере. {HINT}"
    if kind == "remove" and second == "1":
        return f"строгий режим: удалять приложение вместе с данными из дашборда нельзя. {HINT}"
    if kind == "deploy":
        try:
            allowed = normalize(second) in repos
        except PolicyError:
            allowed = False
        if not allowed:
            return (f"строгий режим: репозитория {second} нет в списке разрешённых. Разрешить можно только на самом "
                    f"сервере: sudo /opt/airlok/recipes/strict.py allow {second}")
    return None


def device_keys():
    """Где лежат ключи, положенные через дашборд: включение режима их не убирает, это решает человек."""
    found = []
    for user in pwd.getpwall():
        path = os.path.join(user.pw_dir, ".ssh", "authorized_keys")
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                if any("airlok-device:" in line for line in f):
                    found.append(path)
        except OSError:
            continue
    return found


def service(*args):
    subprocess.run(["systemctl", *args], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def show(path=POLICY):
    strict, repos = read_policy(path)
    print(f"Режим: {'строгий' if strict else 'обычный'}")
    if not strict:
        return
    print("Разрешённые репозитории: " + (", ".join(repos) if repos else "нет, публиковать из дашборда нельзя"))
    print("Запрещено дашборду: " + ", ".join(REFUSED.values()) + ", удаление вместе с данными, чужие переменные окружения")
    print("Журнал всех команд дашборда: /var/log/airlok-commands.log")


def enable(args, path=POLICY):
    _, repos = read_policy(path)
    while args:
        if args[0] != "--allow-repo" or len(args) < 2:
            raise PolicyError("использование: strict.py enable [--allow-repo РЕПОЗИТОРИЙ]…")
        repos.append(normalize(args[1]))
        args = args[2:]
    write_policy(True, repos, path)
    # Логи приложений бывают с секретами, а опись ключей дашборду больше незачем.
    service("disable", "--now", "airlok-logs.service")
    service("start", "--no-block", "airlok-healthcheck.service")
    show(path)
    for key_file in device_keys():
        print(f"Внимание: в {key_file} остались ключи, положенные через дашборд. Если они не ваши, уберите их: "
              f"sudo sed -i '/airlok-device:/d' {key_file}")


def disable(path=POLICY):
    _, repos = read_policy(path)
    write_policy(False, repos, path)
    if os.path.exists("/etc/systemd/system/airlok-logs.service"):
        service("enable", "--now", "airlok-logs.service")
    service("start", "--no-block", "airlok-healthcheck.service")
    show(path)


def main():
    args = sys.argv[1:]
    command = args[0] if args else ""
    if command == "check":
        strict, repos = read_policy()
        why = refusal(strict, repos, *args[1:4])
        if why:
            print(why)
            sys.exit(1)
        return
    if command == "show":
        show()
        return
    if command not in ("enable", "disable", "allow", "deny"):
        sys.exit(__doc__)
    if os.geteuid() != 0:
        sys.exit("Ошибка: запустите через sudo")
    try:
        if command == "enable":
            enable(args[1:])
        elif command == "disable":
            disable()
        else:
            if len(args) != 2:
                raise PolicyError(f"использование: strict.py {command} РЕПОЗИТОРИЙ")
            strict, repos = read_policy()
            repo = normalize(args[1])
            repos = [*repos, repo] if command == "allow" else [r for r in repos if r != repo]
            write_policy(strict, repos)
            show()
    except PolicyError as e:
        sys.exit(f"Ошибка: {e}")


if __name__ == "__main__":
    main()
