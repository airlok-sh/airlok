"""Ключи устройств из дашборда в authorized_keys: python3 -m unittest discover tests

Агент меняет только свои строки: чужой ключ, пропавший из authorized_keys, может оставить
человека без входа на сервер.
"""
import base64
import json
import os
import shutil
import stat
import subprocess
import struct
import sys
import tempfile
import time
import unittest
from unittest import mock

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


class LoginTest(unittest.TestCase):
    """Пароль из дашборда закрывается, только если на сервер вошли ключом устройства."""

    @unittest.skipUnless(shutil.which("ssh-keygen"), "нет ssh-keygen")
    def test_fingerprint_matches_ssh_keygen(self):
        line = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"
        with tempfile.NamedTemporaryFile("w", suffix=".pub") as f:
            f.write(line + "\n")
            f.flush()
            out = subprocess.run(["ssh-keygen", "-lf", f.name], capture_output=True, text=True, check=True).stdout
        self.assertEqual(sshkeys.fingerprint(line.split()[1]), out.split()[1])

    def test_device_login(self):
        text = f"{key(9)} hoster\n{key(1)} airlok-device:{ID1}\n"
        fps = sshkeys.device_fingerprints(text)
        self.assertEqual(fps, {sshkeys.fingerprint(blob("ssh-ed25519", 1))}, "ключ хостера — не устройство")
        fps = {("root", fp) for fp in fps}
        mine, other = sshkeys.fingerprint(blob("ssh-ed25519", 1)), sshkeys.fingerprint(blob("ssh-ed25519", 9))
        journal = (f"Accepted publickey for root from 45.112.192.159 port 44378 ssh2: ED25519 {other}\n"
                   f"Failed publickey for root from 1.2.3.4 port 1 ssh2: ED25519 {mine}\n"
                   f"Accepted publickey for deploy from 1.2.3.4 port 2 ssh2: ED25519 {mine}\n")
        self.assertIsNone(sshkeys.device_login(journal, fps), "чужой ключ, неудачная попытка и другой пользователь не в счёт")
        journal += f"Accepted publickey for root from 1.2.3.4 port 3 ssh2: ED25519 {mine}\n"
        self.assertEqual(sshkeys.device_login(journal, fps), mine)


