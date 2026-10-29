"""Задачи по расписанию и команда перед запуском на своём сервере: python3 -m unittest discover tests

Расписание разбирается по тем же правилам, что у хостинга airlok (internal/cronexpr), и
переводится в OnCalendar таймеров systemd.
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "recipes"))

import cron  # noqa: E402

MSK = " Europe/Moscow"


class ScheduleTest(unittest.TestCase):
    def test_errors(self):
        for bad in ("", "* * * *", "60 * * * *", "* 24 * * *", "* * 0 * *", "*/0 * * * *", "5-1 * * * *",
                    "a * * * *", "* * * * 8", "¹ * * * *", None):
            with self.assertRaises(cron.CronError, msg=bad):
                cron.parse_schedule(bad)

    def test_calendar(self):
        cases = {
            "*/15 * * * *": ["*-*-* *:0,15,30,45:00"],
            "0 9 * * 1-5": ["Mon,Tue,Wed,Thu,Fri *-*-* 9:0:00"],
            "0 0 * * 7": ["Sun *-*-* 0:0:00"],
            "30 2 1,15 * *": ["*-*-1,15 2:30:00"],
            # Пятница 13-го в cron — любое 13-е и любая пятница.
            "0 0 13 * 5": ["*-*-13 0:0:00", "Fri *-*-* 0:0:00"],
            "@daily": ["*-*-* 0:0:00"],
            "5/20 * * * *": ["*-*-* *:5,25,45:00"],
            "* * * * *": ["*-*-* *:*:00"],
            "0 12 */1 6 *": ["*-6-* 12:0:00"],
        }
        for expr, want in cases.items():
            self.assertEqual(cron.on_calendar(cron.parse_schedule(expr)), [w + MSK for w in want], expr)

    def test_jobs(self):
        jobs = cron.parse_jobs([{"schedule": " 0 9 * * * ", "command": "python digest.py"},
                                {"name": "чистка", "schedule": "@hourly", "command": "node clean.js", "timeout_min": 30}])
        self.assertEqual(jobs, [
            {"name": "задача 1", "schedule": "0 9 * * *", "command": "python digest.py", "timeout_min": 10},
            {"name": "чистка", "schedule": "@hourly", "command": "node clean.js", "timeout_min": 30},
        ])
        self.assertEqual(cron.parse_jobs(None), [])
        self.assertEqual(cron.parse_jobs([]), [])
        job = {"schedule": "* * * * *", "command": "x"}
        for bad in ({"a": 1}, [job] * 11, [{**job, "timeout_min": 61}], [{**job, "timeout_min": True}],
                    [{**job, "command": "a\nb"}], [{**job, "command": " "}], [{**job, "user": "root"}],
                    [{**job, "name": "a" * 65}], [{**job, "schedule": "* * *"}], ["0 9 * * *"]):
            with self.assertRaises(cron.CronError, msg=bad):
                cron.parse_jobs(bad)
        with self.assertRaisesRegex(cron.CronError, "«отчёт».*минута"):
            cron.parse_jobs([{"name": "отчёт", "schedule": "61 * * * *", "command": "x"}])


class Server:
    """Каталоги приложений и юнитов во временной папке, вызовы systemctl записываются."""

    def __init__(self, test):
        root = tempfile.mkdtemp()
        self.units = os.path.join(root, "units")
        self.apps = os.path.join(root, "apps")
        os.makedirs(self.units)
        self.calls = []
        for patch in (mock.patch.object(cron, "UNIT_DIR", self.units), mock.patch.object(cron, "APPS_ROOT", self.apps),
                      mock.patch.object(cron.subprocess, "run", self.run)):
            patch.start()
            test.addCleanup(patch.stop)

    def run(self, args, **kw):
        self.calls.append(args)
        return subprocess.CompletedProcess(args, 1 if "fail" in args else 0, "", "Failed to enable unit")

    def jobs(self, app, jobs):
        os.makedirs(os.path.join(self.apps, app, "shared"), exist_ok=True)
        with open(os.path.join(self.apps, app, "shared", "cron.json"), "w", encoding="utf-8") as f:
            json.dump(jobs, f)

    def timers(self):
        return sorted(n for n in os.listdir(self.units) if n.endswith(".timer"))

    def read(self, name):
        with open(os.path.join(self.units, name), encoding="utf-8") as f:
            return f.read()


class SyncTest(unittest.TestCase):
    def test_timers_follow_manifest(self):
        s = Server(self)
        s.jobs("shop", [{"schedule": "0 9 * * *", "command": "a"}, {"schedule": "0 0 13 * 5", "command": "b"}])
        s.jobs("shop-2", [{"schedule": "@daily", "command": "c"}])
        self.assertEqual(cron.sync("shop-2"), 1)
        s.calls.clear()

        self.assertEqual(cron.sync("shop"), 2)
        self.assertEqual(s.timers(), ["airlok-cron@shop-2_0.timer", "airlok-cron@shop_0.timer", "airlok-cron@shop_1.timer"])
        self.assertIn("OnCalendar=*-*-13 0:0:00 Europe/Moscow\nOnCalendar=Fri *-*-* 0:0:00 Europe/Moscow\n",
                      s.read("airlok-cron@shop_1.timer"))
        self.assertIn("cron.py run %i", s.read("airlok-cron@.service"))
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(s.units, "airlok-cron@shop_0.timer")).st_mode), 0o644)
        self.assertEqual(s.calls, [
            ["systemctl", "daemon-reload"],
            ["systemctl", "enable", "--now", "airlok-cron@shop_0.timer", "airlok-cron@shop_1.timer"],
            ["systemctl", "restart", "airlok-cron@shop_0.timer", "airlok-cron@shop_1.timer"],
        ])

        s.calls.clear()
        cron.sync("shop")
        self.assertEqual(s.calls, [["systemctl", "enable", "--now", "airlok-cron@shop_0.timer", "airlok-cron@shop_1.timer"]],
                         "ничего не изменилось — таймеры не перезапускаются")

        s.calls.clear()
        s.jobs("shop", [{"schedule": "30 9 * * *", "command": "a"}])
        cron.sync("shop")
        self.assertEqual(s.calls, [
            ["systemctl", "disable", "--now", "airlok-cron@shop_1.timer"],
            ["systemctl", "daemon-reload"],
            ["systemctl", "enable", "--now", "airlok-cron@shop_0.timer"],
            ["systemctl", "restart", "airlok-cron@shop_0.timer"],
        ])
        self.assertEqual(s.timers(), ["airlok-cron@shop-2_0.timer", "airlok-cron@shop_0.timer"],
                         "таймеры приложения shop-2 не задеты")

        s.calls.clear()
        s.jobs("shop", [])
        self.assertEqual(cron.sync("shop"), 0)
        self.assertEqual(s.timers(), ["airlok-cron@shop-2_0.timer"])
        self.assertEqual(s.calls[0], ["systemctl", "disable", "--now", "airlok-cron@shop_0.timer"])

    def test_without_jobs_file(self):
        s = Server(self)
        self.assertEqual(cron.sync("blog"), 0)
        self.assertEqual(s.timers(), [])

    def test_systemctl_error_is_explained(self):
        Server(self)
        with self.assertRaisesRegex(cron.CronError, "systemctl fail: Failed to enable unit"):
            cron.systemctl("fail")


class RunTest(unittest.TestCase):
    def setUp(self):
        self.server = Server(self)
        self.server.jobs("shop", [{"name": "дайджест", "schedule": "0 9 * * *", "command": "python digest.py", "timeout_min": 2}])
        conf = os.path.join(self.server.apps, "dashboard.env")
        with open(conf, "w", encoding="utf-8") as f:
            f.write("AIRLOK_API_URL=https://app.airlok.ru\n")
        self.spooled = []
        for patch in (mock.patch.object(cron, "DASHBOARD_CONF", conf),
                      mock.patch.object(cron, "spool", lambda app, entries: self.spooled.append((app, entries))),
                      mock.patch.object(cron, "deployed", lambda app: True),
                      mock.patch.object(cron, "wait_deploy", lambda app, limit: True),
                      mock.patch("builtins.print")):
            patch.start()
            self.addCleanup(patch.stop)

    def run_with(self, result):
        calls = []

        def oneoff(app, container, command, timeout):
            calls.append((app, container, command, timeout))
            return result

        with mock.patch.object(cron, "oneoff", oneoff):
            return cron.run("shop_0"), calls

    def test_success_goes_to_app_logs(self):
        code, calls = self.run_with((0, [("2026-10-29T09:00:01Z", "отправлено 3 письма")], 0))
        self.assertEqual(code, 0)
        self.assertEqual(calls, [("shop", "airlok-cron-shop_0", "python digest.py", 120)])
        app, entries = self.spooled[0]
        self.assertEqual(app, "shop")
        self.assertEqual([(e["stream"], e["line"]) for e in entries][:2], [
            ("system", "Задача «дайджест» запущена (0 9 * * *)"),
            ("stdout", "[дайджест] отправлено 3 письма"),
        ])
        self.assertRegex(entries[-1]["line"], r"^Задача «дайджест» выполнена за \d+ с$")

    def test_failure_and_timeout(self):
        code, _ = self.run_with((2, [], 0))
        self.assertEqual(code, 1, "systemd отметит запуск как неудачный")
        self.assertRegex(self.spooled[-1][1][-1]["line"], r"завершилась с ошибкой \(код 2\)")
        code, _ = self.run_with((None, [("2026-10-29T09:00:01Z", "a")], 40))
        self.assertEqual(code, 1)
        entries = self.spooled[-1][1]
        self.assertEqual(entries[-1]["line"], "Задача «дайджест» прервана: дольше 2 мин")
        self.assertEqual(entries[1]["line"], "[дайджест] первые строки вывода пропущены: 40")

    def test_late_start_is_skipped(self):
        at = lambda d, h, m, s: cron.datetime.datetime(2026, 10, d, h, m, s, tzinfo=cron.MSK)  # noqa: E731
        calls = []

        def oneoff(*args):
            calls.append(args)
            return 0, [], 0

        with mock.patch.object(cron, "oneoff", oneoff):
            self.assertEqual(cron.run("shop_0", at(29, 9, 0, 1)), 0)
            self.assertEqual(len(calls), 1, "таймер в начале минуты по расписанию")
            for late in (at(29, 9, 0, 40), at(29, 9, 1, 0)):
                self.assertEqual(cron.run("shop_0", late), 0)
            self.assertEqual(len(calls), 1, "systemd догоняет минуту, пропущенную за долгим прошлым запуском, — как на хостинге, пропускаем")
            cron.run("shop_0")
            self.assertEqual(len(calls), 2, "руками из терминала — сразу")

    def test_matches_like_hosting(self):
        # Те же случаи, что в internal/cronexpr/cronexpr_test.go.
        at = lambda mo, d, h, m: cron.datetime.datetime(2026, mo, d, h, m, tzinfo=cron.MSK)  # noqa: E731
        cases = [("*/15 * * * *", at(10, 21, 10, 45), True), ("*/15 * * * *", at(10, 21, 10, 46), False),
                 ("0 9 * * 1-5", at(10, 21, 9, 0), True), ("0 9 * * 1-5", at(10, 25, 9, 0), False),
                 ("0 0 * * 7", at(10, 25, 0, 0), True), ("30 2 1,15 * *", at(10, 15, 2, 30), True),
                 ("0 0 13 * 5", at(11, 13, 0, 0), True), ("0 0 13 * 5", at(11, 20, 0, 0), True),
                 ("0 0 13 * 5", at(11, 21, 0, 0), False), ("@daily", at(10, 21, 0, 0), True),
                 ("5/20 * * * *", at(10, 21, 3, 45), True)]
        for expr, t, want in cases:
            self.assertEqual(cron.matches(cron.parse_schedule(expr), t), want, f"{expr} {t}")

    def test_jobs_are_reread_after_deploy(self):
        def deploy(app, limit):
            self.server.jobs("shop", [{"name": "дайджест", "schedule": "0 9 * * *", "command": "python digest2.py"}])
            return True

        with mock.patch.object(cron, "wait_deploy", deploy):
            _, calls = self.run_with((0, [], 0))
        self.assertEqual(calls[0][2], "python digest2.py", "команда из выкаченной версии")

        def removed(app, limit):
            self.server.jobs("shop", [{"name": "другая", "schedule": "0 9 * * *", "command": "x"}])
            return True

        with mock.patch.object(cron, "wait_deploy", removed):
            code, calls = self.run_with((0, [], 0))
        self.assertEqual((code, calls), (0, []), "выкатка, которую ждали, убрала задачу")

    def test_removed_job_and_bad_instance(self):
        self.server.jobs("shop", [])
        code, calls = self.run_with((0, [], 0))
        self.assertEqual((code, calls), (0, []), "задачу убрали из airlok.yml — не запускаем")
        for bad in ("shop", "shop_x", "../etc_0", "Shop_0"):
            with self.assertRaises(cron.CronError, msg=bad):
                cron.run(bad)

    def test_without_dashboard(self):
        with mock.patch.object(cron, "DASHBOARD_CONF", "/nonexistent"):
            self.run_with((0, [], 0))
        self.assertEqual(self.spooled, [], "без дашборда вывод только в журнале")


class OneoffTest(unittest.TestCase):
    """Вместо docker — скрипт: проверяется разбор вывода, без настоящих контейнеров."""

    def fake_docker(self, script):
        bin_dir = tempfile.mkdtemp()
        path = os.path.join(bin_dir, "docker")
        with open(path, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\n[ \"$1\" = rm ] && exit 0\n" + script)
        os.chmod(path, 0o755)
        patch = mock.patch.dict(os.environ, {"PATH": bin_dir + os.pathsep + os.environ["PATH"]})
        patch.start()
        self.addCleanup(patch.stop)

    def test_output_and_code(self):
        self.fake_docker("for i in 1 2 3 4 5; do echo строка $i; done\necho\necho '  '\necho ошибка >&2\nexit 3\n")
        with mock.patch.object(cron, "MAX_LINES", 4), mock.patch("builtins.print"):
            code, lines, skipped = cron.oneoff("shop", "airlok-cron-shop_0", "true", 10)
        self.assertEqual(code, 3)
        self.assertEqual([text for _, text in lines], ["строка 3", "строка 4", "строка 5", "ошибка"],
                         "остаются последние строки без пустых: причина ошибки обычно в последних")
        self.assertEqual(skipped, 2)

    def test_timeout(self):
        self.fake_docker("sleep 1\nexit 137\n")
        with mock.patch("builtins.print"):
            code, _, _ = cron.oneoff("shop", "airlok-cron-shop_0", "sleep 100", 0.2)
        self.assertIsNone(code)


if __name__ == "__main__":
    unittest.main()
