"""Разбор команд агента из дашборда: python3 -m unittest discover tests

Значения команды доходят до команд от root, поэтому агент проверяет их сам.
"""
import glob
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
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
    def test_update_takes_only_sha(self):
        sha = "a" * 40
        self.assertEqual(parse({"id": CID, "kind": "update"}), ["ok", CID, "update"])
        self.assertEqual(parse({"id": CID, "kind": "update", "update": {"sha": sha}}), ["ok", CID, "update", sha])
        self.assertEqual(parse({"id": CID, "kind": "update", "deploy": {"app": "x; rm -rf /"}}), ["ok", CID, "update"],
                         "другие поля у обновления не используются, что бы ни прислал дашборд")
        for bad in ("a" * 39, "A" * 40, sha + "\n", "--upload-pack=x", ["a" * 40], None, 7):
            self.assertEqual(parse({"id": CID, "kind": "update", "update": {"sha": bad}}), ["ok", CID, "update"], bad)
        self.assertEqual(parse({"id": CID, "kind": "update", "update": sha}), ["ok", CID, "update"])

    def test_os_upgrade_action_is_checked(self):
        self.assertEqual(parse({"id": CID, "kind": "os_upgrade", "os_upgrade": {"action": "upgrade"}}), ["ok", CID, "os_upgrade", "upgrade"])
        self.assertEqual(parse({"id": CID, "kind": "os_upgrade", "os_upgrade": {"action": "reboot"}}), ["ok", CID, "os_upgrade", "reboot"])
        for bad in ({"action": "reboot; rm -rf /"}, {"action": ["reboot"]}, {}, "reboot", None):
            self.assertEqual(parse({"id": CID, "kind": "os_upgrade", "os_upgrade": bad})[0], "bad", bad)

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
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "keys.json")
            self.assertEqual(parse({"id": CID, "kind": "ssh_keys_remove", "ssh_keys": k}, keys_path=path), ["ok", CID, "ssh_keys"])

    def test_ssh_password(self):
        for login in ("closed", "open"):
            self.assertEqual(parse({"id": CID, "kind": "ssh_password", "ssh_password": {"login": login}}), ["ok", CID, "ssh_password", login, "root"])
        self.assertEqual(parse({"id": CID, "kind": "ssh_password", "ssh_password": {"login": "closed", "user": "ubuntu"}})[-1], "ubuntu")
        for p in (None, "closed", {"login": "close"}, {"login": "closed; reboot"}, {}, {"login": "closed", "user": "root; reboot"},
                  {"login": "closed", "user": ["root"]}):
            self.assertEqual(parse({"id": CID, "kind": "ssh_password", "ssh_password": p})[0], "bad", p)

    def test_data_move(self):
        url = "https://storage.yandexcloud.net/airlok/moves/1/db.sql.gz?X-Amz-Signature=ab%2Fc&X-Amz-Expires=7200"
        cmd = {"id": CID, "kind": "data_move", "data_move": {"op": "export", "app": "shop", "db_url": url, "files_url": ""}}
        self.assertEqual(parse(cmd), ["ok", CID, "data_move", "export", "shop", url, "", ""])
        self.assertEqual(parse({"id": CID, "kind": "data_move", "data_move": {"op": "resume", "app": "shop"}}),
                         ["ok", CID, "data_move", "resume", "shop", "", "", ""])
        cmd = {"id": CID, "kind": "data_move", "data_move": {"op": "export", "app": "shop", "data_url": url}}
        self.assertEqual(parse(cmd), ["ok", CID, "data_move", "export", "shop", "", "", url])
        for m in (None, {"op": "drop", "app": "shop"}, {"op": "export", "app": "../shop"},
                  {"op": "import", "app": "shop", "db_url": "http://storage/x"},
                  {"op": "import", "app": "shop", "db_url": "https://s/x'; reboot"},
                  {"op": "import", "app": "shop", "files_url": "https://s/x y"},
                  {"op": "import", "app": "shop", "data_url": "https://s/x'; reboot"}):
            self.assertEqual(parse({"id": CID, "kind": "data_move", "data_move": m})[0], "bad", m)

    def test_backup_and_restore(self):
        url = "https://storage.yandexcloud.net/airlok/backups/apps/1/2.sql.gz?X-Amz-Signature=ab%2Fc"
        for op in ("backup", "restore"):
            cmd = {"id": CID, "kind": "data_move", "data_move": {"op": op, "app": "shop", "db_url": url}}
            self.assertEqual(parse(cmd), ["ok", CID, "data_move", op, "shop", url, "", ""])
            self.assertEqual(parse({"id": CID, "kind": "data_move", "data_move": {"op": op, "app": "shop"}})[0], "bad",
                             "без ссылки на копию агенту нечего делать")
        # У приложения без базы копия состоит из одной постоянной папки.
        cmd = {"id": CID, "kind": "data_move", "data_move": {"op": "restore", "app": "shop", "data_url": url}}
        self.assertEqual(parse(cmd), ["ok", CID, "data_move", "restore", "shop", "", "", url])

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
    """Тело запросов о ходе и итоге публикации: этапы собираются из отметок рецептов."""

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

    def test_apt_percent(self):
        status = os.path.join(self.tmp.name, "status")
        def percent(text):
            with open(status, "w", encoding="utf-8") as f:
                f.write(text)
            out = subprocess.run([sys.executable, "-c", PAYLOAD_PY, "progress", "running", "", self.log, self.stages, "", status],
                                 capture_output=True, text=True, check=True).stdout
            return json.loads(out)["percent"]
        self.assertEqual(percent(""), 0, "apt ещё ничего не сообщил")
        self.assertEqual(percent("dlstatus:1:50.0:Retrieving file 1 of 2\n"), 15)
        self.assertEqual(percent("dlstatus:2:100:done\npmstatus:curl:50.0:Unpacking curl\n"), 65)
        self.assertEqual(percent("pmstatus:curl:100:Done\n"), 99, "конец приходит с итогом")
        self.assertEqual(percent("pmstatus:curl:50:ok\npmerror:x:60:broken: disk\n"), 65, "чужие строки не сбивают")
        self.assertNotIn("percent", self.payload("progress"), "у публикации процентов нет")

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
        self.assertNotIn("stages", self.payload("result", "failed", "в команде нет параметров публикации"))
        out = subprocess.run([sys.executable, "-c", PAYLOAD_PY, "result", "failed", "отклонена", "", ""],
                             capture_output=True, check=True)
        self.assertEqual(json.loads(out.stdout), {"status": "failed", "message": "отклонена", "log": ""})


