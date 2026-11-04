"""Определение стека и манифест без airlok.yml: python3 -m unittest discover tests

Случаи те же, что у сборщика хостинга Airlok: проект должен публиковаться одинаково.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "recipes"))

from detect import BUN_SETUP, DEFAULT_GO, DEFAULT_NODE, DEFAULT_PYTHON, NPM_INSTALL, DetectError, detect, shell_exec, static_output  # noqa: E402
from dockerfile import guess_port  # noqa: E402
from manifest import ManifestError, parse  # noqa: E402


def project(files):
    root = tempfile.mkdtemp()
    for name, text in files.items():
        path = os.path.join(root, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return root


class DetectTest(unittest.TestCase):
    def check(self, files, **want):
        plan = detect(project(files))
        for key, value in want.items():
            self.assertEqual(plan[key], value, key)

    def test_stacks(self):
        self.check({"index.html": "<h1>hi</h1>"}, stack="static", framework="HTML", build="")
        self.check({"package.json": '{"scripts":{"start":"vite","build":"vite build"},"devDependencies":{"vite":"^6"}}',
                    ".nvmrc": "v20.11.0\n"},
                   stack="static", framework="Vite", node="20", install=NPM_INSTALL, build="npm run build", output="dist")
        self.check({"package.json": '{"dependencies":{"next":"15"},"engines":{"node":">=24"}}'},
                   stack="nextjs", node="24", build="npm run build")
        self.check({"package.json": '{"scripts":{"start":"node server.js"},"dependencies":{"express":"5"}}'},
                   stack="node", framework="Express", node=DEFAULT_NODE, start="node server.js", kind="web")
        self.check({"package.json": '{"scripts":{"build":"nest build","start":"nest start","start:prod":"node dist/main"},'
                                    '"dependencies":{"@nestjs/core":"11"}}'},
                   framework="NestJS", build="npm run build", start="node dist/main")
        self.check({"package.json": '{"main":"bot.js","dependencies":{"telegraf":"4"}}', "bot.js": ""},
                   stack="node", kind="worker", start="node bot.js")

    def test_python(self):
        self.check({"requirements.txt": "fastapi==0.115\nuvicorn[standard]\n", ".python-version": "3.11.9\n",
                    "app/main.py": "from fastapi import FastAPI\n\napi: FastAPI = FastAPI(title='x')\n"},
                   stack="python", framework="FastAPI", python="3.11",
                   start="uvicorn app.main:api --host 0.0.0.0 --port 8080 --proxy-headers --forwarded-allow-ips '*'")
        plan = detect(project({"requirements.txt": "Flask>=3\n", "app.py": "import flask\napp = flask.Flask(__name__)\n"}))
        self.assertEqual(plan["start"], "gunicorn app:app --bind 0.0.0.0:8080 --workers 2 --access-logfile -")
        self.assertIn("pip install --no-cache-dir --disable-pip-version-check gunicorn", plan["install"],
                      "сервер приложений ставится, если проект его не указал")
        self.check({"pyproject.toml": '[project]\ndependencies = ["Django>=5", "gunicorn"]\n', "manage.py": "",
                    "mysite/wsgi.py": "application = None\n", "runtime.txt": "python-3.13.1"},
                   framework="Django", python="3.13",
                   start="gunicorn mysite.wsgi:application --bind 0.0.0.0:8080 --workers 2 --access-logfile -")
        self.check({"requirements.txt": "aiogram==3\n", "bot.py": ""},
                   kind="worker", framework="бот (aiogram)", start="python bot.py", python=DEFAULT_PYTHON)

    def test_go(self):
        build = "mkdir -p .airlok && go build -trimpath -ldflags='-s -w' -o .airlok/app "
        # Как PonchesDev/favicon-uploader: программа в cmd/, рядом Dockerfile и тесты.
        self.check({"go.mod": "module favicon-uploader\n\ngo 1.27.0\n\nrequire (\n\tgo.uber.org/zap v1.28.0\n)\n", "go.sum": "",
                    "Dockerfile": "FROM golang:1.27-alpine\n", "cmd/uploader/main.go": "package main\n",
                    "internal/config/config.go": "package config\n", "internal/config/config_test.go": "package config\n"},
                   stack="go", framework="Go", kind="web", go="1.27", install="go mod download",
                   build=build + "./cmd/uploader", start="/app/.airlok/app")
        self.check({"go.mod": "module shop\n\ngo 1.21\n\nrequire github.com/gin-gonic/gin v1.10.0\n", "main.go": "package main\n",
                    "package.json": '{"devDependencies":{"vite":"6"}}'},
                   stack="go", framework="Gin", go=DEFAULT_GO, install="", build=build + ".")
        self.check({"go.mod": "module bot\n\ngo 1.26\n\nrequire gopkg.in/telebot.v4 v4.0.0\n", "go.sum": "", "vendor/modules.txt": "",
                    "bot.go": "package main\n"},
                   kind="worker", framework="бот (gopkg.in/telebot)", go="1.26", install="")
        self.check({"go.mod": "module bot\n\ngo 1.27\n\nrequire (\n\tgithub.com/go-telegram-bot-api/telegram-bot-api/v5 v5.5.1\n"
                              "\tgithub.com/labstack/echo/v4 v4.12.0\n)\n", "main.go": "package main\n"},
                   kind="web", framework="Echo")
        plan = detect(project({"go.mod": "module x\n\ngo 1.27\n"}), build_command="go build -o bin/srv ./srv",
                      run_command="./bin/srv", go="1.25")
        self.assertEqual((plan["build"], plan["start"], plan["go"]), ("go build -o bin/srv ./srv", "./bin/srv", "1.25"))

    def test_go_errors(self):
        for files, want in (({"go.mod": "module x\n\ngo 1.30\n", "main.go": "package main\n"}, "требует Go 1.30"),
                            ({"go.mod": "module x\n", "x.go": "package x\n", "x_test.go": "package main\n"}, "package main"),
                            ({"go.mod": "module x\n", "cmd/api/main.go": "package main\n", "cmd/worker/main.go": "package main\n"},
                             "./cmd/api, ./cmd/worker")):
            with self.assertRaisesRegex(DetectError, want.replace(".", "\\.")):
                detect(project(files))
        with self.assertRaisesRegex(DetectError, "run.command"):
            detect(project({"go.mod": "module x\n"}), build_command="go build ./...")
        with self.assertRaisesRegex(DetectError, "runtime.go"):
            detect(project({"go.mod": "module x\n", "main.go": "package main\n"}), go="1.20")
        with self.assertRaisesRegex(DetectError, "go.mod"):
            detect(project({"main.go": "package main\n"}), stack="go")

    def test_js_frameworks(self):
        vite = '"devDependencies":{"vite":"7"'
        self.check({"package.json": '{"scripts":{"build":"vite build"},' + vite + ',"@sveltejs/kit":"2","@sveltejs/adapter-node":"5"}}'},
                   stack="node", framework="SvelteKit", start="PROTOCOL_HEADER=x-forwarded-proto node build", bun=False)
        self.check({"package.json": '{"scripts":{"build":"vite build"},' + vite + ',"@sveltejs/kit":"2","@sveltejs/adapter-static":"3"}}'},
                   stack="static", framework="SvelteKit", output="build")
        self.check({"package.json": '{"scripts":{"build":"nuxt build"},"dependencies":{"nuxt":"4"}}'},
                   stack="node", framework="Nuxt", start="node .output/server/index.mjs")
        self.check({"package.json": '{"scripts":{"build":"nuxt generate"},"dependencies":{"nuxt":"4"}}'},
                   stack="static", framework="Nuxt", output=".output/public")
        self.check({"package.json": '{"scripts":{"build":"vite build","start":"node .output/server/index.mjs"},'
                                    '"dependencies":{"@tanstack/react-start":"1"}}'},
                   stack="node", framework="TanStack Start", start="node .output/server/index.mjs")
        self.check({"package.json": '{"scripts":{"build":"vite build"},"dependencies":{"@tanstack/react-start":"1","nitro":"3"}}'},
                   stack="node", framework="TanStack Start", start="node .output/server/index.mjs")
        self.check({"package.json": '{"scripts":{"build":"react-router build"},"devDependencies":{"@react-router/dev":"7"}}',
                    "react-router.config.ts": "export default { ssr: false }\n"},
                   stack="static", framework="React Router", output="build/client")
        self.check({"package.json": '{"scripts":{"build":"astro build","start":"astro dev"},"dependencies":{"astro":"5","@astrojs/node":"9"}}'},
                   stack="node", framework="Astro", start="node ./dist/server/entry.mjs")
        self.check({"package.json": '{"scripts":{"build":"astro build"},"dependencies":{"astro":"5"}}'},
                   stack="static", framework="Astro", output="dist")

    def test_js_framework_problems(self):
        for pkg, want in (('{"scripts":{"build":"vite build"},"devDependencies":{"@sveltejs/kit":"2","@sveltejs/adapter-auto":"6"}}', "adapter-node"),
                          ('{"scripts":{"build":"vite build"},"dependencies":{"@tanstack/react-start":"1"}}', "nitro"),
                          ('{"scripts":{"build":"react-router build"},"devDependencies":{"@react-router/dev":"7"}}', "@react-router/serve"),
                          ('{"scripts":{"build":"astro build"},"dependencies":{"astro":"5","@astrojs/vercel":"8"}}', "@astrojs/node")):
            root = project({"package.json": pkg})
            with self.assertRaisesRegex(DetectError, want):
                detect(root)
            self.assertEqual(detect(root, run_command="node server.js")["start"], "node server.js")

    def test_bun(self):
        frozen = BUN_SETUP + " && bun install --frozen-lockfile"
        self.check({"bun.lock": "{}", "package.json": '{"scripts":{"build":"vite build"},"devDependencies":{"vite":"7"}}'},
                   stack="static", bun=True, install=frozen)
        self.check({"bun.lockb": "x", "package-lock.json": "{}", "package.json": '{"scripts":{"build":"vite build"},"devDependencies":{"vite":"7"}}'},
                   bun=False, install=NPM_INSTALL)
        self.check({"bun.lock": "{}", "src/app.ts": "", "package.json": '{"module":"src/app.ts","dependencies":{"hono":"4"}}'},
                   stack="node", start="bun run src/app.ts", install=frozen)
        self.check({"bun.lock": "{}", "index.ts": "", "package.json": "{}"}, stack="node", start="bun run index.ts")
        self.check({"src/index.ts": "", "package.json": '{"dependencies":{"elysia":"1"}}'},
                   framework="Elysia", start="bun run src/index.ts", install=BUN_SETUP + " && bun install")
        self.check({"package.json": '{"scripts":{"start":"bun run server.ts"}}'}, bun=True, start="bun run server.ts")
        self.check({"package.json": '{"scripts":{"start":"node bun/server.js"}}'}, bun=False)

    def test_errors(self):
        for files in ({"README.md": "# hi"}, {"requirements.txt": "requests\n", "main.py": ""},
                      {"package.json": '{"dependencies":{"lodash":"4"}}'}):
            with self.assertRaises(DetectError, msg=files):
                detect(project(files))
        with self.assertRaises(DetectError):
            detect(project({"index.html": ""}), kind="worker")
        with self.assertRaises(DetectError):
            detect(project({"package.json": "{}"}), stack="node", node="18")

    def test_python_version_from_requires_python(self):
        for spec, want in {">=3.14": "3.14", ">=3.10,<4.0": DEFAULT_PYTHON, "~=3.13": "3.13", "==3.11.*": "3.11",
                           "<4.0,>=3.14": "3.14", ">=3.8": DEFAULT_PYTHON}.items():
            files = {"pyproject.toml": f'[project]\nrequires-python = "{spec}"\ndependencies = ["fastapi"]\n', "main.py": "app = FastAPI()\n"}
            self.assertEqual(detect(project(files))["python"], want, spec)
        with self.assertRaises(DetectError):
            detect(project({"pyproject.toml": '[project]\nrequires-python = ">=3.15"\n', "main.py": ""}))

    def test_static_output(self):
        root = project({"dist/index.html": "", "public/favicon.ico": ""})
        self.assertEqual(static_output(root, "", True), "dist")
        self.assertEqual(static_output(project({"index.html": ""}), "", False), ".")
        with self.assertRaises(DetectError):
            static_output(root, "build", True)


class DockerfileTest(unittest.TestCase):
    GO = {"go.mod": "module x\n", "main.go": "package main\n"}

    def setUp(self):
        os.environ.pop("AIRLOK_KEEP_BUILD", None)
        self.addCleanup(os.environ.pop, "AIRLOK_KEEP_BUILD", None)

    def test_dockerfile_wins_over_files(self):
        values = parse(None, project({**self.GO, "Dockerfile": "FROM scratch\n"}))
        self.assertEqual((values["APP_STACK"], values["APP_KIND"], values["DOCKER_FILE"], values["DOCKER_CONTEXT"]),
                         ("docker", "web", "Dockerfile", ""))

    def test_explicit_stack_cancels_dockerfile(self):
        values = parse({"version": 1, "app": {"stack": "go"}}, project({**self.GO, "Dockerfile": "FROM scratch\n"}))
        self.assertEqual(values["APP_STACK"], "go")

    def test_running_app_keeps_its_build(self):
        os.environ["AIRLOK_KEEP_BUILD"] = "1"
        root = project({**self.GO, "Dockerfile": "FROM scratch\n"})
        self.assertEqual(parse(None, root)["APP_STACK"], "go", "Dockerfile мог лежать в проекте давно и не быть рабочим")
        values = parse({"version": 1, "build": {"dockerfile": "Dockerfile"}}, root)
        self.assertEqual(values["APP_STACK"], "docker", "явная просьба важнее")
        self.assertEqual(parse({"version": 1, "app": {"stack": "docker"}}, root)["APP_STACK"], "docker")

    def test_stack_docker_needs_dockerfile(self):
        with self.assertRaisesRegex(ManifestError, "нет Dockerfile"):
            parse({"version": 1, "app": {"stack": "docker"}}, project(self.GO))
        with self.assertRaisesRegex(ManifestError, "нет файла docker/x"):
            parse({"version": 1, "build": {"dockerfile": "docker/x"}}, project(self.GO))
        with self.assertRaisesRegex(ManifestError, "путь внутри проекта"):
            parse({"version": 1, "build": {"dockerfile": "../x"}}, project({**self.GO, "Dockerfile": "FROM scratch\n"}))

    def test_compose_service(self):
        compose = """
