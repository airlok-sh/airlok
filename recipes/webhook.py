#!/usr/bin/env python3
"""Принимает webhook GitHub о push, проверяет подпись и ставит выкатку в очередь.

Сервис смотрит в интернет, поэтому работает от непривилегированного пользователя и
сам ничего не деплоит: только кладёт «репозиторий + коммит» в очередь, которую
разбирает отдельная служба от root. Deploy key ему недоступен.

Адрес: POST /_deployassistant/hook/<владелец>__<репозиторий>
"""
import hashlib
import hmac
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SECRETS_DIR = "/etc/deployassistant/webhook"
QUEUE_DIR = "/var/lib/deployassistant/queue"
LISTEN = ("127.0.0.1", 9417)
MAX_BODY = 1 << 20
PATH = re.compile(r"^/_deployassistant/hook/([A-Za-z0-9_.-]+__[A-Za-z0-9_.-]+)$")
SHA = re.compile(r"^[0-9a-f]{40}$")


def read_file(path):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return None


class Handler(BaseHTTPRequestHandler):
    server_version = "da-webhook"
    sys_version = ""

    def reply(self, code, text):
        body = (text + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.reply(405, "method not allowed")

    def do_POST(self):
        match = PATH.match(self.path)
        if not match:
            return self.reply(404, "not found")
        slug = match.group(1)
        secret = read_file(f"{SECRETS_DIR}/{slug}.secret")
        if not secret:
            return self.reply(404, "not found")

        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if not 0 < length <= MAX_BODY:
            return self.reply(413, "bad body size")
        body = self.rfile.read(length)

        # Подпись проверяем до разбора JSON: неподписанное тело даже не читаем.
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, self.headers.get("X-Hub-Signature-256", "")):
            self.log_message("отклонено: неверная подпись для %s", slug)
            return self.reply(401, "bad signature")

        event = self.headers.get("X-GitHub-Event", "")
        if event == "ping":
            return self.reply(200, "pong")
        if event != "push":
            return self.reply(202, f"ignored event {event}")

        try:
            payload = json.loads(body)
        except ValueError:
            return self.reply(400, "bad json")
        repo = (payload.get("repository") or {}).get("full_name", "")
        if repo.replace("/", "__") != slug:
            return self.reply(400, "repository mismatch")

        branch = read_file(f"{SECRETS_DIR}/{slug}.branch") or "main"
        if payload.get("ref") != f"refs/heads/{branch}" or payload.get("deleted"):
            return self.reply(202, f"ignored: not a push to {branch}")
        sha = payload.get("after", "")
        if not SHA.match(sha):
            return self.reply(400, "bad commit")

        # Одна задача на репозиторий: новый push перезаписывает ещё не начатый,
        # и выкатывается только последний коммит.
        job = f"{QUEUE_DIR}/{slug}.job"
        tmp = f"{job}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(f"{repo} {sha}\n")
        os.replace(tmp, job)
        self.log_message("в очереди: %s %s", repo, sha[:12])
        self.reply(202, f"queued {sha[:12]}")

    def log_message(self, fmt, *args):
        sys.stderr.write((fmt % args) + "\n")


def main():
    ThreadingHTTPServer(LISTEN, Handler).serve_forever()


if __name__ == "__main__":
    main()
