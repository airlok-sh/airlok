#!/usr/bin/env python3
"""Определяет стек проекта по его файлам: как собрать и как запустить.

Та же логика, что у сборщика хостинга Airlok: проект без airlok.yml публикуется
одинаково и на хостинг, и на свой сервер.

    detect.py static-output КАТАЛОГ [ВЫХОД] [built]  — где после сборки лежит index.html
    detect.py drop-links КАТАЛОГ                     — убрать ссылки за пределы каталога и перечислить их
    detect.py reusable-install КАТАЛОГ СТЕК          — можно ли взять зависимости из снимка прошлой сборки
                                                       и отпечаток того, что ещё влияет на установку
"""
import hashlib
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
# Версии, которые больше не получают исправлений безопасности: их ещё можно выбрать, но
# публикация об этом предупреждает. Тексты те же, что у сборщика хостинга.
UNSUPPORTED_RUNTIMES = [
    ("node", "Node.js", "20", "30.04.2026", "укажите 22 или 24 в .nvmrc, .node-version, engines.node в package.json или runtime.node"),
    ("python", "Python", "3.10", "в октябре 2026", "укажите 3.12 или новее в .python-version, runtime.txt, requires-python в pyproject.toml или runtime.python"),
    ("go", "Go", "1.25", "в августе 2026", 'укажите runtime.go: "1.27" или уберите runtime.go из airlok.yml'),
]
# Кодовые имена LTS-версий Node.js, как их пишут в .nvmrc: lts/jod.
NODE_LTS = {"iron": "20", "jod": "22", "krypton": "24"}
STACKS = ("static", "nextjs", "node", "python", "go")
# Куда сборка кладёт программу Go-приложения.
GO_BINARY = ".airlok/app"
# Куда сборка ставит Bun: в образе Node его нет, а отдельный образ Bun не годится,
# потому что без npm и Node многие пакеты не соберутся.
BUN_BIN = "/app/.airlok/bun/node_modules/.bin"
# Версии менеджеров пакетов, если проект не задал свою в packageManager: точные, чтобы сборка
# не брала каждый раз новый выпуск из npm.
PNPM_VERSION = "9.15.9"
YARN_VERSION = "1.22.22"
BUN_VERSION = "1.4.4"
# Куда сборка ставит Yarn 2+.
COREPACK_DIR = "/app/.airlok/corepack"
# packageManager в package.json: имя, точная версия и, может быть, хеш.
PACKAGE_MANAGER = re.compile(r"^(npm|pnpm|yarn|bun)@(\d+\.\d+\.\d+(?:-[0-9A-Za-z.]+)?)(?:\+sha(?:1|224|256|384|512)\.[0-9a-f]+)?$", re.ASCII)
# Версия формата yarn.lock, который пишет Yarn 2 и новее.
BERRY_METADATA = re.compile(r'^__metadata:\s*\n\s+version:\s*"?(\d+)', re.M)
# Последний Yarn, который пишет yarn.lock этого формата. Без packageManager Yarn ставится по нему:
# более новый обновил бы формат, и yarn install --immutable упал бы.
BERRY_VERSIONS = {"4": "2.4.3", "5": "3.1.1", "6": "3.8.7", "8": "4.13.0", "9": "4.14.1", "10": "4.18.1"}
LATEST_BERRY_LOCK = "10"
# Окружение Yarn 2+: обёртки corepack лежат в папке проекта, а Yarn, который он берёт из реестра
# npm, как и все пакеты сборки, — в кэше сборки приложения (AIRLOK_CACHE задаёт app-deploy.sh):
# работающему приложению он не нужен. node-modules вместо PnP: иначе node server.js и Next.js
# standalone не найдут модули.
COREPACK_ENV = ('export COREPACK_HOME="${AIRLOK_CACHE:-/tmp}/corepack" '
                'COREPACK_NPM_REGISTRY="${COREPACK_NPM_REGISTRY:-https://registry.npmjs.org}" '
                "COREPACK_ENABLE_DOWNLOAD_PROMPT=0 COREPACK_ENABLE_AUTO_PIN=0 COREPACK_ENABLE_STRICT=0 "
                f"YARN_NODE_LINKER=node-modules YARN_ENABLE_TELEMETRY=0 PATH={COREPACK_DIR}/bin:$PATH")


def bun_setup(version):
    return f"npm install --no-audit --no-fund --prefix .airlok/bun bun@{version} && export PATH={BUN_BIN}:$PATH"


BUN_SETUP = bun_setup(BUN_VERSION)
# Сервер, который собирает Nitro (Nuxt, TanStack Start).
NITRO_SERVER = "node .output/server/index.mjs"
# Байткод кода проекта: в работающем приложении папка только для чтения, и без него каждый старт
# компилировал бы весь код заново.
PY_COMPILE = "/app/.venv/bin/python -m compileall -q -x '/\\.venv/' ."

