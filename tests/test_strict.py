"""Строгий режим агента: python3 -m unittest discover tests

Запреты проверяются на самом сервере, поэтому тесты идут от лица недобросовестного дашборда.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

RECIPES = os.path.join(os.path.dirname(__file__), "..", "recipes")
sys.path.insert(0, RECIPES)

import strict  # noqa: E402

CID = "0192e000-0000-7000-8000-000000000001"


class NormalizeTest(unittest.TestCase):
    def test_same_repo_one_record(self):
        for repo in ("Owner/Name", "owner/name", "https://github.com/Owner/Name", "https://github.com/owner/name.git", " owner/name "):
            self.assertEqual(strict.normalize(repo), "owner/name", repo)
        self.assertEqual(strict.normalize("https://gitflic.ru/project/a/b.git"), "https://gitflic.ru/project/a/b.git")

    def test_bad(self):
        for repo in ("", "owner", "a/b/c", "../x", "a/..", "owner/na me", "https://x.ru/a/../b.git", "http://x.ru/a/b.git", "a/b;reboot"):
            with self.assertRaises(strict.PolicyError, msg=repo):
                strict.normalize(repo)


class RefusalTest(unittest.TestCase):
    REPOS = ["owner/shop", "https://gitflic.ru/project/a/b.git"]

    def refuse(self, kind, first="", second="", is_strict=True):
        return strict.refusal(is_strict, self.REPOS, kind, first, second)

    def test_normal_mode_refuses_nothing(self):
        for kind in (*strict.REFUSED, "deploy", "remove", "restart"):
            self.assertIsNone(self.refuse(kind, "shop", "other/repo", is_strict=False), kind)

    def test_access_to_the_server_is_closed(self):
        for kind in ("update", "ssh_keys", "ssh_password", "db_query", "data_move", "os_upgrade"):
            self.assertIn("строгий режим", self.refuse(kind), kind)
        self.assertIn("строгий режим запрещает запросы к базе из дашборда на этом сервере.", self.refuse("db_query"))

    def test_empty_repo_is_refused(self):
        self.assertIsNotNone(self.refuse("deploy", "shop", ""))

    def test_deploy_only_from_allowed_repos(self):
        self.assertIsNone(self.refuse("deploy", "shop", "owner/shop"))
        self.assertIsNone(self.refuse("deploy", "shop", "Owner/Shop"), "GitHub не различает регистр")
        self.assertIsNone(self.refuse("deploy", "shop", "https://gitflic.ru/project/a/b.git"))
        for repo in ("attacker/shop", "owner/shop2", "https://evil.example/owner/shop.git", "owner/shop;reboot"):
            self.assertIn("нет в списке", self.refuse("deploy", "shop", repo), repo)

    def test_remove_keeps_data(self):
        self.assertIsNone(self.refuse("remove", "shop", "0"))
        self.assertIn("вместе с данными", self.refuse("remove", "shop", "1"))

    def test_restart_is_allowed(self):
        self.assertIsNone(self.refuse("restart", "shop", "1"))


class PolicyFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "airlok", "policy.env")

    def test_no_file_is_normal_mode(self):
        self.assertEqual(strict.read_policy(self.path), (False, []))

    def test_round_trip(self):
        strict.write_policy(True, ["b/b", "a/a", "a/a"], self.path)
        self.assertEqual(strict.read_policy(self.path), (True, ["a/a", "b/b"]))
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o644)
        strict.write_policy(False, ["a/a"], self.path)
        self.assertEqual(strict.read_policy(self.path), (False, ["a/a"]))

    def test_only_exact_mode_counts(self):
        os.makedirs(os.path.dirname(self.path))
        for line, want in (("AIRLOK_MODE=strict\n", True), ("AIRLOK_MODE=normal\n", False), ("# AIRLOK_MODE=strict\n", False)):
            with open(self.path, "w") as f:
                f.write(line)
            self.assertEqual(strict.read_policy(self.path)[0], want, line)


class InBranchTest(unittest.TestCase):
    """GitHub по хешу отдаёт и коммиты из форков: коммит вне веток публиковать нельзя."""

    def git(self, *args, env=None):
        base = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        return subprocess.run(["git", "--git-dir", self.repo, *args], env={**base, **(env or {})}, capture_output=True, text=True, check=True).stdout.strip()

    def in_branch(self, sha):
        with open(os.path.join(RECIPES, "github-deploy.sh"), encoding="utf-8") as f:
            func = re.search(r"^in_branch\(\) \{.*?^\}", f.read(), re.S | re.M).group(0)
        return subprocess.run(["bash", "-c", f'{func}\nGIT_DIR_PATH=$1; in_branch "$2"', "bash", self.repo, sha]).returncode == 0

    def test_commit_outside_branches(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.repo = tmp
            subprocess.run(["git", "init", "-q", "--bare", tmp], check=True)
            tree = self.git("hash-object", "-t", "tree", "-w", "/dev/null")
            base = self.git("commit-tree", tree, "-m", "в ветке")
            self.git("update-ref", "refs/heads/main", base)
            tip = self.git("commit-tree", tree, "-p", base, "-m", "конец ветки")
            self.git("update-ref", "refs/heads/main", tip)
            fork = self.git("commit-tree", tree, "-p", base, "-m", "из форка")
            self.assertTrue(self.in_branch(tip))
            self.assertTrue(self.in_branch(base), "старый коммит ветки разрешён")
            self.assertFalse(self.in_branch(fork))


def server_shell():
    return subprocess.run(["bash", "-c", "((BASH_VERSINFO[0] >= 4))"], capture_output=True).returncode == 0


@unittest.skipUnless(server_shell(), "нужен bash 4+, как на сервере")
class HandleTest(unittest.TestCase):
    """handle из agent.sh: что запретил файл на сервере, не запускается вообще."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.policy = os.path.join(self.tmp.name, "policy.env")
        self.audit = os.path.join(self.tmp.name, "audit.log")
        with open(os.path.join(RECIPES, "strict.py"), encoding="utf-8") as f:
            source = f.read().replace('POLICY = "/etc/airlok/policy.env"', f'POLICY = {self.policy!r}')
        with open(os.path.join(self.tmp.name, "strict.py"), "w", encoding="utf-8") as f:
            f.write(source)
        with open(os.path.join(RECIPES, "agent.sh"), encoding="utf-8") as f:
            agent = f.read()
        parse = re.search(r"^PARSE_PY='\n.*?\n'\n", agent, re.S | re.M).group(0)
        funcs = [re.search(rf"^{name}\(\) \{{.*?^\}}", agent, re.S | re.M).group(0) for name in ("audit", "refuse", "handle")]
        self.script = "\n".join([
            'source "$1"; set +e',
            f'POLICY_CONF={self.policy!r} RECIPES_DIR={self.tmp.name!r} RUN_DIR={self.tmp.name!r} AUDIT_LOG={self.audit!r} AUDIT_MAX_BYTES=100000',
            f'ENV_JSON={self.tmp.name!r}/env.json DBQ_JSON={self.tmp.name!r}/q.json KEYS_JSON={self.tmp.name!r}/k.json',
            'send_result() { echo "result $1 $2 $3"; }',
            'dashboard_fetch() { { echo "db-result $2"; cat "${4#@}"; echo; } >&2; }',
            *[f'run_{n}() {{ echo "RUN {n} $*"; }}' for n in ("update", "restart", "remove", "db_query", "ssh_keys", "ssh_password", "data_move", "os_reboot", "os_upgrade", "deploy")],
            parse, *funcs,
            'handle "$2"',
        ])

    def enable(self, *repos):
        strict.write_policy(True, list(repos), self.policy)

    def handle(self, cmd):
        path = os.path.join(self.tmp.name, "cmd.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"id": CID, **cmd}, f)
        return subprocess.run(["bash", "-c", self.script, "bash", os.path.join(RECIPES, "lib.sh"), path], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True).stdout

    def deploy(self, repo="owner/shop", **extra):
        return {"kind": "deploy", "deploy": {"app": "shop", "repo": repo, "sha": "a" * 40, "github_token": "ghs_secret", **extra}}

    DANGEROUS = [
        {"kind": "update"},
        {"kind": "ssh_keys", "ssh_keys": {"user": "root", "keys": [{"id": "x", "key": "ssh-ed25519 AAAA attacker"}]}},
        {"kind": "ssh_keys_remove", "ssh_keys": {"user": "root", "keys": []}},
        {"kind": "ssh_password", "ssh_password": {"login": "open"}},
        {"kind": "os_upgrade", "os_upgrade": {"action": "reboot"}},
        {"kind": "data_move", "data_move": {"op": "export", "app": "shop", "db_url": "https://evil.example/x"}},
        {"kind": "remove", "remove": {"app": "shop", "with_data": True}},
    ]

    def test_normal_mode_runs_everything(self):
        for cmd in self.DANGEROUS:
            self.assertIn("RUN ", self.handle(cmd), cmd)
        self.assertIn("RUN deploy", self.handle(self.deploy("attacker/repo")))

    def test_strict_mode_runs_nothing_dangerous(self):
        self.enable("owner/shop")
        for cmd in self.DANGEROUS:
            out = self.handle(cmd)
            self.assertNotIn("RUN ", out, cmd)
            self.assertIn(f"result {CID} failed", out, cmd)

    def test_db_query_gets_an_answer_on_its_own_route(self):
        self.enable()
        out = self.handle({"kind": "db_query", "db_query": {"app": "shop", "statements": []}})
        self.assertNotIn("RUN ", out)
        self.assertIn(f"db-result /agent/v1/db-queries/{CID}/result", out)
        self.assertIn("строгий режим", json.loads(out.splitlines()[-1])["error"]["message"])
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "q.json")), "текст запроса не остаётся на диске")

    def test_strict_deploy(self):
        self.enable("owner/shop")
        self.assertIn(f"RUN deploy {CID} shop owner/shop", self.handle(self.deploy()))
        out = self.handle(self.deploy("attacker/repo"))
        self.assertNotIn("RUN ", out)
        self.assertIn("нет в списке", out)
        self.assertNotIn("RUN ", self.handle(self.deploy("https://evil.example/owner/shop.git")))

    def test_strict_ignores_env_from_dashboard(self):
        env = [{"name": "DATABASE_URL", "value": "v1:QUJD"}]
        strict.write_policy(False, [], self.policy)
        self.assertRegex(self.handle(self.deploy(env=env)), r"RUN deploy .* 1\s*$")
        self.enable("owner/shop")
        out = self.handle(self.deploy(env=env))
        self.assertRegex(out, r"RUN deploy .* 0\s*$", "значения могли бы увести данные приложения")
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "env.json")))
        out = self.handle({"kind": "restart", "restart": {"app": "shop", "env": env}})
        self.assertRegex(out, rf"RUN restart {CID} shop 0\s*$")

    def test_audit_log(self):
        self.enable("owner/shop")
        self.handle(self.deploy())
        self.handle({"kind": "update"})
        self.handle({"kind": "data_move", "data_move": {"op": "export", "app": "shop", "db_url": "https://evil.example/secret-link"}})
        with open(self.audit, encoding="utf-8") as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn(f"accepted {CID} deploy app=shop repo=owner/shop sha={'a' * 40}", lines[0])
        self.assertIn(f"refused {CID} update", lines[1])
        self.assertIn("refused", lines[2])
        for line in lines:
            self.assertNotIn("ghs_secret", line, "токенов в журнале нет")
            self.assertNotIn("secret-link", line, "ссылок на хранилище в журнале нет")

    def test_audit_log_is_rotated(self):
        self.enable("owner/shop")
        with open(self.audit, "w") as f:
            f.write("x" * 100001)
        self.handle({"kind": "update"})
        self.assertTrue(os.path.exists(self.audit + ".1"))
        self.assertLess(os.path.getsize(self.audit), 1000)

    def test_broken_policy_check_refuses(self):
        self.enable("owner/shop")
        with open(os.path.join(self.tmp.name, "strict.py"), "w") as f:
            f.write("raise SystemExit(3)\n")
        out = self.handle(self.deploy())
        self.assertNotIn("RUN ", out, "сбой проверки — не повод выполнить команду")
        self.assertIn(f"result {CID} failed", out)


if __name__ == "__main__":
    unittest.main()