class ReportTest(unittest.TestCase):
    """Опись ключей и входы для дашборда."""

    def test_inventory(self):
        text = (f"# {key(7)} старый\n{key(9)} hoster@panel\x07\n"
                f"from=\"10.0.0.1\" {key(8)} backup key\n{key(1)} airlok-device:{ID1}\n\n")
        self.assertEqual(sshkeys.inventory(text), [
            {"user": "root", "key": key(9), "comment": "hoster@panel", "device": None},
            {"user": "root", "key": key(8), "comment": "backup key", "device": None},
            {"user": "root", "key": key(1), "comment": "", "device": ID1},
        ], "комментарии в файле не ключи, опции не уходят, управляющие символы вырезаны")

    def test_parse_logins(self):
        fp = sshkeys.fingerprint(blob("ssh-ed25519", 1))
        e = lambda msg, ts, cur: json.dumps({"MESSAGE": msg, "__REALTIME_TIMESTAMP": ts, "__CURSOR": cur})  # noqa: E731
        lines = [
            e(f"Accepted publickey for root from 1.2.3.4 port 5022 ssh2: ED25519 {fp}", "1793390000123456", "c1"),
            e("Failed password for root from 5.6.7.8 port 1 ssh2", "1793390001000000", "c2"),
            e("Accepted password for deploy from 2001:db8::1 port 40000 ssh2", "1793390002000000", "c3"),
            e("Accepted keyboard-interactive/pam for root from 1.2.3.4 port 7 ssh2", "1793390003000000", "c4"),
            "мусор",
            json.dumps({"MESSAGE": [1, 2], "__CURSOR": "c5"}),
        ]
        logins, cursor = sshkeys.parse_logins(lines)
        self.assertEqual(cursor, "c5")
        self.assertEqual(logins, [
            {"at": "2026-10-30T19:53:20.123456Z", "user": "root", "method": "publickey", "ip": "1.2.3.4", "port": 5022, "fingerprint": fp},
            {"at": "2026-10-30T19:53:22.000000Z", "user": "deploy", "method": "password", "ip": "2001:db8::1", "port": 40000, "fingerprint": ""},
            {"at": "2026-10-30T19:53:23.000000Z", "user": "root", "method": "keyboard-interactive", "ip": "1.2.3.4", "port": 7, "fingerprint": ""},
        ], "неудачные попытки не в счёт")

    def test_report_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            keys = os.path.join(tmp, "authorized_keys")
            with open(keys, "w") as f:
                f.write(f"{key(9)} hoster\n")
            journal = {"cursor": None}

            def read_journal(cursor):
                journal["asked"] = cursor
                return ([{"at": "x"}] if cursor else []), "c-new"

            with mock.patch.object(sshkeys, "sshd_conf", return_value=None), \
                    mock.patch.object(sshkeys, "accounts", return_value=[sshkeys.ROOT]), \
                    mock.patch.object(sshkeys, "keys_path", return_value=keys), \
                    mock.patch.object(sshkeys, "read_journal", side_effect=read_journal):
                first = sshkeys.report(tmp)
                self.assertEqual(first, {"ssh_keys": sshkeys.inventory(f"{key(9)} hoster\n"), "ssh_users": ["root"]},
                                 "прошлые входы не досылаются")
                self.assertIsNone(journal["asked"])
                again = sshkeys.report(tmp)
                self.assertIn("ssh_keys", again, "отметка не дошла — опись и место в журнале не запомнены")
                self.assertIsNone(journal["asked"])
                sshkeys.report_sent(tmp)
                after = sshkeys.report(tmp)
                self.assertEqual(after, {"ssh_logins": [{"at": "x"}]}, "опись не менялась — не уходит")
                self.assertEqual(journal["asked"], "c-new")
                self.assertIn("ssh_keys", sshkeys.report(tmp, now=time.time() + 4000), "раз в час опись уходит всё равно")