class PanelDomainTest(unittest.TestCase):
    """Свой домен из дашборда в app-deploy.sh: главнее airlok.yml и помнится для публикаций без агента."""

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
        self.assertEqual(self.deploy().stdout, "shop.example.ru\n", "публикация руками остаётся на домене из дашборда")

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
    """app-remove.sh: без --with-data данные приложения не трогаются, а чужие контейнеры проекта — никогда."""

    STUBS = {
        "docker": 'echo "docker $*" >>"$CALLS"\ncase "$1 $2" in "ps -a") cat "$PS" ;; esac',
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
        with open(os.path.join(self.app, "shared/db.env"), "w") as f:
            f.write("POSTGRES_USER=app\n")
        self.calls = os.path.join(self.tmp.name, "calls")
        self.ps = os.path.join(self.tmp.name, "ps")
        self.containers(("c0ffee", "shop-app-1", self.app), ("cafe", "shop-app-run-1", self.app))

    def tearDown(self):
        self.tmp.cleanup()

    def containers(self, *rows):
        with open(self.ps, "w") as f:
            f.writelines("\t".join(row) + "\n" for row in rows)

    def remove(self, with_data):
        script = 'source "$1"; eval "$2"; APP_DIR=$3 NAME=shop WITH_DATA=$4; stop_containers; remove_files'
        env = {**os.environ, "PATH": self.bin + ":" + os.environ["PATH"], "CALLS": self.calls, "PS": self.ps}
        out = subprocess.run(["bash", "-c", script, "bash", LIB, self.funcs, self.app, with_data], env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        with open(self.calls) as f:
            return f.read().splitlines()

    def test_keeps_data(self):
        calls = self.remove("0")
        down = [c for c in calls if c.startswith("docker compose")]
        self.assertEqual(len(down), 1)
        self.assertTrue(down[0].endswith(" down"), "--remove-orphans убрал бы контейнеры чужого проекта с тем же именем")
        self.assertIn("docker rm -f c0ffee cafe", calls, "разовые контейнеры задач тоже убираются")
        self.assertFalse([c for c in calls if c.startswith("docker volume")], "том базы не трогается")
        self.assertFalse([c for c in calls if c.startswith("userdel")], "uid владельца загрузок остаётся занятым")
        for gone in ("compose.yml", "current", "releases"):
            self.assertFalse(os.path.lexists(os.path.join(self.app, gone)), gone)
        for kept in ("shared/db.env", "shared/backups/1.sql.gz", "shared/uploads/a.png"):
            self.assertTrue(os.path.exists(os.path.join(self.app, kept)), kept)

    def test_with_data(self):
        calls = self.remove("1")
        self.assertTrue([c for c in calls if c.startswith("docker compose") and c.endswith(" down --volumes")], calls)
        self.assertEqual([c for c in calls if c.startswith("docker volume rm")], ["docker volume rm shop_pgdata"],
                         "удаляется только том базы приложения, а не все тома с меткой проекта")
        self.assertIn("userdel app-shop", calls)
        self.assertFalse(os.path.exists(self.app))

    def test_foreign_project(self):
        self.containers(("c0ffee", "shop-app-1", self.app), ("dead", "shop-db-1", "/home/u/shop"), ("beef", "shop-bot-1", ""))
        calls = self.remove("1")
        self.assertFalse([c for c in calls if c.startswith("docker compose")], "compose down остановил бы и чужие службы")
        self.assertIn("docker rm -f c0ffee", calls, "удаляются только свои контейнеры")
        self.assertFalse([c for c in calls if c.startswith(("docker volume rm", "docker network rm"))],
                         "том и сеть проекта общие с чужим compose")
        self.assertFalse(os.path.exists(self.app))

    def test_no_database(self):
        os.remove(os.path.join(self.app, "shared/db.env"))
        calls = self.remove("1")
        self.assertFalse([c for c in calls if c.startswith("docker volume")],
                         "базы Airlok у приложения не было — том ИМЯ_pgdata не его")


class ProjectFreeTest(unittest.TestCase):
    """app-deploy.sh: публикация не идёт поверх проекта Docker Compose, который человек поднял сам под тем же именем."""

    def setUp(self):
        with open(os.path.join(os.path.dirname(AGENT), "app-deploy.sh"), encoding="utf-8") as f:
            src = f.read()
        self.func = re.search(r"^ensure_project_free\(\) \{\n.*?\n\}\n", src, re.M | re.S).group(0)
        self.tmp = tempfile.TemporaryDirectory()
        self.app = os.path.join(self.tmp.name, "shop")
        os.makedirs(os.path.join(self.app, "shared"))
        self.ps = os.path.join(self.tmp.name, "ps")
        self.volumes = os.path.join(self.tmp.name, "volumes")
        open(self.ps, "w").close()
        open(self.volumes, "w").close()
        bin_dir = os.path.join(self.tmp.name, "bin")
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "docker"), "w") as f:
            f.write('#!/bin/sh\ncase "$1 $2" in\n"ps -a") [ -e "$PS" ] && cat "$PS" ;;\n'
                    '"volume inspect") grep -qx "$3" "$VOLUMES" ;;\n*) exit 2 ;;\nesac\n')
        os.chmod(os.path.join(bin_dir, "docker"), 0o755)
        self.env = {**os.environ, "PATH": bin_dir + ":" + os.environ["PATH"], "PS": self.ps, "VOLUMES": self.volumes}

    def tearDown(self):
        self.tmp.cleanup()

    def check(self, *rows, volume=False, db=True, db_env=False):
        with open(self.ps, "w") as f:
            f.writelines("\t".join(row) + "\n" for row in rows)
        with open(self.volumes, "w") as f:
            f.write("shop_pgdata\n" if volume else "")
        if db_env:
            with open(os.path.join(self.app, "shared", "db.env"), "w") as f:
                f.write("POSTGRES_USER=app\n")
        script = 'source "$1"; eval "$2"; APP_NAME=shop APP_DIR=$3 DB_ENABLED=$4; ensure_project_free'
        return subprocess.run(["bash", "-c", script, "bash", LIB, self.func, self.app, "1" if db else "0"],
                              env=self.env, capture_output=True, text=True)

    def test_own_project_passes(self):
        out = self.check(("c0ffee", "shop-app-1", self.app), ("cafe", "shop-db-1", self.app), volume=True, db_env=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.check().returncode, 0, "первая публикация: ни контейнеров, ни тома")

    def test_foreign_containers(self):
        out = self.check(("c0ffee", "shop-app-1", self.app), ("dead", "shop-db-1", "/home/u/shop"), ("beef", "shop-bot-1", ""))
        self.assertEqual(out.returncode, 1)
        self.assertIn("shop-db-1 (из /home/u/shop)", out.stderr)
        self.assertIn("shop-bot-1 (папка неизвестна)", out.stderr, "без метки папки контейнер считается чужим")
        self.assertNotIn("shop-app-1", out.stderr)

    def test_foreign_volume(self):
        out = self.check(volume=True)
        self.assertEqual(out.returncode, 1)
        self.assertIn("shop_pgdata", out.stderr)
        self.assertEqual(self.check(volume=True, db=False).returncode, 0, "без базы приложение этот том не подключает")

    def test_docker_fails(self):
        os.remove(self.ps)
        self.env["PS"] = os.path.join(self.tmp.name, "missing")
        out = subprocess.run(["bash", "-c", 'source "$1"; eval "$2"; APP_NAME=shop APP_DIR=$3 DB_ENABLED=0; ensure_project_free',
                              "bash", LIB, self.func, self.app], env=self.env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 1)
        self.assertIn("Docker не ответил", out.stderr)


