"""Разбор команд агента из дашборда: python3 -m unittest discover tests

Значения команды доходят до команд от root, поэтому агент проверяет их сам.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

AGENT = os.path.join(os.path.dirname(__file__), "..", "recipes", "agent.sh")
LIB = os.path.join(os.path.dirname(__file__), "..", "recipes", "lib.sh")
with open(AGENT, encoding="utf-8") as f:
    SOURCE = f.read()
PARSE_PY = re.search(r"PARSE_PY='\n(.*?)\n'\n", SOURCE, re.S).group(1)
PAYLOAD_PY = re.search(r"PAYLOAD_PY='\n(.*?)\n'\n", SOURCE, re.S).group(1)
CID = "0192e000-0000-7000-8000-000000000001"


def parse(cmd, env_path=os.devnull):
    out = subprocess.run([sys.executable, "-c", PARSE_PY, env_path], input=json.dumps(cmd).encode(), capture_output=True, check=True)
    return out.stdout.decode().split("\0")[:-1]


class ParseTest(unittest.TestCase):
    def test_update_has_no_parameters(self):
        self.assertEqual(parse({"id": CID, "kind": "update"}), ["ok", CID, "update"])
        self.assertEqual(parse({"id": CID, "kind": "update", "deploy": {"app": "x; rm -rf /"}}), ["ok", CID, "update"],
                         "поля у обновления не используются, что бы ни прислал дашборд")

    def test_deploy(self):
        sha = "a" * 40
        got = parse({"id": CID, "kind": "deploy", "deploy": {"app": "shop", "repo": "acme/shop", "sha": sha, "ref": "main",
                                                             "root_dir": "web", "title": "Новая  корзина\n", "github_token": ""}})
        self.assertEqual(got, ["ok", CID, "deploy", "shop", "acme/shop", sha, "main", "web", "Новая корзина", "", "0"],
                         "без env в команде агент не трогает файл переменных")

    def test_deploy_env(self):
        deploy = {"app": "shop", "repo": "acme/shop", "sha": "a" * 40}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "env.json")
            env = [{"name": "API_KEY", "value": "v1:QUJD", "extra": "x"}]
            self.assertEqual(parse({"id": CID, "kind": "deploy", "deploy": {**deploy, "env": env}}, path)[-1], "1")
            with open(path) as f:
                self.assertEqual(json.load(f), [{"name": "API_KEY", "value": "v1:QUJD"}])
            self.assertEqual(parse({"id": CID, "kind": "deploy", "deploy": {**deploy, "env": []}}, path)[-1], "1",
                             "пустой список — в дашборде переменных не осталось, файл очистится")
            for env in ("x", [{"name": "A B", "value": "v1:QUJD"}], [{"name": "A", "value": "plain"}],
                        [{"name": "A", "value": "v1:QUJD\nB=1"}], [{"name": "A"}]):
                self.assertEqual(parse({"id": CID, "kind": "deploy", "deploy": {**deploy, "env": env}}, path)[0], "bad", env)

    def test_restart(self):
        self.assertEqual(parse({"id": CID, "kind": "restart", "restart": {"app": "shop"}}), ["ok", CID, "restart", "shop", "0"])
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "env.json")
            env = [{"name": "API_KEY", "value": "v1:QUJD"}]
            self.assertEqual(parse({"id": CID, "kind": "restart", "restart": {"app": "shop", "env": env}}, path),
                             ["ok", CID, "restart", "shop", "1"])
            with open(path) as f:
                self.assertEqual(json.load(f), env)
        for r in (None, {"app": "../shop"}, {"app": "shop; reboot"}, {"app": "shop", "env": [{"name": "A", "value": "plain"}]}):
            self.assertEqual(parse({"id": CID, "kind": "restart", "restart": r})[0], "bad", r)

    def test_rejects(self):
        for cmd in ({"id": CID, "kind": "reboot"},
                    {"id": "not-a-uuid", "kind": "update"},
                    {"id": CID, "kind": "deploy", "deploy": {"app": "shop", "repo": "acme/shop", "sha": "a" * 40, "root_dir": "../etc"}}):
            self.assertEqual(parse(cmd)[0], "bad", cmd)


class PayloadTest(unittest.TestCase):
    """Тело запросов о ходе и итоге выкатки: этапы собираются из отметок рецептов."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = os.path.join(self.tmp.name, "command.log")
        self.stages = os.path.join(self.tmp.name, "command.stages")
        with open(self.log, "w", encoding="utf-8") as f:
            f.write("→ Сборка: Go\n\x1b[31mОшибка:\x1b[0m сборка не удалась\n")
        open(self.stages, "w").close()

    def tearDown(self):
        self.tmp.cleanup()

    def mark(self, *lines):
        with open(self.stages, "a", encoding="utf-8") as f:
            f.write("".join(line + "\n" for line in lines))

    def payload(self, mode, status="running", message=""):
        out = subprocess.run([sys.executable, "-c", PAYLOAD_PY, mode, status, message, self.log, self.stages],
                             capture_output=True, check=True)
        return json.loads(out.stdout)

    def test_progress(self):
        self.assertEqual(self.payload("progress")["stages"], [], "до первой отметки этапов нет")
        self.mark("1793000000 source running acme/shop · abc1234", "1793000005 build running Go", "1793000009 build running")
        got = self.payload("progress")
        self.assertNotIn("\x1b", got["log"])
        self.assertEqual([s["status"] for s in got["stages"]], ["done", "running", "pending", "pending"])
        self.assertEqual(got["stages"][0], {"key": "source", "status": "done", "note": "acme/shop · abc1234",
                                            "started_at": "2026-10-26T07:33:20Z", "finished_at": "2026-10-26T07:33:25Z"})
        self.assertEqual(got["stages"][1]["note"], "Go", "повторная отметка без пометки её не стирает")
        self.assertEqual(got["stages"][1]["started_at"], "2026-10-26T07:33:25Z", "и не сдвигает начало")

    def test_failed_result(self):
        self.mark("1793000000 source running", "1793000005 build running Go")
        got = self.payload("result", "failed")
        self.assertEqual(got["message"], "сборка не удалась")
        self.assertEqual([s["status"] for s in got["stages"]], ["done", "failed", "pending", "pending"])
        self.assertIn("finished_at", got["stages"][1])

    def test_success_skips_the_rest(self):
        self.mark("1793000000 source running", "1793000001 build skipped версия уже собрана", "1793000002 start running",
                  "garbage", "1793000003 deploy running", "x start running")
        got = self.payload("result", "success")
        self.assertEqual([s["status"] for s in got["stages"]], ["done", "skipped", "done", "skipped"],
                         "у фонового приложения нет сайта, а мусорные строки не считаются")
        self.assertEqual(got["stages"][1]["note"], "версия уже собрана")

    def test_result_without_marks_keeps_dashboard_stages(self):
        self.assertNotIn("stages", self.payload("result", "failed", "в команде нет параметров выкатки"))
        out = subprocess.run([sys.executable, "-c", PAYLOAD_PY, "result", "failed", "отклонена", "", ""],
                             capture_output=True, check=True)
        self.assertEqual(json.loads(out.stdout), {"status": "failed", "message": "отклонена", "log": ""})


