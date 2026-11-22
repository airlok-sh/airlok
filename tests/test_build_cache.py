"""app-deploy.sh: кэш сборки на приложение, приоритет сборки, сжатие и кэш статики в nginx."""

import os
import re
import subprocess
import tempfile
import time
import unittest

RECIPES = os.path.join(os.path.dirname(__file__), "..", "recipes")
LIB = os.path.join(RECIPES, "lib.sh")

with open(os.path.join(RECIPES, "app-deploy.sh"), encoding="utf-8") as f:
    SOURCE = f.read()


def func(name):
    return re.search(r"^%s\(\) \{\n.*?\n\}\n" % name, SOURCE, re.M | re.S).group(0)


def const(name):
    return re.search(r"^%s=.*$" % name, SOURCE, re.M).group(0)


# install, du и df сервера: install только создаёт каталог и пишет, кому его отдали; du берёт размер из
# файла size рядом с каталогом; df — свободное место из файла free за вычетом размеров кэшей, которые
# ещё есть на диске. rm без --one-file-system: его нет в BSD rm.
FAKES = {
    "install": """#!/bin/sh
owner=""
while [ $# -gt 1 ]; do
	case $1 in -o) owner=$2; shift 2 ;; -d) shift ;; -m|-g) shift 2 ;; *) shift ;; esac
done
mkdir -p "$1" && echo "$owner" > "$1.owner"
""",
    "du": """#!/bin/sh
for last; do :; done
printf '%s\\t%s\\n' "$(cat "$last.size" 2>/dev/null || echo 1)" "$last"
""",
    "df": """#!/bin/sh
free=$(cat "$FAKE/free")
for size in "$CACHE_ROOT"/*.size; do
	[ -d "${size%.size}" ] && free=$((free - $(cat "$size")))
done
printf 'Filesystem 1M-blocks Used Available Capacity Mounted\\n/dev/vda1 20000 1000 %s 5%% /\\n' "$free"
""",
    "rm": """#!/bin/sh
for a; do [ "$a" = --one-file-system ] || set -- "$@" "$a"; shift; done
exec /bin/rm "$@"
""",
}

CACHE_FUNCS = "".join(func(n) for n in ("disk_free", "cache_limit", "evict_caches", "deps_sum", "build_cache", "cache_spoiled", "modules_drop"))
CACHE_CONSTS = "\n".join(const(n) for n in ("BUILD_CACHE_MIN_FREE_MB", "DEPS_FILES"))


class BuildCacheTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.fake = os.path.join(self.tmp, "fake")
        self.root = os.path.join(self.tmp, "cache")
        self.dir = os.path.join(self.root, "shop")
        self.release = os.path.join(self.tmp, "release")
        bin_dir = os.path.join(self.tmp, "bin")
        for d in (self.fake, bin_dir, self.release):
            os.makedirs(d)
        for name, text in FAKES.items():
            path = os.path.join(bin_dir, name)
            with open(path, "w") as f:
                f.write(text)
            os.chmod(path, 0o755)
        self.set_free(10000)
        self.write(os.path.join(self.release, "package-lock.json"), "{}")
        self.env = {**os.environ, "PATH": bin_dir + ":" + os.environ["PATH"], "FAKE": self.fake, "CACHE_ROOT": self.root}

    def set_free(self, mb):
        self.write(os.path.join(self.fake, "free"), str(mb))

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def run_sh(self, call, app="shop"):
        script = ('source "$1"; ((BASH_VERSINFO[0] >= 4)) || set +u; eval "$2"; eval "$3"; BUILD_CACHE_ROOT=$4; RELEASE=$5; '
                  f'APP_NAME={app}; BUILD_USER=airlok-build; {call}')
        out = subprocess.run(["bash", "-c", script, "bash", LIB, CACHE_CONSTS, CACHE_FUNCS, self.root, self.release],
                             env=self.env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    def cache(self, app="shop"):
        return self.run_sh('build_cache; echo "[$BUILD_CACHE]"', app).strip().splitlines()[-1]

    def test_created_for_build_user(self):
        self.assertEqual(self.cache(), f"[{self.dir}]")
        with open(self.dir + ".owner") as f:
            self.assertEqual(f.read().strip(), "airlok-build", "в кэш пишет сборка от своего пользователя")
        with open(os.path.join(self.root + ".owner")) as f:
            self.assertEqual(f.read().strip(), "root", "общий каталог кэшей закрыт root")
        self.assertTrue(os.path.exists(self.dir + ".used"))
        self.assertTrue(os.path.exists(self.dir + ".deps"), "отпечаток зависимостей — вне каталога, куда пишет сборка")

    def test_kept_between_builds(self):
        self.cache()
        open(os.path.join(self.dir, "pip"), "w").close()
        self.assertEqual(self.cache(), f"[{self.dir}]")
        self.assertTrue(os.path.exists(os.path.join(self.dir, "pip")))

    def test_link_instead_of_dir_is_replaced(self):
        os.makedirs(self.root)
        target = os.path.join(self.tmp, "elsewhere")
        os.makedirs(target)
        open(os.path.join(target, "keep"), "w").close()
        os.symlink(target, self.dir)
        self.assertEqual(self.cache(), f"[{self.dir}]")
        self.assertFalse(os.path.islink(self.dir))
        self.assertTrue(os.path.exists(os.path.join(target, "keep")), "по ссылке ничего не стирается")

    def test_over_limit_is_wiped(self):
        self.cache()
        open(os.path.join(self.dir, "pip"), "w").close()
        self.write(self.dir + ".size", "2500")
        out = self.run_sh('build_cache; echo "[$BUILD_CACHE]"')
        self.assertIn("больше предела", out, "предел на диске 20 ГБ — 2000 МБ")
        self.assertFalse(os.path.exists(os.path.join(self.dir, "pip")))
        self.assertTrue(out.strip().endswith(f"[{self.dir}]"), "после чистки сборка снова с кэшем")
        os.remove(self.dir + ".size")
        self.write(self.dir + ".size", "1900")
        open(os.path.join(self.dir, "pip"), "w").close()
        self.cache()
        self.assertTrue(os.path.exists(os.path.join(self.dir, "pip")), "1,9 ГБ на диске 20 ГБ — в пределе")

    def test_dependencies_changed(self):
        self.cache()
        for path in ("npm/_cacache/index", "npm/_npx/1/node_modules/.bin/pnpm", "pip/wheel", "go-mod/x", "next/webpack", "xdg/yarn"):
            self.write(os.path.join(self.dir, path), "x")
        self.cache()
        self.assertTrue(os.path.exists(os.path.join(self.dir, "pip/wheel")), "те же зависимости — кэш целиком")
        self.write(os.path.join(self.release, "package-lock.json"), '{"x": 1}')
        out = self.run_sh('build_cache; echo "[$BUILD_CACHE]"')
        self.assertIn("зависимости изменились", out)
        self.assertTrue(os.path.exists(os.path.join(self.dir, "npm/_cacache/index")), "архивы npm сверяются с lock-файлом")
        for path in ("npm/_npx", "pip", "go-mod", "next", "xdg"):
            self.assertFalse(os.path.exists(os.path.join(self.dir, path)), path)
        self.assertNotIn("зависимости изменились", self.run_sh("build_cache"))

    def test_low_disk_evicts_oldest_first(self):
        for app, size in (("old", 800), ("mid", 800), ("new", 800), ("shop", 800)):
            self.cache(app)
            self.write(os.path.join(self.root, app + ".size"), str(size))
        for i, app in enumerate(("old", "mid", "new", "shop")):
            os.utime(os.path.join(self.root, app + ".used"), (1000 + i, 1000 + i))
        os.makedirs(os.path.join(self.root, "nostamp"))
        self.write(os.path.join(self.root, "nostamp.size"), "100")
        # Занято 3300 МБ кэшами, свободно 1198: хватит, если стереть nostamp и old.
        self.set_free(1198 + 3300)
        out = self.run_sh('build_cache; echo "[$BUILD_CACHE]"')
        self.assertFalse(os.path.exists(os.path.join(self.root, "nostamp")), "кэш без времени сборки — первым")
        self.assertFalse(os.path.exists(os.path.join(self.root, "old")))
        self.assertTrue(os.path.exists(os.path.join(self.root, "mid")), "стирается, пока места не хватит")
        self.assertTrue(os.path.exists(self.dir))
        self.assertTrue(out.strip().endswith(f"[{self.dir}]"))

    def test_low_disk_builds_without_cache(self):
        self.cache("other")
        self.cache()
        self.set_free(500)
        self.assertEqual(self.cache(), "[]")
        self.assertFalse(os.path.exists(self.dir), "свой кэш тоже отдаёт место")
        self.assertFalse(os.path.exists(os.path.join(self.root, "other")))

    def snapshot(self, app="shop"):
        os.makedirs(os.path.join(self.root, app + ".modules", "node_modules"), exist_ok=True)
        os.makedirs(os.path.join(self.root, app + ".modules.tmp"), exist_ok=True)
        self.write(os.path.join(self.root, app + ".modules.key"), "k\n")

    def has_snapshot(self, app="shop"):
        return any(os.path.exists(os.path.join(self.root, app + s)) for s in (".modules", ".modules.key", ".modules.tmp"))

    def test_snapshot_goes_with_cache(self):
        self.cache()
        self.snapshot()
        self.cache()
        self.assertTrue(self.has_snapshot(), "те же зависимости — снимок остаётся")
        self.write(os.path.join(self.release, "package-lock.json"), '{"x": 1}')
        self.cache()
        self.assertFalse(self.has_snapshot(), "зависимости изменились — снимок не нужен")
        self.snapshot()
        self.write(self.dir + ".size", "2500")
        self.cache()
        self.assertFalse(self.has_snapshot(), "кэш больше предела — стирается вместе со снимком")

    def test_low_disk_evicts_snapshots(self):
        for app in ("old", "shop"):
            self.cache(app)
            self.write(os.path.join(self.root, app + ".size"), "800")
            self.snapshot(app)
        os.utime(os.path.join(self.root, "old.used"), (1000, 1000))
        self.set_free(1500 + 1600)
        self.cache()
        self.assertFalse(self.has_snapshot("old"), "снимок стирается вместе с кэшем приложения")
        self.assertTrue(self.has_snapshot("shop"))

    def test_spoiled(self):
        log = os.path.join(self.tmp, "build.log")
        for text, spoiled in (("Type error: x is not assignable", False), ("npm error code EINTEGRITY", True),
                              ("ENOSPC: no space left on device, write", True), ("SyntaxError: Unexpected token", False)):
            self.write(log, text + "\n")
            out = self.run_sh(f'BUILD_CACHE=/c; cache_spoiled {log} && echo yes || echo no').strip()
            self.assertEqual(out, "yes" if spoiled else "no", text)
        self.write(log, "EINTEGRITY\n")
        self.assertEqual(self.run_sh(f'BUILD_CACHE=""; cache_spoiled {log} && echo yes || echo no').strip(), "no",
                         "без кэша повторять незачем")


def server_tools():
    """bash 4+ и GNU coreutils, как на сервере: снимок копирует cp --reflink и mv -T."""
    out = subprocess.run(["bash", "-c", "((BASH_VERSINFO[0] >= 4)) && cp --reflink=auto --version && mv -T --version && find -version"],
                         capture_output=True)
    return out.returncode == 0


MODULES_FUNCS = "".join(func(n) for n in ("cache_limit", "deps_sum", "modules_outputs", "plain_path", "modules_key", "modules_drop",
                                          "modules_copy", "modules_save", "modules_restore", "build_steps", "next_cache_script", "build"))
MODULES_CONSTS = "\n".join(const(n) for n in ("DEPS_FILES", "MODULES_REV", "MODULES_MAX_MINUTES"))
# Контейнер сборки — sh в папке релиза; запуски — в calls через \\0: скрипт сборки Next.js многострочный.
# docker отвечает только на image inspect, install соблюдает только -m.
MODULES_STUBS = """
build_container() { printf '%s\\0' "$1" >>"$FAKE/calls"; (cd "$RELEASE" && sh -c "$1") >>"$2" 2>&1; }
build_limits() { :; }
build_slice() { :; }
build_cache() { BUILD_CACHE=$BUILD_CACHE_ROOT/$APP_NAME; mkdir -p "$BUILD_CACHE"; }
cache_spoiled() { return 1; }
"""
MODULES_FAKES = {
    "docker": '#!/bin/sh\n[ "$1 $2" = "image inspect" ] && cat "$FAKE/id"\n',
    "install": '#!/bin/sh\nmode=""\nfor a; do [ "$prev" = -m ] && mode=$a; prev=$a; done\nmkdir -p "$a" && { [ -z "$mode" ] || chmod "$mode" "$a"; }\n',
    "du": '#!/bin/sh\nfor last; do :; done\nif [ -f "$last.size" ]; then printf "%s\\t%s\\n" "$(cat "$last.size")" "$last"; else exec /usr/bin/du "$@"; fi\n',
}


@unittest.skipUnless(server_tools(), "нужны bash 4+ и GNU coreutils, как на сервере")
class ModulesSnapshotTest(unittest.TestCase):
    """Снимок node_modules и .venv: вторая сборка с теми же зависимостями не ставит их заново."""

    INSTALL = ("mkdir -p node_modules/pkg node_modules/.bin && echo $$ > node_modules/pkg/index.js && "
               "ln -s ../pkg/index.js node_modules/.bin/pkg && echo installed")
    BUILD = "test -f node_modules/pkg/index.js && echo ok > dist.txt"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.fake = os.path.join(self.tmp, "fake")
        self.root = os.path.join(self.tmp, "cache")
        self.snap = os.path.join(self.root, "shop.modules")
        bin_dir = os.path.join(self.tmp, "bin")
        for d in (self.fake, bin_dir, self.root):
            os.makedirs(d)
        for name, text in MODULES_FAKES.items():
            with open(os.path.join(bin_dir, name), "w") as f:
                f.write(text)
            os.chmod(os.path.join(bin_dir, name), 0o755)
        self.write(os.path.join(self.fake, "id"), "sha256:one\n")
        self.files = {"package.json": '{"dependencies":{"pkg":"1"}}', "package-lock.json": "{}"}
        self.uid = 4242 if os.geteuid() == 0 else os.geteuid()
        self.env = {**os.environ, "PATH": bin_dir + ":" + os.environ["PATH"], "FAKE": self.fake}
        self.releases = 0

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)

    def calls(self):
        path = os.path.join(self.fake, "calls")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            calls = f.read().split("\0")[:-1]
        os.remove(path)
        return calls

    def publish(self, call="build_steps \"$RELEASE.log\"; echo \"restored=$MODULES_RESTORED\"", links=None, **variables):
        """Новый релиз из self.files и вызов функций рецепта в нём."""
        self.releases += 1
        self.release = os.path.join(self.tmp, f"r{self.releases}")
        for name, text in self.files.items():
            self.write(os.path.join(self.release, name), text)
        for name, target in (links or {}).items():
            os.symlink(target, os.path.join(self.release, name))
        values = dict(APP_NAME="shop", APP_STACK="node", BUILD_IMAGE="node:22-bookworm-slim", BUILD_INSTALL=self.INSTALL,
                      BUILD_CMD=self.BUILD, BUILD_PREFIX="", BUILD_COMPILE="", BUILD_ENV_SUM="env1", BUILD_UID=str(self.uid), BUILD_GID=str(self.uid),
                      APP_FRAMEWORK="Node.js", REUSED="0")
        values.update(variables)
        assigns = "".join(f"{k}={v!r}; " for k, v in values.items())
        script = (f'source "$1"; eval "$2"; eval "$3"; eval "$4"; BUILD_CACHE_ROOT=$5; RELEASE=$6; BUILD_CACHE=$5/shop; BUILD_ENV=(); '
                  f'{assigns}{call}')
        return subprocess.run(["bash", "-c", script, "bash", LIB, MODULES_CONSTS, MODULES_FUNCS, MODULES_STUBS, self.root, self.release],
                              env=self.env, capture_output=True, text=True)

    def ok(self, **kw):
        out = self.publish(**kw)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        return out.stdout

    def test_second_build_takes_snapshot(self):
        out = self.ok()
        self.assertIn("restored=0", out)
        self.assertEqual(self.calls(), [self.INSTALL, self.BUILD], "установка и сборка — разными запусками, снимок между ними")
        self.assertTrue(os.path.isfile(os.path.join(self.snap, "node_modules", "pkg", "index.js")))
        self.assertEqual(oct(os.stat(self.snap).st_mode & 0o777), "0o700")
        first = os.stat(os.path.join(self.release, "node_modules", "pkg", "index.js")).st_mtime_ns

        out = self.ok(BUILD_PREFIX="export X=1")
        self.assertIn("restored=1", out)
        self.assertIn("зависимости не менялись — взяты из прошлой сборки", out)
        self.assertEqual(self.calls(), ["export X=1 && " + self.BUILD], "без установки; окружение установки — сборке")
        index = os.path.join(self.release, "node_modules", "pkg", "index.js")
        self.assertEqual(os.stat(index).st_mtime_ns, first, "время изменения сохраняется")
        self.assertEqual(os.stat(index).st_uid, self.uid, "владелец копии — пользователь сборки")
        self.assertEqual(os.readlink(os.path.join(self.release, "node_modules", ".bin", "pkg")), "../pkg/index.js", "ссылки — ссылками")
        with open(os.path.join(self.release, "dist.txt")) as f:
            self.assertEqual(f.read().strip(), "ok")
        with open(self.release + ".log") as f:
            self.assertIn("взяты из прошлой сборки", f.read())

    def test_key(self):
        self.ok()
        self.calls()
        self.ok()
        self.assertEqual(len(self.calls()), 1)
        changes = [
            ("lock-файл", lambda: self.files.update({"package-lock.json": '{"v": 2}'}), {}),
            ("команда установки", lambda: None, {"BUILD_INSTALL": self.INSTALL + " --x"}),
            ("переменные сборки и версия образа", lambda: None, {"BUILD_ENV_SUM": "env2"}),
            ("выпуск образа", lambda: self.write(os.path.join(self.fake, "id"), "sha256:two\n"), {}),
        ]
        for what, change, variables in changes:
            change()
            out = self.ok(**variables)
            self.assertIn("restored=0", out, what)
            self.assertEqual(len(self.calls()), 2, what)
            self.ok(**variables)
            self.assertEqual(len(self.calls()), 1, f"{what}: следующая сборка снова берёт снимок")

    def test_without_build_command_no_container(self):
        self.ok(BUILD_CMD="")
        self.assertEqual(self.calls(), [self.INSTALL])
        self.assertIn("restored=1", self.ok(BUILD_CMD=""))
        self.assertEqual(self.calls(), [], "контейнер не нужен вовсе")
        self.assertTrue(os.path.isfile(os.path.join(self.release, "node_modules", "pkg", "index.js")))

    def test_not_reusable_installs_as_before(self):
        self.files["package.json"] = '{"scripts":{"postinstall":"prisma generate"}}'
        for _ in range(2):
            self.ok(BUILD_PREFIX="export X=1")
            self.assertEqual(self.calls(), [self.INSTALL + " && " + self.BUILD], "одним запуском, как раньше")
        self.assertFalse(os.path.exists(self.snap))

    def test_linked_dependency_file(self):
        self.files.pop("package-lock.json")
        self.write(os.path.join(self.tmp, "lock.json"), "{}")
        for _ in range(2):
            self.ok(links={"package-lock.json": os.path.join(self.tmp, "lock.json")})
            self.assertEqual(len(self.calls()), 1, "файл-ссылка не входит в отпечаток зависимостей")
        self.assertFalse(os.path.exists(self.snap))

    def test_no_image_no_snapshot(self):
        os.remove(os.path.join(self.fake, "id"))
        self.ok()
        self.assertEqual(len(self.calls()), 1)

    def test_snapshot_is_clean(self):
        install = (self.INSTALL + " && mkfifo node_modules/pipe && touch node_modules/suid && chmod 4755 node_modules/suid && "
                   "mkdir node_modules/sg && chmod 2755 node_modules/sg && ln -s /etc node_modules/etc")
        self.ok(BUILD_INSTALL=install)
        snap = os.path.join(self.snap, "node_modules")
        self.assertFalse(os.path.lexists(os.path.join(snap, "pipe")), "каналы не копируются")
        self.assertEqual(os.stat(os.path.join(snap, "suid")).st_mode & 0o7777, 0o755)
        self.assertEqual(os.stat(os.path.join(snap, "sg")).st_mode & 0o7777, 0o755)
        self.assertEqual(os.readlink(os.path.join(snap, "etc")), "/etc", "по ссылкам копия не ходит")

    def test_link_out_of_release_is_not_saved(self):
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(os.path.join(outside, "secret"))
        out = self.ok(BUILD_INSTALL=f"ln -s {outside} node_modules", BUILD_CMD="")
        self.assertIn("не удалось сохранить зависимости", out)
        self.assertFalse(os.path.exists(self.snap))
        self.assertFalse(os.path.exists(self.snap + ".tmp"))

    def test_link_in_release_is_not_restored(self):
        self.files["package.json"] = '{"dependencies":{"pkg":"1"},"scripts":{"build":"x"}}'
        install = "mkdir -p .airlok/bun/node_modules && echo 1 > .airlok/bun/node_modules/b && " + self.INSTALL
        self.ok(BUILD_INSTALL=install)
        self.assertTrue(os.path.isfile(os.path.join(self.snap, ".airlok", "bun", "node_modules", "b")))
        self.calls()
        target = os.path.join(self.tmp, "target")
        os.makedirs(target)
        self.ok(BUILD_INSTALL=install)
        self.assertEqual(len(self.calls()), 1, "обычный .airlok — из снимка")
        self.assertTrue(os.path.isfile(os.path.join(self.release, ".airlok", "bun", "node_modules", "b")))
        out = self.ok(BUILD_INSTALL="true", BUILD_CMD="", links={".airlok": target})
        self.assertIn("restored=0", out)
        self.assertEqual(os.listdir(target), [], "по ссылке из проекта root не пишет")

    def test_too_big_is_not_kept(self):
        self.write(self.snap + ".tmp.size", "99999")
        out = self.ok()
        self.assertIn("больше предела кэша", out)
        self.assertFalse(os.path.exists(self.snap))
        self.assertFalse(os.path.exists(self.snap + ".tmp"))

    def test_python(self):
        self.files = {"requirements.txt": "flask==3.1.1\n", "app.py": "x = 1\n"}
        install = "mkdir -p .venv/lib && echo 1 > .venv/lib/flask.py && mkdir -p node_modules"
        compile_ = "mkdir -p __pycache__ && echo $$ > __pycache__/app.pyc"
        py = dict(APP_STACK="python", BUILD_INSTALL=install + " && " + compile_, BUILD_CMD="", BUILD_COMPILE=compile_)
        self.ok(**py)
        self.assertEqual(os.listdir(self.snap), [".venv"], "байткод кода проекта в снимок не попадает")
        self.calls()
        self.assertIn("restored=1", self.ok(**py))
        self.assertEqual(self.calls(), [compile_], "после снимка — только байткод кода проекта")
        self.assertTrue(os.path.isfile(os.path.join(self.release, ".venv", "lib", "flask.py")))
        self.assertTrue(os.path.isfile(os.path.join(self.release, "__pycache__", "app.pyc")))
        self.files["requirements.txt"] = "flask>=3\n"
        self.assertIn("restored=0", self.ok(**py), "незакреплённые версии — ставим заново")

    def test_compile_before_build(self):
        self.ok(BUILD_COMPILE="echo c")
        self.calls()
        self.ok(BUILD_COMPILE="echo c")
        self.assertEqual(self.calls(), ["echo c && " + self.BUILD])

    def test_expires(self):
        self.ok()
        self.calls()
        key = self.snap + ".key"
        old = time.time() - 4321 * 60
        os.utime(key, (old, old))
        self.assertIn("restored=0", self.ok(), "снимок старше трёх дней не годится")
        self.assertEqual(len(self.calls()), 2)
        self.assertGreater(os.stat(key).st_mtime, old + 3600, "и сохраняется заново")
        self.assertIn("restored=1", self.ok())

    def test_prisma_schema_in_key(self):
        self.files["package.json"] = '{"dependencies":{"@prisma/client":"6"}}'
        self.files["prisma/schema.prisma"] = "model A {}"
        self.ok()
        self.calls()
        self.assertIn("restored=1", self.ok())
        self.files["prisma/schema.prisma"] = "model B {}"
        self.assertIn("restored=0", self.ok(), "клиент Prisma генерируется по схеме при установке")
        self.assertIn("restored=1", self.ok())

    def test_without_lock_file(self):
        self.files.pop("package-lock.json")
        for _ in range(2):
            self.assertIn("restored=0", self.ok())
        self.assertFalse(os.path.exists(self.snap))

    def test_nextjs_build_keeps_next_cache(self):
        self.ok(APP_STACK="nextjs")
        self.calls()
        self.ok(APP_STACK="nextjs")
        (build,) = self.calls()
        self.assertIn('$AIRLOK_CACHE/next', build)
        self.assertIn(self.BUILD, build)
        self.assertNotIn("mkdir -p node_modules/pkg", build)

    def test_failed_build_drops_snapshot(self):
        self.ok()
        self.calls()
        out = self.publish("build", BUILD_CMD="echo broken >&2; false")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("broken", out.stderr)
        self.assertIn("сборка не удалась, полный лог:", out.stderr, "человек видит причину, а не «неизвестная ошибка»")
        self.assertEqual(len(self.calls()), 1, "сборка шла с зависимостями из снимка")
        self.assertFalse(os.path.exists(self.snap), "следующая сборка поставит зависимости заново")
        self.ok()
        self.assertEqual(len(self.calls()), 2)

    def test_build_writing_outside_project(self):
        # Одинарные кавычки — через \047: publish подставляет значения в bash через repr.
        out = self.publish("build", BUILD_CMD='printf "Error: EACCES: permission denied, mkdir \\047/srv/backend/static\\047\\n" >&2; false')
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("сборка пишет за пределы папки проекта (mkdir '/srv/backend/static')", out.stderr)

    def test_failed_install_keeps_nothing(self):
        out = self.publish("build", BUILD_INSTALL="mkdir node_modules && false")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("сборка не удалась, полный лог:", out.stderr)
        self.assertEqual(len(self.calls()), 1, "после неудачной установки сборка не запускается")
        self.assertFalse(os.path.exists(self.snap))