# Первые слова, перед которыми exec не поставить: синтаксис и встроенные команды оболочки.
SHELL_KEYWORDS = frozenset((
    "!", "{", "[[", ".", ":", "alias", "bg", "break", "case", "cd", "command", "continue", "eval", "exec", "exit", "export", "fg", "for",
    "function", "getopts", "hash", "if", "jobs", "local", "read", "readonly", "return", "select", "set", "shift", "source", "time", "times",
    "trap", "type", "ulimit", "umask", "unalias", "unset", "until", "wait", "while",
))
SHELL_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
SHELL_PROGRAM = re.compile(r"[\w./+@%:~-]+", re.ASCII)
NPM_VARIABLE = re.compile(r"npm_(package|lifecycle|config)_")
SOURCE_DIRS_SKIPPED = {"node_modules", ".git", ".venv", "venv", "__pycache__", "vendor", ".next", "dist", "build", ".airlok"}
SOURCE_EXTS = {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"}
MAX_SOURCE_SIZE = 256 << 10

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
    ("elysia", "Elysia"),
]
# Точка входа приложения на Bun без scripts.start: TypeScript он запускает сам.
BUN_ENTRIES = ["index.ts", "src/index.ts", "server.ts", "src/server.ts", "main.ts", "src/main.ts", "index.js", "server.js"]
BUN_COMMAND = re.compile(r"(^|[\s;&|])bunx?\s")
# Настройки, от которых зависит, будет ли у приложения свой сервер.
REACT_ROUTER_SPA = re.compile(r"\bssr\s*:\s*false\b")
ASTRO_MIDDLEWARE = re.compile(r"""\bmode\s*:\s*["']middleware["']""")
# Файлы, по которым папка без requirements.txt узнаётся как Python-приложение.
PYTHON_START_FILES = ["bot.py", "main.py", "app.py", "run.py", "worker.py", "server.py"]
# Пакет pip для модуля, который импортирует код. Только известные: имя модуля часто не совпадает
# с пакетом (PIL — pillow), а угаданный пакет может оказаться чужим. Список тот же, что у сборщика
# хостинга (internal/builder/pydeps.go).
PYTHON_PACKAGES = {
    "aiogram": "aiogram", "telegram": "python-telegram-bot", "telebot": "pytelegrambotapi", "telethon": "telethon",
    "pyrogram": "pyrogram", "discord": "discord.py", "vkbottle": "vkbottle", "vk_api": "vk_api",
    "fastapi": "fastapi", "starlette": "starlette", "uvicorn": "uvicorn", "flask": "flask", "django": "django",
    "streamlit": "streamlit", "gradio": "gradio", "requests": "requests", "aiohttp": "aiohttp", "httpx": "httpx",
    "websockets": "websockets", "dotenv": "python-dotenv", "yaml": "pyyaml", "PIL": "pillow", "bs4": "beautifulsoup4",
    "lxml": "lxml", "numpy": "numpy", "pandas": "pandas", "matplotlib": "matplotlib", "sklearn": "scikit-learn",
    "cv2": "opencv-python-headless", "sqlalchemy": "sqlalchemy", "psycopg2": "psycopg2-binary", "psycopg": "psycopg[binary]",
    "asyncpg": "asyncpg", "aiosqlite": "aiosqlite", "redis": "redis", "pymongo": "pymongo", "motor": "motor",
    "pydantic": "pydantic", "openai": "openai", "anthropic": "anthropic", "jwt": "pyjwt", "dateutil": "python-dateutil",
    "pytz": "pytz", "apscheduler": "apscheduler", "schedule": "schedule", "aioschedule": "aioschedule",
    "loguru": "loguru", "aiofiles": "aiofiles",
}
# Стандартная библиотека Python агента плюс модули, убранные в 3.12–3.13: их ещё импортирует код
# для старых версий, а сборка может идти на другой версии, чем у агента.
PYTHON_STDLIB = set(sys.stdlib_module_names) | {
    "aifc", "asynchat", "asyncore", "audioop", "cgi", "cgitb", "chunk", "crypt", "distutils", "imghdr", "imp", "lib2to3",
    "mailcap", "msilib", "nis", "nntplib", "ossaudiodev", "pipes", "smtpd", "sndhdr", "spwd", "sunau", "telnetlib", "uu", "xdrlib",
}
PYTHON_IMPORT = re.compile(r"^[ \t]*(?:from[ \t]+([A-Za-z_]\w*)[\w.]*[ \t]+import\b|import[ \t]+([^#\n;]+))", re.M)
# Точка входа Python-приложения ищется в этих файлах по порядку.
PYTHON_ENTRIES = ["main.py", "app.py", "app/main.py", "src/main.py", "server.py", "api.py", "wsgi.py", "asgi.py", "streamlit_app.py"]
SEGMENT = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,99}$")
MAJOR = re.compile(r"\d+(\.\d+)?")
# Одно условие версии Node из engines или .nvmrc: >=20.19.0, ^22, 20.x, v24.1.0.
NODE_CLAUSE = re.compile(r"^(>=|<=|>|<|=|\^|~>?)?v?(\d+|[xX*])(?:\.(\d+|[xX*]))?(?:\.(\d+|[xX*]))?(?:[-+]\S*)?$")
NODE_HYPHEN = re.compile(r"^(\S+)\s+-\s+(\S+)$")
NODE_OP_SPACE = re.compile(r"(>=|<=|>|<|=|\^|~>?)\s+")
# Ограничение версии Python из [project] в pyproject.toml.
REQUIRES_PYTHON = re.compile(r"""^\s*requires-python\s*=\s*["']([^"']+)["']""", re.M)
SPEC_CLAUSE = re.compile(r"^(>=|<=|==|!=|~=|>|<)\s*(\d+)(?:\.(\d+))?")
GO_DIRECTIVE = re.compile(r"^go\s+(\d+\.\d+)", re.M)
GO_PACKAGE_MAIN = re.compile(r"^package\s+main\s*$", re.M)

