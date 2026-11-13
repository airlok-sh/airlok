"""Docker Hub со своего сервера: подсказка к упавшей публикации и проверка при установке.

python3 -m unittest discover tests
"""
import os
import re
import stat
import subprocess
import tempfile
import unittest

RECIPES = os.path.join(os.path.dirname(__file__), "..", "recipes")
LIB = os.path.join(RECIPES, "lib.sh")
with open(os.path.join(RECIPES, "server-base.sh"), encoding="utf-8") as f:
    HUB_CHECK = re.search(r"^hub_check\(\) \{\n.*?\n\}\n", f.read(), re.M | re.S).group(0)
with open(os.path.join(RECIPES, "app-deploy.sh"), encoding="utf-8") as f:
    DEPLOY = f.read()

HUB_FAILURES = {
    "buildkit_429": (
        "#5 [internal] load metadata for docker.io/library/node:22-bookworm-slim\n"
        "#5 ERROR: failed to copy: httpReadSeeker: failed open: unexpected status code "
        "https://registry-1.docker.io/v2/library/node/manifests/sha256:0f3c: 429 Too Many Requests - "
        "Server message: toomanyrequests: You have reached your unauthenticated pull rate limit.\n"
        "ERROR: failed to solve: node:22-bookworm-slim: failed to resolve source metadata for "
        "docker.io/library/node:22-bookworm-slim: failed to copy: 429 Too Many Requests\n"),
    "dockerd_toomanyrequests": (
        "Unable to find image 'node:22-bookworm-slim' locally\n"
        "docker: Error response from daemon: toomanyrequests: You have reached your unauthenticated pull "
        "rate limit. https://www.docker.com/increase-rate-limit.\n"),
    "dockerd_timeout": (
        "Unable to find image 'python:3.12-slim-bookworm' locally\n"
        'docker: Error response from daemon: Get "https://registry-1.docker.io/v2/": net/http: request canceled '
        "while waiting for connection (Client.Timeout exceeded while awaiting headers).\n"),
    "compose_dns": (
        " db Pulling \n"
        ' db Error Head "https://registry-1.docker.io/v2/library/postgres/manifests/16": dial tcp: lookup '
        "registry-1.docker.io on 127.0.0.53:53: server misbehaving\n"),
    "geo_403": (
        "Error response from daemon: error from registry: <html><body><h1>403 Forbidden</h1>\n"
        "Since Docker is a US company, we must comply with US export control regulations.\n"),
    "buildkit_auth": (
        "ERROR: failed to solve: golang:1.24-bookworm: failed to resolve source metadata for "
        "docker.io/library/golang:1.24-bookworm: failed to authorize: failed to fetch anonymous token: "
        'Get "https://auth.docker.io/token?scope=repository%3Alibrary%2Fgolang%3Apull": dial tcp 3.94.224.37:443: i/o timeout\n'),
    "dockerd_r2_config": (
        "Unable to find image 'node:22-bookworm-slim' locally\n"
        "22-bookworm-slim: Pulling from library/node\n"
        "docker: error pulling image configuration: download failed after attempts=6: dial tcp "
        "172.64.66.1:443: i/o timeout.\n"),
    "dockerd_r2_layer_eof": (
        "Unable to find image 'python:3.12-slim-bookworm' locally\n"
        "3.12-slim-bookworm: Pulling from library/python\n"
        "docker: unexpected EOF.\n"),
    "dockerd_r2_layer_reset": (
        "docker: read tcp 10.0.0.5:43210->172.64.66.1:443: read: connection reset by peer.\n"),
    "dockerd_register_layer": (
        "docker: failed to register layer: unexpected EOF.\n"),
    "compose_r2_config": (
        " db Pulling \n"
        " db Error error pulling image configuration: download failed after attempts=6: dial tcp "
        "172.64.66.1:443: i/o timeout\n"
        "Error response from daemon: error pulling image configuration: download failed after attempts=6: "
        "dial tcp 172.64.66.1:443: i/o timeout\n"),
    "buildkit_r2_layer": (
        "#6 sha256:4f4fb700ef54 1.2MB / 49.6MB\n"
        "#6 ERROR: failed to copy: httpReadSeeker: failed open: failed to do request: Get "
        '"https://docker-images-prod.6aa30f8b08e16409b46e0173d6de2f56.r2.cloudflarestorage.com/registry-v2/docker/'
        'registry/v2/blobs/sha256/4f/4f4fb700ef54/data?X-Amz-Algorithm=AWS4-HMAC-SHA256": read tcp '
        "10.0.0.5:51234->172.64.66.1:443: read: connection reset by peer\n"),
    "hub_429_digest_401": (
        "ERROR: failed to copy: httpReadSeeker: failed open: unexpected status code "
        "https://registry-1.docker.io/v2/library/node/blobs/sha256:5e401a: 429 Too Many Requests\n"),
    "hub_503": (
        "ERROR: failed to solve: node:22: failed to resolve source metadata for docker.io/library/node:22: "
        "unexpected status from HEAD request to https://registry-1.docker.io/v2/library/node/manifests/22: "
        "503 Service Unavailable\n"),
}

