"""Ключи устройств из дашборда в authorized_keys: python3 -m unittest discover tests

Агент меняет только свои строки: чужой ключ, пропавший из authorized_keys, может оставить
человека без входа на сервер.
"""
import base64
import os
import stat
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "recipes"))

import sshkeys  # noqa: E402

ID1 = "0192e000-0000-7000-8000-000000000001"
ID2 = "0192e000-0000-7000-8000-000000000002"


def blob(kind, seed):
    raw = struct.pack(">I", len(kind)) + kind.encode() + struct.pack(">I", 32) + bytes([seed]) * 32
    return base64.b64encode(raw).decode()


def key(seed, kind="ssh-ed25519"):
    return f"{kind} {blob(kind, seed)}"


class RequestTest(unittest.TestCase):
    def test_valid(self):
        got = sshkeys.parse_request({"keys": [{"id": ID1, "key": key(1)}, {"id": ID2, "key": key(2, "ecdsa-sha2-nistp256")}]})
        self.assertEqual(got, [(ID1, "ssh-ed25519", blob("ssh-ed25519", 1)), (ID2, "ecdsa-sha2-nistp256", blob("ecdsa-sha2-nistp256", 2))])
        self.assertEqual(sshkeys.parse_request({"keys": []}), [])

    def test_rejects(self):
        ed = blob("ssh-ed25519", 1)
        for bad in ("x", {"keys": "x"}, {"keys": [{"id": "x", "key": key(1)}]}, {"keys": [{"id": ID1.upper(), "key": key(1)}]},
                    {"keys": [{"id": ID1, "key": key(1)}, {"id": ID1, "key": key(2)}]},
                    {"keys": [{"id": ID1, "key": f"ssh-rsa {ed}"}]},
                    {"keys": [{"id": ID1, "key": f"from=\"*\" ssh-ed25519 {ed}"}]},
                    {"keys": [{"id": ID1, "key": f"ssh-ed25519 {ed} me@laptop"}]},
                    {"keys": [{"id": ID1, "key": f"ssh-ed25519 {ed}\nssh-ed25519 {ed}"}]},
                    {"keys": [{"id": ID1, "key": f"ssh-dss {blob('ssh-dss', 1)}"}]},
                    {"keys": [{"id": ID1, "key": "ssh-ed25519 !!!"}]},
                    {"keys": [{"id": ID1, "key": 1}]},
                    {"keys": [{"id": ID1, "key": key(1)}] * 51}):
            with self.assertRaises(sshkeys.Failed, msg=bad):
                sshkeys.parse_request(bad)


class RenderTest(unittest.TestCase):
    def keys(self, *pairs):
        return sshkeys.parse_request({"keys": [{"id": i, "key": k} for i, k in pairs]})

    def test_keeps_foreign_lines(self):
        old = f"# провайдер\n{key(9)} hoster\n\nfrom=\"10.0.0.1\" {key(8)} backup\n"
        new = sshkeys.render(old, self.keys((ID1, key(1))))
        self.assertEqual(new, f"# провайдер\n{key(9)} hoster\n\nfrom=\"10.0.0.1\" {key(8)} backup\n{key(1)} airlok-device:{ID1}\n")
        self.assertEqual(sshkeys.render(new, self.keys((ID1, key(1)))), new, "повторный запуск ничего не меняет")

    def test_replaces_own_lines(self):
        old = f"{key(9)} hoster\n{key(1)} airlok-device:{ID1}\n{key(2)} airlok-device:{ID2}\n"
        self.assertEqual(sshkeys.render(old, self.keys((ID2, key(2)))), f"{key(9)} hoster\n{key(2)} airlok-device:{ID2}\n")
        self.assertEqual(sshkeys.render(old, []), f"{key(9)} hoster\n")

    def test_adopts_copy_of_device_key(self):
        old = f"{key(1)} airlok@laptop\ncommand=\"/bin/true\" {key(1)} old\n# {key(1)} в комментарии\n"
        self.assertEqual(sshkeys.render(old, self.keys((ID1, key(1)))), f"# {key(1)} в комментарии\n{key(1)} airlok-device:{ID1}\n",
                         "иначе отключение устройства не закрыло бы ему доступ")

    def test_has_keys(self):
        self.assertFalse(sshkeys.has_keys(f"\n# {key(1)}\n"))
        self.assertTrue(sshkeys.has_keys(f"no-pty {key(1)}\n"))
        self.assertTrue(sshkeys.has_keys(f"ssh-dss {blob('ssh-dss', 1)}\n"))


