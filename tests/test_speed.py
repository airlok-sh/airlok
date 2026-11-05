"""Замер скорости у посетителей: python3 -m unittest discover tests

Разбор маячков и гистограммы должны совпадать с internal/speed в backend (speed_test.go).
"""
import importlib.util
import json
import os
import re
import tempfile
import types
import unittest

RECIPES = os.path.join(os.path.dirname(__file__), "..", "recipes")


def load(name):
    spec = importlib.util.spec_from_file_location(name, os.path.join(RECIPES, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


speed = load("speed")
metrics = load("metrics")

MINUTE = 1761541200  # 2025-10-27T05:00:00Z
CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/141.0 Safari/537.36"
IPHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) Mobile/15E148"


def beacon(q, at=MINUTE + 5, status=204, method="POST", ua=CHROME):
    return json.dumps({"t": at, "s": status, "m": method, "q": q, "ua": ua})


class BeaconTest(unittest.TestCase):
    def test_parse_like_backend(self):
        page, values = speed.parse_beacon("p=%2Fshop%3Fa%3D1&t=120.4&l=2400&c=35&i=abc&f=-5")
        self.assertEqual(page, "/shop")
        self.assertEqual(values, [120, -1, 2400, -1, 35])
        self.assertIsNone(speed.parse_beacon("p=/&l=999999"), "значение вне диапазона")
        self.assertIsNone(speed.parse_beacon("p=/&l=nan&t=inf"))
        self.assertEqual(speed.parse_beacon("p=https://evil.test/x&t=1")[0], "/", "чужой адрес — корень")
        self.assertEqual(speed.parse_beacon("t=0.5")[1][0], 1, "половина округляется вверх, как math.Round")
        long = speed.parse_beacon("p=/" + "%D0%B6" * 150 + "&t=1")[0]
        self.assertLessEqual(len(long.encode()), speed.MAX_PAGE_LEN)
        self.assertTrue(long.endswith("ж"), "обрезка не рвёт букву")

    def test_device(self):
        self.assertEqual(speed.device_of(IPHONE), "mobile")
        self.assertEqual(speed.device_of("Mozilla/5.0 (iPad; CPU OS 17_0)"), "mobile")
        self.assertEqual(speed.device_of(CHROME), "desktop")

    def test_parse_line_skips_bots_and_junk(self):
        self.assertIsNotNone(speed.parse_line(beacon("p=/&l=100"), metrics.BOT.search))
        self.assertIsNone(speed.parse_line(beacon("p=/&l=100", ua="curl/8.5.0"), metrics.BOT.search))
        self.assertIsNone(speed.parse_line(beacon("p=/&l=100", ua=""), metrics.BOT.search))
        self.assertIsNone(speed.parse_line(beacon("p=/&l=100", status=429), metrics.BOT.search), "лимит nginx")
        self.assertIsNone(speed.parse_line(beacon("p=/&l=100", method="PUT"), metrics.BOT.search))
        self.assertIsNone(speed.parse_line("не json", metrics.BOT.search))

    def test_histogram_bounds_like_backend(self):
        sp = speed.new_minute()
        # Как TestIndex в backend: корзина LCP для значения.
        cases = {0: 0, 50: 0, 51: 1, 2500: 15, 2501: 16, 20000: 24, 20001: 25}
        for v in cases:
            speed.add(sp, "/", "desktop", [-1, -1, v, -1, -1])
        lcp = sp["buckets"]["/\tdesktop"]["lcp"]
        self.assertEqual(len(lcp), 26)
        self.assertEqual(len(sp["buckets"]["/\tdesktop"]["cls"]), 14)
        for v, idx in cases.items():
            self.assertGreaterEqual(lcp[idx], 1, v)
        self.assertEqual(sum(lcp), len(cases))
        self.assertEqual(sum(sp["buckets"]["/\tdesktop"]["inp"]), 0, "нет показателя — нет замера")

    def test_minute_limits(self):
        sp = speed.new_minute()
        for i in range(speed.MAX_PAGES + 5):
            speed.add(sp, "/p%d" % i, "desktop", [100, 200, 300, -1, 0])
        bs = speed.buckets(sp)
        self.assertEqual(len(bs), speed.MAX_PAGES + 1)
        other = [b for b in bs if b["page"] == speed.OTHER]
        self.assertEqual(other[0]["samples"], 5)
        while sp["samples"] < speed.MAX_SAMPLES:
            speed.add(sp, "/", "desktop", [1, -1, -1, -1, -1])
        self.assertFalse(speed.add(sp, "/", "desktop", [1, -1, -1, -1, -1]))


class CollectTest(unittest.TestCase):
    def test_speed_goes_with_minute_and_log_is_cleared(self):
        with tempfile.TemporaryDirectory() as d:
            # Минута без запросов к сайту: посетитель ушёл со страницы позже, чем её открыл.
            open(os.path.join(d, "airlok-shop.access.log"), "w").close()
            log = os.path.join(d, "airlok-shop.speed.log")
            with open(log, "w", encoding="utf-8") as f:
                f.write(beacon("p=/&t=300&f=900&l=1200&c=20") + "\n")
                f.write(beacon("p=/&t=500&l=2600&i=80", ua=IPHONE) + "\n")
                f.write(beacon("p=/&l=100", ua="Googlebot/2.1") + "\n")
                f.write(beacon("p=/&l=100", at=MINUTE + 65) + "\n")
            glob_was = metrics.LOG_GLOB
            metrics.LOG_GLOB = os.path.join(d, "airlok-*.access.log")
            try:
                state = {}
                done = metrics.collect(state, MINUTE + 70)
                left = os.path.getsize(log)
                state = json.loads(json.dumps(state))
                later = metrics.collect(state, MINUTE + 130)
            finally:
                metrics.LOG_GLOB = glob_was
        self.assertEqual(len(done), 1)
        b = done[0]
        self.assertEqual(b["requests"], 0)
        got = {(x["page"], x["device"]): x["samples"] for x in b["speed"]}
        self.assertEqual(got, {("/", "desktop"): 1, ("/", "mobile"): 1})
        self.assertEqual(left, 0, "прочитанный лог маячков стёрт")
        self.assertEqual(sum(later[0]["speed"][0]["lcp"]), 1, "маячок следующей минуты не потерялся")
        self.assertNotIn("Mozilla", json.dumps(done), "браузер в дашборд не уходит")

    def test_flood_is_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            open(os.path.join(d, "airlok-shop.access.log"), "w").close()
            log = os.path.join(d, "airlok-shop.speed.log")
            with open(log, "w", encoding="utf-8") as f:
                f.write((beacon("p=/&l=100") + "\n") * 10)
            glob_was, limit_was = metrics.LOG_GLOB, metrics.MAX_SPEED_READ
            metrics.LOG_GLOB, metrics.MAX_SPEED_READ = os.path.join(d, "airlok-*.access.log"), 100
            try:
                done = metrics.collect({}, MINUTE + 70)
                left = os.path.getsize(log)
            finally:
                metrics.LOG_GLOB, metrics.MAX_SPEED_READ = glob_was, limit_was
        self.assertEqual(done, [])
        self.assertEqual(left, 0)

    def test_speed_apps_from_response(self):
        self.assertEqual(metrics.speed_apps(b'{"speed":["shop","Bad Name",1]}'), {"shop"})
        self.assertEqual(metrics.speed_apps(b'{"speed":[]}'), set())
        self.assertIsNone(metrics.speed_apps(b""), "старый дашборд отвечает 204 без тела")
        self.assertIsNone(metrics.speed_apps(b"{}"))
        self.assertIsNone(metrics.speed_apps(b"[]"))


def site_conf(app):
    """Конфиг сайта из шаблона app-deploy.sh с подставленными значениями."""
    with open(os.path.join(RECIPES, "app-deploy.sh"), encoding="utf-8") as f:
        tpl = re.search(r'write_if_changed "/etc/nginx/sites-available/airlok-\$\{APP_NAME\}\.conf" 0644 <<EOF; then\n(.*?)\nEOF\n',
                        f.read(), re.S).group(1)
    values = {"APP_NAME": app, "APP_DOMAIN": app + ".example.ru", "APP_PORT": "3000", "cert": "/c", "key": "/k",
              "UPLOAD_MAX_MB": "10", "uploads_block": "", "tls_listen": "    listen 443 ssl;"}
    out = re.sub(r"\$\{(\w+)\}", lambda m: values[m.group(1)], tpl)
    return out.replace("\\$", "$").replace("\\\\", "\\")


class ApplyTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.nginx = os.path.join(tmp.name, "nginx")
        os.makedirs(os.path.join(self.nginx, "sites-available"))
        was = speed.NGINX_DIR, speed.SHARE_DIR
        speed.NGINX_DIR, speed.SHARE_DIR = self.nginx, os.path.join(tmp.name, "share")
        self.addCleanup(lambda: setattr(speed, "NGINX_DIR", was[0]) or setattr(speed, "SHARE_DIR", was[1]))
        self.calls = []
        self.nginx_ok = True

    def site(self, app, text=None):
        path = os.path.join(self.nginx, "sites-available", "airlok-%s.conf" % app)
        with open(path, "w", encoding="utf-8") as f:
            f.write(site_conf(app) if text is None else text)
        return path

    def run_cmd(self, args, **_):
        self.calls.append(args[0])
        return types.SimpleNamespace(returncode=0 if self.nginx_ok or args[0] != "nginx" else 1, stderr="bad")

    def snippet(self, app):
        return os.path.join(self.nginx, "snippets", "airlok-speed.%s.conf" % app)

    def test_template_has_include(self):
        text = site_conf("shop")
        self.assertIn(speed.include_line("shop"), text)
        self.assertEqual(speed.with_include(text, "shop"), text, "шаблон уже со строкой — правка не нужна")
        old = text.replace(speed.include_line("shop"), "")
        self.assertEqual(speed.with_include(old, "shop"), text, "сайт старого агента получает ту же строку")
        self.assertIsNone(speed.with_include("server { }", "shop"))

    def test_enable_and_disable(self):
        old = site_conf("shop").replace(speed.include_line("shop"), "")
        site = self.site("shop", old)
        self.site("blog")
        self.assertTrue(speed.apply({"shop", "ghost"}, run=self.run_cmd))
        self.assertEqual(self.calls, ["nginx", "systemctl"])
        with open(self.snippet("shop"), encoding="utf-8") as f:
            snip = f.read()
        self.assertIn(speed.script_tag(), snip)
        self.assertIn("airlok-shop.speed.log airlok_speed", snip)
        self.assertFalse(os.path.exists(self.snippet("blog")))
        self.assertFalse(os.path.exists(self.snippet("ghost")), "приложения без сайта на сервере нет")
        with open(site, encoding="utf-8") as f:
            self.assertIn(speed.include_line("shop"), f.read())
        self.assertTrue(os.path.exists(os.path.join(speed.SHARE_DIR, "speed.js")))
        self.assertTrue(os.path.exists(os.path.join(self.nginx, "conf.d", "airlok-speed.conf")))

        self.calls = []
        self.assertFalse(speed.apply({"shop"}, run=self.run_cmd), "ничего не изменилось — nginx не трогаем")
        self.assertEqual(self.calls, [])

        self.assertTrue(speed.apply(set(), run=self.run_cmd))
        self.assertFalse(os.path.exists(self.snippet("shop")))

    def test_nginx_refused_reverts(self):
        old = site_conf("shop").replace(speed.include_line("shop"), "")
        site = self.site("shop", old)
        self.nginx_ok = False
        self.assertFalse(speed.apply({"shop"}, run=self.run_cmd))
        self.assertEqual(self.calls, ["nginx"], "без reload")
        self.assertFalse(os.path.exists(self.snippet("shop")))
        self.assertFalse(os.path.exists(os.path.join(self.nginx, "conf.d", "airlok-speed.conf")))
        with open(site, encoding="utf-8") as f:
            self.assertEqual(f.read(), old)


if __name__ == "__main__":
    unittest.main()