# Хранилище pnpm — в кэше сборки приложения. Каталог задан явно: иначе pnpm, не сумев сделать жёсткие
# ссылки из другой файловой системы, завёл бы хранилище в самой папке релиза. Кэш смонтирован отдельно
# от релиза, и жёсткие ссылки между ними невозможны, поэтому копирование задано явно и главнее
# package-import-method из .npmrc проекта.
PNPM_STORE = '--store-dir "${AIRLOK_CACHE:-/tmp}/pnpm-store" --package-import-method copy'
NPM_INSTALL = ("if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; "
               f"elif [ -f pnpm-lock.yaml ]; then npx --yes pnpm@{PNPM_VERSION} install --frozen-lockfile {PNPM_STORE}; "
               f"elif [ -f yarn.lock ]; then npx --yes yarn@{YARN_VERSION} install --frozen-lockfile; "
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
        self.module = data.get("module") or ""
        engines = data.get("engines")
        node = engines.get("node") if isinstance(engines, dict) else None
        # "engines": {"node": 20} — число вместо строки встречается в живых проектах.
        self.engines = str(node) if isinstance(node, (str, int, float)) and not isinstance(node, bool) else ""
        self.package_manager = data.get("packageManager")

    def has(self, dep):
        return dep in self.deps

    def manager(self):
        """Менеджер пакетов и его точная версия из packageManager. Версия попадает в команду
        оболочки, поэтому значение не того вида не учитывается: (имя, версия, предупреждение)."""
        spec = self.package_manager
        if spec is None:
            return "", "", ""
        m = PACKAGE_MANAGER.match(spec) if isinstance(spec, str) else None
        if m is None or m.end() != len(spec):
            return "", "", ("packageManager в package.json не разобрать: нужны имя и точная версия, например yarn@4.5.0, — "
                            "менеджер пакетов выбран по lock-файлу")
        return m.group(1), m.group(2), ""


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


def js_app(root, pkg):
    """Как запускается JS-фреймворк, у которого зависимость от Vite или Astro не значит статику:
    (имя, команда сервера, каталог сайта, почему не запустится). Без команды и каталога решает
    scripts.start."""
    scripts = pkg.scripts
    if pkg.has("@sveltejs/kit"):
        if pkg.has("@sveltejs/adapter-node"):
            # Без настоящего протокола SvelteKit отклоняет POST-формы как запросы с чужого сайта.
            return "SvelteKit", "PROTOCOL_HEADER=x-forwarded-proto node build", "", ""
        if pkg.has("@sveltejs/adapter-static"):
            return "SvelteKit", "", "build", ""
        return "SvelteKit", "", "", ("SvelteKit запускается с @sveltejs/adapter-node: поставьте его и укажите в vite.config.ts или svelte.config.js "
                                     "вместо текущего адаптера, либо задайте run.command в airlok.yml")
    if pkg.has("nuxt"):
        if "nuxt generate" in scripts.get("build", ""):
            return "Nuxt", "", ".output/public", ""
        return "Nuxt", NITRO_SERVER, "", ""
    if pkg.has("@tanstack/react-start") or pkg.has("@tanstack/solid-start"):
        if scripts.get("start"):
            return "TanStack Start", "", "", ""
        if pkg.has("nitro"):
            return "TanStack Start", NITRO_SERVER, "", ""
        return "TanStack Start", "", "", ("TanStack Start нужен сервер: поставьте nitro, подключите nitro() из nitro/vite в vite.config.ts, "
                                          "либо задайте run.command в airlok.yml")
    if pkg.has("@react-router/dev"):
        config = "".join(read_text(root, f) for f in ("react-router.config.ts", "react-router.config.js", "react-router.config.mjs"))
        if REACT_ROUTER_SPA.search(config):
            return "React Router", "", "build/client", ""
        if scripts.get("start"):
            return "React Router", "", "", ""
        if pkg.has("@react-router/serve"):
            return "React Router", "node_modules/.bin/react-router-serve ./build/server/index.js", "", ""
        return "React Router", "", "", ("React Router нужен сервер: поставьте @react-router/serve и добавьте scripts.start, "
                                        "либо задайте run.command в airlok.yml")
    if pkg.has("astro") and pkg.has("@astrojs/node"):
        config = "".join(read_text(root, f) for f in ("astro.config.mjs", "astro.config.ts", "astro.config.js"))
        # В режиме middleware Astro — обработчик внутри сервера проекта.
        return "Astro", "" if ASTRO_MIDDLEWARE.search(config) else "node ./dist/server/entry.mjs", "", ""
    if pkg.has("astro") and any(pkg.has(a) for a in ("@astrojs/vercel", "@astrojs/netlify", "@astrojs/cloudflare")):
        return "Astro", "", "", ("этот адаптер Astro работает только на своей платформе: поставьте @astrojs/node и укажите его "
                                 "в astro.config, либо задайте run.command в airlok.yml")
    return "", "", "", ""


def has_bun_lock(root):
    return (exists(root, "bun.lock") or exists(root, "bun.lockb")) and not any(
        exists(root, f) for f in ("package-lock.json", "pnpm-lock.yaml", "yarn.lock"))


def uses_bun(root, build_command, run_command, pkg):
    """Проект ставится или запускается через Bun. Lock-файл рядом с другим менеджером не в счёт:
    Lovable, например, кладёт и bun.lockb, и package-lock.json."""
    if pkg is None:
        return False
    commands = (build_command, run_command, pkg.scripts.get("build", ""), pkg.scripts.get("start", ""), pkg.scripts.get("start:prod", ""))
    return has_bun_lock(root) or pkg.has("elysia") or any(BUN_COMMAND.search(c) for c in commands)


def bun_entry(root, pkg):
    """Файл, который запустит Bun, если у проекта нет scripts.start."""
    return next((f for f in (pkg.module, pkg.main, *BUN_ENTRIES) if f and exists(root, f)), "")


def js_install(root, pkg, bun):
    """Установка зависимостей: {install, manager, corepack, warnings}. Менеджер выбирает lock-файл,
    а версию — packageManager из package.json. Bun ставится внутрь проекта через npm и остаётся
    доступен сборке."""
    name, version, warning = pkg.manager() if pkg is not None else ("", "", "")
    deps = {"install": NPM_INSTALL, "manager": "", "corepack": False, "warnings": [warning] if warning else []}
    if bun:
        setup = bun_setup(version if name == "bun" else BUN_VERSION)
        deps["install"] = setup + " && bun install" + (" --frozen-lockfile" if has_bun_lock(root) else "")
    elif exists(root, "package-lock.json"):
        pass
    elif exists(root, "pnpm-lock.yaml"):
        deps["manager"] = "pnpm"
        if name == "pnpm":
            deps["install"] = f"npx --yes pnpm@{version} install --frozen-lockfile {PNPM_STORE}"
    elif exists(root, "yarn.lock"):
        deps["manager"] = "yarn"
        lock = BERRY_METADATA.search(read_text(root, "yarn.lock"))
        if name == "yarn" and not version.startswith("1."):
            # corepack сам возьмёт версию из packageManager и сверит её хеш, если он указан.
            deps.update(install=berry_install(""), corepack=True)
        elif name == "yarn":
            deps["install"] = f"npx --yes yarn@{version} install --frozen-lockfile"
        elif lock:
            yarn = BERRY_VERSIONS.get(lock.group(1))
            if yarn is None:
                yarn = BERRY_VERSIONS[LATEST_BERRY_LOCK]
                deps["warnings"].append(f"yarn.lock записан незнакомой сборке версией Yarn — ставлю Yarn {yarn}. Если установка упадёт, "
                                        "укажите свою версию в packageManager в package.json: её записывает yarn set version")
            deps.update(install=berry_install(yarn), corepack=True)
    return deps


def berry_install(version):
    """Установка Yarn 2+ через corepack; version — какой Yarn поставить, если packageManager его не задаёт."""
    install = (f"{COREPACK_ENV} && mkdir -p {COREPACK_DIR}/bin && corepack enable --install-directory {COREPACK_DIR}/bin yarn")
    if version:
        install += f" && corepack install -g yarn@{version}"
    return install + " && yarn install --immutable"


def apply_js(plan, deps):
    plan.update(install=deps["install"], manager=deps["manager"], corepack=deps["corepack"])
    plan["warnings"] = plan["warnings"] + deps["warnings"]


def detect(root, stack="", kind="", build_command="", build_output="", run_command="", node="", python="", go=""):
    """План сборки: то, что задано в airlok.yml, плюс найденное по файлам проекта."""
    pkg = load_package(root)
    py_deps = "\n".join(read_text(root, f) for f in ("requirements.txt", "pyproject.toml", "Pipfile")).lower()
    is_python = any(exists(root, f) for f in ("requirements.txt", "pyproject.toml", "Pipfile"))

    plan = {"stack": stack, "framework": "", "kind": kind, "node": "", "python": "", "go": "", "bun": False,
            "install": "", "build": "", "output": "", "start": "", "manager": "", "corepack": False, "warnings": []}
    if not plan["stack"]:
        plan["stack"], plan["framework"] = guess_stack(root, pkg, is_python)
        if not plan["stack"]:
            raise DetectError("не удалось понять, что это за проект: нет package.json, requirements.txt, pyproject.toml, "
                              "go.mod, index.html или main.py. Укажите стек в airlok.yml (app.stack: static, nextjs, node, python или go) "
                              "или каталог проекта внутри репозитория")

    if plan["stack"] in ("static", "nextjs", "node"):
        plan["node"] = node_version(root, node, pkg)
        plan["bun"] = uses_bun(root, build_command, run_command, pkg)
    elif plan["stack"] == "python":
        plan["python"] = python_version(root, python)
    elif plan["stack"] == "go":
        plan["go"] = go_version(root, go)

    if plan["kind"] == "worker" and plan["stack"] in ("static", "nextjs"):
        raise DetectError("фоновым (app.type: worker) может быть только Node-, Python- или Go-приложение")
    if plan["stack"] == "static":
        plan["kind"] = "web"
        return plan_static(root, pkg, build_command, build_output, plan)
    if plan["stack"] == "nextjs":
        if pkg is None:
            raise DetectError("для Next.js нужен package.json")
        apply_js(plan, js_install(root, pkg, plan["bun"]))
        plan.update(build=build_command or "npm run build", kind="web")
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
        name, _, output, _ = js_app(root, pkg)
        if name:
            return ("static" if output else "node"), name
        server = next((name for dep, name in NODE_SERVERS if pkg.has(dep)), "")
        # У CRA и Vite тоже бывает scripts.start, но это сервер разработки, а не прод.
        if not server:
            for dep, _, name in STATIC_BUILDERS:
                if pkg.has(dep):
                    return "static", name
        if server or pkg.scripts.get("start"):
            return "node", server
        if any(pkg.has(b) for b in NODE_BOTS) or (has_bun_lock(root) and bun_entry(root, pkg)):
            return "node", ""
    if is_python:
        return "python", ""
    if exists(root, "index.html"):
        return "static", "HTML"
    # Новички часто приносят один main.py без requirements.txt.
    if any(exists(root, f) for f in PYTHON_START_FILES):
        return "python", ""
    return "", ""


def plan_static(root, pkg, build_command, build_output, plan):
    plan["output"] = build_output
    if build_command:
        plan["build"] = build_command
    elif pkg is not None and pkg.scripts.get("build"):
        plan["build"] = "npm run build"
    if pkg is not None and plan["build"]:
        apply_js(plan, js_install(root, pkg, plan["bun"]))
    if not plan["output"] and plan["build"] and pkg is not None:
        plan["output"] = js_app(root, pkg)[2]
    if not plan["output"] and plan["build"] and pkg is not None:
        plan["output"] = next((out for dep, out, _ in STATIC_BUILDERS if pkg.has(dep)), "")
    plan["framework"] = plan["framework"] or "статический сайт"
    return plan


def shell_words(command):
    """Где начинаются слова команды. None — в ней несколько процессов, подстановка команды или
    незакрытая кавычка. Перенаправления ввода-вывода такой команде не мешают."""
    starts, quote, in_word, i = [], "", False, 0
    while i < len(command):
        ch = command[i]
        if quote == "'":
            quote = "" if ch == "'" else quote
        elif ch == "`" or command.startswith("$(", i):
            return None
        elif ch == "\\":
            if not in_word:
                starts.append(i)
                in_word = True
            i += 1
        elif quote == '"':
            quote = "" if ch == '"' else quote
        elif ch in ";|()\n\r" or ch == "&" and command[i - 1 : i] not in (">", "<"):
            return None
        elif ch in " \t":
            in_word = False
        else:
            if ch in "'\"":
                quote = ch
            if not in_word:
                starts.append(i)
                in_word = True
        i += 1
    return None if quote else starts


def shell_exec(command):
    """Та же команда с exec перед программой: иначе главным процессом останется sh, и приложение
    не получит SIGTERM. Переменные впереди остаются: FOO=1 exec node передаёт их программе в dash,
    bash и busybox. None — exec сюда не поставить."""
    starts = shell_words(command)
    for i, start in enumerate(starts or []):
        word = command[start : starts[i + 1] if i + 1 < len(starts) else None].rstrip(" \t")
        if SHELL_ASSIGNMENT.match(word):
            continue
        if not SHELL_PROGRAM.fullmatch(word) or word in SHELL_KEYWORDS:
            return None
        return command[:start] + "exec " + command[start:]
    return None


def mentions_npm_variable(root):
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SOURCE_DIRS_SKIPPED]
        for name in files:
            path = os.path.join(folder, name)
            if os.path.splitext(name)[1] not in SOURCE_EXTS or os.path.islink(path) or os.path.getsize(path) > MAX_SOURCE_SIZE:
                continue
            with open(path, errors="replace") as f:
                if NPM_VARIABLE.search(f.read()):
                    return True
    return False


