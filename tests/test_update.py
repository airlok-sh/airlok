"""update.sh: ставится только коммит из main, подписанный ключом Airlok.

python3 -m unittest discover tests
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

RECIPES = os.path.join(os.path.dirname(__file__), "..", "recipes")
LIB = os.path.join(RECIPES, "lib.sh")
INSTALL = os.path.join(RECIPES, "..", "install.sh")
# Без глобальных настроек git человека: там бывает подпись всех коммитов своим ключом.
GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
# install -D, которого нет в BSD install на macOS.
PORTABLE_WRITE = ('write_if_changed() { local tmp; tmp=$(mktemp); cat >"$tmp"; '
                  'if [[ -f $1 ]] && cmp -s "$tmp" "$1"; then rm -f "$tmp"; return 1; fi; mv "$tmp" "$1"; }')
KEY_LINE = re.compile(r'^\S+ namespaces="git" (ssh-ed25519|sk-ssh-ed25519@openssh\.com) [A-Za-z0-9+/]+=*( .*)?$')


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, env=GIT_ENV, capture_output=True, text=True, check=True).stdout.strip()


@unittest.skipUnless(shutil.which("git") and shutil.which("ssh-keygen"), "нужны git и ssh-keygen")
class UpdateCodeTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.key = self.keygen("release")
        self.other = self.keygen("other")
        self.signers = os.path.join(self.tmp, "allowed_signers")
        self.origin = os.path.join(self.tmp, "origin")
        self.clone = os.path.join(self.tmp, "clone")
        git("init", "-q", "-b", "main", self.origin)
        self.base = self.commit("base", key=self.key)
        git("clone", "-q", self.origin, self.clone)

    def keygen(self, name):
        path = os.path.join(self.tmp, name)
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", name, "-f", path], check=True)
        return path

    def trust(self, *keys):
        with open(self.signers, "w", encoding="utf-8") as f:
            f.write("# ключи Airlok\n")
            for k in keys:
                with open(k + ".pub", encoding="utf-8") as pub:
                    f.write('release@airlok.ru namespaces="git" %s\n' % pub.read().strip())

    def commit(self, name, key=None, repo=None):
        repo = repo or self.origin
        with open(os.path.join(repo, name), "w", encoding="utf-8") as f:
            f.write(name)
        git("add", name, cwd=repo)
        sign = ["-c", "gpg.format=ssh", "-c", "user.signingkey=" + key] if key else []
        git(*sign, "commit", "-q", *(["-S"] if key else []), "-m", name, cwd=repo)
        return git("rev-parse", "HEAD", cwd=repo)

    def update(self, rev="FETCH_HEAD"):
        script = ('source "$1"; SIGNERS_CONF=$2; git -C "$3" fetch -q origin main; update_code "$3" "$4"; '
                  'git -C "$3" rev-parse HEAD')
        return subprocess.run(["bash", "-c", script, "bash", LIB, self.signers, self.clone, rev], env=GIT_ENV,
                              capture_output=True, text=True)

    def head(self):
        return git("rev-parse", "HEAD", cwd=self.clone)

    def test_signed_version_is_installed(self):
        self.trust(self.key)
        sha = self.commit("next", key=self.key)
        out = self.update()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.head(), sha)

    def test_unsigned_version_is_refused(self):
        self.trust(self.key)
        self.commit("evil")
        out = self.update()
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("не подписана ключом Airlok", out.stderr)
        self.assertEqual(self.head(), self.base, "прежняя версия остаётся")

    def test_version_signed_by_unknown_key_is_refused(self):
        self.trust(self.key)
        self.commit("evil", key=self.other)
        out = self.update()
        self.assertIn("не подписана ключом Airlok", out.stderr)
        self.assertEqual(self.head(), self.base)

    def test_any_listed_key_is_accepted(self):
        self.trust(self.key, self.other)
        sha = self.commit("next", key=self.other)
        self.assertEqual(self.update().returncode, 0)
        self.assertEqual(self.head(), sha)

    def test_without_keys_signature_is_not_checked(self):
        sha = self.commit("next")
        out = self.update()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.head(), sha, "сервер со старой версии агента обновляется как раньше")

    def test_named_signed_commit_below_unsigned_head(self):
        self.trust(self.key)
        sha = self.commit("next", key=self.key)
        self.commit("unsigned")
        out = self.update(sha)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.head(), sha)

    def test_installed_version_is_not_rechecked(self):
        self.trust(self.key)
        unsigned = self.commit("unsigned")
        git("pull", "-q", "--ff-only", cwd=self.clone)
        for rev in ("FETCH_HEAD", self.base):
            out = self.update(rev)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertEqual(self.head(), unsigned, "ставить нечего: ни ошибки, ни отката")

    def test_commit_outside_main_is_refused(self):
        self.trust(self.key)
        git("checkout", "-q", "-b", "side", cwd=self.clone)
        side = self.commit("side", key=self.key, repo=self.clone)
        git("checkout", "-q", "main", cwd=self.clone)
        out = self.update(side)
        self.assertIn("нет в ветке main", out.stderr)
        self.assertEqual(self.head(), self.base)


@unittest.skipUnless(shutil.which("git") and shutil.which("ssh-keygen"), "нужны git и ssh-keygen")
class UpdateScriptTest(unittest.TestCase):
    """Весь update.sh на локальном origin: порядок шагов, а не только функции lib.sh."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.key = UpdateCodeTest.keygen(self, "release")
        self.other = UpdateCodeTest.keygen(self, "other")
        self.conf = os.path.join(self.tmp, "etc", "allowed_signers")
        self.policy = os.path.join(self.tmp, "etc", "policy.env")
        os.makedirs(os.path.dirname(self.conf))
        self.origin = os.path.join(self.tmp, "origin")
        self.clone = os.path.join(self.tmp, "clone")
        git("init", "-q", "-b", "main", self.origin)
        os.makedirs(os.path.join(self.origin, "recipes"))
        with open(LIB, encoding="utf-8") as f:
            lib = f.read()
        # Пути /etc и проверка root подменены дописанными в конец определениями: lib.sh подключается
        # целиком до первого вызова.
        lib += "\n%s\nneed_root() { :; }\nSIGNERS_CONF=%s\nPOLICY_CONF=%s\nDASHBOARD_CONF=%s\n" % (
            PORTABLE_WRITE, self.conf, self.policy, os.path.join(self.tmp, "etc", "dashboard.env"))
        self.write("recipes/lib.sh", lib)
        shutil.copy(os.path.join(RECIPES, "update.sh"), os.path.join(self.origin, "recipes", "update.sh"))
        for stub in ("server-base.sh", "notify.sh"):
            self.write("recipes/" + stub, "#!/bin/sh\nexit 0\n", mode=0o755)

    def write(self, path, content, mode=0o644):
        full = os.path.join(self.origin, path)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
        os.chmod(full, mode)

    def signers(self, *keys):
        lines = ["# ключи Airlok\n"]
        for k in keys:
            with open(k + ".pub", encoding="utf-8") as pub:
                lines.append('release@airlok.ru namespaces="git" %s\n' % pub.read().strip())
        return "".join(lines)

    def commit(self, name, key=None, signers=None):
        self.write(name, name)
        if signers is not None:
            self.write("recipes/allowed_signers", signers)
        git("add", "-A", cwd=self.origin)
        sign = ["-c", "gpg.format=ssh", "-c", "user.signingkey=" + key] if key else []
        git(*sign, "commit", "-q", *(["-S"] if key else []), "-m", name, cwd=self.origin)
        return git("rev-parse", "HEAD", cwd=self.origin)

    def install(self):
        git("clone", "-q", self.origin, self.clone)

    def run_update(self, *args):
        return subprocess.run(["bash", os.path.join(self.clone, "recipes", "update.sh"), *args], env=GIT_ENV,
                              capture_output=True, text=True)

    def head(self):
        return git("rev-parse", "HEAD", cwd=self.clone)

    def conf_text(self):
        with open(self.conf, encoding="utf-8") as f:
            return f.read()

    def test_keys_of_installed_version_check_the_next_one(self):
        base = self.commit("base", signers=self.signers(self.key))
        self.install()
        evil = self.commit("evil")
        out = self.run_update("--to", evil)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("не подписана ключом Airlok", out.stderr)
        self.assertEqual(self.head(), base)
        self.assertEqual(self.conf_text(), self.signers(self.key), "ключи взяты из стоящей версии до получения новой")
        self.assertEqual(self.run_update().returncode, 0)
        self.assertEqual(self.head(), base, "без названной версии неподписанная не ставится")

    def test_unverified_version_cannot_bring_its_keys(self):
        base = self.commit("base", key=self.key, signers=self.signers(self.key))
        self.install()
        self.assertEqual(self.run_update().returncode, 0)
        evil = self.commit("evil", key=self.other, signers=self.signers(self.other))
        out = self.run_update("--to", evil)
        self.assertIn("не подписана ключом Airlok", out.stderr)
        self.assertEqual(self.head(), base)
        self.assertEqual(self.conf_text(), self.signers(self.key))

    def test_key_rotation_by_signed_version(self):
        self.commit("base", key=self.key, signers=self.signers(self.key))
        self.install()
        rotated = self.commit("rotate", key=self.key, signers=self.signers(self.other))
        out = self.run_update()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.head(), rotated)
        self.assertEqual(self.conf_text(), self.signers(self.other))
        nxt = self.commit("next", key=self.other)
        out = self.run_update()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.head(), nxt)

    def test_without_keys_updates_as_before(self):
        self.commit("base")
        self.install()
        sha = self.commit("next")
        out = self.run_update()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.head(), sha)
        self.assertFalse(os.path.exists(self.conf))

    def test_bad_arguments_are_refused(self):
        base = self.commit("base")
        self.install()
        self.commit("next")
        for args in (["--to", "zz"], ["--to"], ["--force"]):
            out = self.run_update(*args)
            self.assertIn("после --to нужен хеш", out.stderr, args)
            self.assertEqual(self.head(), base, args)

    def test_unsigned_head_of_main_is_not_released_yet(self):
        self.commit("base", key=self.key, signers=self.signers(self.key))
        self.install()
        signed = self.commit("signed", key=self.key)
        self.commit("unsigned")
        out = self.run_update()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.head(), signed, "последняя подписанная версия")
        self.assertIn("без подписи Airlok", out.stdout)
        out = self.run_update()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.head(), signed, "без новой подписанной версии обновлять нечего, это не ошибка")

    def test_strict_mode_shows_signature_and_waits(self):
        base = self.commit("base", key=self.key, signers=self.signers(self.key))
        self.install()
        with open(self.policy, "w", encoding="utf-8") as f:
            f.write("AIRLOK_MODE=strict\n")
        unsigned = self.commit("unsigned")
        out = self.run_update()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("НЕ подписана ключом Airlok", out.stdout)
        self.assertEqual(self.head(), base)
        out = self.run_update("--to", unsigned[:12])
        self.assertIn("не подписана ключом Airlok", out.stderr)
        self.assertEqual(self.head(), base)


