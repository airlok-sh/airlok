"""Остановка службы агента посреди работы: публикация не оставляет сайт лежать, контейнер сборки и
недособранный релиз не переживают её, а у каждой команды есть итог."""

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import unittest

try:
    import test_agent
except ImportError:
    from tests import test_agent

RECIPES = os.path.join(os.path.dirname(__file__), "..", "recipes")
LIB = os.path.join(RECIPES, "lib.sh")


def read(name):
    with open(os.path.join(RECIPES, name), encoding="utf-8") as f:
        return f.read()


DEPLOY = read("app-deploy.sh")
LIB_SRC = read("lib.sh")
AGENT = read("agent.sh")
REMOVE = read("app-remove.sh")
PAYLOAD_PY = re.search(r"PAYLOAD_PY='\n(.*?)\n'\n", AGENT, re.S).group(1)
CID = "0192e000-0000-7000-8000-000000000001"
CID2 = "0192e000-0000-7000-8000-000000000002"


def func(src, name):
    return (re.search(r"^%s\(\) \{ .*\}$" % name, src, re.M)
            or re.search(r"^%s\(\) [{(]\n.*?^[})]$" % name, src, re.S | re.M)).group(0) + "\n"


def const(src, name):
    return re.search(r"^%s=.*$" % name, src, re.M).group(0) + "\n"


def server_shell():
    return subprocess.run(["bash", "-c", "((BASH_VERSINFO[0] >= 4))"], capture_output=True).returncode == 0


class Fakes:
    """Каталог с подделками команд: имя → текст скрипта."""

    def __init__(self, t, scripts):
        self.bin = os.path.join(t, "bin")
        os.makedirs(self.bin, exist_ok=True)
        for name, text in scripts.items():
            path = os.path.join(self.bin, name)
            with open(path, "w") as f:
                f.write("#!/bin/sh\n" + text)
            os.chmod(path, 0o755)

    def env(self, t, **extra):
        return {**os.environ, "PATH": self.bin + ":" + os.environ["PATH"], "T": t, **extra}


def lines(path):
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return f.read().splitlines()


def wait_file(path, seconds=10):
    deadline = time.monotonic() + seconds
    while not os.path.exists(path):
        if time.monotonic() > deadline:
            raise AssertionError("не дождались " + path)
        time.sleep(0.05)


class BuildStopTest(unittest.TestCase):
    """Сборку остановили: контейнер убирается сразу, а не через минуты, и релиз без сборки не остаётся."""

    # docker run не выходит на TERM, пока жив контейнер (там sh с PID 1 сигнал не замечает); docker rm -f
    # убирает контейнер, и тогда выходит и клиент.
    DOCKER = """
echo "$*" >>"$T/docker"
case $1 in
run)
	trap '' TERM
	echo $$ >"$T/client"
	: >"$T/started"
	i=0
	while [ $i -lt 300 ]; do sleep 0.1; i=$((i + 1)); done
	;;
rm) [ -f "$T/client" ] && kill -9 "$(cat "$T/client")" 2>/dev/null; rm -f "$T/client" ;;
esac
exit 0
"""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.t = tmp.name
        self.fakes = Fakes(self.t, {"docker": self.DOCKER})
        self.release = os.path.join(self.t, "app", "releases", "r1")
        os.makedirs(self.release)
        open(self.release + ".building", "w").close()

    def start(self, phase):
        funcs = "".join(func(DEPLOY, n) for n in ("build_container", "drop_build", "on_term"))
        script = f"""
source "$1"
((BASH_VERSINFO[0] >= 4)) || set +u
{funcs}
JOBS_DIR=$T/jobs AIRLOK_DETACHED=1 PUBLISH_UNIT=airlok-publish-shop APP_NAME=shop APP_DIR=$T/app RELEASE=$T/app/releases/r1
INTERRUPTED=0 BUILD_ENV=() BUILD_PARENT=() BUILD_CACHE="" BUILD_MEM=1024 BUILD_MEM_SWAP=2048 BUILD_UID=1 BUILD_GID=1 BUILD_IMAGE=node
mkdir -p "$JOBS_DIR"
job_phase "$PUBLISH_UNIT" {phase}
trap 'rc=$?; ((rc == 0)) || drop_build; echo "exit $rc INTERRUPTED=$INTERRUPTED" >"$T/exit"' EXIT
trap on_term TERM INT HUP
build_container "npm ci" "$T/log"
"""
        return subprocess.Popen(["bash", "-c", script, "bash", LIB], env=self.fakes.env(self.t),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_term_stops_build_at_once(self):
        proc = self.start("build")
        wait_file(os.path.join(self.t, "started"))
        started = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=10)
        self.assertLess(time.monotonic() - started, 5, "сборка не ждёт, пока контейнер доработает")
        self.assertEqual(proc.returncode, 143)
        calls = lines(os.path.join(self.t, "docker"))
        self.assertEqual(calls[0], "rm -f airlok-build-shop", "контейнер прошлой оборванной сборки — до новой")
        self.assertTrue(calls[1].startswith("run --rm --name airlok-build-shop"))
        self.assertEqual(calls[2:], ["rm -f airlok-build-shop"], "контейнер сборки убран")
        self.assertFalse(os.path.exists(self.release), "недособранный релиз убран")
        self.assertFalse(os.path.exists(self.release + ".building"))
        self.assertEqual(lines(os.path.join(self.t, "exit")), ["exit 143 INTERRUPTED=1"])