def start_script(root, pkg, name, via_npm):
    """Чем запустить скрипт package.json: им самим, если он простой. npm завершается по SIGTERM, не
    дождавшись приложения. npm остаётся, если нужны его хуки или переменные, которые задаёт только он."""
    script = pkg.scripts[name].strip()
    hooks = "pre" + name in pkg.scripts or "post" + name in pkg.scripts
    if hooks or not shell_exec(script) or NPM_VARIABLE.search(script) or mentions_npm_variable(root):
        return via_npm
    return script


def plan_node(root, pkg, build_command, run_command, plan):
    if pkg is None:
        raise DetectError("для Node-приложения нужен package.json")
    apply_js(plan, js_install(root, pkg, plan["bun"]))
    plan["build"] = build_command or ("npm run build" if pkg.scripts.get("build") else "")
    app_name, app_start, _, app_problem = js_app(root, pkg)
    start = run_command
    if not start:
        if app_problem:
            raise DetectError(app_problem)
        if app_start:
            start = app_start
        elif pkg.scripts.get("start:prod"):
            # NestJS: start — через CLI из devDependencies, а нужен собранный код.
            start = start_script(root, pkg, "start:prod", "npm run start:prod")
        elif pkg.scripts.get("start"):
            start = start_script(root, pkg, "start", "npm start")
        elif plan["bun"] and bun_entry(root, pkg):
            start = "bun run " + bun_entry(root, pkg)
        elif pkg.main and exists(root, pkg.main):
            start = "node " + pkg.main
        else:
            start = next(("node " + f for f in ("server.js", "index.js", "app.js", "main.js") if exists(root, f)), "")
    if not start:
        raise DetectError("не понятно, как запустить приложение: добавьте scripts.start в package.json или run.command в airlok.yml")
    plan["start"] = start
    if not plan["kind"]:
        plan["kind"] = "web"
        has_server = bool(app_name) or any(pkg.has(dep) for dep, _ in NODE_SERVERS)
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


