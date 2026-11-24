"""Подсчёт обновлений системы: python3 -m unittest discover tests"""
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

PATH = os.path.join(os.path.dirname(__file__), "..", "recipes", "osupdates.py")
spec = importlib.util.spec_from_file_location("osupdates", PATH)
osupdates = importlib.util.module_from_spec(spec)
spec.loader.exec_module(osupdates)

SIMULATION = """Reading package lists...
Calculating upgrade...
The following upgrades have been deferred due to phasing:
  python3-update-manager
The following NEW packages will be installed:
  linux-image-7.0.0-42-generic
Inst base-files [14ubuntu6] (14ubuntu6.2 Ubuntu:26.04/resolute-updates [amd64])
Inst libsystemd0 [259.5-0ubuntu3.4] (259.9-0ubuntu0.1 Ubuntu:26.04/resolute-updates [amd64]) [systemd:amd64 ]
Inst openssl [3.5.0-1] (3.5.0-1ubuntu1 Ubuntu:26.04/resolute-updates, Ubuntu:26.04/resolute-security [amd64])
Inst libssl3t64 [3.5.0-1] (3.5.0-1ubuntu1 Ubuntu:26.04/resolute-security [amd64]) [openssl:amd64 ]
Inst linux-image-7.0.0-42-generic (7.0.0-42.42 Ubuntu:26.04/resolute-updates, Ubuntu:26.04/resolute-security [amd64])
Conf base-files (14ubuntu6.2 Ubuntu:26.04/resolute-updates [amd64])
Conf openssl (3.5.0-1ubuntu1 Ubuntu:26.04/resolute-security [amd64])
"""


class ParseTest(unittest.TestCase):
    def test_counts_only_what_will_be_installed(self):
        self.assertEqual(osupdates.parse_simulation(SIMULATION), {"total": 5, "security": 3})

    def test_security_is_decided_by_the_new_version_origin(self):
        line = "Inst security-tool [1] (2 Ubuntu:26.04/resolute-updates [amd64]) [libsecurity-security:amd64 ]"
        self.assertEqual(osupdates.parse_simulation(line), {"total": 1, "security": 0}, "зависимый пакет в хвосте строки — не источник")

    def test_nothing_to_install(self):
        self.assertEqual(osupdates.parse_simulation("Reading package lists...\n0 upgraded, 0 newly installed\n"), {"total": 0, "security": 0})


class CacheTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.source = os.path.join(self.dir, "status")
        with open(self.source, "w", encoding="utf-8"):
            pass
        os.utime(self.source, (1000, 1000))
        self.calls = 0

    def count(self):
        self.calls += 1
        return {"total": self.calls, "security": 0}

    def counts(self, now=None):
        return osupdates.counts(self.dir, count=self.count, sources=(self.source,), now=now)

    def test_counted_once_while_nothing_changes(self):
        self.assertEqual(self.counts()["total"], 1)
        self.assertEqual(self.counts()["total"], 1)
        self.assertEqual(self.calls, 1)

    def test_counted_again_when_packages_change(self):
        self.counts()
        future = time.time() + 60
        os.utime(self.source, (future, future))
        self.assertEqual(self.counts()["total"], 2)

    def test_counted_again_after_a_day(self):
        self.counts()
        self.assertEqual(self.counts(now=time.time() + osupdates.MAX_AGE + 60)["total"], 2)

    def test_failed_count_keeps_no_cache(self):
        def broken():
            raise subprocess.SubprocessError("apt")
        with self.assertRaises(subprocess.SubprocessError):
            osupdates.counts(self.dir, count=broken, sources=(self.source,))
        self.assertFalse(os.path.exists(os.path.join(self.dir, osupdates.CACHE)))

    def test_report_says_nothing_when_count_fails(self):
        with tempfile.TemporaryDirectory() as empty:
            cache = os.path.join(empty, "no-such-dir", "x")
            self.assertEqual(osupdates.report(cache), {})


