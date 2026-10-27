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


def line(status, path, method="GET", took=0.005, sent=10, at=MINUTE + 5):
    return "%.3f %d %.3f %d %s %s" % (at, status, took, sent, method, path)


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


class CollectTest(unittest.TestCase):
    def test_finished_minute_goes_with_routes(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "airlok-shop.access.log"), "w", encoding="utf-8") as f:
                f.write(line(200, "/api/items/5", took=0.010) + "\n")
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
        self.assertEqual((b["app"], b["requests"], b["status_5xx"]), ("shop", 2, 1))
        self.assertEqual(b["routes"], [{
            "method": "GET", "route": "/api/items/:id", "requests": 2, "status_4xx": 0, "status_5xx": 1,
            "p50_ms": 10, "p95_ms": 30,
        }])
        self.assertEqual([r["route"] for r in later[0]["routes"]], ["/"])


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