class ReleaseLinksTest(unittest.TestCase):
    """app-deploy.sh: сборка от root не уходит по ссылкам из кода за пределы релиза."""

    def setUp(self):
        with open(os.path.join(os.path.dirname(AGENT), "app-deploy.sh"), encoding="utf-8") as f:
            src = f.read()
        self.funcs = "\n".join(re.search(r"^%s\(\) \{\n.*?\n\}\n" % name, src, re.M | re.S).group(0)
                               for name in ("release_path", "next_cache"))
        self.tmp = tempfile.TemporaryDirectory()
        self.release = os.path.join(self.tmp.name, "release")
        self.outside = os.path.join(self.tmp.name, "outside")
        os.makedirs(os.path.join(self.release, "sub"))
        os.makedirs(self.outside)
        open(os.path.join(self.release, "sub", "Dockerfile"), "w").close()
        self.env = dict(os.environ)
        if subprocess.run(["realpath", "-e", "/"], capture_output=True).returncode != 0:
            # На macOS realpath без -e: подставляем тот же смысл — путь должен существовать.
            bin_dir = os.path.join(self.tmp.name, "bin")
            os.makedirs(bin_dir)
            with open(os.path.join(bin_dir, "realpath"), "w") as f:
                f.write('#!/bin/sh\n[ "$1" = -e ] && shift\nexec python3 -c \'import os, sys\n'
                        'p = os.path.realpath(sys.argv[1])\nsys.exit(1) if not os.path.exists(p) else print(p)\' "$1"\n')
            os.chmod(os.path.join(bin_dir, "realpath"), 0o755)
            self.env["PATH"] = bin_dir + ":" + self.env["PATH"]

    def tearDown(self):
        self.tmp.cleanup()

    def run_func(self, call):
        script = 'source "$1"; eval "$2"; RELEASE=$3; ' + call
        return subprocess.run(["bash", "-c", script, "bash", LIB, self.funcs, self.release], env=self.env, capture_output=True, text=True)

    def test_release_path(self):
        os.symlink("sub", os.path.join(self.release, "ctx"))
        os.symlink(self.outside, os.path.join(self.release, "etc"))
        os.symlink("../outside", os.path.join(self.release, "up"))
        real = os.path.realpath(self.release)
        for name, want in (("", real), ("sub", real + "/sub"), ("ctx", real + "/sub"), ("ctx/Dockerfile", real + "/sub/Dockerfile")):
            out = self.run_func(f'release_path "{name}"')
            self.assertEqual((out.returncode, out.stdout), (0, want), name)
        for name in ("etc", "up", "..", "missing"):
            self.assertNotEqual(self.run_func(f'release_path "{name}"').returncode, 0, name)

    def test_next_cache(self):
        out = self.run_func("next_cache")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(os.path.isdir(os.path.join(self.release, ".next", "cache")))
        for link in (".next", ".next/cache"):
            shutil.rmtree(os.path.join(self.release, ".next"))
            if link == ".next/cache":
                os.makedirs(os.path.join(self.release, ".next"))
            os.symlink(self.outside, os.path.join(self.release, link))
            out = self.run_func("next_cache")
            self.assertEqual(out.returncode, 1, link)
            self.assertIn(".next ссылкой", out.stderr)
            self.assertEqual(os.listdir(self.outside), [], "за пределами релиза ничего не создано")
            os.unlink(os.path.join(self.release, link))
            os.makedirs(os.path.join(self.release, ".next"), exist_ok=True)


class UploadsLayoutTest(unittest.TestCase):
    """app-deploy.sh: закрытые загрузки не принадлежат группе nginx, открытые — принадлежат."""

    def setUp(self):
        with open(os.path.join(os.path.dirname(AGENT), "app-deploy.sh"), encoding="utf-8") as f:
            src = f.read()
        self.funcs = "\n".join(re.search(r"^%s\(\) \{\n.*?\n\}\n" % name, src, re.M | re.S).group(0)
                               for name in ("layout", "uploads_private"))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app_dir = os.path.join(self.tmp.name, "apps", "shop")
        self.uploads = os.path.join(self.app_dir, "shared", "uploads")
        self.bin = os.path.join(self.tmp.name, "bin")
        self.calls = os.path.join(self.tmp.name, "calls")
        self.group = os.path.join(self.tmp.name, "group")
        os.makedirs(self.bin)
        fakes = {
            # Каталоги создаём по-настоящему: layout смотрит, есть ли уже загрузки.
            "install": 'for a; do last=$a; done; mkdir -p "$last"',
            "stat": 'cat "%s"' % self.group,
            "python3": "",
            "chgrp": "",
            "chmod": "",
        }
        for name, body in fakes.items():
            path = os.path.join(self.bin, name)
            with open(path, "w") as f:
                f.write('#!/bin/bash\necho "%s $*" >>"%s"\n%s\n' % (name, self.calls, body))
            os.chmod(path, 0o755)

    def run_layout(self, public, existing_group=None):
        if existing_group:
            os.makedirs(self.uploads)
            with open(self.group, "w") as f:
                f.write(existing_group + "\n")
        script = ('source "$1"; eval "$2"; mount_noexec() { :; }; APPS_ROOT=$3/apps APP_DIR=$3/apps/shop APP_USER=app-shop '
                  'APP_GID=1234 UPLOADS_ENABLED=1 UPLOADS_PUBLIC=$4 DATA_ENABLED=0; layout; uploads_private')
        out = subprocess.run(["bash", "-c", script, "bash", LIB, self.funcs, self.tmp.name, str(int(public))],
                             env={**os.environ, "PATH": self.bin + os.pathsep + os.environ["PATH"]}, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        with open(self.calls) as f:
            return [line.replace(self.tmp.name, "") for line in f.read().splitlines()]

    def test_private_is_created_without_nginx_group(self):
        calls = self.run_layout(False)
        self.assertIn("install -d -m 750 -o app-shop -g 1234 /apps/shop/shared/uploads", calls)
        self.assertNotIn("www-data", " ".join(calls))
        self.assertEqual(calls[-2:], ["chgrp 1234 /apps/shop/shared/uploads", "chmod 00750 /apps/shop/shared/uploads"])

    def test_private_existing_is_closed_only_after_site(self):
        calls = self.run_layout(False, existing_group="www-data")
        self.assertFalse([c for c in calls if c.endswith("/shared/uploads") and c.startswith("install")],
                         "до нового конфига сайта прежний ещё может отдавать загрузки")
        self.assertEqual(calls[-2:], ["chgrp 1234 /apps/shop/shared/uploads", "chmod 00750 /apps/shop/shared/uploads"])

    def test_public_is_served_by_nginx(self):
        calls = self.run_layout(True)
        self.assertIn("install -d -m 2750 -o app-shop -g www-data /apps/shop/shared/uploads", calls)
        self.assertFalse([c for c in calls if c.split()[0] in ("python3", "chgrp", "chmod")])

    def test_public_after_private_regroups_files(self):
        calls = self.run_layout(True, existing_group="app-shop")
        regroup = calls.index("python3 %s/serverbase.py uploads-group /apps/shop/shared/uploads www-data"
                               % os.path.realpath(os.path.dirname(AGENT)))
        self.assertLess(regroup, calls.index("install -d -m 2750 -o app-shop -g www-data /apps/shop/shared/uploads"))

    def test_public_already_served_is_not_walked(self):
        calls = self.run_layout(True, existing_group="www-data")
        self.assertFalse([c for c in calls if c.startswith("python3")])


class HeartbeatTest(unittest.TestCase):
    def test_reports_caps(self):
        with open(os.path.join(os.path.dirname(AGENT), "healthcheck.sh"), encoding="utf-8") as f:
            code = re.search(r"HEARTBEAT_PY='\n(.*?)\n'\n", f.read(), re.S).group(1)
        compile(code, "HEARTBEAT_PY", "exec")
        self.assertIn('"caps": ["release_links"]', code)
        if not os.path.exists("/proc/meminfo"):
            self.skipTest("отметка читает /proc, как на сервере")
        out = subprocess.run([sys.executable, "-c", code, "v", "null", "false", "", os.devnull, os.devnull, "0"],
                             capture_output=True, text=True, check=True)
        self.assertIn("release_links", json.loads(out.stdout)["caps"])


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


class WaitKeyLoginTest(unittest.TestCase):
    """Пока агент ждёт вход по ключу, новые ключи устройств должны доходить до сервера."""

    def run_wait(self, logins, wait=30):
        func = re.search(r"^wait_key_login\(\) \{.*?^\}$", SOURCE, re.S | re.M).group(0)
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "sshkeys.py"), "w", encoding="utf-8") as f:
                f.write("import os, sys\n"
                        f"p = {os.path.join(tmp, 'rc')!r}\n"
                        "rc = open(p).read().split()\n"
                        "open(p, 'w').write(' '.join(rc[1:] or rc))\n"
                        "sys.exit(int(rc[0]))\n")
            with open(os.path.join(tmp, "rc"), "w", encoding="utf-8") as f:
                f.write(logins)
            script = f"""
RECIPES_DIR={tmp} RUN_DIR={tmp} SSH_LOGIN_WAIT={wait} SSH_LOGIN_SLICE=1
dashboard_fetch() {{ echo "$2" >>{tmp}/urls; if (($(wc -l <{tmp}/urls) == 2)); then echo x >"$3"; echo 200; else echo 204; fi; }}
handle() {{ echo handled >>{tmp}/events; }}
{func}
wait_key_login 100 {tmp}/log
echo "rc=$?"
"""
            out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, check=True, timeout=60).stdout
            events = open(os.path.join(tmp, "events")).read().split() if os.path.exists(os.path.join(tmp, "events")) else []
            urls = open(os.path.join(tmp, "urls")).read().split()
            return out.strip(), events, urls

    def test_keys_arrive_while_waiting(self):
        out, events, urls = self.run_wait("2 2 2 0")
        self.assertEqual(out, "rc=0")
        self.assertEqual(events, ["handled"], "ключи применены, не дожидаясь конца ожидания")
        self.assertEqual(len(urls), 4)
        self.assertTrue(all("only=ssh_keys" in u for u in urls), "другие команды во время ожидания не берём")

    def test_timeout(self):
        out, _, _ = self.run_wait("2", wait=2)
        self.assertEqual(out, "rc=2")

    def test_nothing_to_check_stops_at_once(self):
        out, _, urls = self.run_wait("1")
        self.assertEqual(out, "rc=1")
        self.assertEqual(len(urls), 1)


