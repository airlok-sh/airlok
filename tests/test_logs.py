"""Отправка вывода приложений в дашборд: python3 -m unittest discover tests"""
import http.server
import importlib.util
import io
import json
import os
import threading
import unittest

RECIPES = os.path.join(os.path.dirname(__file__), "..", "recipes")
spec = importlib.util.spec_from_file_location("logs", os.path.join(RECIPES, "logs.py"))
logs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(logs)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class ParseTest(unittest.TestCase):
    def test_docker_timestamps(self):
        ts, ns, text = logs.parse_line("2026-10-27T10:00:00.123456789Z GET / 200\n")
        self.assertEqual((ts, text), ("2026-10-27T10:00:00.123456789Z", "GET / 200"))
        self.assertEqual(ns % 10**9, 123456789)
        # Docker отрезает нули в конце долей секунды: сравнивать строки нельзя.
        self.assertLess(logs.parse_line("2026-10-27T10:00:00.9Z a")[1], logs.parse_line("2026-10-27T10:00:01Z b")[1])
        self.assertEqual(logs.parse_line("2026-10-27T10:00:00.5Z ")[2], "")
        self.assertIsNone(logs.parse_line("не время строка"))

    def test_since_round_trip(self):
        ns = logs.parse_ts("2026-10-27T10:00:00.000000042Z")
        self.assertTrue(logs.docker_since(ns).endswith(".000000042"))

    def test_response(self):
        apps, live = logs.parse_response(json.dumps({"apps": ["blog", "../x", 5], "live": ["blog", "shop"]}))
        self.assertEqual((apps, live), ({"blog"}, {"blog"}), "чужие имена и live без включённых логов отбрасываются")
        self.assertIsNone(logs.parse_response(b"<html>"))
        self.assertIsNone(logs.parse_response(b"[]"))


class BufferTest(unittest.TestCase):
    def test_rate_limit_reports_skipped(self):
        clock = Clock()
        buf = logs.Buffer(clock)
        for i in range(logs.BURST + 50):
            buf.add("spam", "t", "stdout", str(i))
        buf.add("calm", "t", "stdout", "ok")
        batch = buf.take_batch()
        self.assertEqual(sum(1 for e in batch if e["app"] == "spam" and e["stream"] == "stdout"), logs.BURST)
        note = [e for e in batch if e["stream"] == "system"]
        self.assertEqual(len(note), 1)
        self.assertIn("пропущено строк вывода: 50", note[0]["line"])
        self.assertIn("calm", {e["app"] for e in batch}, "спам одного приложения не мешает другим")
        clock.t += 1
        for _ in range(logs.RATE + 10):
            buf.add("spam", "t", "stdout", "x")
        self.assertEqual(len([e for e in buf.take_batch() if e["stream"] == "stdout"]), logs.RATE, "за секунду пополняется RATE строк")

    def test_batch_limits_and_requeue(self):
        buf = logs.Buffer(Clock())
        line = "x" * 4000
        for _ in range(400):
            buf.add("a", "t", "stdout", line)
        first = buf.take_batch()
        self.assertLessEqual(sum(logs.Buffer.weight(e) for e in first), logs.MAX_BATCH_BYTES)
        self.assertGreater(len(buf), 0)
        buf.requeue(first)
        again = buf.take_batch()
        self.assertEqual(again, first, "неотправленная пачка уходит первой и в том же порядке")

    def test_overflow_drops_oldest(self):
        clock = Clock()
        buf = logs.Buffer(clock)
        line = "y" * logs.MAX_LINE
        n = logs.MAX_BUFFER_BYTES // logs.Buffer.weight({"line": line}) + 10
        for i in range(n):
            # Медленнее лимита строк в секунду: переполняется именно буфер.
            clock.t += 0.01
            buf.add("a", str(i), "stdout", line)
        self.assertLessEqual(buf.size, logs.MAX_BUFFER_BYTES)
        batch = buf.take_batch()
        self.assertNotEqual(batch[0]["ts"], "0", "выброшены самые старые")
        while len(buf):
            batch += buf.take_batch()
        notes = [e["line"] for e in batch if e["stream"] == "system"]
        self.assertEqual(len(notes), 1)
        self.assertIn("не сохранилось строк вывода: 10", notes[0])

    def test_long_line_is_cut(self):
        buf = logs.Buffer(Clock())
        buf.add("a", "t", "stdout", "z" * (logs.MAX_LINE * 2))
        self.assertEqual(len(buf.take_batch()[0]["line"]), logs.MAX_LINE)


