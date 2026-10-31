"""Подсчёт запросов по логам nginx: python3 -m unittest discover tests

Шаблоны адресов должны совпадать с edge хостинга (internal/edge/routes_test.go в backend).
"""
import importlib.util
import json
import os
import tempfile
import unittest

spec = importlib.util.spec_from_file_location("metrics", os.path.join(os.path.dirname(__file__), "..", "recipes", "metrics.py"))
metrics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metrics)

MINUTE = 1761541200  # 2025-10-27T05:00:00Z


def line(status, path, method="GET", took=0.005, sent=10, at=MINUTE + 5, upstream=None):
    """Строка лога в формате airlok; время приложения по умолчанию равно длительности запроса."""
    if upstream is None:
        upstream = "%.3f" % took
    return '%.3f %d %.3f %d %s %s "%s"' % (at, status, took, sent, method, path, upstream)


class RouteOfTest(unittest.TestCase):
    def test_same_as_edge(self):
        cases = [
            ("GET", "/", "GET", "/"),
            ("GET", "/api/orders/", "GET", "/api/orders"),
            ("GET", "//api//orders", "GET", "/api/orders"),
            ("POST", "/api/orders/123/items", "POST", "/api/orders/:id/items"),
            ("GET", "/api/v1/users/7", "GET", "/api/v1/users/:id"),
            ("GET", "/u/550e8400-e29b-41d4-a716-446655440000", "GET", "/u/:id"),
            ("GET", "/c/507f1f77bcf86cd799439011", "GET", "/c/:id"),
            ("GET", "/blog/2026-10-22-launch", "GET", "/blog/:id"),
            ("GET", "/reset/eyJhbGciOiJIUzI1NiJ9xyz1", "GET", "/reset/:id"),
            ("GET", "/каталог/свечи", "GET", "/каталог/свечи"),
            ("GET", "/a/b/c/d/e/f/g/h", "GET", "/a/b/c/d/e/f/…"),
            ("PROPFIND", "/dav", "OTHER", "/dav"),
            ("GET", "/_next/static/chunks/main-abc.js", "*", metrics.ROUTE_STATIC),
            ("GET", "/logo.PNG", "*", metrics.ROUTE_STATIC),
            ("GET", "/.env", "*", metrics.ROUTE_UNKNOWN),
            ("POST", "/wp-login.php", "*", metrics.ROUTE_UNKNOWN),
            ("-", "-", "*", metrics.ROUTE_UNKNOWN),
        ]
        for method, path, want_method, want_route in cases:
            self.assertEqual(metrics.route_of(method, path), (want_method, want_route), path)

    def test_long_route_is_cut(self):
        seg = "ж" * 25
        self.assertEqual(metrics.route_template("/" + seg), "/" + seg, "длинный слаг кириллицей — не идентификатор")
        r = metrics.route_template(("/" + seg) * 5)
        self.assertLessEqual(len(r.encode()), metrics.MAX_ROUTE_LEN + len("…".encode()))
        self.assertTrue(r.endswith("…"))