class LockTest(unittest.TestCase):
    HOLD = "import fcntl, sys, time; f = open(sys.argv[1], 'r+'); fcntl.lockf(f, fcntl.LOCK_EX); print('ok', flush=True); time.sleep(float(sys.argv[2]))"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.lock = os.path.join(tmp.name, "lock-frontend")
        with open(self.lock, "w", encoding="utf-8"):
            pass

    def hold(self, seconds):
        p = subprocess.Popen([sys.executable, "-c", self.HOLD, self.lock, str(seconds)], stdout=subprocess.PIPE)
        self.addCleanup(p.wait)
        self.addCleanup(p.stdout.close)
        self.addCleanup(p.kill)
        p.stdout.readline()
        return p

    def test_free_lock_does_not_wait(self):
        self.assertTrue(osupdates.wait_lock(0, paths=(self.lock,), poll=0.01))

    def test_missing_lock_file_is_free(self):
        self.assertEqual(osupdates.busy((self.lock + ".none",)), [])

    def test_gives_up_when_lock_stays_busy(self):
        self.hold(30)
        self.assertEqual(osupdates.busy((self.lock,)), [self.lock])
        self.assertFalse(osupdates.wait_lock(0.05, paths=(self.lock,), poll=0.01))

    def test_waits_until_lock_is_released(self):
        self.hold(0.3)
        self.assertEqual(osupdates.busy((self.lock,)), [self.lock])
        self.assertTrue(osupdates.wait_lock(10, paths=(self.lock,), poll=0.05))
        self.assertEqual(osupdates.busy((self.lock,)), [])

    @unittest.skipUnless(os.path.exists("/proc/locks"), "нужен Linux")
    def test_names_real_holder(self):
        p = self.hold(30)
        self.assertIn(f"PID {p.pid}", osupdates.holder((self.lock,)))


class HolderTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.proc = os.path.join(tmp.name, "proc")
        os.mkdir(self.proc)
        self.lock = os.path.join(tmp.name, "lock-frontend")
        with open(self.lock, "w", encoding="utf-8"):
            pass
        st = os.stat(self.lock)
        self.key = "%02x:%02x:%d" % (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)

    def write(self, rel, text):
        path = os.path.join(self.proc, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)

    def process(self, pid, comm, cgroup):
        self.write(f"{pid}/comm", comm + "\n")
        self.write(f"{pid}/cgroup", cgroup)

    def test_lock_pid_skips_waiters_and_other_files(self):
        locks = ("1: POSIX  ADVISORY  WRITE 111 fd:01:999 0 EOF\n"
                 f"2: POSIX  ADVISORY  WRITE 222 {self.key} 0 EOF\n"
                 f"2: -> POSIX  ADVISORY  WRITE 333 {self.key} 0 EOF\n")
        st = os.stat(self.lock)
        self.assertEqual(osupdates.lock_pid(locks, st.st_dev, st.st_ino), 222)
        self.assertIsNone(osupdates.lock_pid("", st.st_dev, st.st_ino))

    def test_unattended_upgrades_by_unit(self):
        self.write("locks", f"1: POSIX  ADVISORY  WRITE 1234 {self.key} 0 EOF\n")
        self.process(1234, "unattended-upgr", "0::/system.slice/apt-daily-upgrade.service\n")
        self.assertEqual(osupdates.holder((self.lock,), self.proc), "ставятся автообновления Ubuntu (unattended-upgr, PID 1234)")

    def test_manual_apt_over_ssh(self):
        self.write("locks", f"1: POSIX  ADVISORY  WRITE 77 {self.key} 0 EOF\n")
        self.process(77, "apt-get", "0::/user.slice/user-0.slice/session-3.scope\n")
        self.assertEqual(osupdates.holder((self.lock,), self.proc), "кто-то ставит пакеты вручную по SSH (apt-get, PID 77)")

    def test_unknown_process_is_named_as_is(self):
        self.write("locks", f"1: POSIX  ADVISORY  WRITE 55 {self.key} 0 EOF\n")
        self.process(55, "mystery", "0::/system.slice/mystery.service\n")
        self.assertEqual(osupdates.holder((self.lock,), self.proc), "mystery, PID 55")

    def test_no_procfs_gives_none(self):
        self.assertIsNone(osupdates.holder((self.lock,), self.proc))

    def test_wait_lock_prints_each_new_holder(self):
        steps = [[self.lock], [self.lock], [self.lock], []]
        holders = iter(["Ubuntu проверяет обновления (apt.systemd.daily, PID 1)", "Ubuntu проверяет обновления (apt.systemd.daily, PID 1)",
                        "ставятся автообновления Ubuntu (unattended-upgr, PID 2)"])
        out = io.StringIO()
        with mock.patch.object(osupdates, "busy", lambda paths: steps.pop(0)), \
                mock.patch.object(osupdates, "holder", lambda paths, proc: next(holders)), redirect_stdout(out):
            self.assertTrue(osupdates.wait_lock(10, paths=(self.lock,), poll=0))
        self.assertEqual(out.getvalue().splitlines(), [
            "  система занята: Ubuntu проверяет обновления (apt.systemd.daily, PID 1), ждём…",
            "  система занята: ставятся автообновления Ubuntu (unattended-upgr, PID 2), ждём…",
        ])


if __name__ == "__main__":
    unittest.main()