services:
  bot:
    build:
      context: ./srv
      dockerfile: docker/App
      target: prod
      args: {FLAVOR: full, FROM_ENV: "${FROM_ENV}"}
    ports: ["127.0.0.1:9000:3000"]
    environment:
      DATABASE_URL: postgres://x
      REQUIRED_KEY: ${REQUIRED_KEY:?нужен ключ}
      LOG_LEVEL: info
      WITH_DEFAULT: ${WITH_DEFAULT:-yes}
      OTHER: ${SOMETHING}
  db: {image: "postgres:16"}
  cache: {image: "redis:7"}
"""
        values = parse(None, project({"compose.yml": compose, "srv/docker/App": "FROM scratch\n", "srv/x": "1"}))
        self.assertEqual((values["DOCKER_CONTEXT"], values["DOCKER_FILE"], values["DOCKER_TARGET"], values["DOCKER_PORT"]),
                         ("srv", "srv/docker/App", "prod", 3000))
        self.assertEqual(values["DB_ENABLED"], 1, "Postgres из compose.yml — это база приложения")
        self.assertEqual(values["DOCKER_ARGS"], "FLAVOR=full", "значение из окружения в манифесте неизвестно")
        self.assertEqual(values["DOCKER_DEFAULTS"], "LOG_LEVEL=info\nWITH_DEFAULT=yes")
        self.assertIn("REQUIRED_KEY", values["ENV_NAMES"].split())
        self.assertNotIn("DATABASE_URL", values["ENV_NAMES"].split())
        self.assertIn("cache", values["DOCKER_WARNINGS"])

    def test_compose_with_two_builds(self):
        compose = "services:\n  a: {build: ./a}\n  b: {build: ./b}\n"
        with self.assertRaisesRegex(ManifestError, "несколько собираемых сервисов"):
            parse(None, project({"compose.yml": compose, "a/Dockerfile": "FROM scratch\n", "b/Dockerfile": "FROM scratch\n"}))
        same = "services:\n  migrate: {build: .}\n  web: {build: ., ports: ['8000:8000']}\n"
        values = parse(None, project({"compose.yml": same, "Dockerfile": "FROM scratch\n"}))
        self.assertEqual(values["DOCKER_PORT"], 8000, "одна сборка у двух сервисов: берём сервис с портом")

    def test_compose_outside_project(self):
        for build in ("../x", "/etc", "https://github.com/a/b.git"):
            with self.assertRaises(ManifestError, msg=build):
                parse(None, project({"compose.yml": f"services:\n  a: {{build: '{build}'}}\n", "Dockerfile": "FROM scratch\n"}))

    def test_broken_compose_is_skipped(self):
        values = parse(None, project({"compose.yml": "services: [\n", **self.GO}))
        self.assertEqual(values["APP_STACK"], "go")

    def test_run_port(self):
        values = parse({"version": 1, "run": {"port": 5000}}, project({"Dockerfile": "FROM scratch\n"}))
        self.assertEqual(values["DOCKER_PORT"], 5000)

    def test_guess_port(self):
        cases = [({"ExposedPorts": {"3000/tcp": {}}}, 3000), ({"ExposedPorts": {"53/udp": {}}}, 8080),
                 ({"Cmd": ["uvicorn", "app:app"]}, 8000), ({"Cmd": ["uvicorn", "app:app", "--port", "9000"]}, 9000),
                 ({"Entrypoint": ["gunicorn", "--bind", "0.0.0.0:7000"]}, 7000), ({}, 8080),
                 ({"ExposedPorts": {"80/tcp": {}, "443/tcp": {}}}, 80)]
        for config, want in cases:
            self.assertEqual(guess_port(config), want, config)


class ManifestTest(unittest.TestCase):
    def test_data_folder(self):
        values = parse({"version": 1, "capabilities": {"data": {"enabled": True}}}, project({"go.mod": "module x\n", "main.go": "package main\n"}))
        self.assertEqual(values["DATA_ENABLED"], 1)
        self.assertEqual(parse(None, project({"go.mod": "module x\n", "main.go": "package main\n"}))["DATA_ENABLED"], 0)
        with self.assertRaisesRegex(ManifestError, "у статического сайта"):
            parse({"version": 1, "capabilities": {"data": {"enabled": True}}}, project({"index.html": "<html>"}))

    def test_without_manifest(self):
        values = parse(None, project({"requirements.txt": "flask\n", "app.py": "app = Flask(__name__)\n"}))
        self.assertEqual(values["APP_STACK"], "python")
        self.assertEqual(values["APP_NAME"], "", "имя даст дашборд или папка проекта")
        self.assertEqual(values["APP_DOMAIN"], "", "адрес выдаст рецепт")
        self.assertEqual((values["DB_ENABLED"], values["UPLOADS_ENABLED"]), (0, 0))
        values = parse({"version": 1, "runtime": {"go": "1.26"}}, project({"go.mod": "module x\n", "main.go": "package main\n"}))
        self.assertEqual((values["APP_STACK"], values["GO_VERSION"]), ("go", "1.26"))

    def test_manifest_overrides_detection(self):
        values = parse({"version": 1, "app": {"name": "shop", "stack": "node", "domain": "Shop.Example.ru"},
                        "run": {"command": "node dist/server.js"}, "runtime": {"node": "20"}},
                       project({"package.json": "{}"}))
        self.assertEqual((values["APP_DOMAIN"], values["RUN_CMD"], values["NODE_VERSION"]),
                         ("shop.example.ru", "exec node dist/server.js", "20"))

    def test_env_from_example(self):
        # Как .env.example у PonchesDev/favicon-uploader; правила те же, что у сборщика хостинга.
        example = ("LISTEN_ADDR=:8080\nPORT=8080\n\nS3_BUCKET=\nS3_ACCESS_KEY=\nS3_SECRET_KEY=your-secret-key\n\n"
                   "# По умолчанию https://storage.yandexcloud.net/<S3_BUCKET>.\nPUBLIC_BASE_URL=\n# CACHE_CONTROL=\n"
                   "export MAX_FILE_SIZE_MB=\"10\" # мегабайты\n\n# Токен, минимум 16 символов\nUPLOAD_TOKEN=\n")
        files = {"go.mod": "module x\n", "main.go": "package main\n", ".env.example": example}
        values = parse(None, project(files))
        self.assertEqual(values["ENV_NAMES"], "S3_BUCKET S3_ACCESS_KEY S3_SECRET_KEY UPLOAD_TOKEN",
                         "пустое значение и заглушка обязательны, «по умолчанию», значение и комментарий — нет")
        values = parse({"version": 1, "env": {"UPLOAD_TOKEN": {"required": False}, "ADMIN_ID": "Telegram id администратора"}},
                       project(files))
        self.assertEqual(values["ENV_NAMES"], "ADMIN_ID S3_BUCKET S3_ACCESS_KEY S3_SECRET_KEY", "airlok.yml важнее .env.example")

    def test_env_spec(self):
        root = project({"go.mod": "module x\n", "main.go": "package main\n"})
        values = parse({"version": 1, "env": {"S3_BUCKET": "Бакет", "SECRET": {"secret": True},
                                              "LIMIT": {"default": "10"}, "REGION": {"default": "ru", "required": True}}}, root)
        self.assertEqual(values["ENV_NAMES"], "S3_BUCKET SECRET REGION", "без default — обязательная")
        self.assertEqual(parse({"version": 1, "env": ["A", "B"]}, root)["ENV_NAMES"], "A B", "список — все обязательные")
        values = parse({"version": 1, "capabilities": {"database": {"enabled": True}}},
                       project({"go.mod": "module x\n", "main.go": "package main\n", ".env.example": "DATABASE_URL=\nDEBUG=\n"}))
        self.assertEqual(values["ENV_NAMES"], "DEBUG", "DATABASE_URL задаёт рецепт, когда заводит базу сам")
        values = parse({"version": 1, "env": {"SENTRY_AUTH_TOKEN": {"secret": True, "build": True},
                                              "NEXT_PUBLIC_DEBUG": {"default": "0", "build": False}, "API_URL": "Адрес API"}}, root)
        self.assertEqual((values["ENV_BUILD"], values["ENV_NO_BUILD"]), ("SENTRY_AUTH_TOKEN", "NEXT_PUBLIC_DEBUG"),
                         "без build решает имя: это делает сборка по файлам переменных")
        for env in ({"A": {"unknown": 1}}, {"A": {"required": "yes"}}, {"A": {"build": "yes"}}, 5, ["lower"], {"PORT": "порт"}):
            with self.assertRaises(ManifestError, msg=env):
                parse({"version": 1, "env": env}, root)

    def test_health(self):
        root = project({"index.html": ""})
        self.assertEqual(parse({"version": 1}, root)["HEALTH_PATH"], "", "без run.health проверяется главная")
        self.assertEqual(parse({"version": 1, "run": {"health": "/api/health?full=1"}}, root)["HEALTH_PATH"], "/api/health?full=1")

    def test_release_and_cron(self):
        root = project({"requirements.txt": "fastapi\n", "main.py": "app = FastAPI()\n"})
        values = parse({"version": 1}, root)
        self.assertEqual((values["RELEASE_CMD"], values["CRON_JOBS"]), ("", "[]"))
        values = parse({"version": 1, "run": {"release": " alembic upgrade head "},
                        "capabilities": {"cron": [{"name": "дайджест", "schedule": "0 9 * * 1-5", "command": "python digest.py"}]}},
                       root)
        self.assertEqual(values["RELEASE_CMD"], "alembic upgrade head")
        self.assertEqual(json.loads(values["CRON_JOBS"]),
                         [{"name": "дайджест", "schedule": "0 9 * * 1-5", "command": "python digest.py", "timeout_min": 10}])
        with self.assertRaisesRegex(ManifestError, "дайджест"):
            parse({"version": 1, "capabilities": {"cron": [{"name": "дайджест", "schedule": "0 9", "command": "x"}]}}, root)
        static = project({"index.html": ""})
        for data in ({"version": 1, "run": {"release": "npm run migrate"}},
                     {"version": 1, "capabilities": {"cron": [{"schedule": "@daily", "command": "node a.js"}]}}):
            with self.assertRaisesRegex(ManifestError, "статического сайта", msg=data):
                parse(data, static)
        self.assertEqual(parse({"version": 1, "capabilities": {"cron": []}}, static)["CRON_JOBS"], "[]")

    def test_rejects(self):
        root = project({"index.html": ""})
        for data in ({"version": 2},
                     {"version": 1, "app": {"stack": "php"}},
                     {"version": 1, "run": {"command": "node a.js\nrm -rf /"}},
                     {"version": 1, "build": {"output": "../etc"}},
                     {"version": 1, "run": {"health": "healthz"}},
                     {"version": 1, "run": {"health": "//evil.example/x"}},
                     {"version": 1, "run": {"health": "/a b"}},
                     {"version": 1, "capabilities": {"database": {"enabled": True}}}):
            with self.assertRaises(ManifestError, msg=data):
                parse(data, root)


class ShellExecTest(unittest.TestCase):
    def test_simple_commands(self):
        for command, want in {
            "node server.js": "exec node server.js",
            "/app/server": "exec /app/server",
            "./bin/run --x=1": "exec ./bin/run --x=1",
            "npm start": "exec npm start",
            "  node server.js": "  exec node server.js",
            "PROTOCOL_HEADER=x-forwarded-proto node build": "PROTOCOL_HEADER=x-forwarded-proto exec node build",
            """A=1 B="x y" C='p;q' node build""": """A=1 B="x y" C='p;q' exec node build""",
            "FOO=$BAR node build": "FOO=$BAR exec node build",
            "uvicorn main:app --forwarded-allow-ips '*'": "exec uvicorn main:app --forwarded-allow-ips '*'",
            "streamlit run app.py --server.port ${PORT:-1}": "exec streamlit run app.py --server.port ${PORT:-1}",
            'node -e "a(); b && c | d"': 'exec node -e "a(); b && c | d"',
            "node x\\;y": "exec node x\\;y",
            "node server.js 2>&1": "exec node server.js 2>&1",
            "node server.js > /tmp/log": "exec node server.js > /tmp/log",
        }.items():
            self.assertEqual(shell_exec(command), want, command)

    def test_left_as_is(self):
        for command in (
            "", "   ", "FOO=1", "FOO=1 BAR=2 ",
            "exec node server.js", "FOO=1 exec node server.js",
            "a && b", "a || b", "a; b", "a | b", "a &", "(a)", "{ a; }", "a\nb",
            "mkdir -p x && exec frankenphp php-server",
            "node $(cat file)", "node `cat file`", 'node "$(cat file)"', "FOO=$(cat f) node x",
            'node "unclosed', "node 'unclosed", "$BIN --flag", '"my app" --flag',
            "2>&1 node x", "> log node x", "a &> log",
            "cd sub", ". ./env", "export A=1", "ulimit -n 100", "time node x", "if", "! a", "[[ a ]]",
        ):
            self.assertIsNone(shell_exec(command), command)

    def test_start_script(self):
        pkg = lambda scripts: '{"scripts":{%s}}' % scripts  # noqa: E731
        for files, want in (
            ({"package.json": pkg('"start":"node server.js"')}, "node server.js"),
            ({"package.json": pkg('"start":"NODE_OPTIONS=--max-old-space-size=256 node server.js"')},
             "NODE_OPTIONS=--max-old-space-size=256 node server.js"),
            ({"package.json": pkg('"start":"prisma migrate deploy && node server.js"')}, "npm start"),
            ({"package.json": pkg('"prestart":"node gen.js","start":"node server.js"')}, "npm start"),
            ({"package.json": pkg('"poststart":"node gen.js","start":"node server.js"')}, "npm start"),
            ({"package.json": pkg('"start":"node server.js --v=$npm_package_version"')}, "npm start"),
            ({"package.json": pkg('"start":"node server.js"'), "server.js": "console.log(process.env.npm_package_version)\n"}, "npm start"),
            ({"package.json": pkg('"start":"node server.js"'), "node_modules/x/i.js": "process.env.npm_package_version\n"}, "node server.js"),
        ):
            self.assertEqual(detect(project(files))["start"], want, files)


if __name__ == "__main__":
    unittest.main()