class ResultQueueTest(unittest.TestCase):
    """Итог команды не теряется, пока дашборд недоступен, и не держит агента."""

    FUNCS = "\n".join((re.search(r"^%s\(\) \{ .*\}$" % name, SOURCE, re.M)
                       or re.search(r"^%s\(\) [{(]\n.*?^[})]$" % name, SOURCE, re.S | re.M)).group(0)
                      for name in ("jitter", "send_result", "result_file", "results_pending", "uptime_cs", "result_age", "drop_result",
                                   "flush_results", "results_hold"))
    ID = CID

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = os.path.join(self.tmp.name, "results")
        self.code("204")
        self.uptime(100000)

    def uptime(self, seconds):
        with open(os.path.join(self.tmp.name, "uptime"), "w") as f:
            f.write("%d.42 1234.56\n" % seconds)

    def put(self, age, suffix="", cid=CID):
        """Итог, пролежавший в очереди age секунд (по часам с загрузки)."""
        os.makedirs(self.dir, exist_ok=True)
        name = "%015d-%s-success%s.json" % ((100000 - age) * 100, cid, suffix)
        with open(os.path.join(self.dir, name), "w") as f:
            f.write("{}")
        return name

    def code(self, code):
        with open(os.path.join(self.tmp.name, "code"), "w") as f:
            f.write(code)

    def run_sh(self, body, kind=""):
        t = self.tmp.name
        script = f"""
set -u
RESULTS_DIR={self.dir} RESULT_KEEP=3600 RESULT_HOLD=600 MAX_RESULTS=3 CMD_KIND={kind} UPTIME={t}/uptime RUN_DIR={t}
{STOP_MESSAGE_LINE}
flock() {{ return 0; }}
dashboard_fetch() {{ echo "$2" >>{t}/urls; cp "${{4#@}}" {t}/body; cat {t}/code; }}
{self.FUNCS}
{body}
"""
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30,
                              env={**os.environ, "PAYLOAD_PY": PAYLOAD_PY})

    def queued(self):
        return sorted(n for n in os.listdir(self.dir) if not n.startswith(".")) if os.path.isdir(self.dir) else []

    def urls(self):
        path = os.path.join(self.tmp.name, "urls")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return f.read().split()

    def send(self, status="success", kind="restart"):
        return self.run_sh(f'send_result {self.ID} {status} "сообщение"', kind)

    def test_delivered_at_once(self):
        out = self.send()
        self.assertIn(f"команда {self.ID}: success", out.stdout, out.stderr)
        self.assertEqual(self.queued(), [])
        self.assertEqual(self.urls(), [f"/agent/v1/commands/{self.ID}/result"])
        with open(os.path.join(self.tmp.name, "body"), encoding="utf-8") as f:
            self.assertEqual(json.load(f)["message"], "сообщение")

    def test_dashboard_down_keeps_result_and_does_not_wait(self):
        self.code("000")
        out = self.send(status="failed")
        self.assertIn("пока не доставлен", out.stdout, out.stderr)
        self.assertEqual(len(self.queued()), 1)
        self.assertEqual(len(self.urls()), 1, "одна попытка, без пауз: агент свободен для следующей команды")
        self.code("204")
        out = self.run_sh("flush_results")
        self.assertIn(f"команда {self.ID}: failed", out.stdout, out.stderr)
        self.assertEqual(self.queued(), [])

    def test_rejected_result_is_dropped(self):
        self.code("400")
        self.send()
        self.assertEqual(self.queued(), [])

    def test_stops_on_first_failure_and_keeps_order(self):
        self.code("503")
        for i in range(2):
            self.run_sh(f'send_result 0192e000-0000-7000-8000-00000000000{i} success ""')
        self.assertEqual(len(self.queued()), 2)
        os.remove(os.path.join(self.tmp.name, "urls"))
        self.run_sh("flush_results")
        self.assertEqual(self.urls(), ["/agent/v1/commands/0192e000-0000-7000-8000-000000000000/result"])

    def test_old_and_extra_results_are_dropped(self):
        old = self.put(7200)
        for age in range(4):
            self.put(age, cid=CID[:-1] + str(age + 2))
        self.code("000")
        self.run_sh("flush_results")
        self.assertEqual(len(self.queued()), 3, "очередь не растёт без предела")
        self.assertNotIn(old, self.queued())

    def test_hold_only_for_deploy(self):
        self.code("000")
        self.send(kind="restart")
        self.assertEqual(self.run_sh("results_hold").returncode, 1, "итог перезапуска не мешает брать команды")
        self.uptime(100001)
        self.send(kind="deploy")
        self.assertEqual(self.run_sh("results_hold").returncode, 0, "итог выкатки — сначала его, потом новые команды")
        self.uptime(100002)
        self.send(kind="unknown")
        self.assertEqual([n.endswith("-hold.json") for n in self.queued()], [False, True, True])

    def test_hold_and_keep_expire(self):
        self.put(900, suffix="-hold")
        self.assertEqual(self.run_sh("results_hold").returncode, 1)
        self.put(300, suffix="-hold")
        self.assertEqual(self.run_sh("results_hold").returncode, 0)
        self.uptime(100000 + 400)
        self.assertEqual(self.run_sh("results_hold").returncode, 1, "через RESULT_HOLD агент снова берёт команды")

    def test_age_does_not_depend_on_wall_clock(self):
        self.put(30, suffix="-hold")
        out = self.run_sh('date() { echo 1000000000; }; results_hold; echo "hold=$?"; result_age "$(ls "$RESULTS_DIR")"')
        self.assertEqual(out.stdout.split(), ["hold=0", "30"], out.stderr)

    def test_impossible_age_counts_as_expired(self):
        self.put(-50, suffix="-hold")
        self.assertEqual(self.run_sh("results_hold").returncode, 1, "итог «из будущего» не держит агента вечно")
        self.run_sh("flush_results")
        self.assertEqual(self.queued(), [], "заведомо старые итоги отбрасываются без отправки")
        self.assertEqual(self.urls(), [])
        self.put(10, suffix="-hold")
        os.remove(os.path.join(self.tmp.name, "uptime"))
        self.assertEqual(self.run_sh("results_hold").returncode, 1, "без часов с загрузки не держим")

    def test_reissued_command_drops_its_old_result(self):
        self.code("000")
        self.send(kind="deploy")
        other = self.put(5, cid="0192e000-0000-7000-8000-0000000000ff")
        out = self.run_sh(f"drop_result {self.ID}")
        self.assertIn("пришла снова", out.stdout, out.stderr)
        self.assertEqual(self.queued(), [other], "итог прежнего запуска не закроет новый, чужие итоги остаются")

    def test_handle_drops_old_result_before_running(self):
        handle = re.search(r"^handle\(\) \{\n(.*?)^\}$", SOURCE, re.S | re.M).group(1)
        self.assertLess(handle.index('drop_result "${f[1]}"'), handle.index("case ${f[2]:-} in"))

    def test_unsaved_result_is_logged(self):
        os.makedirs(self.dir)
        out = self.run_sh(f'python3() {{ return 1; }}; send_result {self.ID} failed ""')
        self.assertIn("не удалось сохранить", out.stdout, out.stderr)
        self.assertEqual(os.listdir(self.dir), [])

    def test_jitter(self):
        values = {int(v) for v in self.run_sh("for i in $(seq 50); do jitter 10; done").stdout.split()}
        self.assertTrue(values <= set(range(10, 16)) and len(values) > 1, values)