def imported_modules(src):
    """Модули верхнего уровня из import и from … import, без относительных."""
    out = []
    for frm, names in PYTHON_IMPORT.findall(src):
        if frm:
            out.append(frm)
            continue
        for part in names.split(","):
            name = part.strip("() \t\\").split(" ", 1)[0].split(".", 1)[0]
            if name:
                out.append(name)
    return out


def package_files(root, name):
    """.py-файлы своего пакета, не больше сотни: в огромной папке это уже не пакет приложения."""
    out = []
    for dirpath, dirs, files in os.walk(os.path.join(root, name)):
        dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in ("__pycache__", "venv"))
        for f in sorted(files):
            path = os.path.join(dirpath, f)
            if f.endswith(".py") and os.path.isfile(path) and not os.path.islink(path):
                out.append(os.path.relpath(path, root))
                if len(out) >= 100:
                    return out
    return out


def infer_python_packages(root):
    """Пакеты pip по импортам кода, когда у проекта нет requirements.txt. Код читается из корня и из
    своих пакетов, которые он импортирует: чужие исходники в папке на список не влияют."""
    local, files = set(), []
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if name.endswith(".py") and os.path.isfile(path) and not os.path.islink(path):
            local.add(name[:-3])
            files.append(name)
        elif os.path.isdir(path) and not os.path.islink(path) and not name.startswith("."):
            local.add(name)

    modules, scanned = set(), set()
    while files:
        for m in imported_modules(read_text(root, files.pop(0))):
            if m not in local:
                modules.add(m)
            elif m not in scanned:
                scanned.add(m)
                files += package_files(root, m)

    unknown = sorted(m for m in modules if m not in PYTHON_PACKAGES and m not in PYTHON_STDLIB)
    if unknown:
        raise DetectError("у Python-приложения нет requirements.txt, а по импортам не понять, какие пакеты ставить: "
                          f"{', '.join(unknown)}. Положите в корень requirements.txt со списком пакетов (pip freeze > requirements.txt)")
    return sorted(PYTHON_PACKAGES[m] for m in modules if m in PYTHON_PACKAGES)