class NextCacheTest(unittest.TestCase):
    """Скрипт сборки Next.js: .next/cache из кэша приложения и обратно."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.app = os.path.join(tmp.name, "app")
        self.cache = os.path.join(tmp.name, "cache")
        os.makedirs(self.app)
        os.makedirs(self.cache)

    def build(self, command):
        script = subprocess.run(["bash", "-c", 'eval "$1"; next_cache_script "$2"', "bash", func("next_cache_script"), command],
                                capture_output=True, text=True, check=True).stdout
        return subprocess.run(["sh", "-c", script], cwd=self.app, env={**os.environ, "AIRLOK_CACHE": self.cache},
                              capture_output=True, text=True)

    def test_round_trip(self):
        out = self.build("mkdir -p .next/cache/webpack && echo 1 > .next/cache/webpack/pack # комментарий")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(os.path.exists(os.path.join(self.cache, "next", "webpack", "pack")))
        self.assertFalse(os.path.exists(os.path.join(self.app, ".next", "cache")), "в релизе кэш не остаётся")

        os.rmdir(os.path.join(self.app, ".next"))
        out = self.build("test -f .next/cache/webpack/pack && echo 2 > .next/cache/webpack/pack")
        self.assertEqual(out.returncode, 0, "следующая сборка видит кэш прошлой: " + out.stderr)
        with open(os.path.join(self.cache, "next", "webpack", "pack")) as f:
            self.assertEqual(f.read().strip(), "2")

    def test_failed_build_keeps_cache(self):
        os.makedirs(os.path.join(self.cache, "next"))
        open(os.path.join(self.cache, "next", "old"), "w").close()
        out = self.build("rm -rf .next/cache && mkdir -p .next/cache && false")
        self.assertNotEqual(out.returncode, 0, "неудачная сборка остаётся неудачной")
        self.assertTrue(os.path.exists(os.path.join(self.cache, "next", "old")))

    def test_without_next_cache(self):
        out = self.build("true")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.cache, "next")))


class BuildContainerTest(unittest.TestCase):
    """Аргументы docker run сборки: кэш, вес CPU, память."""

    def run_build(self, meminfo, cache, parent="()"):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = os.path.join(tmp, "bin")
            os.makedirs(bin_dir)
            with open(os.path.join(bin_dir, "docker"), "w") as f:
                f.write('#!/bin/sh\nfor a; do echo "$a"; done\n')
            os.chmod(os.path.join(bin_dir, "docker"), 0o755)
            proc = os.path.join(tmp, "meminfo")
            with open(proc, "w") as f:
                f.write(meminfo)
            funcs = const("BUILD_MEM_RESERVE_MB") + "\n" + func("build_limits").replace("/proc/meminfo", proc) + func("build_container")
            # set +u: пустой массив в bash 3 (macOS) под set -u — ошибка, на сервере bash 5.
            script = ('source "$1"; ((BASH_VERSINFO[0] >= 4)) || set +u; eval "$2"; BUILD_ENV=(); APP_NAME=shop; BUILD_UID=998; BUILD_GID=998; RELEASE=/srv/apps/shop/releases/x; '
                      f'BUILD_IMAGE=node:22-bookworm-slim; BUILD_CACHE={cache!r}; BUILD_PARENT={parent}; build_limits; build_container "npm ci" "$3"; cat "$3"')
            out = subprocess.run(["bash", "-c", script, "bash", LIB, funcs, os.path.join(tmp, "log")],
                                 env={**os.environ, "PATH": bin_dir + ":" + os.environ["PATH"]}, capture_output=True, text=True)
            self.assertEqual(out.returncode, 0, out.stderr)
            return out.stdout.splitlines()

    def flag(self, args, name):
        return args[args.index(name) + 1]

    def test_small_server(self):
        args = self.run_build("MemTotal:  987654 kB\nMemAvailable: 614400 kB\nSwapTotal: 2097148 kB\n", "/var/lib/airlok/cache/shop")
        self.assertEqual(self.flag(args, "--memory"), "1024m", "next build нужен хотя бы 1 ГБ")
        self.assertEqual(self.flag(args, "--memory-swap"), "2048m", "swap сверху — не больше самой памяти")
        self.assertEqual(self.flag(args, "--cpu-shares"), "512", "без slice — доля, зависший сайт не задушит сборку")
        self.assertNotIn("--cgroup-parent", args)
        self.assertIn("/var/lib/airlok/cache/shop:/cache", args)
        self.assertIn("npm_config_cache=/cache/npm", args)
        self.assertIn("GOMODCACHE=/cache/go-mod", args)

    def test_large_server_without_swap(self):
        args = self.run_build("MemTotal: 8000000 kB\nMemAvailable: 7000000 kB\nSwapTotal: 0 kB\n", "")
        self.assertEqual(self.flag(args, "--memory"), "5859m")
        self.assertEqual(self.flag(args, "--memory-swap"), "5859m", "без swap — без swap")
        self.assertFalse(any(a.endswith(":/cache") for a in args), "без кэша на диске каталог не монтируется")
        self.assertIn("AIRLOK_CACHE=/tmp/airlok-cache", args)

    def test_build_in_slice(self):
        args = self.run_build("MemTotal: 2000000 kB\nMemAvailable: 1500000 kB\nSwapTotal: 0 kB\n", "", "(--cgroup-parent airlokbuild.slice)")
        self.assertEqual(self.flag(args, "--cgroup-parent"), "airlokbuild.slice", "вес задаёт slice, а не пересчёт долей Docker")
        self.assertNotIn("--cpu-shares", args)

    def test_busy_server_keeps_memory_for_sites(self):
        args = self.run_build("MemTotal: 4000000 kB\nMemAvailable: 1800000 kB\nSwapTotal: 2097148 kB\n", "")
        self.assertEqual(self.flag(args, "--memory"), "1501m", "не больше свободной памяти за вычетом 256 МБ сайтам")
        self.assertEqual(self.flag(args, "--memory-swap"), "3002m")


STATIC_CONF = re.search(r"^static_conf\(\) \{\n.*?\nEOF\n.*?\n\}\n", SOURCE, re.M | re.S).group(0)
# Регулярки location с кэшем на год.
IMMUTABLE = [m.group(1).replace("\\$", "$") for m in
             re.finditer(r'location ~ "(.*?)" \{\n\s*try_files \\\$uri =404;\n\s*add_header Cache-Control "public, max-age=31536000, immutable";', STATIC_CONF)]


def immutable(path):
    return any(re.search(r, path) for r in IMMUTABLE)


class StaticCacheTest(unittest.TestCase):
    def test_immutable_only_for_build_output(self):
        self.assertEqual(len(IMMUTABLE), 2)
        for path in ("/assets/index-BxYz12ab.js", "/assets/index-a1b2c3d4.css", "/assets/vendor.3f9a1c2e7b.mjs",
                     "/assets/chunks/About-Dk_3x-Zq.js", "/_astro/Layout.DgK3hb1x.css", "/_astro/photo.Ab12cd34.webp",
                     "/_nuxt/CXa7d6sQ.js", "/_nuxt/entry.B_x-12ab.css", "/_next/static/chunks/main-abc.js",
                     "/_next/static/media/font.woff2", "/_app/immutable/entry/start.js"):
            self.assertTrue(immutable(path), path)
        for path in ("/assets/bg-Gradient.png", "/assets/img-Mountain.jpg", "/assets/photo-20241105.jpg", "/assets/cv-Ivanov12.pdf",
                     "/assets/index-BxYz12ab.png", "/assets/fonts/inter-Ab12cd34.woff2", "/assets/main-20241105.js",
                     "/assets/app-abcdefgh.js", "/assets/app-ABCD1234.js", "/assets/logo.svg", "/assets/index-BxYz12ab.js.map",
                     "/_nuxt/builds/latest.json", "/_nuxt/builds/meta/0f1e2d3c.json", "/index-BxYz12ab.js", "/assets/",
                     "/static/js/main.a1b2c3d4.js"):
            self.assertFalse(immutable(path), path)

    def test_conf(self):
        self.assertEqual(STATIC_CONF.count("try_files \\$uri =404;"), 2, "без файла — 404, а не index.html на год")
        server = STATIC_CONF[STATIC_CONF.index("server {"):STATIC_CONF.index("location")]
        self.assertIn("add_header Cache-Control no-cache always;", server,
                      "всё, что не immutable, — HTML, /style.css, /data.json, картинки из public/assets — перепроверяется")
        self.assertEqual(STATIC_CONF.count("add_header"), 3, "у location / своего add_header нет: no-cache сервера действует и там")


class SiteGzipTest(unittest.TestCase):
    def test_site_compresses(self):
        tpl = re.search(r'write_if_changed "/etc/nginx/sites-available/airlok-\$\{APP_NAME\}\.conf" 0644 <<EOF; then\n(.*?)\nEOF\n',
                        SOURCE, re.S).group(1)
        https = tpl[tpl.index("ssl_certificate"):]
        for line in ("gzip on;", "gzip_vary on;", "gzip_proxied any;", "gzip_min_length 1024;"):
            self.assertIn("    " + line, https)
        types = https[https.index("gzip_types"):].split(";")[0]
        for t in ("text/css", "application/javascript", "application/json", "image/svg+xml", "text/plain"):
            self.assertIn(t, types)
        self.assertNotIn("text/event-stream", types, "поток событий не сжимается")
        self.assertNotIn("text/html", types, "text/html nginx сжимает и так, повтор — предупреждение")
        self.assertEqual(https.count("gzip on;"), 1)
        self.assertNotIn("gzip", https[https.index("location / {"):], "сниппет замера скорости сам включает gzip в location /")


class StaticConfCheckTest(unittest.TestCase):
    """static_conf: новый конфиг nginx статики сначала проверяет nginx образа."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name
        self.app_dir = os.path.join(self.tmp, "app")
        self.release = os.path.join(self.tmp, "release")
        self.conf = os.path.join(self.app_dir, "shared", "static.conf")
        os.makedirs(os.path.join(self.app_dir, "shared"))
        os.makedirs(self.release)
        open(os.path.join(self.release, "index.html"), "w").close()
        bin_dir = os.path.join(self.tmp, "bin")
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "docker"), "w") as f:
            f.write('#!/bin/sh\necho "$*" >> "$FAKE/calls"\n[ -e "$FAKE/fails" ] && { echo "nginx: [emerg] unknown directive" >&2; exit 1; }\nexit 0\n')
        os.chmod(os.path.join(bin_dir, "docker"), 0o755)
        self.env = {**os.environ, "PATH": bin_dir + ":" + os.environ["PATH"], "FAKE": self.tmp}

    def run_conf(self, arg=""):
        script = ('source "$1"; ((BASH_VERSINFO[0] >= 4)) || set +u; eval "$2"; eval "$3"; APP_DIR=$4; RELEASE=$5; BUILD_CMD=""; BUILD_OUTPUT=""; '
                  f'static_conf {arg}')
        consts = "\n".join(const(n) for n in ("STATIC_IMAGE", "CONTAINER_PORT"))
        return subprocess.run(["bash", "-c", script, "bash", LIB, consts, func("static_root") + STATIC_CONF,
                               self.app_dir, self.release], env=self.env, capture_output=True, text=True)

    def calls(self):
        path = os.path.join(self.tmp, "calls")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return f.read().splitlines()

    def test_checked_before_use(self):
        out = self.run_conf()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(len(self.calls()), 1)
        self.assertIn("--entrypoint nginx", self.calls()[0])
        self.assertTrue(self.calls()[0].endswith("-t -q"))
        with open(self.conf) as f:
            self.assertIn("listen 8080;", f.read())
        self.assertEqual(oct(os.stat(self.conf).st_mode & 0o777), "0o644", "nginx образа читает конфиг не от root")
        self.assertEqual(self.run_conf().returncode, 0)
        self.assertEqual(len(self.calls()), 1, "тот же конфиг второй раз не проверяется")

    def test_refused_keeps_old(self):
        with open(self.conf, "w") as f:
            f.write("old\n")
        open(os.path.join(self.tmp, "fails"), "w").close()
        out = self.run_conf()
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("остались прежними", out.stderr)
        self.assertIn("unknown directive", out.stderr)
        with open(self.conf) as f:
            self.assertEqual(f.read(), "old\n")
        self.assertFalse(os.path.exists(self.conf + ".new"))

    def test_rollback_skips_check(self):
        out = self.run_conf("--checked")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.calls(), [])


