"""Установка пакетов в server-base.sh и install.sh, когда apt занят: python3 -m unittest discover tests"""
import os
import re
import stat
import subprocess
import tempfile
import unittest

RECIPES = os.path.join(os.path.dirname(__file__), "..", "recipes")
LIB = os.path.join(RECIPES, "lib.sh")
with open(os.path.join(RECIPES, "server-base.sh"), encoding="utf-8") as f:
    FUNC = re.search(r"^apt_install\(\) \{\n.*?\n\}\n", f.read(), re.M | re.S).group(0)
with open(os.path.join(RECIPES, "..", "install.sh"), encoding="utf-8") as f:
    INSTALL_GIT = re.search(r"^install_git\(\) \{\n.*?\n\}\n", f.read(), re.M | re.S).group(0)


class AptCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bin = os.path.join(tmp.name, "bin")
        os.makedirs(self.bin)
        self.log = os.path.join(tmp.name, "apt.log")
        self.calls = os.path.join(tmp.name, "calls")

    def fake(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write('#!/bin/bash\necho "%s $*" >>"%s"\n%s\n' % (name, self.calls, body))
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)

    def called(self):
        with open(self.calls, encoding="utf-8") as f:
            return f.read().splitlines()


class AptInstallTest(AptCase):
    def install(self, wait=600):
        script = 'source "$1"; eval "$2"; APT_LOG=$3 APT_LOCK_WAIT=$4; sleep() { :; }; apt_install certbot certbot; echo done'
        return subprocess.run(["bash", "-c", script, "bash", LIB, FUNC, self.log, str(wait)], capture_output=True, text=True,
                              env={**os.environ, "PATH": self.bin + os.pathsep + os.environ["PATH"]})

    def test_waits_for_lock_before_apt(self):
        self.fake("python3", "exit 0")
        self.fake("apt-get", "exit 0")
        out = self.install()
        self.assertEqual(out.stdout, "done\n", out.stderr)
        calls = self.called()
        self.assertTrue(calls[0].startswith("python3 ") and calls[0].endswith("wait-lock 600"), calls[0])
        self.assertEqual([c.split()[0] for c in calls[1:]], ["apt-get", "apt-get"])

    def test_timeout_is_explained_and_apt_does_not_run(self):
        self.fake("python3", "exit 3")
        self.fake("apt-get", "exit 0")
        out = self.install()
        self.assertEqual(out.returncode, 1)
        self.assertIn("занята другой установкой уже 10 мин", out.stderr)
        self.assertFalse([c for c in self.called() if c.startswith("apt-get")])

    def test_lock_taken_after_check_is_retried(self):
        self.fake("python3", "exit 0")
        self.fake("apt-get", 'grep -q "wait-lock" "%s" && [[ $(grep -c "^python3" "%s") -ge 2 ]] && exit 0\n'
                  "echo 'E: Could not get lock /var/lib/apt/lists/lock'; exit 100" % (self.calls, self.calls))
        out = self.install()
        self.assertEqual(out.stdout, "done\n", out.stderr)
        self.assertEqual(len([c for c in self.called() if c.startswith("python3")]), 2)

    def test_lock_still_taken_at_deadline_is_explained(self):
        self.fake("python3", "exit 0")
        self.fake("apt-get", "echo 'E: Unable to acquire the dpkg frontend lock'; exit 100")
        out = self.install(wait=0)
        self.assertEqual(out.returncode, 1)
        self.assertIn("занята другой установкой", out.stderr)

    def test_other_apt_failure_points_to_log(self):
        self.fake("python3", "exit 0")
        self.fake("apt-get", "echo 'E: Failed to fetch'; exit 100")
        out = self.install()
        self.assertEqual(out.returncode, 1)
        self.assertIn("не удалось установить certbot", out.stderr)
        self.assertIn(self.log, out.stderr)
        self.assertEqual(len([c for c in self.called() if c.startswith("python3")]), 1, "сбой не из-за блокировки не повторяем")

    def test_failed_check_does_not_stop_install(self):
        self.fake("python3", "exit 127")
        self.fake("apt-get", "exit 0")
        self.assertEqual(self.install().stdout, "done\n")


class InstallGitTest(AptCase):
    def install(self, func=INSTALL_GIT):
        script = 'die() { echo "$*" >&2; exit 1; }; sleep() { :; }; eval "$1"; install_git; echo done'
        return subprocess.run(["bash", "-c", script, "bash", func], capture_output=True, text=True,
                              env={**os.environ, "PATH": self.bin + os.pathsep + os.environ["PATH"]})

    def test_installs_when_apt_is_free(self):
        self.fake("apt-get", "exit 0")
        self.assertEqual(self.install().stdout, "done\n")
        self.assertEqual(len(self.called()), 2)

    def test_retries_while_apt_is_busy(self):
        self.fake("apt-get", '[[ $(grep -c update "%s") -ge 3 ]] && exit 0\n'
                  "echo 'E: Could not get lock /var/lib/apt/lists/lock'; exit 100" % self.calls)
        out = self.install()
        self.assertIn("ждём", out.stdout)
        self.assertEqual(out.stdout.count("ждём"), 1, "просим подождать один раз")
        self.assertTrue(out.stdout.endswith("done\n"), out.stderr)

    def test_gives_up_when_deadline_passes(self):
        self.fake("apt-get", "echo 'E: Unable to acquire the dpkg frontend lock'; exit 100")
        out = self.install(func=INSTALL_GIT.replace("SECONDS + 600", "SECONDS"))
        self.assertEqual(out.returncode, 1)
        self.assertIn("занята другой установкой уже 10 мин", out.stderr)

    def test_other_failure_is_not_retried(self):
        self.fake("apt-get", "echo 'E: Failed to fetch'; exit 100")
        out = self.install()
        self.assertEqual(out.returncode, 1)
        self.assertIn("не удалось поставить git", out.stderr)
        self.assertEqual(len(self.called()), 1)


if __name__ == "__main__":
    unittest.main()
