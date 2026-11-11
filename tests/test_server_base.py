"""server-base.sh и install.sh: apt занят, повторный запуск не переписывает настройки сервера.

python3 -m unittest discover tests
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

RECIPES = os.path.join(os.path.dirname(__file__), "..", "recipes")
LIB = os.path.join(RECIPES, "lib.sh")
with open(os.path.join(RECIPES, "server-base.sh"), encoding="utf-8") as f:
    SRC = f.read()
with open(os.path.join(RECIPES, "..", "install.sh"), encoding="utf-8") as f:
    INSTALL_GIT = re.search(r"^install_git\(\) \{\n.*?\n\}\n", f.read(), re.M | re.S).group(0)

spec = importlib.util.spec_from_file_location("serverbase", os.path.join(RECIPES, "serverbase.py"))
serverbase = importlib.util.module_from_spec(spec)
spec.loader.exec_module(serverbase)


def func(*names):
    """Текст функций server-base.sh: многострочных и в одну строку."""
    out = []
    for name in names:
        m = re.search(r"^%s\(\) \{\n.*?\n\}\n" % name, SRC, re.M | re.S) or re.search(r"^%s\(\) \{.*\}\n" % name, SRC, re.M)
        out.append(m.group(0))
    return "\n".join(out)


FUNC = func("apt_install")
# conf_set пишет через install -D, которого нет в BSD install на macOS.
PORTABLE_WRITE = ('write_if_changed() { local tmp; tmp=$(mktemp); cat >"$tmp"; '
                  'if [[ -f $1 ]] && cmp -s "$tmp" "$1"; then rm -f "$tmp"; return 1; fi; mv "$tmp" "$1"; }')


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


OURS = {"log-driver": "json-file", "log-opts": {"max-size": "10m", "max-file": "3"}, "live-restore": True}
# daemon.json, который писал server-base.sh прежних версий.
OLD_AIRLOK = {**OURS, "no-new-privileges": True}


class DaemonMergeTest(unittest.TestCase):
    def test_empty_gets_log_rotation_and_live_restore(self):
        self.assertEqual(serverbase.merge_daemon({}), OURS)
        self.assertEqual(serverbase.merge_daemon({}, no_new_privileges=True), OLD_AIRLOK)

    def test_own_settings_are_kept(self):
        conf = {"data-root": "/mnt/docker", "registry-mirrors": ["https://mirror.gcr.io"], "dns": ["1.1.1.1"]}
        self.assertEqual(serverbase.merge_daemon(conf), {**conf, **OURS})
        self.assertNotIn("log-opts", conf, "аргумент не меняется")

    def test_own_log_driver_is_not_touched(self):
        conf = {"log-driver": "journald"}
        self.assertEqual(serverbase.merge_daemon(conf), {"log-driver": "journald", "live-restore": True})

    def test_own_log_opts_win(self):
        merged = serverbase.merge_daemon({"log-opts": {"max-size": "100m"}})
        self.assertEqual(merged["log-opts"], {"max-size": "100m", "max-file": "3"})

    def test_explicit_live_restore_off_and_swarm(self):
        self.assertFalse(serverbase.merge_daemon({"live-restore": False})["live-restore"])
        self.assertNotIn("live-restore", serverbase.merge_daemon({}, swarm=True), "в swarm Docker с live-restore не стартует")

    def test_keys_set_by_dockerd_flags_are_skipped(self):
        flags = "{ path=/usr/bin/dockerd ; argv[]=/usr/bin/dockerd --log-driver=journald --live-restore -H fd:// }"
        self.assertEqual(serverbase.merge_daemon({}, flags), {})

    def test_existing_no_new_privileges_stays(self):
        self.assertEqual(serverbase.merge_daemon(OLD_AIRLOK), OLD_AIRLOK)


class DaemonFileTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = os.path.join(tmp.name, "docker", "daemon.json")

    def write(self, text, mode=0o644):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(self.path, mode)

    def read(self):
        with open(self.path, encoding="utf-8") as f:
            return f.read()

    def merge(self, **kw):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return serverbase.daemon_json(self.path, kw.get("flags", ""), kw.get("swarm", False), kw.get("nnp", False))

    def test_missing_file_is_created_and_second_run_changes_nothing(self):
        self.assertEqual(self.merge(), 0)
        self.assertEqual(json.loads(self.read()), OURS)
        before = self.read()
        self.assertEqual(self.merge(), serverbase.EXIT_UNCHANGED)
        self.assertEqual(self.read(), before)

    def test_mirrors_and_mode_survive(self):
        self.write('{"registry-mirrors": ["https://mirror.gcr.io"], "data-root": "/mnt/docker"}', 0o600)
        self.assertEqual(self.merge(), 0)
        conf = json.loads(self.read())
        self.assertEqual(conf["registry-mirrors"], ["https://mirror.gcr.io"])
        self.assertEqual(conf["data-root"], "/mnt/docker")
        self.assertEqual(conf["log-opts"], OURS["log-opts"])
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_old_airlok_file_is_left_alone(self):
        text = '{\n  "log-driver": "json-file",\n  "log-opts": { "max-size": "10m", "max-file": "3" },\n  "no-new-privileges": true,\n  "live-restore": true\n}\n'
        self.write(text)
        self.assertEqual(self.merge(), serverbase.EXIT_UNCHANGED)
        self.assertEqual(self.read(), text, "форматирование не меняем, Docker не перезапускается")

    def test_broken_json_is_not_touched(self):
        for text in ('{"data-root": "/mnt/docker",}', '["not", "object"]'):
            self.write(text)
            self.assertEqual(self.merge(), serverbase.EXIT_NOT_JSON)
            self.assertEqual(self.read(), text)


SS_TCP = """LISTEN 0 4096 0.0.0.0:22 0.0.0.0:*
LISTEN 0 511 0.0.0.0:80 0.0.0.0:*
LISTEN 0 4096 127.0.0.1:3000 0.0.0.0:*
LISTEN 0 4096 127.0.0.53%lo:53 0.0.0.0:*
LISTEN 0 4096 [::1]:5432 [::]:*
LISTEN 0 4096 [::ffff:127.0.0.1]:6379 *:*
LISTEN 0 4096 *:8443 *:*
LISTEN 0 4096 [::]:22 [::]:*
LISTEN 0 4096 203.0.113.5:2053 0.0.0.0:*
"""
SS_UDP = """UNCONN 0 0 0.0.0.0:51820 0.0.0.0:*
UNCONN 0 0 127.0.0.54:53 0.0.0.0:*
UNCONN 0 0 [fe80::1%eth0]:546 [::]:*
UNCONN 0 0 [::]:51820 [::]:*
"""


class PortsTest(unittest.TestCase):
    def test_ssh_ports_from_sshd_and_socket(self):
        sshd = "port 22\nport 2222\nlistenaddress 0.0.0.0:2200\naddressfamily any\n"
        self.assertEqual(serverbase.ssh_ports(sshd, "[::]:2222 (Stream) 0.0.0.0:2022 (Stream)"), [22, 2022, 2200, 2222])
        self.assertEqual(serverbase.ssh_ports("", ""), [22], "без sshd -T не закрываем стандартный порт")

    def test_listening_ports_skip_localhost(self):
        self.assertEqual(serverbase.listening_ports(SS_TCP, SS_UDP),
                         ["22/tcp", "80/tcp", "2053/tcp", "8443/tcp", "51820/udp"])


class FirewallTest(AptCase):
    def setUp(self):
        super().setUp()
        self.conf = os.path.join(os.path.dirname(self.bin), "server.env")
        self.status = os.path.join(os.path.dirname(self.bin), "ufw-status")
        self.fake("ufw", '[[ $1 == status ]] && cat "%s"; exit 0' % self.status)
        self.fake("sshd", "printf 'port 22\\nport 2222\\n'")
        self.fake("systemctl", "echo inactive; exit 3")
        self.fake("ss", 'case "$*" in *-tln*) printf %%s "%s" ;; *-uln*) printf %%s "%s" ;; esac' % (SS_TCP, SS_UDP))

    def run_firewall(self, first_run, active=False, state=None, **env):
        with open(self.status, "w") as f:
            f.write("Status: active\n\nTo Action From\n" if active else "Status: inactive\n")
        if state:
            with open(self.conf, "w") as f:
                f.write("LETSENCRYPT=1\nFIREWALL=%s\n" % state)
        script = 'source "$1"; eval "$2"; %s; SERVER_CONF=$3 FIRST_RUN=$4; firewall' % PORTABLE_WRITE
        out = subprocess.run(["bash", "-c", script, "bash", LIB, func("firewall"), self.conf, "yes" if first_run else "no"],
                             capture_output=True, text=True, env={**os.environ, "PATH": self.bin + os.pathsep + os.environ["PATH"], **env})
        self.assertEqual(out.returncode, 0, out.stderr)
        return out

    def ufw(self):
        return [c[len("ufw "):] for c in self.called() if c.startswith("ufw ") and c != "ufw status"]

    def state(self):
        with open(self.conf) as f:
            return f.read()

    def test_first_run_keeps_listening_ports_open(self):
        out = self.run_firewall(first_run=True)
        calls = self.ufw()
        allowed = [c.split()[1] for c in calls if c.startswith("allow ")]
        self.assertEqual(allowed, ["22/tcp", "2222/tcp", "80/tcp", "443/tcp", "2053/tcp", "8443/tcp", "51820/udp"])
        self.assertEqual(calls[-3:], ["default deny incoming", "default allow outgoing", "--force enable"], "сначала разрешаем, потом включаем")
        self.assertIn("51820/udp", out.stdout)
        self.assertEqual(self.state(), "FIREWALL=airlok\n")

    def test_first_run_with_no_firewall_option(self):
        self.run_firewall(first_run=True, AIRLOK_FIREWALL="off")
        self.assertEqual(self.ufw(), [])
        self.assertEqual(self.state(), "FIREWALL=off\n")

    def test_update_does_not_enable_ufw_turned_off_by_human(self):
        for state in (None, "airlok"):
            out = self.run_firewall(first_run=False, state=state)
            self.assertEqual(self.ufw(), [])
            self.assertIn("FIREWALL=off\n", self.state())
            self.assertIn("не включает", out.stdout)
        self.assertIn("LETSENCRYPT=1\n", self.state(), "другие настройки сервера остаются")

    def test_active_ufw_only_gets_ssh_and_web_ports(self):
        self.run_firewall(first_run=False, active=True)
        self.assertEqual(self.ufw(), ["allow 22/tcp", "allow 2222/tcp", "allow 80/tcp", "allow 443/tcp"])
        self.assertEqual(self.state(), "FIREWALL=airlok\n")

    def test_active_ufw_keeps_recorded_state(self):
        self.run_firewall(first_run=False, active=True, state="off")
        self.assertIn("FIREWALL=off\n", self.state())


class DockerDaemonTest(AptCase):
    def setUp(self):
        super().setUp()
        self.path = os.path.join(os.path.dirname(self.bin), "daemon.json")
        self.fake("systemctl", '[[ $1 == show ]] && echo "{ argv[]=/usr/bin/dockerd -H fd:// }"\n'
                  '[[ $1 == restart && -n ${RESTART_FAILS:-} ]] && exit 1\n[[ $1 == reload ]] && touch "$RELOADED"\nexit 0')
        self.fake("docker", 'case "$*" in\n'
                  '*LiveRestoreEnabled*) [[ -n ${LIVE_RESTORE:-} || ( -e $RELOADED && -z ${NO_LIVE_RESTORE:-} ) ]] && echo true || echo false ;;\n'
                  '*Swarm*) echo inactive ;;\n"ps -aq") printf %s "${CONTAINERS:-}" ;;\n"ps -q") printf %s "${RUNNING:-}" ;;\nesac')

    def run_daemon(self, first_run=False, **env):
        funcs = func("docker_daemon", "docker_apply", "live_restore_on").replace("/etc/docker/daemon.json", self.path)
        script = 'source "$1"; eval "$2"; sleep() { :; }; FIRST_RUN=$3; docker_daemon'
        return subprocess.run(["bash", "-c", script, "bash", LIB, funcs, "yes" if first_run else "no"], capture_output=True, text=True,
                              env={**os.environ, "PATH": self.bin + os.pathsep + os.environ["PATH"],
                                   "RELOADED": self.path + ".reloaded", **env})

    def write(self, conf):
        with open(self.path, "w") as f:
            json.dump(conf, f)

    def read(self):
        with open(self.path) as f:
            return json.load(f)

    def systemctl(self):
        return [c for c in self.called() if c.startswith("systemctl ") and not c.startswith("systemctl show")]

    def test_merges_and_restarts_once(self):
        self.write({"data-root": "/mnt/docker"})
        out = self.run_daemon()
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(self.read(), {"data-root": "/mnt/docker", **OURS})
        self.assertIn("systemctl restart docker", self.systemctl())
        os.remove(self.calls)
        self.assertEqual(self.run_daemon().returncode, 0)
        self.assertNotIn("systemctl restart docker", self.systemctl(), "без изменений Docker не перезапускаем")

    def test_clean_server_gets_no_new_privileges(self):
        self.assertEqual(self.run_daemon(first_run=True).returncode, 0)
        self.assertEqual(self.read(), OLD_AIRLOK)

    def test_server_with_containers_does_not(self):
        self.assertEqual(self.run_daemon(first_run=True, CONTAINERS="c0ffee\n").returncode, 0)
        self.assertEqual(self.read(), OURS)

    def test_failed_restart_restores_file(self):
        self.write({"data-root": "/mnt/docker"})
        out = self.run_daemon(RESTART_FAILS="1")
        self.assertEqual(out.returncode, 1)
        self.assertIn("вернули прежний", out.stderr)
        self.assertEqual(self.read(), {"data-root": "/mnt/docker"})

    def test_running_containers_survive_via_live_restore(self):
        self.write({"data-root": "/mnt/docker"})
        self.assertEqual(self.run_daemon(RUNNING="c0ffee\n").returncode, 0)
        self.assertEqual(self.systemctl()[:2], ["systemctl reload docker", "systemctl restart docker"])

    def test_no_restart_when_live_restore_is_off(self):
        self.write({"live-restore": False})
        out = self.run_daemon(RUNNING="c0ffee\n", NO_LIVE_RESTORE="1")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertNotIn("systemctl restart docker", self.systemctl(), "иначе контейнеры остановятся")
        self.assertIn("после перезапуска Docker", out.stdout)

    def test_broken_file_is_left_and_docker_not_restarted(self):
        with open(self.path, "w") as f:
            f.write("{oops")
        out = self.run_daemon()
        self.assertEqual(out.returncode, 0, out.stderr)
        with open(self.path) as f:
            self.assertEqual(f.read(), "{oops")
        self.assertNotIn("systemctl restart docker", self.systemctl())


class DockerPackagesTest(AptCase):
    def setUp(self):
        super().setUp()
        self.fake("dpkg-query", 'for p in ${INSTALLED:-}; do [[ $p == "${@: -1}" ]] && { printf "ii "; exit 0; }; done; exit 1')
        self.fake("apt-cache", 'for p in ${CANDIDATES:-}; do [[ $p == "$2" ]] && { printf "%s:\\n  Candidate: 5:27.3.1-1\\n" "$2"; exit 0; }; done\n'
                  'printf "%s:\\n  Candidate: (none)\\n" "$2"')

    def docker(self, works="compose buildx"):
        self.fake("docker", 'for c in %s; do [[ $1 == "$c" ]] && exit 0; done; exit 1' % works)

    def packages(self, snap=False, **env):
        # Только заглушки: настоящий docker на машине с тестами не должен попасть в выбор.
        os.symlink(shutil.which("dirname"), os.path.join(self.bin, "dirname"))
        snap_docker = os.path.join(os.path.dirname(self.bin), "snap-docker")
        if snap:
            open(snap_docker, "w").close()
        funcs = func("pkg_installed", "apt_candidate", "docker_packages").replace("/snap/bin/docker", snap_docker)
        script = 'source "$1"; eval "$2"; docker_packages; echo "${DOCKER_PKGS[*]:-}|${DOCKER_NOTE}"'
        return subprocess.run([shutil.which("bash"), "-c", script, "bash", LIB, funcs],
                              capture_output=True, text=True, env={**os.environ, "PATH": self.bin, **env})

    def test_clean_server_gets_ubuntu_docker(self):
        self.assertEqual(self.packages().stdout, "docker.io docker-compose-v2 docker-buildx|docker\n")

    def test_docker_io_keeps_getting_compose_and_buildx(self):
        self.docker()
        self.assertEqual(self.packages(INSTALLED="docker.io").stdout, "docker.io docker-compose-v2 docker-buildx|docker\n")

    def test_snap_docker_blocks_adding_docker_io(self):
        out = self.packages(snap=True)
        self.assertEqual(out.returncode, 1)
        self.assertIn("Docker стоит из snap", out.stderr)

    def test_snap_docker_in_path_is_not_taken_for_own_docker(self):
        # Под sudo /snap/bin в PATH: snap-версия отвечает на command -v docker и умеет compose.
        self.docker()
        out = self.packages(snap=True)
        self.assertEqual(out.returncode, 1, out.stdout)
        self.assertIn("Docker стоит из snap", out.stderr)

    def test_snap_next_to_installed_docker_io_does_not_stop_update(self):
        self.docker()
        out = self.packages(snap=True, INSTALLED="docker.io")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(out.stdout.endswith("docker.io docker-compose-v2 docker-buildx|docker\n"))
        self.assertIn("snap", out.stdout)

    def test_docker_ce_is_left_alone(self):
        self.docker()
        self.assertEqual(self.packages(INSTALLED="docker-ce containerd.io").stdout, "|Docker CE оставлен как был\n")

    def test_docker_ce_gets_missing_plugins_from_its_repo(self):
        self.docker(works="")
        out = self.packages(INSTALLED="docker-ce", CANDIDATES="docker-compose-plugin")
        self.assertEqual(out.stdout, "docker-compose-plugin|Docker CE оставлен как был\n", "buildx без кандидата не ставим")

    def test_docker_ce_without_compose_anywhere(self):
        self.docker(works="")
        out = self.packages(INSTALLED="docker-ce")
        self.assertEqual(out.returncode, 1)
        self.assertIn("docker-compose-plugin", out.stderr)

    def test_unknown_docker_without_compose(self):
        self.docker(works="")
        out = self.packages()
        self.assertEqual(out.returncode, 1)
        self.assertIn("Второй Docker рядом ставить не будем", out.stderr)

    def test_unknown_docker_with_compose(self):
        self.docker()
        self.assertEqual(self.packages().stdout, "|Docker оставлен как был\n")


class DefaultSiteTest(AptCase):
    PACKAGE = "server {\n\tlisten 80 default_server;\n\troot /var/www/html;\n}\n"

    def setUp(self):
        super().setUp()
        root = os.path.dirname(self.bin)
        self.nginx = os.path.join(root, "nginx")
        self.www = os.path.join(root, "www")
        os.makedirs(os.path.join(self.nginx, "sites-available"))
        os.makedirs(os.path.join(self.nginx, "sites-enabled"))
        os.makedirs(self.www)
        self.site = os.path.join(self.nginx, "sites-available", "default")
        with open(self.site, "w") as f:
            f.write(self.PACKAGE)
        os.symlink(self.site, os.path.join(self.nginx, "sites-enabled", "default"))
        open(os.path.join(self.www, "index.nginx-debian.html"), "w").close()
        md5 = hashlib.md5(self.PACKAGE.encode()).hexdigest()
        self.fake("dpkg", 'echo "nginx-common: $2"')
        self.fake("dpkg-query", 'printf " /etc/nginx/nginx.conf 0123\\n %s %s\\n"' % (os.path.realpath(self.site), md5))

    def pristine(self):
        funcs = func("default_site_pristine").replace("/etc/nginx", os.path.realpath(self.nginx)).replace("/var/www/html", self.www)
        out = subprocess.run(["bash", "-c", 'eval "$1"; default_site_pristine', "bash", funcs], capture_output=True, text=True,
                             env={**os.environ, "PATH": self.bin + os.pathsep + os.environ["PATH"]})
        return out.returncode == 0

    def test_package_default_is_removable(self):
        self.assertTrue(self.pristine())

    def test_edited_default_stays(self):
        with open(self.site, "a") as f:
            f.write("# VPN fallback\n")
        self.assertFalse(self.pristine())

    def test_own_files_in_www_stay(self):
        open(os.path.join(self.www, "index.html"), "w").close()
        self.assertFalse(self.pristine())


if __name__ == "__main__":
    unittest.main()