class StageMarkTest(unittest.TestCase):
    def run_stage(self, env):
        script = 'source "$1"; stage build "Next.js"; stage_skip site; echo ok'
        return subprocess.run(["bash", "-c", script, "bash", LIB], env={**os.environ, **env}, capture_output=True, text=True)

    def test_writes_only_for_agent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "stages")
            self.assertEqual(self.run_stage({"AIRLOK_STAGE_FILE": path}).stdout, "ok\n")
            with open(path, encoding="utf-8") as f:
                lines = f.read().splitlines()
            self.assertEqual([line.split(" ", 1)[1] for line in lines], ["build running Next.js", "site skipped "])
            self.assertTrue(lines[0].split(" ")[0].isdigit())
        env = {k: v for k, v in os.environ.items() if k != "AIRLOK_STAGE_FILE"}
        out = subprocess.run(["bash", "-c", 'source "$1"; stage build x; echo ok', "bash", LIB], env=env, capture_output=True, text=True)
        self.assertEqual(out.stdout, "ok\n", "без агента отметки никуда не пишутся и не роняют рецепт")


if __name__ == "__main__":
    unittest.main()


class HealthTest(unittest.TestCase):
    """Когда приложение считается живым: при выкатке и при проверке сайтов."""

    def ok(self, code, path):
        r = subprocess.run(["bash", "-c", 'source "$1"; health_ok "$2" "$3"', "bash", LIB, code, path], capture_output=True)
        return r.returncode == 0

    def test_home_page_needs_any_answer_but_5xx(self):
        for code, want in (("200", True), ("301", True), ("401", True), ("404", True), ("500", False), ("000", False)):
            self.assertEqual(self.ok(code, ""), want, code)

    def test_health_path_needs_success(self):
        for code, want in (("200", True), ("204", True), ("302", True), ("401", False), ("404", False), ("503", False), ("000", False)):
            self.assertEqual(self.ok(code, "/healthz"), want, code)

    def test_release_health(self):
        with tempfile.TemporaryDirectory() as tmp:
            rel = os.path.join(tmp, "abc")
            script = 'source "$1"; printf "[%s]" "$(release_health "$2")"'
            run = lambda: subprocess.run(["bash", "-c", script, "bash", LIB, rel], capture_output=True, text=True).stdout
            self.assertEqual(run(), "[]", "релиз без файла — собран до run.health, проверяется главная")
            with open(rel + ".health", "w") as f:
                f.write("/healthz")
            self.assertEqual(run(), "[/healthz]")
