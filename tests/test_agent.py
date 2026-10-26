"""Разбор команд агента из дашборда: python3 -m unittest discover tests

Значения команды доходят до команд от root, поэтому агент проверяет их сам.
"""
import json
import os
import re
import subprocess
import sys
import unittest

AGENT = os.path.join(os.path.dirname(__file__), "..", "recipes", "agent.sh")
with open(AGENT, encoding="utf-8") as f:
    PARSE_PY = re.search(r"PARSE_PY='\n(.*?)\n'\n", f.read(), re.S).group(1)
CID = "0192e000-0000-7000-8000-000000000001"


def parse(cmd):
    out = subprocess.run([sys.executable, "-c", PARSE_PY], input=json.dumps(cmd).encode(), capture_output=True, check=True)
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
        self.assertEqual(got, ["ok", CID, "deploy", "shop", "acme/shop", sha, "main", "web", "Новая корзина", ""])

    def test_rejects(self):
        for cmd in ({"id": CID, "kind": "reboot"},
                    {"id": "not-a-uuid", "kind": "update"},
                    {"id": CID, "kind": "deploy", "deploy": {"app": "shop", "repo": "acme/shop", "sha": "a" * 40, "root_dir": "../etc"}}):
            self.assertEqual(parse(cmd)[0], "bad", cmd)


if __name__ == "__main__":
    unittest.main()