class FollowerReadTest(unittest.TestCase):
    def follower(self, after=0):
        # Без docker: читаем подготовленный вывод так же, как вывод docker logs.
        f = logs.Follower.__new__(logs.Follower)
        f.app, f.cid, f.service, f.buf = "blog", "c1", "app", logs.Buffer(Clock())
        f.after = {"stdout": after, "stderr": after}
        f.lock = threading.Lock()
        return f

    def test_streams_have_own_boundaries(self):
        f = self.follower()
        f.read(io.BytesIO(b"2026-10-27T10:00:01Z late\n2026-10-27T10:00:02Z later\n"), "stdout")
        f.read(io.BytesIO(b"2026-10-27T10:00:00.5Z early error\n"), "stderr")
        lines = [(e["stream"], e["line"]) for e in f.buf.take_batch()]
        self.assertIn(("stderr", "early error"), lines, "строка stderr раньше прочитанных stdout не теряется")
        self.assertEqual(f.position(), logs.parse_ts("2026-10-27T10:00:02Z"))

    def test_since_boundary_is_skipped(self):
        f = self.follower(after=logs.parse_ts("2026-10-27T10:00:01Z"))
        f.service = "db"
        f.read(io.BytesIO(b"2026-10-27T10:00:01Z sent\n2026-10-27T10:00:01.1Z new\nbroken\n"), "stdout")
        self.assertEqual([e["line"] for e in f.buf.take_batch()], ["[db] new"])


class DashboardTest(unittest.TestCase):
    def test_post_reuses_connection(self):
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                seen.append((self.path, self.headers["Authorization"], len(body["entries"]), self.client_address[1]))
                out = json.dumps({"apps": ["blog"], "live": []}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *args):
                pass

        srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            dash = logs.Dashboard("http://127.0.0.1:%d" % srv.server_port, "airlok_srv_x")
            code, data = dash.post([{"app": "blog", "ts": "t", "stream": "stdout", "line": "привет"}])
            self.assertEqual(code, 200)
            self.assertEqual(logs.parse_response(data), ({"blog"}, set()))
            dash.post([])
        finally:
            srv.shutdown()
        self.assertEqual([s[:3] for s in seen], [("/agent/v1/logs", "Bearer airlok_srv_x", 1), ("/agent/v1/logs", "Bearer airlok_srv_x", 0)])
        self.assertEqual(seen[0][3], seen[1][3], "одно соединение на все отправки")

    def test_unreachable(self):
        self.assertEqual(logs.Dashboard("http://127.0.0.1:1", "t").post([]), (0, b""))


class FakeFollower:
    def __init__(self, app, cid, service, after):
        self.app, self.cid, self.service, self.after = app, cid, service, after
        self.dead = self.stopped = False

    def alive(self):
        return not self.dead

    def position(self):
        return self.after

    def stop(self):
        self.stopped = True


class ReconcileTest(unittest.TestCase):
    def test_follows_only_enabled_running_containers(self):
        followers, positions, started = {}, {"c2": 77}, []

        def start(app, cid, service, after):
            started.append((app, cid, service, after))
            return FakeFollower(app, cid, service, after)

        running = {"c1": ("blog", "app"), "c2": ("blog", "db"), "c3": ("shop", "app")}
        logs.reconcile({"blog"}, running, followers, positions, start)
        self.assertEqual(sorted(started), [("blog", "c1", "app", 0), ("blog", "c2", "db", 77)])

        # Выкатка пересоздала контейнер, у shop включили логи, контейнер c2 перезапустился.
        followers["c1"].after = 500
        followers["c2"].dead = True
        old = followers["c1"]
        started.clear()
        logs.reconcile({"blog", "shop"}, {"c2": ("blog", "db"), "c4": ("blog", "app"), "c3": ("shop", "app")}, followers, positions, start)
        self.assertTrue(old.stopped)
        self.assertEqual(positions["c1"], 500)
        self.assertEqual(sorted(followers), ["c2", "c3", "c4"])
        self.assertEqual(sorted(s[1] for s in started), ["c2", "c3", "c4"])

        started.clear()
        followers["c3"].after = 900
        logs.reconcile(set(), {"c3": ("shop", "app")}, followers, positions, start)
        self.assertEqual(followers, {}, "логи выключили — читать перестаём")
        self.assertEqual(started, [])
        self.assertEqual(positions["c3"], 900, "пока дашборд не ответил, место не трогаем")

        logs.reconcile(set(), {"c3": ("shop", "app")}, followers, positions, start, off_since=5000)
        self.assertEqual(positions["c3"], 5000, "выключено — место сдвигается на сейчас")
        logs.reconcile({"shop"}, {"c3": ("shop", "app")}, followers, positions, start, off_since=6000)
        self.assertEqual(started, [("shop", "c3", "app", 5000)], "после включения вывод за выключенное время не досылается")

    def test_prune_positions(self):
        now = 10 * logs.KEEP_POSITION_NS
        kept = logs.prune_positions({"old": now - logs.KEEP_POSITION_NS, "new": now - 1}, now)
        self.assertEqual(kept, {"new": now - 1})


if __name__ == "__main__":
    unittest.main()