def term_tree(pid):
    """TERM процессу и всем его потомкам разом — как systemd всей группе службы."""
    out = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True).stdout
    children = {}
    for line in out.splitlines():
        if line.strip():
            p, parent = map(int, line.split())
            children.setdefault(parent, []).append(p)
    tree, i = [pid], 0
    while i < len(tree):
        tree += children.get(tree[i], [])
        i += 1
    for p in tree:
        try:
            os.kill(p, signal.SIGTERM)
        except ProcessLookupError:
            pass


def agent_globals(run_dir):
    """Глобальные переменные agent.sh — как он их задаёт до любой функции, но с каталогами во временном."""
    paths = {"RUN_DIR": run_dir, "STATE_DIR": run_dir, "RESULTS_DIR": os.path.join(run_dir, "results"),
             "AUDIT_LOG": os.path.join(run_dir, "audit.log")}
    lines = []
    for line in SOURCE[:SOURCE.index("\nPARSE_PY='")].splitlines():
        m = re.match(r"^([A-Z_][A-Z0-9_]*)=", line)
        if not m or m.group(1) in ("SELF", "SELF_INODE"):
            continue
        lines.append("%s=%s" % (m.group(1), shlex.quote(paths[m.group(1)])) if m.group(1) in paths else line)
    return "\n".join(lines)


STOP_MESSAGE_LINE = re.search(r"^STOP_MESSAGE=.*$", SOURCE, re.M).group(0)
HANDLE_FUNCS = ("handle", "drop_result", "results_pending", "results_flusher", "jitter", "long_running", "reap_long",
                "wait_long", "must_wait_long", "long_job", "stop_recipe", "recipe_wait", "start_long", "stop_agent", "stop_current",
                "sent_already", "sent_trim", "job_pending", "resume_later")


def agent_funcs(*names):
    """Текст функций agent.sh: однострочных и многострочных."""
    return "\n".join((re.search(r"^%s\(\) \{ .*\}$" % name, SOURCE, re.M)
                      or re.search(r"^%s\(\) [{(]\n.*?^[})]$" % name, SOURCE, re.S | re.M)).group(0) for name in names)


