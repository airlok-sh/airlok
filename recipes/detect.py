#!/usr/bin/env python3
"""Определяет стек проекта по его файлам: как собрать и как запустить.

Та же логика, что у сборщика хостинга airlok: проект без airlok.yml выкатывается
одинаково и на хостинг, и на свой сервер.

    detect.py static-output КАТАЛОГ [ВЫХОД] [built]  — где после сборки лежит index.html
"""
import json
import os
import re
import sys

DEFAULT_NODE = "22"
DEFAULT_PYTHON = "3.12"
DEFAULT_GO = "1.27"
NODE_VERSIONS = ["20", "22", "24"]
PYTHON_VERSIONS = ["3.10", "3.11", "3.12", "3.13", "3.14"]
GO_VERSIONS = ["1.25", "1.26", "1.27"]
STACKS = ("static", "nextjs", "node", "python", "go")
# Куда сборка кладёт программу Go-приложения.
GO_BINARY = ".airlok/app"

# Статические сайты: зависимость сборщика → куда он кладёт готовый сайт.
STATIC_BUILDERS = [
    ("astro", "dist", "Astro"),
    ("vitepress", ".vitepress/dist", "VitePress"),
    ("@docusaurus/core", "build", "Docusaurus"),
    ("gatsby", "public", "Gatsby"),
    ("react-scripts", "build", "Create React App"),
    ("@11ty/eleventy", "_site", "Eleventy"),
    ("parcel", "dist", "Parcel"),
    ("vite", "dist", "Vite"),
]
# Боты на long polling: без веб-фреймворка такое приложение — фоновое.
NODE_BOTS = ["telegraf", "grammy", "node-telegram-bot-api", "discord.js", "vk-io"]
PYTHON_BOTS = ["aiogram", "python-telegram-bot", "pytelegrambotapi", "telethon", "pyrogram", "discord.py", "vkbottle"]
GO_BOTS = ["github.com/go-telegram-bot-api/telegram-bot-api", "gopkg.in/telebot", "gopkg.in/tucnak/telebot", "github.com/go-telegram/bot",
           "github.com/mymmrac/telego", "github.com/PaulSonOfLars/gotgbot", "github.com/NicoNex/echotron", "github.com/bwmarrin/discordgo",
           "github.com/SevereCloud/vksdk"]
GO_SERVERS = [
    ("github.com/gin-gonic/gin", "Gin"), ("github.com/labstack/echo", "Echo"), ("github.com/gofiber/fiber", "Fiber"),
    ("github.com/go-chi/chi", "chi"), ("github.com/gorilla/mux", "Gorilla"), ("github.com/valyala/fasthttp", "fasthttp"),
]
NODE_SERVERS = [
    ("@nestjs/core", "NestJS"), ("express", "Express"), ("fastify", "Fastify"), ("koa", "Koa"),
    ("hono", "Hono"), ("@hapi/hapi", "Hapi"), ("@adonisjs/core", "AdonisJS"), ("@remix-run/serve", "Remix"),
]
# Точка входа Python-приложения ищется в этих файлах по порядку.
PYTHON_ENTRIES = ["main.py", "app.py", "app/main.py", "src/main.py", "server.py", "api.py", "wsgi.py", "asgi.py", "streamlit_app.py"]
SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,99}$")
MAJOR = re.compile(r"\d+(\.\d+)?")
# Ограничение версии Python из [project] в pyproject.toml.
REQUIRES_PYTHON = re.compile(r"""^\s*requires-python\s*=\s*["']([^"']+)["']""", re.M)
SPEC_CLAUSE = re.compile(r"^(>=|<=|==|!=|~=|>|<)\s*(\d+)(?:\.(\d+))?")
GO_DIRECTIVE = re.compile(r"^go\s+(\d+\.\d+)", re.M)
GO_PACKAGE_MAIN = re.compile(r"^package\s+main\s*$", re.M)

NPM_INSTALL = ("if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; "
               "elif [ -f pnpm-lock.yaml ]; then npx --yes pnpm@9 install --frozen-lockfile; "
               "elif [ -f yarn.lock ]; then npx --yes yarn@1 install --frozen-lockfile; "
               "else npm install --no-audit --no-fund; fi")


class DetectError(Exception):
    pass


def exists(root, name):
    return os.path.exists(os.path.join(root, name))