PROJECT_FAILURES = {
    "pull_access_denied": (
        "docker: Error response from daemon: pull access denied for nodee, repository does not exist or may "
        "require 'docker login': denied: requested access to the resource is denied.\n"),
    "tag_not_found": (
        "ERROR: failed to solve: node:99: failed to resolve source metadata for docker.io/library/node:99: "
        "docker.io/library/node:99: not found\n"),
    "npm": (
        "npm ERR! code ETIMEDOUT\nnpm ERR! network request to https://registry.npmjs.org/left-pad failed, "
        "reason: connect ETIMEDOUT 104.16.0.35:443\n"),
    "wrong_login_401": (
        "ERROR: failed to solve: node:22: failed to resolve source metadata for docker.io/library/node:22: "
        "failed to authorize: failed to fetch oauth token: unexpected status from GET request to "
        "https://auth.docker.io/token: 401 Unauthorized\n"),
    "register_layer_disk_full": (
        "docker: failed to register layer: write /usr/lib/x86_64-linux-gnu/libLLVM.so.15: no space left on device.\n"),
    "digest_with_429": (
        "ERROR: failed to solve: node:22: failed to resolve source metadata for docker.io/library/node:22: "
        "https://registry-1.docker.io/v2/library/node/manifests/sha256:ab429c: connection established\n"),
    "app_output_eof": (
        "+ npm run build\nError: unexpected EOF while parsing config\n"),
    "other_registry_429": (
        "ERROR: failed to solve: ghcr.io/acme/base:1: unexpected status code https://ghcr.io/v2/acme/base/"
        "manifests/1: 429 Too Many Requests\n"),
}


def hint(text):
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
        f.write(text)
    try:
        out = subprocess.run(["bash", "-c", 'source "$1"; hub_hint "$2"', "bash", LIB, f.name],
                             capture_output=True, text=True)
    finally:
        os.unlink(f.name)
    if out.returncode != 0:
        raise AssertionError(out.stderr)
    return out.stdout


class HubHintTest(unittest.TestCase):
    def test_hub_failures_get_hint(self):
        for name, text in HUB_FAILURES.items():
            with self.subTest(name):
                out = hint("> npm ci\n" + text)
                self.assertTrue(out.startswith(". Причина — Docker Hub"), out)
                self.assertIn('"registry-mirrors": ["https://mirror.gcr.io"]', out)
                self.assertIn("systemctl reload docker", out)
                self.assertNotIn("\n", out, "текст ошибки уходит в дашборд одной строкой")
                self.assertFalse(out.endswith("."), "точку в конце ставит die или notify")
                self.assertIn("docker info", out)

    def test_project_failures_get_none(self):
        for name, text in PROJECT_FAILURES.items():
            with self.subTest(name):
                self.assertEqual(hint(text), "")

    def test_missing_log_gets_none(self):
        out = subprocess.run(["bash", "-c", 'source "$1"; hub_hint /nonexistent/build.log; echo "rc=$?"', "bash", LIB],
                             capture_output=True, text=True)
        self.assertEqual(out.stdout, "rc=0\n", out.stderr)

    def test_only_new_part_of_log_counts(self):
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            f.write(HUB_FAILURES["dockerd_timeout"])
        self.addCleanup(os.unlink, f.name)
        script = ('source "$1"; log=$2; mark=$({ wc -c <"$log"; } 2>/dev/null || echo 0); '
                  'echo "Error: container app exited (1)" >>"$log"; hub_hint <(tail -c +$((mark + 1)) "$log")')
        out = subprocess.run(["bash", "-c", script, "bash", LIB, f.name], capture_output=True, text=True)
        self.assertEqual((out.returncode, out.stdout), (0, ""), out.stderr)

    def test_deploy_failures_carry_hint(self):
        for start in ("сборка по ${DOCKER_FILE} не удалась", "сборка не удалась, полный лог",
                      "база не запустилась", "приложение не запустилось"):
            with self.subTest(start):
                line = next(l for l in DEPLOY.splitlines() if 'die "' + start in l)
                self.assertIn("hub_hint", line)


class HubCheckTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.bin = os.path.join(tmp.name, "bin")
        os.makedirs(self.bin)
        self.calls = os.path.join(tmp.name, "calls")
        self.fake("docker", 'printf %s "${MIRRORS:-}"')
        self.fake("curl", '[[ -n ${CURL_FAILS:-} ]] && { printf 000; exit 28; }\nprintf %s "${CODE:-401}"')

    def fake(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write('#!/bin/bash\necho "%s $*" >>"%s"\n%s\n' % (name, self.calls, body))
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)

    def check(self, **env):
        out = subprocess.run(["bash", "-c", 'source "$1"; eval "$2"; hub_check; echo done', "bash", LIB, HUB_CHECK],
                             capture_output=True, text=True,
                             env={**os.environ, "PATH": self.bin + os.pathsep + os.environ["PATH"], **env})
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(out.stdout.endswith("done\n"), "проверка не останавливает установку")
        return out.stdout

    def curl_called(self):
        with open(self.calls, encoding="utf-8") as f:
            return any(c.startswith("curl ") for c in f)

    def test_reachable_hub(self):
        self.assertIn("Docker Hub отвечает", self.check())

    def test_unreachable_hub_is_warned_about(self):
        out = self.check(CURL_FAILS="1")
        self.assertIn("не отвечает (нет ответа)", out)
        self.assertIn("registry-mirrors", out)

    def test_forbidden_hub_shows_code(self):
        self.assertIn("не отвечает (код 403)", self.check(CODE="403"))

    def test_mirror_skips_hub(self):
        out = self.check(MIRRORS="https://mirror.gcr.io ", CURL_FAILS="1")
        self.assertIn("через зеркало https://mirror.gcr.io", out)
        self.assertFalse(self.curl_called())


if __name__ == "__main__":
    unittest.main()