class LongCommandTest(unittest.TestCase):
    """Долгая команда идёт в фоне, а агент тем временем берёт только быстрые и только у дашборда, который это умеет."""

    FUNCS = agent_funcs("long_running", "reap_long", "wait_long", "must_wait_long", "long_job", "stop_recipe", "recipe_wait", "start_long",
                        "stop_agent", "stop_current", "sent_already", "job_pending", "resume_later",
                        "side_poll", "poll_once")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.t = self.tmp.name
        open(os.path.join(self.t, "conf"), "w").close()

    def run_sh(self, body, codes=(), timeout=30):
        """codes — ответы дашборда по порядку (200 — с командой «cmd-N»), дальше 204."""
        t = self.t
        script = f"""
set -u
T={t}
RUN_DIR=$T STATE_DIR=$T WAIT=25 SIDE_WAIT=10 SIDE_RECHECK=600 QUICK_KINDS=db_query,ssh_keys,ssh_password,restart,remove
LONG_PID="" LONG_APP="" LONG_ID="" LONG_KIND="" LONG_CHILD="" STOP_GRACE=40 SIDE_POLL=list SIDE_POLL_AT=0 BACKOFF=5
CUR_ID="" CUR_KIND="" CUR_APP="" CUR_LOG="" RESUME=$T/resume
{STOP_MESSAGE_LINE}
RESULTS_DIR=$T/results
ENV_JSON=$T/env.json PANEL_ENV=$T/panel.env ENV_ERR=$T/env.err
LONG_ENV_JSON=$T/long-env.json LONG_PANEL_ENV=$T/long-panel.env LONG_ENV_ERR=$T/long-env.err
DASHBOARD_CONF=$T/conf SELF=$T/self SELF_INODE=same
CODES=({' '.join(codes)})
stat() {{ echo same; }}
flush_results() {{ :; }}
results_pending() {{ return 1; }}
send_result() {{ echo "main result $1 $2 stop=${{LONG_STOP:-0}} kind=${{CMD_KIND:-}}" >>$T/events; }}
results_hold() {{ [[ -e $T/hold ]]; }}
jitter() {{ echo "$1"; }}
sleep() {{ echo "sleep $1" >>$T/events; }}
dashboard_fetch() {{
	echo "$2" >>$T/urls
	local n
	n=$(wc -l <$T/urls)
	n=${{n// /}}
	local c=${{CODES[n-1]:-204}}
	[[ $c == 200 ]] && echo "cmd-$n" >"$3"
	echo "$c"
}}
handle() {{ echo "handle $(cat "$1") long=$(long_running && echo yes || echo no)" >>$T/events; rm -f "$1"; }}
fake_long() {{ /bin/sleep "$1" & recipe_wait $!; echo "long done ENV_JSON=${{ENV_JSON##*/}} stop=${{LONG_STOP:-0}}" >>$T/events; }}
{self.FUNCS}
{body}
"""
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=timeout)

    def read(self, name):
        path = os.path.join(self.t, name)
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return f.read().splitlines()

    def test_quick_command_runs_while_long_one_does(self):
        out = self.run_sh("start_long id-1 shop fake_long 1; poll_once; wait_long; poll_once", codes=("200",))
        self.assertEqual(out.returncode, 0, out.stderr)
        urls = self.read("urls")
        self.assertEqual(urls[0], "/agent/v1/commands/next?wait=10&only=db_query,ssh_keys,ssh_password,restart,remove&busy_app=shop")
        self.assertEqual(urls[1], "/agent/v1/commands/next?wait=25", "после долгой — обычный опрос, и только тогда")
        events = self.read("events")
        self.assertEqual(events[0], "handle cmd-1 long=yes", "быструю команду не ждали")
        self.assertIn("long done ENV_JSON=long-env.json stop=0", events, "у долгой свои файлы переменных")

    def test_finished_long_command_is_reaped(self):
        out = self.run_sh("start_long id-1 shop fake_long 0; /bin/sleep 1; poll_once; echo \"pid=[$LONG_PID]\"")
        self.assertIn("pid=[]", out.stdout, out.stderr)
        self.assertEqual(self.read("urls"), ["/agent/v1/commands/next?wait=25"])

    def test_old_dashboard_without_kinds_list(self):
        out = self.run_sh("start_long id-1 shop fake_long 1; poll_once; poll_once; echo \"mode=$SIDE_POLL\"; poll_once; "
                          "echo \"pid=[$LONG_PID]\"", codes=("400", "400"))
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual([u.split("&", 1)[1] for u in self.read("urls")],
                         ["only=db_query,ssh_keys,ssh_password,restart,remove&busy_app=shop", "only=ssh_keys"],
                         "список не знают — проверенный опрос ключей, не знают и его — не опрашиваем")
        self.assertIn("mode=none", out.stdout)
        self.assertIn("pid=[]", out.stdout, "без опроса просто ждём долгую, как раньше")
        self.assertIn("long done ENV_JSON=long-env.json stop=0", self.read("events"))

    def test_kinds_list_is_tried_again_later(self):
        self.run_sh("start_long id-1 shop fake_long 1; SIDE_POLL=none SIDE_POLL_AT=-700; poll_once; wait_long")
        self.assertIn("only=db_query", self.read("urls")[0])

    def test_held_deploy_result_allows_only_quick_commands(self):
        open(os.path.join(self.t, "hold"), "w").close()
        self.run_sh("poll_once")
        self.assertEqual(self.read("urls"), ["/agent/v1/commands/next?wait=10&only=db_query,ssh_keys,ssh_password,restart,remove"],
                         "пока итог выкатки не доставлен, дашборд не должен вернуть её в очередь")

    def test_what_waits_for_the_long_command(self):
        script = ('LONG_PID=1 LONG_APP=shop; for c in "deploy blog" "data_move blog" "update" "os_upgrade" "restart shop" '
                  '"remove shop" "restart blog" "remove blog" "db_query shop" "ssh_keys" "ssh_password"; do '
                  'set -- $c; must_wait_long "$1" "${2:-}" && echo "$c: wait" || echo "$c: now"; done; '
                  'LONG_PID=""; must_wait_long deploy blog || echo "idle: now"')
        self.assertEqual(self.run_sh(script).stdout.splitlines(), [
            "deploy blog: wait", "data_move blog: wait", "update: wait", "os_upgrade: wait", "restart shop: wait",
            "remove shop: wait", "restart blog: now", "remove blog: now", "db_query shop: now", "ssh_keys: now",
            "ssh_password: now", "idle: now"])

    def test_stop_lets_long_command_report(self):
        started = time.monotonic()
        out = self.run_sh("start_long id-1 shop fake_long 30; /bin/sleep 0.5; stop_agent; echo not-reached", timeout=20)
        self.assertLess(time.monotonic() - started, 10, "рецепт обрывается, а не дорабатывает")
        self.assertNotIn("not-reached", out.stdout)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("long done ENV_JSON=long-env.json stop=1", self.read("events"), "итог «прервано» успевает уйти")

    def test_interrupted_result_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            funcs = ResultQueueTest.FUNCS
            script = f"""
set -u
RESULTS_DIR={tmp}/results RESULT_KEEP=3600 RESULT_HOLD=600 MAX_RESULTS=3 CMD_KIND=deploy UPTIME={tmp}/uptime LONG_STOP=1 RUN_DIR={tmp}
{STOP_MESSAGE_LINE}
echo "100.00 1.0" >{tmp}/uptime
flock() {{ return 0; }}
dashboard_fetch() {{ cp "${{4#@}}" {tmp}/body; echo 204; }}
{funcs}
send_result {CID} failed ""
"""
            subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30, env={**os.environ, "PAYLOAD_PY": PAYLOAD_PY},
                           check=True)
            with open(os.path.join(tmp, "body"), encoding="utf-8") as f:
                self.assertIn("прервано", json.load(f)["message"])