class SliceRefusedTest(unittest.TestCase):
    def refused(self, log, parent=True):
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            f.write(log)
        self.addCleanup(os.remove, f.name)
        parent = "BUILD_PARENT=(--cgroup-parent airlokbuild.slice)" if parent else "BUILD_PARENT=()"
        out = subprocess.run(["bash", "-c", f'eval "$1"; {parent}; slice_refused "$2" && echo yes || echo no', "bash",
                              func("slice_refused"), f.name], capture_output=True, text=True)
        return out.stdout.strip() == "yes"

    def test_only_docker_errors(self):
        self.assertTrue(self.refused("#1 [internal] load build definition\nERROR: failed to solve: invalid cgroup parent: x\n"))
        self.assertTrue(self.refused("Error response from daemon: cgroup-parent for systemd cgroup should be a valid slice\n"))
        self.assertFalse(self.refused("#7 3.1 ERROR: cannot read /sys/fs/cgroup/memory.max\nERROR: failed to solve: process "
                                      '"/bin/sh -c npm run build" did not complete successfully: exit code: 1\n'),
                         "вывод шага сборки про cgroup — ошибка проекта")
        self.assertFalse(self.refused("ERROR: failed to solve: process \"/bin/sh -c cat /sys/fs/cgroup/x\" did not complete successfully: exit code: 1\n"))
        self.assertFalse(self.refused("ERROR: invalid cgroup parent\n", parent=False), "без slice повторять нечего")


if __name__ == "__main__":
    unittest.main()