def plan_python(root, deps, run_command, plan):
    install = ["python -m venv /app/.venv"]
    pip = "/app/.venv/bin/pip install --disable-pip-version-check"
    if exists(root, "requirements.txt"):
        install.append(pip + " -r requirements.txt")
    elif exists(root, "pyproject.toml"):
        install.append(pip + " .")
    else:
        pkgs = infer_python_packages(root)
        if pkgs:
            # Имена — из нашего списка, кавычки нужны из-за psycopg[binary].
            install.append(pip + " " + " ".join(f"'{p}'" for p in pkgs))
        deps += "\n" + "\n".join(pkgs)

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
    install.append(PY_COMPILE)
    plan["install"] = " && ".join(install)
    plan["compile"] = PY_COMPILE
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
    """Версия Go для директивы go в go.mod. Директива — наименьшая подходящая версия, а язык
    и GODEBUG модуля задаёт она сама, поэтому код собирается версией по умолчанию или новее:
    старые версии перестают получать исправления безопасности."""
    if wanted:
        if wanted not in GO_VERSIONS:
            raise DetectError(f"runtime.go — одна из версий {', '.join(GO_VERSIONS)}")
        return wanted
    m = GO_DIRECTIVE.search(read_text(root, "go.mod"))

    def key(v):
        return tuple(map(int, v.split(".")))
    if m is None or key(m.group(1)) <= key(DEFAULT_GO):
        return DEFAULT_GO
    if key(m.group(1)) > key(GO_VERSIONS[-1]):
        raise DetectError(f"go.mod требует Go {m.group(1)}, а поддерживаются {GO_VERSIONS[0]}–{GO_VERSIONS[-1]}")
    return GO_VERSIONS[-1]


def node_version(root, wanted, pkg):
    if wanted:
        if wanted not in NODE_VERSIONS:
            raise DetectError(f"runtime.node — одна из версий {', '.join(NODE_VERSIONS)}")
        return wanted
    for spec in (version_line(read_text(root, ".nvmrc")), version_line(read_text(root, ".node-version")),
                 (pkg.engines if pkg else "").strip()):
        if spec:
            v = pick_node(spec)
            if v:
                return v
    return DEFAULT_NODE


def pick_node(spec):
    """Версия по умолчанию, если подходит, иначе самая новая подходящая. Ограничение, которое пускает
    любую версию (lts/*, node, ">=20"), выбора не сужает: тогда решает следующий источник, а без него —
    версия по умолчанию, так что engines ">=20" получает 22."""
    fit = [v for v in NODE_VERSIONS if fits_node(v, spec)]
    if not fit or len(fit) == len(NODE_VERSIONS):
        return ""
    return DEFAULT_NODE if DEFAULT_NODE in fit else fit[-1]


def version_line(text):
    """Версия из .nvmrc или .node-version: первая строка, кроме комментариев."""
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            return line
    return ""


def fits_node(version, spec):
    """Подходит ли мажорная версия Node под ограничение в духе npm semver: "20",
    "^20.19.0 || >=22.12.0", ">=20 <23", "18 - 22", "lts/jod". Сравниваются только мажорные
    версии: в образе всегда последний выпуск своей ветки."""
    spec = spec.strip()
    if spec.lower().startswith("lts/"):
        name = spec[4:].lower()
        return NODE_LTS[name] == version if name in NODE_LTS else True
    major = int(version)
    return any(fits_node_range(major, alt.strip()) for alt in spec.split("||"))


