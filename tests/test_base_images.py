"""app-deploy.sh: базовые образы обновляются при публикации, но не чаще раза в сутки и не ценой публикации."""

import os
import re
import subprocess
import tempfile
import unittest

RECIPES = os.path.join(os.path.dirname(__file__), "..", "recipes")
LIB = os.path.join(RECIPES, "lib.sh")

# docker: образы из файла present, идентификатор из файла id; pull удаётся, если нет файла pull-fails,
# и меняет идентификатор. Все вызовы — в calls.
FAKE_DOCKER = """#!/bin/sh
echo "$*" >> "$FAKE/calls"
case "$1 $2" in
"pull -q") [ -e "$FAKE/pull-fails" ] && exit 1; echo "$3" >> "$FAKE/present"; echo sha256:new > "$FAKE/id" ;;
"image inspect")
	for last; do :; done
	grep -qxF "$last" "$FAKE/present" || exit 1
	[ "$3" = --format ] && cat "$FAKE/id"
	;;
esac
exit 0
"""


class BaseImagesTest(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(RECIPES, "app-deploy.sh"), encoding="utf-8") as f:
            src = f.read()
        self.funcs = "\n".join(re.search(r"^%s\(\) \{\n.*?\n\}\n" % name, src, re.M | re.S).group(0)
                               for name in ("pull_base", "base_images", "base_unreachable"))
        self.consts = "\n".join(re.search(r"^%s=.*$" % name, src, re.M).group(0)
                                for name in ("PG_IMAGE", "BASE_PULL_MINUTES", "BASE_RETRY_MINUTES"))
        self.tmp = tempfile.TemporaryDirectory()
        self.fake = os.path.join(self.tmp.name, "fake")
        os.makedirs(self.fake)
        bin_dir = os.path.join(self.tmp.name, "bin")
        os.makedirs(bin_dir)
        with open(os.path.join(bin_dir, "docker"), "w") as f:
            f.write(FAKE_DOCKER)
        os.chmod(os.path.join(bin_dir, "docker"), 0o755)
        self.write("id", "sha256:old\n")
        self.write("present", "")
        self.env = {**os.environ, "PATH": bin_dir + ":" + os.environ["PATH"], "FAKE": self.fake}

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text):
        with open(os.path.join(self.fake, name), "w") as f:
            f.write(text)

    def calls(self):
        path = os.path.join(self.fake, "calls")
        if not os.path.exists(path):
            return []
        with open(path) as f:
            return f.read().splitlines()

    def run_func(self, call, **variables):
        assigns = "".join(f"{k}={v!r}; " for k, v in variables.items())
        script = f'source "$1"; eval "$2"; eval "$3"; BASE_STAMPS=$4/stamps; {assigns}{call}'
        return subprocess.run(["bash", "-c", script, "bash", LIB, self.consts, self.funcs, self.tmp.name],
                              env=self.env, capture_output=True, text=True)

    def test_pull_once_a_day(self):
        out = self.run_func("pull_base node:22-bookworm-slim")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.calls(), ["pull -q node:22-bookworm-slim"])
        self.run_func("pull_base node:22-bookworm-slim")
        self.assertEqual(len(self.calls()), 1, "свежий образ второй раз за сутки не скачивается")

    def test_failed_pull_does_not_stop_publish(self):
        self.write("pull-fails", "")
        self.write("present", "node:22-bookworm-slim\n")
        out = self.run_func("pull_base node:22-bookworm-slim; echo done")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("собираю на прежнем", out.stdout)
        self.assertIn("done", out.stdout)
        self.run_func("pull_base node:22-bookworm-slim")
        self.assertEqual(len([c for c in self.calls() if c.startswith("pull")]), 1, "после неудачи — не чаще раза в час")

        os.remove(os.path.join(self.fake, "pull-fails"))
        out = self.run_func("pull_base python:3.12-slim-bookworm")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("прежнем", out.stdout)

    def test_go_release_tracks_build_image(self):
        self.write("present", "golang:1.27-bookworm\n")
        go = dict(APP_STACK="go", BUILD_IMAGE="golang:1.27-bookworm", RUN_IMAGE="golang:1.27-bookworm", DB_ENABLED="0",
                  BUILD_ENV_SUM="abc")
        old = self.run_func("base_images; echo $BUILD_ENV_SUM", **go)
        self.assertEqual(old.returncode, 0, old.stderr)
        self.assertEqual(self.calls().count("pull -q golang:1.27-bookworm"), 1)
        again = self.run_func("base_images; echo $BUILD_ENV_SUM", **go)
        self.assertEqual(old.stdout, again.stdout, "та же база — тот же отпечаток, релиз берётся готовым")
        self.write("id", "sha256:newer\n")
        newer = self.run_func("base_images; echo $BUILD_ENV_SUM", **go)
        self.assertNotEqual(old.stdout, newer.stdout, "новый Go — релиз собирается заново")
        self.assertNotEqual(old.stdout.strip(), "abc")

    def node_sum(self, version, **extra):
        image = f"node:{version}-bookworm-slim"
        out = self.run_func("base_images; echo $BUILD_ENV_SUM", APP_STACK="node", BUILD_IMAGE=image, RUN_IMAGE=image,
                            DB_ENABLED="1", BUILD_ENV_SUM="abc", **extra)
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.strip()

    def test_node_release_tracks_version_not_image_id(self):
        self.write("present", "node:22-bookworm-slim\n")
        first = self.node_sum("22")
        self.assertIn("pull -q postgres:16", self.calls())
        self.write("id", "sha256:newer\n")
        self.assertEqual(self.node_sum("22"), first, "новый выпуск той же ветки Node.js живёт в образе запуска: релиз берётся готовым")
        self.assertNotEqual(first, "abc", "отпечаток релиза старого агента не совпадает: релиз один раз собирается заново")
        self.assertNotEqual(self.node_sum("20"), first, "node_modules под Node.js 20 не запускаются на 22")

    def test_python_release_tracks_version(self):
        def py_sum(version):
            image = f"python:{version}-slim-bookworm"
            out = self.run_func("base_images; echo $BUILD_ENV_SUM", APP_STACK="python", BUILD_IMAGE=image,
                                RUN_IMAGE=image, DB_ENABLED="0", BUILD_ENV_SUM="")
            self.assertEqual(out.returncode, 0, out.stderr)
            return out.stdout.strip()
        self.assertNotEqual(py_sum("3.12"), py_sum("3.13"))
        self.assertNotEqual(py_sum("3.12"), "")

    def test_dockerfile_stack(self):
        out = self.run_func("base_images; echo \"[$BUILD_ENV_SUM]\"", APP_STACK="docker", BUILD_IMAGE="", RUN_IMAGE="",
                            DB_ENABLED="0", BUILD_ENV_SUM="")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.calls(), [], "базу из FROM обновляет docker build --pull")
        self.assertEqual(out.stdout.strip(), "[]", "отпечаток релиза по Dockerfile не меняется")

    def test_base_unreachable(self):
        logs = {
            "ERROR: failed to solve: node:22: failed to resolve source metadata for docker.io/library/node:22: "
            "failed to authorize: failed to fetch anonymous token: unexpected status: 429 Too Many Requests": True,
            'Error response from daemon: Get "https://registry-1.docker.io/v2/": net/http: TLS handshake timeout': True,
            'ERROR: failed to solve: process "/bin/sh -c npm ci" did not complete successfully: exit code: 1': False,
            "npm ERR! network request to https://registry.npmjs.org/x failed, reason: connect ETIMEDOUT": False,
            # Опечатка в FROM: реестр ответил, образа нет — без --pull сборка не поможет.
            "ERROR: failed to solve: nodee:22: failed to resolve source metadata for docker.io/library/nodee:22: "
            "pull access denied, repository does not exist or may require authorization": False,
            "ERROR: failed to solve: node:99: failed to resolve source metadata for docker.io/library/node:99: "
            "docker.io/library/node:99: not found": False,
            "ERROR: failed to solve: ghcr.io/acme/base:1: failed to resolve source metadata for ghcr.io/acme/base:1: "
            "failed to authorize: failed to fetch oauth token: unexpected status: 401 Unauthorized": False,
            "ERROR: failed to solve: ghcr.io/acme/base:1: failed to resolve source metadata for ghcr.io/acme/base:1: "
            'failed to do request: Head "https://ghcr.io/v2/acme/base/manifests/1": dial tcp: lookup ghcr.io: no such host': True,
        }
        for text, want in logs.items():
            path = os.path.join(self.tmp.name, "build.log")
            with open(path, "w") as f:
                f.write(text + "\n")
            out = self.run_func(f"base_unreachable {path!r}")
            self.assertEqual(out.returncode == 0, want, text)


if __name__ == "__main__":
    unittest.main()