@unittest.skipUnless(shutil.which("flock"), "нужен flock, как на сервере")
class NginxLockTest(unittest.TestCase):
    """Публикация одного приложения и удаление другого идут разом: правки nginx и certbot — по очереди."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lock = os.path.join(self.tmp.name, "nginx.lock")

    def hold(self, seconds):
        """Другой процесс держит замок seconds секунд; вернуть, когда он его взял."""
        ready = os.path.join(self.tmp.name, "ready")
        proc = subprocess.Popen(["bash", "-c", 'source "$1"; NGINX_LOCK=$2; nginx_lock; : >"$3"; sleep "$4"; nginx_unlock',
                                 "bash", LIB, self.lock, ready, str(seconds)])
        self.addCleanup(proc.wait)
        deadline = time.monotonic() + 10
        while not os.path.exists(ready) and time.monotonic() < deadline:
            time.sleep(0.02)
        return proc

    def locked(self, body, wait=1):
        return subprocess.run(["bash", "-c", 'source "$1"; NGINX_LOCK=$2 NGINX_LOCK_WAIT=$3; ' + body, "bash", LIB, self.lock, str(wait)],
                              capture_output=True, text=True, timeout=30)

    def test_waits_and_fails_loudly(self):
        self.hold(3)
        out = self.locked("nginx_lock; echo inside")
        self.assertEqual(out.returncode, 1)
        self.assertNotIn("inside", out.stdout, "без замка конфиги nginx не трогаем")
        self.assertIn("занят", out.stderr)
        self.assertIn("inside", self.locked("nginx_lock; echo inside", wait=10).stdout, "дождались — правим")

    def test_unlock_releases_at_once(self):
        ready = os.path.join(self.tmp.name, "ready")
        proc = subprocess.Popen(["bash", "-c", 'source "$1"; NGINX_LOCK=$2; nginx_lock; nginx_unlock; : >"$3"; sleep 3',
                                 "bash", LIB, self.lock, ready])
        self.addCleanup(proc.wait)
        while not os.path.exists(ready):
            time.sleep(0.02)
        self.assertIn("inside", self.locked("nginx_lock; echo inside").stdout)

    def test_remove_site_keeps_files_until_lock(self):
        with open(os.path.join(os.path.dirname(AGENT), "app-remove.sh"), encoding="utf-8") as f:
            src = f.read()
        root = self.tmp.name
        func = re.search(r"^remove_site\(\) \{\n.*?\n\}\n", src, re.M | re.S).group(0).replace("/etc/", root + "/etc/") \
            .replace("/var/log/", root + "/var/log/")
        for d in ("etc/nginx/sites-available", "etc/nginx/sites-enabled", "bin"):
            os.makedirs(os.path.join(root, d))
        site = os.path.join(root, "etc/nginx/sites-available/airlok-shop.conf")
        with open(site, "w") as f:
            f.write("server {\n    server_name shop.example.ru;\n}\n")
        for tool in ("nginx", "systemctl"):
            with open(os.path.join(root, "bin", tool), "w") as f:
                f.write("#!/bin/sh\nexit 0\n")
            os.chmod(os.path.join(root, "bin", tool), 0o755)
        self.hold(3)
        env = {**os.environ, "PATH": os.path.join(root, "bin") + os.pathsep + os.environ["PATH"]}
        script = 'source "$1"; eval "$2"; NGINX_LOCK=$3 NGINX_LOCK_WAIT=1 NAME=shop DOMAIN_RE=x; remove_site'
        out = subprocess.run(["bash", "-c", script, "bash", LIB, func, self.lock], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(out.returncode, 1)
        self.assertTrue(os.path.exists(site), "сайт снимаем только под замком: иначе nginx -t увидел бы чужой недопроверенный конфиг")
        script = script.replace("NGINX_LOCK_WAIT=1", "NGINX_LOCK_WAIT=10")
        out = subprocess.run(["bash", "-c", script, "bash", LIB, func, self.lock], capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertFalse(os.path.exists(site))


@unittest.skipUnless(server_shell() and shutil.which("flock"), "нужны bash 4+, GNU coreutils и flock, как на сервере")
class HandleLongTest(unittest.TestCase):
    """Настоящий handle: выкатка уходит в фон, быстрая команда идёт рядом, а вторая выкатка, обновление агента
    и перезапуск собираемого приложения ждут её конца; остановка службы обрывает выкатку с итогом «прервано»."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.t = self.tmp.name
        with open(os.path.join(self.t, "strict.py"), "w") as f:
            f.write("import sys\nsys.exit(0)\n")

    def script(self, body, stubs):
        return f"""
source "$1"
set +e
{agent_globals(self.t)}
T={self.t}
RECIPES_DIR=$T POLICY_CONF=$T/policy
audit() {{ :; }}
{stubs}
{agent_funcs(*HANDLE_FUNCS)}
{body}
"""

    def run_handle(self, *commands):
        calls = []
        for i, cmd in enumerate(commands):
            with open(os.path.join(self.t, "cmd%d.json" % i), "w") as f:
                json.dump(cmd, f)
            calls.append("handle $T/cmd%d.json" % i)
            if i == 0:
                calls.append("until_file $T/started")
        # Порядок держится на явных отметках, а не на скорости: быстрая команда идёт после старта сборки,
        # а сборка кончается только после неё.
        stubs = """
until_file() { local i; for i in $(seq 200); do [[ -e $1 ]] && return 0; /bin/sleep 0.05; done; return 1; }
run_deploy() {
	echo "deploy $2 start log=${CMD_LOG##*/} env=${ENV_JSON##*/}" >>$T/events
	: >$T/started
	until_file $T/release
	echo "deploy $2 end" >>$T/events
}
run_restart() { echo "restart $2 log=${CMD_LOG##*/}" >>$T/events; [[ $2 == blog ]] && : >$T/release; }
run_update() { echo "update" >>$T/events; }
"""
        script = self.script("; ".join(calls) + "; wait_long; cat $T/events", stubs)
        return subprocess.run(["bash", "-c", script, "bash", LIB], capture_output=True, text=True, timeout=60,
                              env={**os.environ, "PARSE_PY": PARSE_PY})

    def deploy(self, cid, app):
        return {"id": cid, "kind": "deploy", "deploy": {"app": app, "repo": "acme/" + app, "sha": "a" * 40}}

    def restart(self, cid, app):
        return {"id": cid, "kind": "restart", "restart": {"app": app}}

    def test_quick_runs_now_same_app_and_second_deploy_wait(self):
        ids = ["0192e000-0000-7000-8000-00000000000%d" % i for i in range(5)]
        out = self.run_handle(self.deploy(ids[0], "shop"), self.restart(ids[1], "blog"), self.restart(ids[2], "shop"),
                              self.deploy(ids[3], "blog"), {"id": ids[4], "kind": "update"})
        events = [line for line in out.stdout.splitlines() if not line.startswith("команда")]
        self.assertEqual(sorted(events), sorted(["deploy shop start log=command.log env=long-env.json",
                                                 "restart blog log=side-command.log", "deploy shop end", "restart shop log=command.log",
                                                 "deploy blog start log=command.log env=long-env.json", "deploy blog end", "update"]),
                         out.stderr)
        at = {e.split(" log=")[0]: i for i, e in enumerate(events)}
        self.assertLess(at["restart blog"], at["deploy shop end"], "быстрая команда другого приложения не ждёт сборку")
        for before, after in (("deploy shop end", "restart shop"), ("deploy shop end", "deploy blog start"),
                              ("deploy blog end", "update")):
            self.assertLess(at[before], at[after], f"{after} ждёт {before}")
        self.assertIn(f"команда {ids[2]}: ждёт конца команды {ids[0]}", out.stdout)
        self.assertIn(f"команда {ids[4]}: ждёт конца команды {ids[3]}", out.stdout, "обновление агента — только свободным")

    def test_old_dashboard_falls_back_to_one_by_one(self):
        with open(os.path.join(self.t, "cmd.json"), "w") as f:
            json.dump(self.deploy(CID, "shop"), f)
        stubs = """
SELF=$T/self SELF_INODE=same DASHBOARD_CONF=$T/conf
: >$T/conf
stat() { echo same; }
sleep() { :; }
CODES=(200 400 400)
dashboard_fetch() {
	echo "GET $2" >>$T/events
	local n
	n=$(grep -c ^GET $T/events)
	local c=${CODES[n-1]:-204}
	[[ $c == 200 ]] && cp $T/cmd.json "$3"
	# Сборка кончается только после опроса only=ssh_keys: дальше агент должен ждать её без опроса.
	[[ $2 == *only=ssh_keys* ]] && : >$T/release
	echo "$c"
}
run_deploy() {
	echo "deploy $2 start" >>$T/events
	local i
	for i in $(seq 200); do [[ -e $T/release ]] && break; /bin/sleep 0.05; done
	echo "deploy $2 end" >>$T/events
}
"""
        script = self.script("", stubs)
        script += agent_funcs("poll_once", "side_poll", "flush_results", "results_hold", "result_age", "uptime_cs")
        script += "\npoll_once; poll_once; poll_once; poll_once; poll_once; cat $T/events\n"
        out = subprocess.run(["bash", "-c", script, "bash", LIB], capture_output=True, text=True, timeout=60,
                             env={**os.environ, "PARSE_PY": PARSE_PY})
        events = out.stdout.splitlines()
        events = [e for e in events if e.startswith(("GET", "deploy"))]
        gets = [e for e in events if e.startswith("GET")]
        self.assertEqual(gets, ["GET /agent/v1/commands/next?wait=25",
                                "GET /agent/v1/commands/next?wait=10&only=db_query,ssh_keys,ssh_password,restart,remove&busy_app=shop",
                                "GET /agent/v1/commands/next?wait=10&only=ssh_keys",
                                "GET /agent/v1/commands/next?wait=25"], out.stdout + out.stderr)
        last_get = max(i for i, e in enumerate(events) if e.startswith("GET"))
        self.assertEqual(events[last_get], gets[3])
        self.assertLess(events.index("deploy shop end"), last_get, "без опроса ждём сборку и только потом берём новые")

    def test_stop_interrupts_deploy_and_reports(self):
        with open(os.path.join(self.t, "github-deploy.sh"), "w") as f:
            f.write("#!/bin/bash\necho '→ Сборка'\nexec sleep 30\n")
        os.chmod(os.path.join(self.t, "github-deploy.sh"), 0o755)
        with open(os.path.join(self.t, "cmd.json"), "w") as f:
            json.dump(self.deploy(CID, "shop"), f)
        stubs = """
dashboard_fetch() { [[ $2 == */result ]] && cp "${4#@}" $T/result.json; echo 204; }
"""
        script = self.script("", stubs)
        script += agent_funcs("send_result", "result_file", "uptime_cs", "result_age", "flush_results", "stream_progress", "job_pending", "run_deploy")
        script += "\ntrap stop_agent TERM; handle $T/cmd.json; sleep 1; kill -TERM $$; echo not-reached\n"
        started = time.monotonic()
        out = subprocess.run(["bash", "-c", script, "bash", LIB], capture_output=True,
                             text=True, timeout=60, env={**os.environ, "PARSE_PY": PARSE_PY, "PAYLOAD_PY": PAYLOAD_PY})
        self.assertLess(time.monotonic() - started, 20, "сборку оборвали, а не ждали")
        self.assertNotIn("not-reached", out.stdout)
        with open(os.path.join(self.t, "result.json"), encoding="utf-8") as f:
            body = json.load(f)
        self.assertEqual(body["status"], "failed", out.stdout + out.stderr)
        self.assertIn("прервано", body["message"])

    def test_term_to_whole_tree_keeps_result(self):
        """Как systemd при остановке службы: TERM разом всем процессам — рецепту, оболочке долгой команды,
        основному циклу и тем, кто в этот миг сохраняет итог. Итог не должен теряться ни разу."""
        with open(os.path.join(self.t, "github-deploy.sh"), "w") as f:
            f.write('#!/bin/bash\n: >"$(dirname "$0")/started"\nexec sleep "$(cat "$(dirname "$0")/delay")"\n')
        os.chmod(os.path.join(self.t, "github-deploy.sh"), 0o755)
        with open(os.path.join(self.t, "cmd.json"), "w") as f:
            json.dump(self.deploy(CID, "shop"), f)
        stubs = "dashboard_fetch() { echo 000; }\n"
        script = self.script("", stubs)
        script += agent_funcs("send_result", "result_file", "uptime_cs", "result_age", "flush_results", "stream_progress", "job_pending", "run_deploy")
        # Основной цикл в это время занят своим: дочерний процесс, которого тоже касается TERM.
        script += "\nSTOP_GRACE=2; trap stop_agent TERM; handle $T/cmd.json; while :; do reap_long; /bin/sleep 5; done\n"
        results = os.path.join(self.t, "results")
        lost = []
        for i in range(30):
            shutil.rmtree(results, ignore_errors=True)
            # handle удаляет разобранную команду: на каждый повтор — заново.
            with open(os.path.join(self.t, "cmd.json"), "w") as f:
                json.dump(self.deploy(CID, "shop"), f)
            for name in ("started", "events"):
                if os.path.exists(os.path.join(self.t, name)):
                    os.remove(os.path.join(self.t, name))
            # Сборка то ещё идёт, то кончилась сама в тот же миг, что пришёл TERM.
            recipe, signal_after = (0.3, 0.0), (0.05, 0.15, 0.0, 0.3, 0.1)
            with open(os.path.join(self.t, "delay"), "w") as f:
                f.write(str(recipe[i % 2]))
            log = open(os.path.join(self.t, "run.log"), "w")
            proc = subprocess.Popen(["bash", "-c", script, "bash", LIB], stdout=log, stderr=subprocess.STDOUT,
                                    start_new_session=True, env={**os.environ, "PARSE_PY": PARSE_PY, "PAYLOAD_PY": PAYLOAD_PY})
            log.close()
            deadline = time.monotonic() + 10
            while not os.path.exists(os.path.join(self.t, "started")) and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(os.path.exists(os.path.join(self.t, "started")), "сборка не началась")
            time.sleep(signal_after[i % len(signal_after)])
            started = time.monotonic()
            term_tree(proc.pid)
            proc.wait(timeout=90)
            took = time.monotonic() - started
            if not glob.glob(os.path.join(results, f"*-{CID}-*.json")) or took > 20:
                with open(os.path.join(self.t, "run.log"), encoding="utf-8", errors="replace") as f:
                    lost.append((i, round(took, 1), f.read()[-1500:]))
        self.assertEqual(lost, [], "итог выкатки потерян или остановка затянулась")