class ConfTest(unittest.TestCase):
    def test_keys_path(self):
        self.assertEqual(sshkeys.keys_path(None), "/root/.ssh/authorized_keys")
        self.assertEqual(sshkeys.keys_path({"authorizedkeysfile": ".ssh/authorized_keys .ssh/authorized_keys2"}), "/root/.ssh/authorized_keys")
        self.assertEqual(sshkeys.keys_path({"authorizedkeysfile": "/etc/ssh/keys/%u"}), "/etc/ssh/keys/root")
        self.assertEqual(sshkeys.keys_path({"authorizedkeysfile": "%h/.ssh/k%%"}), "/root/.ssh/k%")
        for bad in ("none", "/etc/ssh/keys/%U"):
            with self.assertRaises(sshkeys.Failed, msg=bad):
                sshkeys.keys_path({"authorizedkeysfile": bad})

    def test_root_password_login(self):
        self.assertTrue(sshkeys.root_password_login({"passwordauthentication": "yes", "permitrootlogin": "yes"}))
        self.assertTrue(sshkeys.root_password_login({"kbdinteractiveauthentication": "yes", "permitrootlogin": "yes"}))
        self.assertFalse(sshkeys.root_password_login({"passwordauthentication": "yes", "permitrootlogin": "prohibit-password"}))
        self.assertFalse(sshkeys.root_password_login({"passwordauthentication": "no", "permitrootlogin": "yes"}))
        self.assertFalse(sshkeys.root_password_login(None), "sshd не ответил — считаем, что пароля нет")


class ApplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, ".ssh", "authorized_keys")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return f.read()

    def test_creates_file(self):
        keys = sshkeys.parse_request({"keys": [{"id": ID1, "key": key(1)}]})
        self.assertTrue(sshkeys.apply(self.path, keys, False))
        self.assertEqual(self.read(), f"{key(1)} airlok-device:{ID1}\n")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.path)).st_mode), 0o700)
        inode = os.stat(self.path).st_ino
        self.assertFalse(sshkeys.apply(self.path, keys, False))
        self.assertEqual(os.stat(self.path).st_ino, inode, "без изменений файл не переписывается")

    def test_nothing_to_do(self):
        self.assertFalse(sshkeys.apply(self.path, [], False))
        self.assertFalse(os.path.exists(self.path))

    def test_keeps_last_key(self):
        self.write(f"{key(1)} airlok-device:{ID1}\n")
        with self.assertRaises(sshkeys.Failed):
            sshkeys.apply(self.path, [], False)
        self.assertEqual(self.read(), f"{key(1)} airlok-device:{ID1}\n")
        self.assertTrue(sshkeys.apply(self.path, [], True), "с входом по паролю убрать последний ключ можно")
        self.assertEqual(self.read(), "")

    def test_removes_device_next_to_foreign_key(self):
        self.write(f"{key(9)} hoster\n{key(1)} airlok-device:{ID1}\n")
        self.assertTrue(sshkeys.apply(self.path, [], False))
        self.assertEqual(self.read(), f"{key(9)} hoster\n")

    def test_refuses_symlink(self):
        target = os.path.join(self.tmp.name, "elsewhere")
        open(target, "w").close()
        os.makedirs(os.path.dirname(self.path))
        os.symlink(target, self.path)
        with self.assertRaises(sshkeys.Failed):
            sshkeys.apply(self.path, sshkeys.parse_request({"keys": [{"id": ID1, "key": key(1)}]}), False)


if __name__ == "__main__":
    unittest.main()
