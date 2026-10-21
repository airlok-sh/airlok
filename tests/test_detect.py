"""Определение стека и манифест без airlok.yml: python3 -m unittest discover tests

Случаи те же, что у сборщика хостинга airlok: проект должен выкатываться одинаково.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "recipes"))

from detect import DEFAULT_NODE, DEFAULT_PYTHON, NPM_INSTALL, DetectError, detect, static_output  # noqa: E402
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

    def test_errors(self):
        for files in ({"README.md": "# hi"}, {"requirements.txt": "requests\n", "main.py": ""},
                      {"package.json": '{"dependencies":{"lodash":"4"}}'}):
            with self.assertRaises(DetectError, msg=files):
                detect(project(files))
        with self.assertRaises(DetectError):
            detect(project({"index.html": ""}), kind="worker")
        with self.assertRaises(DetectError):
            detect(project({"package.json": "{}"}), stack="node", node="18")

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

    def test_manifest_overrides_detection(self):
        values = parse({"version": 1, "app": {"name": "shop", "stack": "node", "domain": "Shop.Example.ru"},
                        "run": {"command": "node dist/server.js"}, "runtime": {"node": "20"}},
                       project({"package.json": "{}"}))
        self.assertEqual((values["APP_DOMAIN"], values["RUN_CMD"], values["NODE_VERSION"]),
                         ("shop.example.ru", "node dist/server.js", "20"))

    def test_rejects(self):
        root = project({"index.html": ""})
        for data in ({"version": 2},
                     {"version": 1, "app": {"stack": "php"}},
                     {"version": 1, "run": {"command": "node a.js\nrm -rf /"}},
                     {"version": 1, "build": {"output": "../etc"}},
                     {"version": 1, "capabilities": {"database": {"enabled": True}}}):
            with self.assertRaises(ManifestError, msg=data):
                parse(data, root)


if __name__ == "__main__":
    unittest.main()
