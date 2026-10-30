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


def parse(cmd, env_path=os.devnull, query_path=os.devnull, keys_path=os.devnull):
    out = subprocess.run([sys.executable, "-c", PARSE_PY, env_path, query_path, keys_path], input=json.dumps(cmd).encode(),
                         capture_output=True, check=True)
    return out.stdout.decode().split("\0")[:-1]


class ParseTest(unittest.TestCase):
    def test_update_has_no_parameters(self):
        self.assertEqual(parse({"id": CID, "kind": "update"}), ["ok", CID, "update"])
        self.assertEqual(parse({"id": CID, "kind": "update", "deploy": {"app": "x; rm -rf /"}}), ["ok", CID, "update"],
                         "поля у обновления не используются, что бы ни прислал дашборд")

    def test_db_query_goes_to_file(self):
        q = {"app": "shop", "statements": [{"sql": "SELECT 'a\x00'; DROP", "params": []}], "max_rows": 50, "max_cell": 300}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "q.json")
            self.assertEqual(parse({"id": CID, "kind": "db_query", "db_query": q}, query_path=path), ["ok", CID, "db_query"],
                             "текст запроса в shell не попадает: его проверяет и выполняет dbquery.py")
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), q)
        self.assertEqual(parse({"id": CID, "kind": "db_query", "db_query": "SELECT 1"})[0], "bad")

    def test_ssh_keys_go_to_file(self):
        k = {"keys": [{"id": CID, "key": "ssh-ed25519 AAAA; reboot"}]}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "keys.json")
            self.assertEqual(parse({"id": CID, "kind": "ssh_keys", "ssh_keys": k}, keys_path=path), ["ok", CID, "ssh_keys"],
                             "ключи в shell не попадают: их проверяет и записывает sshkeys.py")
            with open(path, encoding="utf-8") as f:
                self.assertEqual(json.load(f), k)
        self.assertEqual(parse({"id": CID, "kind": "ssh_keys", "ssh_keys": ["ssh-ed25519 AAAA"]})[0], "bad")

    def test_deploy(self):
        sha = "a" * 40
        got = parse({"id": CID, "kind": "deploy", "deploy": {"app": "shop", "repo": "acme/shop", "sha": sha, "ref": "main",
                                                             "root_dir": "web", "title": "Новая  корзина\n", "github_token": ""}})
        self.assertEqual(got, ["ok", CID, "deploy", "shop", "acme/shop", sha, "main", "web", "Новая корзина", "", "", "0", ""],
                         "без env в команде агент не трогает файл переменных")

    def test_deploy_by_link(self):
        sha, auth = "a" * 40, "YWNtZTp0b2s="
        deploy = {"app": "shop", "repo": "https://gitflic.ru/project/acme/shop.git", "sha": sha, "ref": "main", "git_auth": auth}
        got = parse({"id": CID, "kind": "deploy", "deploy": deploy})
        self.assertEqual(got[:5], ["ok", CID, "deploy", "shop", "https://gitflic.ru/project/acme/shop.git"])
        self.assertEqual(got[-1], auth)
        for repo in ("https://gitverse.ru/acme.git", "http://gitverse.ru/acme/shop.git", "https://gitverse.ru/acme/../shop.git",
                     "https://user:x@gitverse.ru/acme/shop.git", "https://gitverse.ru/acme/shop", "https://gitverse.ru/-x/shop.git"):
            self.assertEqual(parse({"id": CID, "kind": "deploy", "deploy": {**deploy, "repo": repo}})[0], "bad", repo)
        for bad in ("a b", "x\n", "токен"):
            self.assertEqual(parse({"id": CID, "kind": "deploy", "deploy": {**deploy, "git_auth": bad}})[0], "bad", bad)

    def test_deploy_domain(self):
        deploy = {"app": "shop", "repo": "acme/shop", "sha": "a" * 40}
        for domain in ("shop.example.ru", "xn--80aairftm.xn--p1ai", "a1.b-c.io"):
            self.assertEqual(parse({"id": CID, "kind": "deploy", "deploy": {**deploy, "domain": domain}})[-3], domain)
        for domain in ("Shop.Example.ru", "shop..ru", "-shop.ru", "shop.ru; reboot", "shop.ru\n", "shop", "x" * 250 + ".ru", 42):
            self.assertEqual(parse({"id": CID, "kind": "deploy", "deploy": {**deploy, "domain": domain}})[0], "bad", domain)

    def test_deploy_env(self):
        deploy = {"app": "shop", "repo": "acme/shop", "sha": "a" * 40}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "env.json")
            env = [{"name": "API_KEY", "value": "v1:QUJD", "extra": "x"}]
            self.assertEqual(parse({"id": CID, "kind": "deploy", "deploy": {**deploy, "env": env}}, path)[-2], "1")
            with open(path) as f:
                self.assertEqual(json.load(f), [{"name": "API_KEY", "value": "v1:QUJD"}])
            self.assertEqual(parse({"id": CID, "kind": "deploy", "deploy": {**deploy, "env": []}}, path)[-2], "1",
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

    def test_remove(self):
        self.assertEqual(parse({"id": CID, "kind": "remove", "remove": {"app": "shop"}}), ["ok", CID, "remove", "shop", "0"])
        self.assertEqual(parse({"id": CID, "kind": "remove", "remove": {"app": "shop", "with_data": False}})[-1], "0")
        self.assertEqual(parse({"id": CID, "kind": "remove", "remove": {"app": "shop", "with_data": True}}), ["ok", CID, "remove", "shop", "1"])
        for r in (None, {"app": "../shop"}, {"app": "shop; reboot"}, {"app": "shop", "with_data": "false"}, {"app": "shop", "with_data": 1}):
            self.assertEqual(parse({"id": CID, "kind": "remove", "remove": r})[0], "bad", r)

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

    def payload(self, mode, status="running", message="", title=""):
        out = subprocess.run([sys.executable, "-c", PAYLOAD_PY, mode, status, message, self.log, self.stages, title],
                             capture_output=True, check=True)
        return json.loads(out.stdout)

    def test_commit_title_goes_with_result(self):
        path = os.path.join(self.tmp.name, "command.title")
        with open(path, "w", encoding="utf-8") as f:
            f.write("Корзина\n")
        self.assertEqual(self.payload("result", "success", "Готово", path)["title"], "Корзина")
        self.assertNotIn("title", self.payload("result", "success", "Готово"), "у GitHub заголовок дашборд знает сам")

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


class PanelDomainTest(unittest.TestCase):
    """Свой домен из дашборда в app-deploy.sh: главнее airlok.yml и помнится для выкаток без агента."""

    def setUp(self):
        with open(os.path.join(os.path.dirname(AGENT), "app-deploy.sh"), encoding="utf-8") as f:
            src = f.read()
        self.funcs = "\n".join([re.search(r"^DOMAIN_RE=.*$", src, re.M).group(0)] + [
            re.search(r"^%s\(\) \{\n.*?\n\}\n" % name, src, re.M | re.S).group(0) for name in ("panel_domain", "save_panel_domain")])
        self.tmp = tempfile.TemporaryDirectory()
        os.makedirs(os.path.join(self.tmp.name, "shared"))
        self.saved = os.path.join(self.tmp.name, "shared", "panel.domain")

    def tearDown(self):
        self.tmp.cleanup()

    def deploy(self, kind="web", **env):
        script = 'source "$1"; eval "$2"; APP_DIR=$3 APP_KIND=$4 APP_DOMAIN=yml.example.ru; panel_domain; save_panel_domain; echo "$APP_DOMAIN"'
        base = {k: v for k, v in os.environ.items() if k != "AIRLOK_APP_DOMAIN"}
        return subprocess.run(["bash", "-c", script, "bash", LIB, self.funcs, self.tmp.name, kind],
                              env={**base, **env}, capture_output=True, text=True)

    def test_dashboard_wins_and_is_remembered(self):
        self.assertEqual(self.deploy(AIRLOK_APP_DOMAIN="shop.example.ru").stdout, "shop.example.ru\n")
        with open(self.saved) as f:
            self.assertEqual(f.read(), "shop.example.ru\n")
        self.assertEqual(self.deploy().stdout, "shop.example.ru\n", "выкатка руками остаётся на домене из дашборда")

    def test_removed_in_dashboard(self):
        self.deploy(AIRLOK_APP_DOMAIN="shop.example.ru")
        self.assertEqual(self.deploy(AIRLOK_APP_DOMAIN="").stdout, "yml.example.ru\n")
        self.assertFalse(os.path.exists(self.saved))
        self.assertEqual(self.deploy().stdout, "yml.example.ru\n")

    def test_worker_has_no_site(self):
        out = self.deploy("worker", AIRLOK_APP_DOMAIN="shop.example.ru")
        self.assertTrue(out.stdout.endswith("yml.example.ru\n"), out.stdout)
        self.assertIn("фонового", out.stdout)

    def test_rejects_bad_domain(self):
        self.assertNotEqual(self.deploy(AIRLOK_APP_DOMAIN="shop.ru; reboot").returncode, 0)
        with open(self.saved, "w") as f:
            f.write("../../etc\n")
        self.assertNotEqual(self.deploy().returncode, 0, "испорченный файл на сервере не доходит до nginx")


def server_shell():
    """bash 4+ и GNU rm, как на сервере: рецепты на них и рассчитаны."""
    bash = subprocess.run(["bash", "-c", "((BASH_VERSINFO[0] >= 4)) && rm --one-file-system -f /nonexistent"], capture_output=True)
    return bash.returncode == 0


@unittest.skipUnless(server_shell(), "нужны bash 4+ и GNU coreutils, как на сервере")
class RemoveTest(unittest.TestCase):
    """app-remove.sh: без --with-data данные приложения не трогаются."""

    STUBS = {
        "docker": 'echo "docker $*" >>"$CALLS"\ncase "$1 $2" in "ps -aq") echo c0ffee ;; "volume ls") echo shop_pgdata ;; esac',
        "systemd-escape": "echo srv-apps-shop-shared-uploads.mount",
        "systemctl": 'echo "systemctl $*" >>"$CALLS"',
        "userdel": 'echo "userdel $*" >>"$CALLS"',
        "id": "exit 0",
        "findmnt": "exit 1",
    }

    def setUp(self):
        with open(os.path.join(os.path.dirname(AGENT), "app-remove.sh"), encoding="utf-8") as f:
            src = f.read()
        self.funcs = "\n".join([re.search(r"^compose\(\) \{.*\}$", src, re.M).group(0)] + [
            re.search(r"^%s\(\) \{\n.*?\n\}\n" % name, src, re.M | re.S).group(0) for name in ("stop_containers", "remove_files")])
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = os.path.join(self.tmp.name, "bin")
        os.makedirs(self.bin)
        for name, body in self.STUBS.items():
            path = os.path.join(self.bin, name)
            with open(path, "w") as f:
                f.write("#!/bin/sh\n" + body + "\n")
            os.chmod(path, 0o755)
        self.app = os.path.join(self.tmp.name, "shop")
        for d in ("releases/abc", "shared/backups", "shared/uploads"):
            os.makedirs(os.path.join(self.app, d))
        for name in ("compose.yml", "shared/db.env", "shared/cron.json", "shared/backups/1.sql.gz", "shared/uploads/a.png"):
            open(os.path.join(self.app, name), "w").close()
        os.symlink(os.path.join(self.app, "releases/abc"), os.path.join(self.app, "current"))
        self.calls = os.path.join(self.tmp.name, "calls")

    def tearDown(self):
        self.tmp.cleanup()

    def remove(self, with_data):
        script = 'source "$1"; eval "$2"; APP_DIR=$3 NAME=shop WITH_DATA=$4; stop_containers; remove_files'
        env = {**os.environ, "PATH": self.bin + ":" + os.environ["PATH"], "CALLS": self.calls}
        out = subprocess.run(["bash", "-c", script, "bash", LIB, self.funcs, self.app, with_data], env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        with open(self.calls) as f:
            return f.read().splitlines()

    def test_keeps_data(self):
        calls = self.remove("0")
        self.assertIn("down --remove-orphans", calls[0])
        self.assertNotIn("--volumes", calls[0])
        self.assertIn("docker rm -f c0ffee", calls, "разовые контейнеры задач тоже убираются")
        self.assertFalse([c for c in calls if c.startswith("docker volume")], "том базы не трогается")
        self.assertFalse([c for c in calls if c.startswith("userdel")], "uid владельца загрузок остаётся занятым")
        for gone in ("compose.yml", "current", "releases"):
            self.assertFalse(os.path.lexists(os.path.join(self.app, gone)), gone)
        for kept in ("shared/db.env", "shared/backups/1.sql.gz", "shared/uploads/a.png"):
            self.assertTrue(os.path.exists(os.path.join(self.app, kept)), kept)

    def test_with_data(self):
        calls = self.remove("1")
        self.assertIn("--volumes", calls[0])
        self.assertIn("docker volume rm shop_pgdata", calls)
        self.assertIn("userdel app-shop", calls)
        self.assertFalse(os.path.exists(self.app))


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