class UserSyncTest(unittest.TestCase):
    """Ключи устройств — выбранному пользователю, у прежнего они убираются."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        me = (os.getuid(), os.getgid())
        self.root = sshkeys.Account("root", *me, os.path.join(self.tmp.name, "root"))
        self.ubuntu = sshkeys.Account("ubuntu", *me, os.path.join(self.tmp.name, "home", "ubuntu"))
        for a in (self.root, self.ubuntu):
            os.makedirs(a.home)
        self.accs = [self.root, self.ubuntu]
        self.conf = {"passwordauthentication": "no", "permitrootlogin": "prohibit-password"}

    def tearDown(self):
        self.tmp.cleanup()

    def file(self, a):
        return os.path.join(a.home, ".ssh", "authorized_keys")

    def read(self, a):
        with open(self.file(a)) as f:
            return f.read()

    def write(self, a, text):
        os.makedirs(os.path.dirname(self.file(a)), exist_ok=True)
        with open(self.file(a), "w") as f:
            f.write(text)

    def test_moves_keys_to_chosen_user(self):
        self.write(self.root, f"{key(9)} hoster\n{key(1)} airlok-device:{ID1}\n")
        msg = sshkeys.sync({"user": "ubuntu", "keys": [{"id": ID1, "key": key(1)}]}, self.conf, self.accs)
        self.assertTrue(msg.startswith("Готово:"), msg)
        self.assertEqual(self.read(self.ubuntu), f"{key(1)} airlok-device:{ID1}\n")
        self.assertEqual(self.read(self.root), f"{key(9)} hoster\n", "у root строка устройства убрана, ключ хостера на месте")

    def test_keeps_last_key_of_previous_user(self):
        self.write(self.root, f"{key(1)} airlok-device:{ID1}\n")
        msg = sshkeys.sync({"user": "ubuntu", "keys": [{"id": ID1, "key": key(1)}]}, self.conf, self.accs)
        self.assertIn("у root прежние ключи устройств оставлены", msg)
        self.assertEqual(self.read(self.root), f"{key(1)} airlok-device:{ID1}\n", "root не остался без входа")
        self.assertEqual(self.read(self.ubuntu), f"{key(1)} airlok-device:{ID1}\n")

    def test_unknown_user(self):
        with self.assertRaises(sshkeys.Failed):
            sshkeys.sync({"user": "nobody", "keys": []}, self.conf, self.accs)
        with self.assertRaises(sshkeys.Failed):
            sshkeys.sync({"user": ["root"], "keys": []}, self.conf, self.accs)

    def test_refuses_ssh_dir_link_outside_home(self):
        elsewhere = os.path.join(self.tmp.name, "elsewhere")
        os.makedirs(elsewhere)
        os.symlink(elsewhere, os.path.join(self.ubuntu.home, ".ssh"))
        with self.assertRaises(sshkeys.Failed):
            sshkeys.sync({"user": "ubuntu", "keys": [{"id": ID1, "key": key(1)}]}, self.conf, self.accs)
        self.assertEqual(os.listdir(elsewhere), [], "через ссылку агент ничего не пишет")

    def test_inventory_and_login_by_user(self):
        self.write(self.ubuntu, f"{key(1)} airlok-device:{ID1}\n")
        fp = sshkeys.fingerprint(blob("ssh-ed25519", 1))
        allowed = {("ubuntu", f) for f in sshkeys.device_fingerprints(self.read(self.ubuntu))}
        self.assertIsNone(sshkeys.device_login(f"Accepted publickey for root from 1.2.3.4 port 1 ssh2: ED25519 {fp}", allowed),
                          "ключ устройства у ubuntu, а вошли root'ом")
        self.assertEqual(sshkeys.device_login(f"Accepted publickey for ubuntu from 1.2.3.4 port 1 ssh2: ED25519 {fp}", allowed), fp)
        self.assertEqual(sshkeys.inventory(self.read(self.ubuntu), "ubuntu")[0]["user"], "ubuntu")


class ConfTest(unittest.TestCase):
    def test_keys_path(self):
        self.assertEqual(sshkeys.keys_path(None), "/root/.ssh/authorized_keys")
        self.assertEqual(sshkeys.keys_path({"authorizedkeysfile": ".ssh/authorized_keys .ssh/authorized_keys2"}), "/root/.ssh/authorized_keys")
        self.assertEqual(sshkeys.keys_path({"authorizedkeysfile": "/etc/ssh/keys/%u"}), "/etc/ssh/keys/root")
        self.assertEqual(sshkeys.keys_path({"authorizedkeysfile": "%h/.ssh/k%%"}), "/root/.ssh/k%")
        for bad in ("none", "/etc/ssh/keys/%U"):
            with self.assertRaises(sshkeys.Failed, msg=bad):
                sshkeys.keys_path({"authorizedkeysfile": bad})

    def test_password_login(self):
        self.assertTrue(sshkeys.password_login({"passwordauthentication": "yes", "permitrootlogin": "yes"}))
        self.assertTrue(sshkeys.password_login({"kbdinteractiveauthentication": "yes", "permitrootlogin": "yes"}))
        self.assertFalse(sshkeys.password_login({"passwordauthentication": "yes", "permitrootlogin": "prohibit-password"}))
        self.assertTrue(sshkeys.password_login({"passwordauthentication": "yes", "permitrootlogin": "no"}, "ubuntu"),
                        "PermitRootLogin касается только root")
        self.assertFalse(sshkeys.password_login({"passwordauthentication": "no", "permitrootlogin": "yes"}))
        self.assertFalse(sshkeys.password_login(None), "sshd не ответил — считаем, что пароля нет")

    def test_keys_path_of_user(self):
        ubuntu = sshkeys.Account("ubuntu", 1000, 1000, "/home/ubuntu")
        self.assertEqual(sshkeys.keys_path(None, ubuntu), "/home/ubuntu/.ssh/authorized_keys")
        self.assertEqual(sshkeys.keys_path({"authorizedkeysfile": "/etc/ssh/keys/%u"}, ubuntu), "/etc/ssh/keys/ubuntu")


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