class SwitchStopTest(unittest.TestCase):
    """Сигнал остановки посреди смены версии не обрывает её: служба публикации доводит её до конца."""

    def run_phase(self, phase):
        with tempfile.TemporaryDirectory() as t:
            script = f"""
source "$1"
{func(DEPLOY, "on_term")}
JOBS_DIR=$T/jobs AIRLOK_DETACHED=1 PUBLISH_UNIT=airlok-publish-shop INTERRUPTED=0
mkdir -p "$JOBS_DIR"
job_phase "$PUBLISH_UNIT" {phase}
trap on_term TERM
: >"$T/ready"
/bin/sleep 1.5
echo "контейнер сменён"
"""
            proc = subprocess.Popen(["bash", "-c", script, "bash", LIB], env={**os.environ, "T": t},
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            wait_file(os.path.join(t, "ready"))
            proc.send_signal(signal.SIGTERM)
            out, _ = proc.communicate(timeout=10)
            return proc.returncode, out

    def test_finish_phase_completes(self):
        rc, out = self.run_phase("finish")
        self.assertEqual(rc, 0)
        self.assertIn("доводим её до конца", out)
        self.assertIn("контейнер сменён", out)

    def test_build_phase_stops(self):
        rc, out = self.run_phase("build")
        self.assertEqual(rc, 143)
        self.assertNotIn("контейнер сменён", out)


class DetachTest(unittest.TestCase):
    """detach: рецепт идёт службой systemd; прервали ожидание — служба останавливается, если не доводит дело."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.t = tmp.name
        os.makedirs(os.path.join(self.t, "systemd"))
        self.fakes = Fakes(self.t, {
            "systemd-run": 'printf "%s\\n" "$@" >"$T/run-args"\n: >"$T/client"\n'
                           '[ -e "$T/slow" ] && exec /bin/sleep 5\nexit "$(cat "$T/rc" 2>/dev/null || echo 0)"\n',
            "systemctl": 'echo "$*" >>"$T/systemctl"\n[ "$1" = is-active ] && [ -e "$T/active" ] && exit 0\n[ "$1" = is-active ] && exit 3\nexit 0\n',
        })
        self.addCleanup(lambda: subprocess.run(["pkill", "-f", "sleep 5"], capture_output=True))

    def start(self, keep="job_finishing"):
        detach = func(LIB_SRC, "detach").replace("/run/systemd/system", os.path.join(self.t, "systemd"))
        script = f"""
source "$1"
{detach}
JOBS_DIR=$T/jobs
AIRLOK_APP_NAME=shop AIRLOK_RELEASE_TITLE="Новая главная" AIRLOK_GITHUB_TOKEN=secret AIRLOK_APP_DOMAIN="" AIRLOK_JOB_ID=cmd-1
export AIRLOK_APP_NAME AIRLOK_RELEASE_TITLE AIRLOK_GITHUB_TOKEN AIRLOK_APP_DOMAIN AIRLOK_JOB_ID
detach airlok-publish-shop {keep} /opt/airlok/recipes/app-deploy.sh /tmp/src || echo "без systemd"
"""
        return subprocess.Popen(["bash", "-c", script, "bash", LIB], env=self.fakes.env(self.t),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

    def test_runs_as_unit_and_passes_exit_code(self):
        with open(os.path.join(self.t, "rc"), "w") as f:
            f.write("7")
        proc = self.start()
        proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 7, "итог службы — итог рецепта")
        args = lines(os.path.join(self.t, "run-args"))
        self.assertIn("--unit=airlok-publish-shop", args)
        self.assertIn("--pipe", args)
        self.assertIn("--setenv=AIRLOK_DETACHED=1", args)
        self.assertIn("--setenv=AIRLOK_APP_NAME=shop", args)
        self.assertIn("--setenv=AIRLOK_RELEASE_TITLE=Новая главная", args)
        self.assertFalse(any("TOKEN" in a for a in args), "окружение службы видно всем — токена там нет")
        self.assertEqual(args[args.index("--") + 1:], ["/opt/airlok/recipes/app-deploy.sh", "/tmp/src"])
        self.assertIn("--setenv=AIRLOK_EMPTY=AIRLOK_APP_DOMAIN", args, "пустую переменную systemd не передал бы")
        self.assertEqual(lines(os.path.join(self.t, "jobs", "airlok-publish-shop.owner")), ["cmd-1"], "чья служба — для итога после перезапуска агента")

    def test_empty_variables_restored_in_unit(self):
        out = subprocess.run(["bash", "-c", 'source "$1"; echo "domain=[${AIRLOK_APP_DOMAIN+set}] marker=[${AIRLOK_EMPTY-}]"', "bash", LIB],
                             env={**os.environ, "AIRLOK_EMPTY": "AIRLOK_APP_DOMAIN,PATH"}, capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "domain=[set] marker=[]", "свой домен убрали в дашборде — в службе он тоже пустой")

    def interrupt(self, phase="", **kw):
        open(os.path.join(self.t, "slow"), "w").close()
        proc = self.start(**kw)
        wait_file(os.path.join(self.t, "client"))
        if phase:
            # Шаг отмечает уже сама служба: detach перед её запуском стирает прежние отметки.
            with open(os.path.join(self.t, "jobs", "airlok-publish-shop.phase"), "w") as f:
                f.write(phase + "\n")
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 143)
        return lines(os.path.join(self.t, "systemctl"))

    def test_interrupted_before_switch_stops_unit(self):
        self.assertIn("stop --no-block airlok-publish-shop", self.interrupt(phase="build"))

    def test_interrupted_during_switch_keeps_unit(self):
        self.assertNotIn("stop --no-block airlok-publish-shop", self.interrupt(phase="finish"))

    def test_restart_is_always_finished(self):
        self.assertNotIn("stop --no-block airlok-publish-shop", self.interrupt(keep="true"))

    def test_busy_unit(self):
        open(os.path.join(self.t, "active"), "w").close()
        proc = self.start()
        _, err = proc.communicate(timeout=10)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ещё не закончил", err)
        self.assertFalse(os.path.exists(os.path.join(self.t, "run-args")))

    def test_inside_unit_runs_inline(self):
        script = 'source "$1"\n' + func(LIB_SRC, "detach") + 'AIRLOK_DETACHED=1; detach u true echo x || echo inline'
        out = subprocess.run(["bash", "-c", script, "bash", LIB], env=self.fakes.env(self.t), capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "inline")


class ReleaseCleanupTest(unittest.TestCase):
    def test_unfinished_releases(self):
        with tempfile.TemporaryDirectory() as t:
            rel = os.path.join(t, "app", "releases")
            for name in ("old", "cur", "broken", "built"):
                os.makedirs(os.path.join(rel, name))
            for name in ("cur", "broken"):
                open(os.path.join(rel, name + ".building"), "w").close()
            os.symlink(os.path.join(rel, "cur"), os.path.join(t, "app", "current"))
            script = func(DEPLOY, "drop_unfinished") + 'APP_DIR=$1/app; drop_unfinished'
            subprocess.run(["bash", "-c", script, "bash", t], check=True)
            self.assertEqual(sorted(os.listdir(rel)), ["built", "cur", "old"], "работающий релиз не трогаем, даже помеченный")


class AgentStopTest(unittest.TestCase):
    """Итог команды, которую оборвала остановка службы агента."""

    def run_sh(self, body, active=False):
        with tempfile.TemporaryDirectory() as t:
            funcs = "".join(func(AGENT, n) for n in ("stop_current", "sent_already", "job_pending", "resume_later", "resume_jobs"))
            script = f"""
set -u
T={t}
RUN_DIR=$T STATE_DIR=$T RESUME=$T/resume JOBS_DIR=$T/jobs DEPLOY_TIMEOUT=60 CUR_ID="" CUR_KIND="" CUR_APP="" CUR_LOG=""
{const(AGENT, "STOP_MESSAGE")}
send_result() {{ echo "result $1 $2 msg=[$3] log=${{4:-}} stages=${{5:-}} stop=${{LONG_STOP:-0}} kind=${{CMD_KIND:-}}" >>$T/events; echo "$1" >>$T/sent; }}
refuse() {{ echo "refuse $1 $2 $3" >>$T/events; }}
systemctl() {{ echo "systemctl $*" >>$T/events; {"return 0" if active else "return 3"}; }}
sleep() {{ :; }}
mkdir -p $T/jobs
{funcs}
{body}
cat $T/events 2>/dev/null; echo "resume=[$(cat $T/resume 2>/dev/null)]"
"""
            out = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30)
            self.assertEqual(out.returncode, 0, out.stderr)
            return [line for line in out.stdout.splitlines() if not line.startswith("команда")]

    def test_quick_command_gets_interrupted_result(self):
        out = self.run_sh(f"CUR_ID={CID} CUR_KIND=remove CUR_APP=shop CUR_LOG=/l; stop_current")
        self.assertIn(f"result {CID} failed msg=[] log=/l stages= stop=1 kind=remove", out)

    def test_restart_finishing_in_its_unit_is_resumed(self):
        out = self.run_sh(f"echo {CID} >$T/jobs/airlok-restart-shop.owner; CUR_ID={CID} CUR_KIND=restart CUR_APP=shop CUR_LOG=/l; stop_current", active=True)
        self.assertFalse(any(line.startswith("result") for line in out), "итог — после перезапуска, настоящий")
        self.assertIn(f"resume=[{CID} restart airlok-restart-shop /l]", out)

    def test_restart_that_just_finished_keeps_its_result(self):
        """Служба успела закончить сама, пока агент останавливался: итог — её, а не «прервано»."""
        body = f'echo {CID} >$T/jobs/airlok-restart-shop.owner; printf "success\\n\\n" >$T/jobs/airlok-restart-shop.outcome; CUR_ID={CID} CUR_KIND=restart CUR_APP=shop CUR_LOG=/l; stop_current'
        out = self.run_sh(body)
        self.assertFalse(any(line.startswith("result") for line in out), out)
        self.assertIn(f"resume=[{CID} restart airlok-restart-shop /l]", out)
        body = f'echo {CID} >$T/jobs/airlok-restart-shop.owner; printf "interrupted\\n\\n" >$T/jobs/airlok-restart-shop.outcome; CUR_ID={CID} CUR_KIND=restart CUR_APP=shop CUR_LOG=/l; stop_current'
        self.assertIn(f"result {CID} failed msg=[] log=/l stages= stop=1 kind=restart", self.run_sh(body), "оборванную службу ждать незачем")

    def test_other_commands_unit_is_not_ours(self):
        """Итог прошлой публикации или идущая публикация из очереди webhook — не итог этой команды:
        она до своей службы не дошла, поэтому «прервано»."""
        body = (f'echo {CID2} >$T/jobs/airlok-restart-shop.owner; printf "success\\n\\n" >$T/jobs/airlok-restart-shop.outcome; '
                f'CUR_ID={CID} CUR_KIND=restart CUR_APP=shop CUR_LOG=/l; stop_current')
        self.assertIn(f"result {CID} failed msg=[] log=/l stages= stop=1 kind=restart", self.run_sh(body))
        body = f': >$T/jobs/airlok-restart-shop.owner; CUR_ID={CID} CUR_KIND=restart CUR_APP=shop CUR_LOG=/l; stop_current'
        out = self.run_sh(body, active=True)
        self.assertIn(f"result {CID} failed msg=[] log=/l stages= stop=1 kind=restart", out, "служба из очереди webhook")
        self.assertIn("resume=[]", out)

    def test_special_kinds(self):
        out = self.run_sh(f"CUR_ID={CID} CUR_KIND=db_query; stop_current")
        self.assertTrue(any(line.startswith(f"refuse {CID} db_query прервано") for line in out), "запрос к базе ждёт ответа на своей ручке")
        out = self.run_sh(f"CUR_ID={CID} CUR_KIND=os_upgrade; stop_current")
        self.assertFalse(any(line.startswith(("result", "refuse")) for line in out), "итог обновления системы отправит resume_os_job")

    def test_result_already_sent(self):
        out = self.run_sh(f"echo {CID} >$T/sent; CUR_ID={CID} CUR_KIND=update; stop_current")
        self.assertFalse(any(line.startswith("result") for line in out), "второй итог, «прервано», не нужен")

    def test_resume_sends_real_result(self):
        body = (f'printf "success\\n\\n" >$T/jobs/airlok-publish-shop.outcome; resume_later {CID} deploy airlok-publish-shop /log; '
                f'resume_later {CID2} restart airlok-restart-blog /side; rm -f $T/events; resume_jobs')
        out = self.run_sh(body)
        results = [line for line in out if line.startswith("result")]
        self.assertEqual(len(results), 2, out)
        self.assertTrue(results[0].startswith(f"result {CID} success msg=[] log=/log stages="), results[0])
        self.assertTrue(results[0].endswith("command.stages stop=0 kind=deploy"))
        self.assertTrue(results[1].startswith(f"result {CID2} failed msg=[прервано"), "служба кончилась без итога — её оборвали")
        self.assertIn("resume=[]", out)


@unittest.skipUnless(server_shell() and shutil.which("flock") and shutil.which("setsid"), "нужны bash 4+, flock и setsid, как на сервере")
class AgentHandleStopTest(unittest.TestCase):
    """Настоящий handle и stop_agent: TERM разом всей службе, как от systemd."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.t = tmp.name
        with open(os.path.join(self.t, "strict.py"), "w") as f:
            f.write("import sys\nsys.exit(0)\n")

    def run_agent(self, cmd, stubs, recipe):
        with open(os.path.join(self.t, "cmd.json"), "w") as f:
            json.dump(cmd, f)
        with open(os.path.join(self.t, "recipe.sh"), "w") as f:
            f.write("#!/bin/bash\n" + recipe)
        os.chmod(os.path.join(self.t, "recipe.sh"), 0o755)
        names = test_agent.HANDLE_FUNCS + ("send_result", "result_file", "uptime_cs", "result_age", "flush_results", "stream_progress",
                                           "run_deploy", "run_restart", "job_pending", "refuse")
        script = f"""
source "$1"
set +e
{test_agent.agent_globals(self.t)}
T={self.t}
RECIPES_DIR=$T POLICY_CONF=$T/policy STOP_GRACE=2
audit() {{ :; }}
dashboard_fetch() {{ [[ $2 == */result ]] && {{ echo "${{2}}" >>$T/posts; cp "${{4#@}}" "$T/result-$(wc -l <$T/posts | tr -d ' ').json"; }}; echo 204; }}
{stubs}
{test_agent.agent_funcs(*names)}
trap stop_agent TERM
handle $T/cmd.json
wait_long
"""
        proc = subprocess.Popen(["bash", "-c", script, "bash", LIB], env={**os.environ, "PARSE_PY": test_agent.PARSE_PY,
                                "PAYLOAD_PY": PAYLOAD_PY}, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        wait_file(os.path.join(self.t, "started"))
        # Как systemd: TERM всем процессам службы разом, и тем, кого timeout увёл в свою группу.
        subprocess.run(["pkill", "-TERM", "-s", str(proc.pid)], check=True)
        out, err = proc.communicate(timeout=30)
        bodies = []
        for i in range(1, len(lines(os.path.join(self.t, "posts"))) + 1):
            with open(os.path.join(self.t, "result-%d.json" % i), encoding="utf-8") as f:
                bodies.append(json.load(f))
        return lines(os.path.join(self.t, "posts")), bodies, out + err

    def test_quick_restart_interrupted(self):
        stubs = 'RECIPES_DIR=$T; ln -sf $T/recipe.sh $T/app-restart.sh\n'
        posts, bodies, out = self.run_agent({"id": CID, "kind": "restart", "restart": {"app": "shop"}}, stubs,
                                            ': >"$(dirname "$0")/started"\nexec sleep 30\n')
        self.assertEqual(posts, [f"/agent/v1/commands/{CID}/result"], out)
        self.assertEqual(bodies[0]["status"], "failed")
        self.assertIn("прервано", bodies[0]["message"])

    def test_deploy_interrupted_once_and_no_stray_sleep(self):
        stubs = 'ln -sf $T/recipe.sh $T/github-deploy.sh\nSTOP_GRACE=37\n'
        recipe = ': >"$(dirname "$0")/started"\ntrap "sleep 0.5; exit 1" TERM\nsleep 30 & wait\n'
        posts, bodies, out = self.run_agent({"id": CID, "kind": "deploy", "deploy": {"app": "shop", "repo": "acme/shop", "sha": "a" * 40}},
                                            stubs, recipe)
        self.assertEqual(posts, [f"/agent/v1/commands/{CID}/result"], "итог «прервано» — один раз: " + out)
        self.assertIn("прервано", bodies[0]["message"])
        time.sleep(0.5)
        stray = subprocess.run(["pgrep", "-f", "sleep 37"], capture_output=True, text=True).stdout.strip()
        self.assertEqual(stray, "", "sleep убийцы рецепта не остаётся висеть в службе")


class RemoveLeftoversTest(unittest.TestCase):
    """app-remove.sh: журналы nginx, по которым приложение снова появилось бы в дашборде, и копия репозитория."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.t = tmp.name
        for d in ("apps/shop/shared", "apps/blog/shared", "git", "github", "log"):
            os.makedirs(os.path.join(self.t, d))
        # rm без --one-file-system: его нет в BSD rm.
        self.fakes = Fakes(self.t, {"rm": 'for a; do [ "$a" = --one-file-system ] || set -- "$@" "$a"; shift; done\nexec /bin/rm "$@"\n'})

    def run_fn(self, name, body):
        text = func(REMOVE, name)
        for old, new in (("/var/lib/airlok/git", "$T/git"), ("/etc/airlok/github", "$T/github"), ("/var/log/nginx", "$T/log"),
                         ("/etc/nginx", "$T/nginx"), ("/etc/ssl/airlok", "$T/ssl")):
            text = text.replace(old, new)
        script = f"""
source "$1"
T={self.t}
APPS_ROOT=$T/apps NAME=shop APP_DIR=$T/apps/shop
{const(REMOVE, "DOMAIN_RE")}nginx_lock() {{ :; }}
nginx_unlock() {{ :; }}
{text}
{body}
"""
        out = subprocess.run(["bash", "-c", script, "bash", LIB], capture_output=True, text=True, env=self.fakes.env(self.t))
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout

    def touch(self, *names):
        for name in names:
            path = os.path.join(self.t, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write("acme__shop\n" if name.endswith("git.slug") else "")

    def test_logs_removed(self):
        self.touch("log/airlok-shop.access.log", "log/airlok-shop.access.log.1", "log/airlok-shop.visits.log.2.gz",
                   "log/airlok-shop-2.access.log", "log/airlok-shopx.visits.log")
        self.run_fn("remove_site", "remove_site")
        self.assertEqual(sorted(os.listdir(os.path.join(self.t, "log"))), ["airlok-shop-2.access.log", "airlok-shopx.visits.log"])

    def test_git_cache(self):
        os.makedirs(os.path.join(self.t, "git", "acme__shop.git"))
        self.touch("apps/shop/shared/git.slug", "apps/blog/shared/git.slug")
        self.run_fn("remove_git_cache", "remove_git_cache")
        self.assertTrue(os.path.isdir(os.path.join(self.t, "git", "acme__shop.git")), "из того же репозитория публикуется blog")
        os.remove(os.path.join(self.t, "apps", "blog", "shared", "git.slug"))
        os.makedirs(os.path.join(self.t, "github", "acme__shop"))
        self.run_fn("remove_git_cache", "remove_git_cache")
        self.assertTrue(os.path.isdir(os.path.join(self.t, "git", "acme__shop.git")), "репозиторий настроен на сервере руками")
        os.rmdir(os.path.join(self.t, "github", "acme__shop"))
        self.run_fn("remove_git_cache", "remove_git_cache")
        self.assertFalse(os.path.exists(os.path.join(self.t, "git", "acme__shop.git")))

    def test_unknown_repo_left(self):
        os.makedirs(os.path.join(self.t, "git", "acme__shop.git"))
        self.run_fn("remove_git_cache", "remove_git_cache")
        self.assertTrue(os.path.isdir(os.path.join(self.t, "git", "acme__shop.git")), "приложение старой публикации репозитория не помнит")


if __name__ == "__main__":
    unittest.main()