@unittest.skipUnless(shutil.which("git") and shutil.which("ssh-keygen"), "нужны git и ssh-keygen")
class InstallScriptTest(unittest.TestCase):
    """install.sh с airlok.ru: первая установка и повторный запуск ставят только подписанную версию."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.key = UpdateCodeTest.keygen(self, "release")
        self.other = UpdateCodeTest.keygen(self, "other")
        self.conf = os.path.join(self.tmp, "etc", "allowed_signers")
        self.dir = os.path.join(self.tmp, "opt", "airlok")
        os.makedirs(os.path.dirname(self.dir))
        self.origin = os.path.join(self.tmp, "origin")
        git("init", "-q", "-b", "main", self.origin)
        os.makedirs(os.path.join(self.origin, "recipes"))
        path = os.path.join(self.origin, "recipes", "server-base.sh")
        with open(path, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\nexit 0\n")
        os.chmod(path, 0o755)

    def script(self, *keys):
        with open(INSTALL, encoding="utf-8") as f:
            s = f.read()
        embedded = UpdateScriptTest.signers(self, *keys).replace("# ключи Airlok\n", "")
        # Настоящие ключи релизов уступают место тестовым.
        s, n = re.subn(r"(release_signers\(\) \{\n\tcat <<'EOF'\n).*?EOF\n", lambda m: m.group(1) + "EOF\n", s, flags=re.S)
        self.assertEqual(n, 1, "release_signers")
        for old, new in (
            ('[[ $EUID -eq 0 ]] || die "запустите через sudo"\n', ""),
            ("[[ -r /etc/os-release ]] &&", "true ||"),
            ("DIR=/opt/airlok\n", "DIR=%s\n" % self.dir),
            ("SIGNERS_CONF=/etc/airlok/allowed_signers\n", "SIGNERS_CONF=%s\n" % self.conf),
            ("\tcat <<'EOF'\nEOF\n", "\tcat <<'EOF'\n%sEOF\n" % embedded),
        ):
            self.assertEqual(s.count(old), 1, old)
            s = s.replace(old, new)
        path = os.path.join(self.tmp, "install.sh")
        with open(path, "w", encoding="utf-8") as f:
            f.write(s)
        return path

    def commit(self, name, key=None):
        with open(os.path.join(self.origin, name), "w", encoding="utf-8") as f:
            f.write(name)
        git("add", "-A", cwd=self.origin)
        sign = ["-c", "gpg.format=ssh", "-c", "user.signingkey=" + key] if key else []
        git(*sign, "commit", "-q", *(["-S"] if key else []), "-m", name, cwd=self.origin)
        return git("rev-parse", "HEAD", cwd=self.origin)

    def run_install(self, *keys):
        return subprocess.run(["bash", self.script(*keys)], env={**GIT_ENV, "AIRLOK_REPO": self.origin},
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)

    def head(self):
        return git("rev-parse", "HEAD", cwd=self.dir)

    def test_fresh_install_takes_latest_signed_version(self):
        self.commit("base", key=self.key)
        signed = self.commit("signed", key=self.key)
        self.commit("unsigned")
        out = self.run_install(self.key)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.head(), signed)
        self.assertFalse(os.path.exists(os.path.join(self.dir, "unsigned")), "неподписанный код не попал на диск")
        with open(self.conf, encoding="utf-8") as f:
            self.assertEqual(f.read(), UpdateScriptTest.signers(self, self.key).replace("# ключи Airlok\n", ""))

    def test_fresh_install_without_signed_version_is_refused(self):
        self.commit("base", key=self.other)
        out = self.run_install(self.key)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("нет версии, подписанной ключом Airlok", out.stderr)
        self.assertFalse(os.path.exists(self.dir), "непроверенный клон не остаётся: повторный запуск не примет его за стоящую версию")
        self.assertFalse(os.path.exists(self.dir + ".new"))

    def test_without_embedded_keys_installs_as_before(self):
        self.commit("base", key=self.key)
        tip = self.commit("unsigned")
        out = self.run_install()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.head(), tip)
        self.assertFalse(os.path.exists(self.conf))

    def test_reinstall_updates_only_to_signed_version(self):
        base = self.commit("base", key=self.key)
        self.assertEqual(self.run_install(self.key).returncode, 0)
        self.commit("evil")
        out = self.run_install(self.key)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.head(), base, "повторная установка не обходит проверку подписи")
        signed = self.commit("next", key=self.key)
        out = self.run_install(self.key)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.head(), signed)

    def test_reinstall_keeps_server_keys(self):
        self.commit("base", key=self.other)
        os.makedirs(os.path.dirname(self.conf))
        server = UpdateScriptTest.signers(self, self.other)
        with open(self.conf, "w", encoding="utf-8") as f:
            f.write(server)
        out = self.run_install(self.key)
        self.assertEqual(out.returncode, 0, out.stderr)
        with open(self.conf, encoding="utf-8") as f:
            self.assertEqual(f.read(), server, "ключи сервера могла сменить подписанная версия, установщик их не откатывает")


class SignersTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.conf = os.path.join(self.tmp, "conf")

    def install(self, content):
        src = os.path.join(self.tmp, "src")
        with open(src, "w", encoding="utf-8") as f:
            f.write(content)
        script = 'source "$1"; eval "$2"; SIGNERS_CONF=$3; install_signers "$4"; has_signers "$3" && echo yes || echo no'
        out = subprocess.run(["bash", "-c", script, "bash", LIB, PORTABLE_WRITE, self.conf, src],
                             capture_output=True, text=True, check=True)
        return out.stdout.strip()

    def test_file_without_keys_keeps_existing(self):
        self.assertEqual(self.install("# пусто\n\n"), "no")
        key = 'release@airlok.ru namespaces="git" ssh-ed25519 AAAAC3Nza old\n'
        self.assertEqual(self.install(key), "yes")
        self.assertEqual(self.install("# ключи убрали по ошибке\n"), "yes")
        with open(self.conf, encoding="utf-8") as f:
            self.assertEqual(f.read(), key)

    def test_new_keys_replace_old(self):
        self.install('release@airlok.ru namespaces="git" ssh-ed25519 AAAAold\n')
        new = 'release@airlok.ru namespaces="git" ssh-ed25519 AAAAnew\n'
        self.install(new)
        with open(self.conf, encoding="utf-8") as f:
            self.assertEqual(f.read(), new)

    def test_install_script_embeds_repo_keys(self):
        with open(os.path.join(RECIPES, "allowed_signers"), encoding="utf-8") as f:
            repo = [l.strip() for l in f if l.strip() and not l.lstrip().startswith("#")]
        with open(INSTALL, encoding="utf-8") as f:
            m = re.search(r"release_signers\(\) \{\n\tcat <<'EOF'\n(.*?)EOF\n", f.read(), re.S)
        self.assertIsNotNone(m)
        embedded = [l.strip() for l in m.group(1).splitlines() if l.strip()]
        self.assertEqual(embedded, repo, "ключи в install.sh — те же, что в recipes/allowed_signers")

    def test_repo_keys_are_well_formed(self):
        with open(os.path.join(RECIPES, "allowed_signers"), encoding="utf-8") as f:
            lines = [l.strip() for l in f if l.strip() and not l.lstrip().startswith("#")]
        for line in lines:
            self.assertRegex(line, KEY_LINE, "ключ только для подписей git, только ed25519")


if __name__ == "__main__":
    unittest.main()
