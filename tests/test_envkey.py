"""Переменные из дашборда, зашифрованные под ключ сервера: python3 -m unittest discover tests

Шифротексты здесь делает openssl с открытым ключом — так же, как браузер и CLI.
"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "recipes"))
import envkey  # noqa: E402

CHUNK = 318


def openssl(*args, data=b""):
    return subprocess.run(["openssl", *args], input=data, capture_output=True, check=True).stdout


def seal(pub, app, name, value):
    data = value.encode()
    parts = [data[i:i + CHUNK] for i in range(0, len(data), CHUNK)] or [b""]
    out = b""
    for i, part in enumerate(parts):
        label = f"airlok-env/v1/{app}/{name}/{i}/{len(parts)}".encode().hex()
        out += openssl("pkeyutl", "-encrypt", "-pubin", "-inkey", pub, "-pkeyopt", "rsa_padding_mode:oaep",
                       "-pkeyopt", "rsa_oaep_md:sha256", "-pkeyopt", "rsa_mgf1_md:sha256",
                       "-pkeyopt", f"rsa_oaep_label:{label}", data=part)
    return "v1:" + base64.b64encode(out).decode()


class EnvKeyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.key = os.path.join(cls.tmp.name, "env.key")
        cls.pub = os.path.join(cls.tmp.name, "env.pub")
        openssl("genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:3072", "-out", cls.key)
        openssl("pkey", "-in", cls.key, "-pubout", "-out", cls.pub)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_roundtrip(self):
        for value in ("", "sk-123", "ключ с пробелами", "x" * 1000 + "ё" * 300, "-----BEGIN KEY-----\nabc\n-----END KEY-----\n"):
            self.assertEqual(envkey.decrypt(self.key, "shop", "API_KEY", seal(self.pub, "shop", "API_KEY", value)), value)

    def test_bound_to_app_and_name(self):
        sealed = seal(self.pub, "shop", "API_KEY", "секрет")
        for app, name in (("blog", "API_KEY"), ("shop", "DEBUG")):
            with self.assertRaises(envkey.EnvError, msg=f"{app}/{name}"):
                envkey.decrypt(self.key, app, name, sealed)

    def test_blocks_cannot_be_reordered_or_cut(self):
        raw = base64.b64decode(seal(self.pub, "shop", "BIG", "a" * CHUNK + "b" * CHUNK)[3:])
        a, b = raw[:384], raw[384:]
        for bad in (b + a, a, a + a):
            with self.assertRaises(envkey.EnvError):
                envkey.decrypt(self.key, "shop", "BIG", "v1:" + base64.b64encode(bad).decode())

    def test_malformed(self):
        for bad in (None, "plain", "v1:", "v1:!!!", "v1:" + base64.b64encode(b"x" * 100).decode()):
            with self.assertRaises(envkey.EnvError, msg=repr(bad)):
                envkey.decrypt(self.key, "shop", "API_KEY", bad)

    def test_panel(self):
        data = [{"name": "API_KEY", "value": seal(self.pub, "shop", "API_KEY", "sk-1")},
                {"name": "GREETING", "value": seal(self.pub, "shop", "GREETING", "it's $HOME\n")}]
        self.assertEqual(envkey.panel(self.key, "shop", data), "API_KEY='sk-1'\nGREETING=\"it's $$HOME\\n\"\n")
        self.assertEqual(envkey.panel(self.key, "shop", []), "")
        for bad in ([{"name": "PORT", "value": seal(self.pub, "shop", "PORT", "1")}],
                    [{"name": "AIRLOK_X", "value": seal(self.pub, "shop", "AIRLOK_X", "1")}],
                    data + data[:1], "x", [{"value": "v1:"}]):
            with self.assertRaises(envkey.EnvError):
                envkey.panel(self.key, "shop", bad)

    def test_cli(self):
        data = json.dumps([{"name": "A", "value": seal(self.pub, "shop", "A", "1")}]).encode()
        script = os.path.join(os.path.dirname(envkey.__file__), "envkey.py")
        r = subprocess.run([sys.executable, script, "panel", self.key, "shop"], input=data, capture_output=True)
        self.assertEqual((r.returncode, r.stdout), (0, b"A='1'\n"))
        r = subprocess.run([sys.executable, script, "panel", self.key, "blog"], input=data, capture_output=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn("прежний ключ", r.stderr.decode())


class DotenvTest(unittest.TestCase):
    def test_reads_what_it_writes(self):
        for value in ("plain", "", "with $VAR # hash", "it's", 'back\\slash "q"\nnext $X\ttab\r', "$$"):
            line = envkey.dotenv_line("A", value)
            self.assertEqual(envkey.dotenv_value(line.split("=", 1)[1]), value, line)

    def test_reads_hand_written(self):
        cases = {"x # comment": "x", "  spaced  ": "spaced", "'a # b'": "a # b", '"a\\nb"': "a\nb", "a=b": "a=b"}
        for raw, value in cases.items():
            self.assertEqual(envkey.dotenv_value(raw), value, raw)

    def test_build_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            user, panel = os.path.join(tmp, "user.env"), os.path.join(tmp, "panel.env")
            with open(user, "w") as f:
                f.write("# мои\nexport NEXT_PUBLIC_API=http://old\nVITE_TITLE='Свечи'\nSECRET=s\nSENTRY_AUTH_TOKEN=t\nPORT=1\nbad line\n")
            with open(panel, "w") as f:
                f.write(envkey.dotenv_line("NEXT_PUBLIC_API", "https://api.example.ru") + "\n" +
                        envkey.dotenv_line("NEXT_PUBLIC_DEBUG", "1") + "\n")
            got = envkey.build_env("SENTRY_AUTH_TOKEN", "NEXT_PUBLIC_DEBUG", [user, panel, os.path.join(tmp, "нет")])
            self.assertEqual(got, {"NEXT_PUBLIC_API": "https://api.example.ru", "SENTRY_AUTH_TOKEN": "t", "VITE_TITLE": "Свечи"},
                             "panel.env важнее user.env; секреты без build: true в сборку не идут")
            script = os.path.join(os.path.dirname(envkey.__file__), "envkey.py")
            r = subprocess.run([sys.executable, script, "build-env", "", "", user], capture_output=True, check=True)
            self.assertEqual(r.stdout.split(b"\0")[:-1], ["NEXT_PUBLIC_API=http://old".encode(), "VITE_TITLE=Свечи".encode()])

    def test_quoting(self):
        cases = {
            "plain": "A='plain'",
            "with $VAR and # hash": "A='with $VAR and # hash'",
            "it's": "A=\"it's\"",
            'back\\slash "q"\nnext $X': 'A="back\\\\slash \\"q\\"\\nnext $$X"',
            "": "A=''",
        }
        for value, line in cases.items():
            self.assertEqual(envkey.dotenv_line("A", value), line, value)


if __name__ == "__main__":
    unittest.main()