def read_text(root, name):
    try:
        with open(os.path.join(root, name), encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return ""


class Package:
    def __init__(self, data):
        self.scripts = data.get("scripts") or {}
        self.deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
        self.main = data.get("main") or ""
        self.engines = (data.get("engines") or {}).get("node") or ""

    def has(self, dep):
        return dep in self.deps


def load_package(root):
    raw = read_text(root, "package.json")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError as e:
        raise DetectError(f"package.json не разобрать: {e}")
    if not isinstance(data, dict):
        raise DetectError("package.json не разобрать: ожидается объект")
    return Package(data)


def detect(root, stack="", kind="", build_command="", build_output="", run_command="", node="", python="", go=""):
    """План сборки: то, что задано в airlok.yml, плюс найденное по файлам проекта."""
    pkg = load_package(root)
    py_deps = "\n".join(read_text(root, f) for f in ("requirements.txt", "pyproject.toml", "Pipfile")).lower()
    is_python = any(exists(root, f) for f in ("requirements.txt", "pyproject.toml", "Pipfile"))

    plan = {"stack": stack, "framework": "", "kind": kind, "node": "", "python": "", "go": "",
            "install": "", "build": "", "output": "", "start": ""}
    if not plan["stack"]:
        plan["stack"], plan["framework"] = guess_stack(root, pkg, is_python)
        if not plan["stack"]:
            raise DetectError("не удалось понять, что это за проект: нет package.json, requirements.txt, pyproject.toml, "
                              "go.mod или index.html. Укажите стек в airlok.yml (app.stack: static, nextjs, node, python или go) "
                              "или каталог проекта внутри репозитория")

    if plan["stack"] in ("static", "nextjs", "node"):
        plan["node"] = node_version(root, node, pkg)
    elif plan["stack"] == "python":
        plan["python"] = python_version(root, python)
    elif plan["stack"] == "go":
        plan["go"] = go_version(root, go)

    if plan["kind"] == "worker" and plan["stack"] in ("static", "nextjs"):
        raise DetectError("фоновым (app.type: worker) может быть только Node-, Python- или Go-приложение")
    if plan["stack"] == "static":
        plan["kind"] = "web"
        return plan_static(pkg, build_command, build_output, plan)
    if plan["stack"] == "nextjs":
        if pkg is None:
            raise DetectError("для Next.js нужен package.json")
        plan.update(install=NPM_INSTALL, build=build_command or "npm run build", kind="web")
        plan["framework"] = plan["framework"] or "Next.js"
        return plan
    if plan["stack"] == "node":
        return plan_node(root, pkg, build_command, run_command, plan)
    if plan["stack"] == "python":
        return plan_python(root, py_deps, run_command, plan)
    if plan["stack"] == "go":
        return plan_go(root, build_command, run_command, plan)
    raise DetectError(f"неизвестный стек {plan['stack']!r}")


def guess_stack(root, pkg, is_python):
    # go.mod в корне — Go-приложение, даже если рядом package.json для сборки стилей или фронтенда.
    if exists(root, "go.mod"):
        return "go", ""
    if pkg is not None:
        if pkg.has("next"):
            return "nextjs", "Next.js"
        server = next((name for dep, name in NODE_SERVERS if pkg.has(dep)), "")
        # У CRA и Vite тоже бывает scripts.start, но это сервер разработки, а не прод.
        if not server:
            for dep, _, name in STATIC_BUILDERS:
                if pkg.has(dep):
                    return "static", name
        if server or pkg.scripts.get("start"):
            return "node", server
        if any(pkg.has(b) for b in NODE_BOTS):
            return "node", ""
    if is_python:
        return "python", ""
    if exists(root, "index.html"):
        return "static", "HTML"
    return "", ""


def plan_static(pkg, build_command, build_output, plan):
    plan["output"] = build_output
    if build_command:
        plan["install"], plan["build"] = NPM_INSTALL, build_command
    elif pkg is not None and pkg.scripts.get("build"):
        plan["install"], plan["build"] = NPM_INSTALL, "npm run build"
    if pkg is None and build_command:
        plan["install"] = ""
    if not plan["output"] and plan["build"] and pkg is not None:
        plan["output"] = next((out for dep, out, _ in STATIC_BUILDERS if pkg.has(dep)), "")
    plan["framework"] = plan["framework"] or "статический сайт"
    return plan


def plan_node(root, pkg, build_command, run_command, plan):
    if pkg is None:
        raise DetectError("для Node-приложения нужен package.json")
    plan["install"] = NPM_INSTALL
    plan["build"] = build_command or ("npm run build" if pkg.scripts.get("build") else "")
    start = run_command
    if not start:
        if pkg.scripts.get("start:prod"):
            # NestJS: start — через CLI из devDependencies, а нужен собранный код.
            start = "npm run start:prod"
        elif pkg.scripts.get("start"):
            start = "npm start"
        elif pkg.main and exists(root, pkg.main):
            start = "node " + pkg.main
        else:
            start = next(("node " + f for f in ("server.js", "index.js", "app.js", "main.js") if exists(root, f)), "")
    if not start:
        raise DetectError("не понятно, как запустить приложение: добавьте scripts.start в package.json или run.command в airlok.yml")
    plan["start"] = start
    if not plan["kind"]:
        plan["kind"] = "web"
        has_server = any(pkg.has(dep) for dep, _ in NODE_SERVERS)
        bot = next((b for b in NODE_BOTS if pkg.has(b)), "")
        if bot and not has_server:
            plan["kind"], plan["framework"] = "worker", f"бот ({bot})"
    plan["framework"] = plan["framework"] or "Node.js"
    return plan


def python_dep(deps, name):
    """Есть ли пакет: имя в начале строки requirements.txt или в кавычках в pyproject.toml."""
    return re.search(r"(^|[\"'\s])" + re.escape(name) + r"($|[\s\"'\[=<>~!;,])", deps, re.M) is not None


def python_app(root, *ctors):
    """module:var объекта приложения, например app.main:app."""
    assign = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(:[^=]+)?=\s*(?:\w+\.)?(" + "|".join(map(re.escape, ctors)) + ")", re.M)
    for f in PYTHON_ENTRIES:
        m = assign.search(read_text(root, f))
        if m:
            return f[:-3].replace("/", ".") + ":" + m.group(1)
    return ""


def find_python(root, marker):
    return next((f for f in PYTHON_ENTRIES if marker in read_text(root, f)), "")


def django_wsgi(root):
    for name in sorted(os.listdir(root)):
        if os.path.isdir(os.path.join(root, name)) and exists(root, name + "/wsgi.py") and SEGMENT.match(name):
            return name + ".wsgi"
    return ""


def plan_python(root, deps, run_command, plan):
    install = ["python -m venv /app/.venv"]
    pip = "/app/.venv/bin/pip install --no-cache-dir --disable-pip-version-check"
    if exists(root, "requirements.txt"):
        install.append(pip + " -r requirements.txt")
    elif exists(root, "pyproject.toml"):
        install.append(pip + " .")
    else:
        raise DetectError("для Python-приложения нужен requirements.txt или pyproject.toml")

    def has(name):
        return python_dep(deps, name)

    start, extra = run_command, []
    web = any(has(p) for p in ("streamlit", "django", "fastapi", "starlette", "litestar", "flask", "gradio"))
    bot = next((b for b in PYTHON_BOTS if has(b)), "")
    if not plan["kind"]:
        plan["kind"] = "worker" if bot and not web else "web"
    if plan["kind"] == "worker":
        plan["framework"] = f"бот ({bot})" if bot else "фоновое приложение"
        if not start:
            start = next(("python " + f for f in ("bot.py", "main.py", "app.py", "run.py", "worker.py") if exists(root, f)), "")
        if not start:
            raise DetectError("не нашли, что запускать: положите bot.py или main.py, либо укажите run.command в airlok.yml")
    if not start:
        if has("streamlit"):
            plan["framework"] = "Streamlit"
            entry = find_python(root, "import streamlit")
            if not entry:
                raise DetectError("не нашли файл Streamlit-приложения: укажите run.command в airlok.yml")
            start = f"streamlit run {entry} --server.port 8080 --server.address 0.0.0.0 --server.headless true"
        elif has("django"):
            plan["framework"] = "Django"
            module = django_wsgi(root)
            if not module:
                raise DetectError("не нашли wsgi.py Django-проекта: укажите run.command в airlok.yml")
            start = f"gunicorn {module}:application --bind 0.0.0.0:8080 --workers 2 --access-logfile -"
            extra.append("gunicorn")
        elif has("fastapi") or has("starlette") or has("litestar"):
            plan["framework"] = "FastAPI" if has("fastapi") else "ASGI"
            target = python_app(root, "FastAPI(", "Starlette(", "Litestar(")
            if not target:
                raise DetectError("не нашли объект приложения (app = FastAPI()): укажите run.command в airlok.yml")
            start = f"uvicorn {target} --host 0.0.0.0 --port 8080 --proxy-headers --forwarded-allow-ips '*'"
            extra.append("uvicorn")
        elif has("flask"):
            plan["framework"] = "Flask"
            target = python_app(root, "Flask(")
            if not target:
                raise DetectError("не нашли объект приложения (app = Flask(__name__)): укажите run.command в airlok.yml")
            start = f"gunicorn {target} --bind 0.0.0.0:8080 --workers 2 --access-logfile -"
            extra.append("gunicorn")
        elif has("gradio"):
            plan["framework"] = "Gradio"
            entry = find_python(root, "import gradio")
            if not entry:
                raise DetectError("не нашли файл Gradio-приложения: укажите run.command в airlok.yml")
            start = "python " + entry
        else:
            raise DetectError("не понятно, как запустить Python-приложение: поддерживаются FastAPI, Flask, Django, Streamlit "
                              "и Gradio, для остального укажите run.command в airlok.yml")
    # Сервер приложений ставим сами, если проект его не указал: без него не запуститься.
    install += [f"{pip} {p}" for p in extra if not has(p)]
    install.append("/app/.venv/bin/python -m compileall -q -x '/\\.venv/' .")
    plan["install"] = " && ".join(install)
    plan["start"] = start
    plan["framework"] = plan["framework"] or "Python"
    return plan


def plan_go(root, build_command, run_command, plan):
    mod = read_text(root, "go.mod")
    if not mod:
        raise DetectError("для Go-приложения нужен go.mod")
    # С vendor/ зависимости уже в репозитории, без go.sum их нет вовсе.
    if not exists(root, "vendor/modules.txt") and exists(root, "go.sum"):
        plan["install"] = "go mod download"
    plan["build"], start = build_command, run_command
    if not plan["build"]:
        plan["build"] = f"mkdir -p .airlok && go build -trimpath -ldflags='-s -w' -o {GO_BINARY} {go_main_package(root)}"
        start = start or "/app/" + GO_BINARY
    if not start:
        raise DetectError("со своей build.command укажите и run.command в airlok.yml: рецепт не знает, куда она кладёт программу")
    plan["start"] = start
    server = next((name for dep, name in GO_SERVERS if go_dep(mod, dep)), "")
    plan["framework"] = server
    if not plan["kind"]:
        plan["kind"] = "web"
        # net/http из стандартной библиотеки по go.mod не видно, поэтому бот с вебхуком без
        # фреймворка задаёт app.type: web сам.
        bot = next((b for b in GO_BOTS if go_dep(mod, b)), "")
        if bot and not server:
            plan["kind"], plan["framework"] = "worker", f"бот ({bot})"
    plan["framework"] = plan["framework"] or "Go"
    return plan


def go_dep(mod, dep):
    """Есть ли модуль в require go.mod. Сравнивается префикс пути: у модулей бывают суффиксы
    мажорной версии (/v5) и разные пути одной библиотеки (gopkg.in/telebot.v3)."""
    return re.search(r"^(?:require)?\s+" + re.escape(dep) + r"[./\s]", mod, re.M) is not None


def go_main_package(root):
    """Какой пакет собирать: корень проекта или единственный cmd/*."""
    if is_go_main(root, "."):
        return "."
    try:
        names = sorted(os.listdir(os.path.join(root, "cmd")))
    except OSError:
        names = []
    mains = ["./cmd/" + n for n in names if SEGMENT.match(n) and is_go_main(root, "cmd/" + n)]
    if not mains:
        raise DetectError("не нашли package main ни в корне, ни в cmd/*: укажите build.command и run.command в airlok.yml")
    if len(mains) > 1:
        raise DetectError(f"в проекте несколько программ ({', '.join(mains)}): укажите нужную в build.command и run.command "
                          f"в airlok.yml, например go build -o .airlok/app {mains[0]} и /app/.airlok/app")
    return mains[0]


def is_go_main(root, pkg):
    path = os.path.join(root, pkg)
    if not os.path.isdir(path):
        return False
    return any(n.endswith(".go") and not n.endswith("_test.go") and os.path.isfile(os.path.join(path, n))
               and GO_PACKAGE_MAIN.search(read_text(path, n)) for n in os.listdir(path))


def go_version(root, wanted):
    """Версия из директивы go в go.mod: это наименьшая подходящая версия, а новые сборщики
    собирают и старый код, поэтому слишком старая директива получает последнюю версию."""
    if wanted:
        if wanted not in GO_VERSIONS:
            raise DetectError(f"runtime.go — одна из версий {', '.join(GO_VERSIONS)}")
        return wanted
    m = GO_DIRECTIVE.search(read_text(root, "go.mod"))
    if m is None:
        return DEFAULT_GO
    if m.group(1) in GO_VERSIONS:
        return m.group(1)
    if tuple(map(int, m.group(1).split("."))) > tuple(map(int, GO_VERSIONS[-1].split("."))):
        raise DetectError(f"go.mod требует Go {m.group(1)}, а поддерживаются {GO_VERSIONS[0]}–{GO_VERSIONS[-1]}")
    return DEFAULT_GO


def node_version(root, wanted, pkg):
    if wanted:
        if wanted not in NODE_VERSIONS:
            raise DetectError(f"runtime.node — одна из версий {', '.join(NODE_VERSIONS)}")
        return wanted
    for src in (read_text(root, ".nvmrc"), read_text(root, ".node-version"), pkg.engines if pkg else ""):
        m = MAJOR.search(src)
        if m and m.group(0).split(".")[0] in NODE_VERSIONS:
            return m.group(0).split(".")[0]
    return DEFAULT_NODE


def python_version(root, wanted):
    if wanted:
        if wanted not in PYTHON_VERSIONS:
            raise DetectError(f"runtime.python — одна из версий {', '.join(PYTHON_VERSIONS)}")
        return wanted
    for src in (read_text(root, ".python-version"), read_text(root, "runtime.txt")):
        m = MAJOR.search(src.strip().removeprefix("python-"))
        if m and m.group(0) in PYTHON_VERSIONS:
            return m.group(0)
    req = REQUIRES_PYTHON.search(read_text(root, "pyproject.toml"))
    if req is None or fits_python(DEFAULT_PYTHON, req.group(1)):
        return DEFAULT_PYTHON
    for v in PYTHON_VERSIONS:
        if fits_python(v, req.group(1)):
            return v
    raise DetectError(f"pyproject.toml требует Python {req.group(1)}, а поддерживаются {PYTHON_VERSIONS[0]}–{PYTHON_VERSIONS[-1]}")


def fits_python(version, spec):
    """Подходит ли X.Y под requires-python вроде ">=3.10,<4" или "~=3.12". Патч не важен:
    в образе всегда последний патч версии."""
    v_major, v_minor = (int(x) for x in version.split("."))
    for clause in spec.split(","):
        m = SPEC_CLAUSE.match(clause.strip())
        if not m:
            continue  # неизвестная форма ограничения — не мешаем сборке
        major, has_minor = int(m.group(2)), m.group(3) is not None
        cmp = v_major - major
        if cmp == 0 and has_minor:
            cmp = v_minor - int(m.group(3))
        op = m.group(1)
        ok = {">=": cmp >= 0, ">": cmp > 0, "<=": cmp <= 0, "<": cmp < 0, "==": cmp == 0,
              "!=": cmp != 0 or not has_minor, "~=": cmp >= 0 and v_major == major}[op]
        if not ok:
            return False
    return True


def subprojects(root):
    """Подкаталоги, похожие на отдельные проекты: в монорепозитории приложение не в корне."""
    markers = ("package.json", "requirements.txt", "pyproject.toml", "go.mod", "index.html")
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return []
    return [n for n in names if SEGMENT.match(n) and not n.startswith(".") and n != "node_modules"
            and os.path.isdir(os.path.join(root, n)) and any(exists(root, os.path.join(n, m)) for m in markers)]


def static_output(root, output, built):
    """Каталог готового сайта: заданный или первый из обычных, где есть index.html."""
    if output:
        candidates = [output]
    elif built:
        candidates = ["dist", "build", "out", "public", "_site", "."]
    else:
        candidates = [".", "public"]
    for c in candidates:
        if exists(root, os.path.join(c, "index.html")):
            return os.path.normpath(c)
    if output:
        raise DetectError(f"в каталоге сайта {output!r} нет index.html — проверьте build.output в airlok.yml")
    raise DetectError("после сборки не нашли index.html в dist, build, out или public — укажите build.output в airlok.yml")


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "static-output":
        output = sys.argv[3] if len(sys.argv) > 3 else ""
        try:
            print(static_output(sys.argv[2], output, "built" in sys.argv[4:]))
        except DetectError as e:
            sys.exit(f"Ошибка: {e}")
        return
    sys.exit("использование: detect.py static-output КАТАЛОГ [ВЫХОД] [built]")


if __name__ == "__main__":
    main()