if __name__ == "__main__":
    unittest.main()


class HealthTest(unittest.TestCase):
    """Когда приложение считается живым: при публикации и при проверке сайтов."""

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


class StoreLinkTest(unittest.TestCase):
    """app-data.sh: временная ссылка хранилища уходит в curl через stdin, а не аргументом — иначе её видно в ps."""

    LINK = "https://s3.example/b/x.sql.gz?X-Amz-Signature=abc&X-Amz-Expires=7200"

    def setUp(self):
        with open(os.path.join(os.path.dirname(AGENT), "app-data.sh"), encoding="utf-8") as f:
            src = f.read()
        self.funcs = "\n".join([re.search(r"^STORE_URL_RE=.*$", src, re.M).group(0),
                                re.search(r"^check_store_url\(\) \{.*\}$", src, re.M).group(0)] + [
            re.search(r"^%s\(\) \{\n.*?\n\}\n" % name, src, re.M | re.S).group(0) for name in ("put", "get")])
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = os.path.join(self.tmp.name, "bin")
        os.makedirs(self.bin)
        with open(os.path.join(self.bin, "curl"), "w") as f:
            f.write('#!/bin/sh\nprintf "%s\\n" "$@" >"$ARGV"\ncat >"$STDIN"\n')
        os.chmod(os.path.join(self.bin, "curl"), 0o755)
        self.argv = os.path.join(self.tmp.name, "argv")
        self.stdin = os.path.join(self.tmp.name, "stdin")
        self.file = os.path.join(self.tmp.name, "db.sql.gz")
        open(self.file, "w").close()

    def tearDown(self):
        self.tmp.cleanup()

    def run_func(self, call, link):
        env = {**os.environ, "PATH": self.bin + ":" + os.environ["PATH"], "ARGV": self.argv, "STDIN": self.stdin}
        return subprocess.run(["bash", "-c", 'source "$1"; eval "$2"; ' + call, "bash", LIB, self.funcs, self.file, link],
                              env=env, capture_output=True, text=True)

    def read(self, path):
        with open(path) as f:
            return f.read()

    def test_link_not_in_argv(self):
        for call in ('put "$3" "$4"', 'get "$4" "$3"'):
            with self.subTest(call=call):
                out = self.run_func(call, self.LINK)
                self.assertEqual(out.returncode, 0, out.stderr)
                argv = self.read(self.argv).splitlines()
                self.assertNotIn(self.LINK, self.read(self.argv))
                self.assertIn("-K", argv)
                self.assertEqual(self.read(self.stdin), 'url = "%s"\n' % self.LINK)

    def test_rejects_link_that_breaks_config(self):
        for link in ('https://s3.example/x"\nupload-file = "/etc/shadow', "https://s3.example/x\\y", "https://s3.example/x'y",
                     "http://s3.example/x", ""):
            for call in ('put "$3" "$4"', 'get "$4" "$3"'):
                with self.subTest(link=link, call=call):
                    out = self.run_func(call, link)
                    self.assertNotEqual(out.returncode, 0)
                    self.assertIn("некорректная ссылка хранилища", out.stderr)
                    self.assertFalse(os.path.exists(self.argv), "curl не должен запускаться")