def fits_node_range(major, r):
    h = NODE_HYPHEN.match(r)
    if h:
        return fits_node_clause(major, ">=" + h.group(1)) and fits_node_clause(major, "<=" + h.group(2))
    return all(fits_node_clause(major, c) for c in NODE_OP_SPACE.sub(r"\1", r).split())


def fits_node_clause(major, clause):
    m = NODE_CLAUSE.match(clause)
    if not m or not m.group(2).isdigit():
        return True  # node, latest, *, x — не ограничение версии
    want = int(m.group(2))
    # >20.1 пускает новые выпуски 20.x, а >20 — только 21 и выше; <20.5 пускает ранние 20.x,
    # а <20 и <20.0.0 — нет.
    minor, patch = m.group(3) or "", m.group(4) or ""
    specific = minor.isdigit()
    above_branch = specific and (int(minor) > 0 or (patch.isdigit() and int(patch) > 0))
    op = m.group(1) or ""
    if op == ">=":
        return major >= want
    if op == ">":
        return major >= want if specific else major > want
    if op == "<=":
        return major <= want
    if op == "<":
        return major <= want if above_branch else major < want
    return major == want


def runtime_warnings(plan):
    """Предупреждения о версиях среды без исправлений безопасности и о том, что из настроек
    проекта сборка не учла."""
    versions = dict(plan)
    # Без сборки статике Node не нужен.
    if plan["stack"] == "static" and not plan.get("install") and not plan.get("build"):
        versions["node"] = ""
    return [f"{name} {version} больше не получает исправлений безопасности (поддержка закончилась {ended}) — {hint}"
            for key, name, version, ended, hint in UNSUPPORTED_RUNTIMES if versions.get(key) == version] + plan.get("warnings", [])


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


def drop_outside_links(root):
    """Удаляет ссылки, которые ведут за пределы каталога, и возвращает их пути. Код из репозитория
    читают и собирают от root: ссылка на /etc отдала бы ему файлы сервера. Абсолютные ссылки тоже
    удаляются: внутрь проекта на сервере они не ведут, путь у репозитория там другой."""
    real_root = os.path.realpath(root)
    links = [os.path.join(folder, name) for folder, dirs, files in os.walk(root) for name in dirs + files
             if os.path.islink(os.path.join(folder, name))]
    # Сначала решаем по всем, потом удаляем: ссылка на ссылку наружу иначе зависела бы от порядка обхода.
    outside = [p for p in links
               if os.path.isabs(os.readlink(p)) or os.path.commonpath([real_root, os.path.realpath(p)]) != real_root]
    for path in outside:
        os.unlink(path)
    return sorted(os.path.relpath(p, root) for p in outside)


# Скрипты проекта, которые npm, pnpm, Yarn и Bun запускают при установке. Они могут читать любые
# файлы проекта, и их результат по файлам зависимостей не предсказать.
LIFECYCLE_SCRIPTS = ("preinstall", "install", "postinstall", "preprepare", "prepare", "postprepare", "dependencies")
# Зависимости из папок проекта: их содержимое в отпечаток зависимостей не входит.
LOCAL_DEP_PREFIXES = ("file:", "link:", "portal:", "workspace:", "patch:")
# Lock-файлы, которые закрепляют все версии: без них установка каждый раз берёт свежие, а снимок
# заморозил бы их.
NODE_LOCKS = ("package-lock.json", "npm-shrinkwrap.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock", "bun.lockb")
# Откуда postinstall @prisma/client генерирует клиент в node_modules/.prisma: схема не входит в
# файлы зависимостей, и её отпечаток добавляется к ключу снимка.
PRISMA_FILES = ("schema.prisma", "prisma", "prisma.config.ts", "prisma.config.js", "prisma.config.mjs", "prisma.config.cjs",
                "prisma.config.mts")
MAX_EXTRA_FILES = 1000
# Строки requirements.txt, которые не тянут ничего, кроме пакетов из индекса.
PIP_OPTIONS = ("-i ", "--index-url", "--extra-index-url", "--trusted-host", "--prefer-binary", "--only-binary", "--no-binary",
               "--pre")
PINNED_REQUIREMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[^\]]*\])?\s*===?\s*[^\s*,]+$")
HASH_OPTION = re.compile(r"--hash[= ]\S+")


class NotReusable(Exception):
    pass


def reusable_install(root, stack):
    """Отпечаток того, что кроме файлов зависимостей и команды установки задаёт её результат ("" —
    ничего), или None — снимок прошлой сборки проекту не подходит: установка зависит от того, что
    по файлам зависимостей не предсказать, или берёт свежие версии. Правила — как у сборщика
    хостинга (internal/builder/snapshot.go)."""
    try:
        if stack == "python":
            return "" if pinned_requirements(root) else None
        if stack in ("node", "nextjs", "static"):
            return reusable_node(root)
    except NotReusable:
        return None
    # Go и так берёт модули из кэша без распаковки, а Dockerfile собирает сам Docker.
    return None


def regular_file(root, name):
    path = os.path.join(root, name)
    return os.path.isfile(path) and not os.path.islink(path)