class AddLineTest(unittest.TestCase):
    def test_routes(self):
        lines = [
            line(200, "/api/orders/1"),
            line(404, "/api/orders/2"),
            line(404, "/no-such-page"),
            line(200, "/app.js"),
            line(200, "/\\xD0\\xBA\\xD0\\xB0\\xD1\\x82\\xD0\\xB0\\xD0\\xBB\\xD0\\xBE\\xD0\\xB3"),
            line(200, "/file with space"),
        ]
        lines += [line(200, "/page-%s%s" % (chr(97 + i // 26), chr(97 + i % 26))) for i in range(metrics.MAX_APP_ROUTES + 5)]
        minutes = {}
        for ln in lines:
            metrics.add_line(minutes, ln)
        m = minutes[str(MINUTE)]
        routes = m["routes"]
        self.assertEqual(sum(r["requests"] for r in routes.values()), m["requests"], "каждый запрос попадает ровно в один адрес")
        self.assertEqual(routes["GET /api/orders/:id"]["requests"], 2, "404 известного адреса остаётся у него")
        self.assertEqual(routes["GET /api/orders/:id"]["status_4xx"], 1)
        self.assertEqual(routes["* <unknown>"]["requests"], 1)
        self.assertEqual(routes["* <static>"]["requests"], 1)
        self.assertIn("GET /каталог", routes)
        self.assertIn("GET /file with space", routes)
        self.assertGreater(routes["* <other>"]["requests"], 0, "сверх лимита — в «прочее»")
        self.assertLessEqual(len(routes), metrics.MAX_APP_ROUTES + 3)

    def test_old_format_counts_without_routes(self):
        minutes = {}
        metrics.add_line(minutes, "%.3f 502 0.120 0" % (MINUTE + 1))
        m = minutes[str(MINUTE)]
        self.assertEqual((m["requests"], m["status_5xx"], m["routes"]), (1, 1, {}))

    def test_app_time_instead_of_request_time(self):
        # Сканер медленно шлёт тело POST: nginx принимал его 1,3 с, а приложение ответило 405 сразу.
        cases = [
            ("0.001", 1),
            ("-", 1309),
            ("0.002, 0.004", 6),
            ("0.003 : 0.005", 8),
            ("-, 0.004 : -", 4),
        ]
        for upstream, want in cases:
            minutes = {}
            metrics.add_line(minutes, line(405, "/", method="POST", took=1.309, upstream=upstream))
            m = minutes[str(MINUTE)]
            self.assertEqual((m["ms"], m["routes"]["POST /"]["ms"]), ([want], [want]), upstream)

    def test_path_before_app_time(self):
        minutes = {}
        metrics.add_line(minutes, line(200, "/say \\x22hi\\x22", took=0.5, upstream="0.003"))
        metrics.add_line(minutes, "%.3f 200 0.004 10 GET /old with space" % (MINUTE + 1))
        metrics.add_line(minutes, '%.3f 200 0.004 10 GET /cut "0.00' % (MINUTE + 1))
        routes = minutes[str(MINUTE)]["routes"]
        self.assertEqual(routes['GET /say "hi"']["ms"], [3], "кавычка в пути экранирована и не путает разбор")
        self.assertEqual(routes["GET /old with space"]["ms"], [4], "строка без времени приложения — по длительности")
        self.assertEqual(minutes[str(MINUTE)]["requests"], 2, "строка с незакрытой кавычкой отброшена")


class CollectTest(unittest.TestCase):
    def test_finished_minute_goes_with_routes(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "airlok-shop.access.log"), "w", encoding="utf-8") as f:
                # Формат сменился посреди минуты: строки до reload nginx — без времени приложения.
                f.write("%.3f 200 0.020 10 GET /api/items/4\n" % (MINUTE + 1))
                f.write(line(200, "/api/items/5", took=1.200, upstream="0.010") + "\n")
                f.write(line(500, "/api/items/6", took=0.030) + "\n")
                f.write(line(200, "/", at=MINUTE + 65) + "\n")
            glob_was = metrics.LOG_GLOB
            metrics.LOG_GLOB = os.path.join(d, "airlok-*.access.log")
            try:
                state = {}
                done = metrics.collect(state, MINUTE + 70)
                # Состояние переживает перезапуск через JSON.
                state = json.loads(json.dumps(state))
                later = metrics.collect(state, MINUTE + 130)
            finally:
                metrics.LOG_GLOB = glob_was
        self.assertEqual(len(done), 1)
        b = done[0]
        self.assertEqual((b["app"], b["requests"], b["status_5xx"]), ("shop", 3, 1))
        self.assertEqual((b["p50_ms"], b["p95_ms"]), (20, 30))
        self.assertEqual(b["routes"], [{
            "method": "GET", "route": "/api/items/:id", "requests": 3, "status_4xx": 0, "status_5xx": 1,
            "p50_ms": 20, "p95_ms": 30,
        }])
        self.assertEqual([r["route"] for r in later[0]["routes"]], ["/"])


CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/141.0 Safari/537.36"


def visit(at=MINUTE + 5, status=200, uri="/", ip="5.1.2.3", ua=CHROME, ref="", **extra):
    v = {"t": at, "s": status, "ip": ip, "host": "shop.example.ru", "uri": uri, "ct": "text/html; charset=utf-8",
         "dest": "document", "rsc": "", "sp": "", "pu": "", "nrp": "", "ref": ref, "ua": ua}
    v.update(extra)
    return json.dumps(v)


def geo_file():
    """База стран в формате internal/geoip: 5.0.0.0/8 — RU, остальное неизвестно, IPv6 нет."""
    starts = [bytes([0, 0, 0, 0]), bytes([5, 0, 0, 0]), bytes([6, 0, 0, 0])]
    return (b"AIRGEO1\n" + (3).to_bytes(4, "big") + b"".join(starts) + b"--RU--" + (0).to_bytes(4, "big"))


class VisitsTest(unittest.TestCase):
    """Правила те же, что у edge хостинга (internal/edge/visits_test.go в backend)."""

    def test_page_view(self):
        cases = [
            ({}, 200, True),
            ({}, 304, True),
            ({}, 404, False),
            ({"ct": "application/json"}, 200, False),
            ({"dest": "iframe"}, 200, False),
            ({"sp": "prefetch;prerender"}, 200, False),
            ({"rsc": "1", "dest": "empty", "ct": "text/x-component"}, 200, True),
            ({"rsc": "1", "nrp": "1"}, 200, False),
        ]
        for extra, status, want in cases:
            v = json.loads(visit(**extra))
            self.assertEqual(metrics.page_view(v, status, CHROME), want, extra)
        self.assertFalse(metrics.page_view(json.loads(visit()), 200, "Mozilla/5.0 (compatible; YandexBot/3.0)"))
        self.assertFalse(metrics.page_view(json.loads(visit()), 200, ""))

    def test_source_and_device(self):
        self.assertEqual(metrics.visit_source("", "", "shop.example.ru"), "")
        self.assertEqual(metrics.visit_source("", "https://www.Yandex.ru/", "shop.example.ru"), "yandex.ru")
        self.assertEqual(metrics.visit_source("utm_source=VK", "https://vk.com/", "shop.example.ru"), "vk")
        self.assertIsNone(metrics.visit_source("", "https://shop.example.ru/a", "www.shop.example.ru"))
        self.assertEqual(metrics.device("Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) Mobile/15E148"), "mobile")
        self.assertEqual(metrics.device("Mozilla/5.0 (Linux; Android 13; SM-X710) Chrome/141.0 Safari/537.36"), "tablet")
        self.assertEqual(metrics.device(CHROME), "desktop")

    def test_geoip(self):
        geo = metrics.GeoIP(geo_file())
        self.assertEqual(geo.country("5.1.2.3"), "RU")
        self.assertEqual(geo.country("::ffff:5.1.2.3"), "RU")
        self.assertEqual(geo.country("7.7.7.7"), "")
        self.assertEqual(geo.country("2a02:6b8::1"), "")
        with self.assertRaises(ValueError):
            metrics.GeoIP(geo_file()[:-1])

    def test_visits_go_with_minute(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "airlok-shop.access.log"), "w", encoding="utf-8") as f:
                for _ in range(5):
                    f.write(line(200, "/") + "\n")
                f.write(line(200, "/", at=MINUTE + 65) + "\n")
            visits = os.path.join(d, "airlok-shop.visits.log")
            with open(visits, "w", encoding="utf-8") as f:
                f.write(visit(ref="https://yandex.ru/") + "\n")
                f.write(visit(uri="/price?utm_source=tg", ref="https://shop.example.ru/") + "\n")
                f.write(visit(uri="/%D1%86%D0%B5%D0%BD%D1%8B", ref="https://shop.example.ru/") + "\n")
                f.write(visit(ua="curl/8.5.0") + "\n")
                f.write(visit(ip="7.7.7.7", ua="Mozilla/5.0 (iPhone; CPU iPhone OS 18_0) Mobile/15E148") + "\n")
                f.write("не json\n")
            glob_was = metrics.LOG_GLOB
            metrics.LOG_GLOB = os.path.join(d, "airlok-*.access.log")
            try:
                salts = {}
                done = metrics.collect({}, MINUTE + 70, salts, metrics.GeoIP(geo_file()))
                left = os.path.getsize(visits)
            finally:
                metrics.LOG_GLOB = glob_was
        self.assertEqual(len(done), 1)
        v = done[0]["visits"]
        self.assertEqual(v["views"], 4)
        self.assertEqual(len(v["visitors"]), 2, "тот же IP и браузер — один посетитель")
        self.assertEqual(v["pages"], {"/": 2, "/price": 1, "/цены": 1})
        self.assertEqual(v["sources"], {"yandex.ru": 1, "tg": 1, "": 1}, "переход внутри сайта — не заход")
        self.assertEqual(v["devices"], {"desktop": 3, "mobile": 1})
        self.assertEqual(v["countries"], {"RU": 3, "": 1})
        self.assertEqual(left, 0, "прочитанный лог посещений с IP стёрт")
        self.assertEqual(list(salts), [metrics.msk_day(MINUTE)])
        self.assertNotIn("5.1.2.3", json.dumps(done), "IP в дашборд не уходит")


class BatchTest(unittest.TestCase):
    def test_batch_fits_body_limit(self):
        route = {"method": "GET", "route": "/x" * 50, "requests": 1, "status_4xx": 0, "status_5xx": 0, "p50_ms": 1, "p95_ms": 1}
        pending = [{"app": "a", "minute": "2025-10-27T05:00:00Z", "routes": [route] * 53}] * 100
        n = metrics.next_batch(pending)
        self.assertGreater(n, 0)
        self.assertLessEqual(len(json.dumps({"buckets": pending[:n]})), metrics.MAX_BATCH_BYTES + 1024)
        self.assertEqual(metrics.next_batch(pending[:3]), 3)


if __name__ == "__main__":
    unittest.main()
