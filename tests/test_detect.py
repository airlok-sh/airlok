"""Определение стека и манифест без airlok.yml: python3 -m unittest discover tests

Случаи те же, что у сборщика хостинга airlok: проект должен выкатываться одинаково.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "recipes"))

from detect import DEFAULT_GO, DEFAULT_NODE, DEFAULT_PYTHON, NPM_INSTALL, DetectError, detect, static_output  # noqa: E402
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
                   stack="node", framework="Express", node=DEFAULT_NODE, start="npm start", kind="web")
        self.check({"package.json": '{"scripts":{"build":"nest build","start":"nest start","start:prod":"node dist/main"},'
                                    '"dependencies":{"@nestjs/core":"11"}}'},
                   framework="NestJS", build="npm run build", start="npm run start:prod")
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


class ManifestTest(unittest.TestCase):
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
                         ("shop.example.ru", "node dist/server.js", "20"))

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


if __name__ == "__main__":
    unittest.main()