def pinned_requirements(root):
    """В requirements.txt все версии закреплены и нет ничего, что читает другие файлы. Без него
    пакеты выводятся из импортов, без версий."""
    if not regular_file(root, "requirements.txt"):
        return False
    try:
        with open(os.path.join(root, "requirements.txt"), encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return False
    for line in text.replace("\r\n", "\n").replace("\\\n", " ").split("\n"):
        line = HASH_OPTION.sub("", line.split(" #", 1)[0]).strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("-"):
            # -r, -c, -e и --find-links читают другие файлы и папки.
            if not line.startswith(PIP_OPTIONS):
                return False
        elif not PINNED_REQUIREMENT.match(line.split(";", 1)[0].strip()):
            return False
    return True


def reusable_node(root):
    if os.path.lexists(os.path.join(root, "pnpm-workspace.yaml")):
        raise NotReusable
    if not any(regular_file(root, name) for name in NODE_LOCKS):
        raise NotReusable
    try:
        with open(os.path.join(root, "package.json"), encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        raise NotReusable from None
    if not isinstance(data, dict) or "workspaces" in data:
        raise NotReusable
    scripts = data.get("scripts") or {}
    if not isinstance(scripts, dict) or any(name in scripts for name in LIFECYCLE_SCRIPTS):
        raise NotReusable
    # Правки пакетов из папки проекта (pnpm и Bun) тоже не входят в отпечаток.
    pnpm = data.get("pnpm")
    if "patchedDependencies" in data or (isinstance(pnpm, dict) and "patchedDependencies" in pnpm):
        raise NotReusable
    deps = [data.get(k) or {} for k in ("dependencies", "devDependencies", "optionalDependencies")]
    groups = deps + [data.get(k) or {} for k in ("overrides", "resolutions")]
    if isinstance(pnpm, dict):
        groups.append(pnpm.get("overrides") or {})
    if not all(local_deps_free(g) for g in groups):
        raise NotReusable
    if not any("@prisma/client" in d or "prisma" in d for d in deps):
        return ""
    paths = list(PRISMA_FILES)
    prisma = data.get("prisma")
    schema = prisma.get("schema") if isinstance(prisma, dict) else None
    if schema is not None:
        if not isinstance(schema, str) or not schema or os.path.isabs(schema):
            raise NotReusable
        schema = os.path.normpath(schema)
        if schema == ".." or schema.startswith("../"):
            raise NotReusable
        paths.append(schema)
    return files_fingerprint(root, paths)


def files_fingerprint(root, paths):
    """Отпечаток обычных файлов paths внутри root (каталоги — целиком). Ссылки не читаются: путь
    через ссылку — NotReusable, ссылки внутри каталогов пропускаются, как и всё, кроме файлов."""
    h = hashlib.sha256()
    count = 0
    for rel in paths:
        if rel != "." and any(os.path.islink(os.path.join(root, *rel.split("/")[:i + 1])) for i in range(len(rel.split("/")))):
            raise NotReusable
        top = os.path.join(root, rel)
        if os.path.isdir(top):
            files = []
            for folder, dirs, names in os.walk(top, onerror=raise_walk):
                dirs.sort()
                files += [os.path.join(folder, n) for n in sorted(names)]
        elif os.path.lexists(top):
            files = [top]
        else:
            continue
        for path in files:
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            count += 1
            if count > MAX_EXTRA_FILES:
                raise NotReusable
            try:
                with open(path, "rb") as f:
                    body = f.read()
            except OSError:
                raise NotReusable from None
            h.update(f"{os.path.relpath(path, root)}\0{len(body)}\0".encode())
            h.update(body)
    return h.hexdigest()


def raise_walk(err):
    raise NotReusable from err


def local_deps_free(deps):
    """В словаре зависимостей (у overrides — вложенном) нет ссылок на папки проекта."""
    if not isinstance(deps, dict):
        return False
    for value in deps.values():
        if isinstance(value, dict):
            if not local_deps_free(value):
                return False
        elif not isinstance(value, str) or value.startswith(LOCAL_DEP_PREFIXES):
            return False
    return True


def build_prefix(plan):
    """Окружение, которое установка оставляет сборке, когда они идут разными запусками контейнера."""
    parts = []
    if plan.get("corepack"):
        parts.append(COREPACK_ENV)
    if plan.get("bun"):
        parts.append(f"export PATH={BUN_BIN}:$PATH")
    return " && ".join(parts)


def printable(text):
    return "".join(c if c.isprintable() else "?" for c in text)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "drop-links":
        dropped = [printable(p) for p in drop_outside_links(sys.argv[2])]
        if dropped:
            more = f" и ещё {len(dropped) - 20}" if len(dropped) > 20 else ""
            print(", ".join(dropped[:20]) + more)
        return
    if len(sys.argv) >= 3 and sys.argv[1] == "static-output":
        output = sys.argv[3] if len(sys.argv) > 3 else ""
        try:
            print(static_output(sys.argv[2], output, "built" in sys.argv[4:]))
        except DetectError as e:
            sys.exit(f"Ошибка: {e}")
        return
    if len(sys.argv) == 4 and sys.argv[1] == "reusable-install":
        extra = reusable_install(sys.argv[2], sys.argv[3])
        if extra is None:
            sys.exit(1)
        print(extra)
        return
    sys.exit("использование: detect.py static-output КАТАЛОГ [ВЫХОД] [built] | drop-links КАТАЛОГ | reusable-install КАТАЛОГ СТЕК")


if __name__ == "__main__":
    main()
