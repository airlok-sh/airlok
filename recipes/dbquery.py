#!/usr/bin/env python3
"""Запрос только на чтение к базе приложения на этом сервере — для просмотра базы в дашборде.

    dbquery.py < запрос.json > ответ.json

Агент передаёт поле db_query своей команды: {"app", "statements": [{"sql", "params"}],
"max_rows", "max_cell"}. Ответ — {"results": [{"columns", "rows", "truncated"}]} или
{"error": {"kind": "sql" | "no_database" | "failed", "code", "message", "position"}}.

Запрос идёт от роли airlok_ro: она только читает (pg_read_all_data) и не суперпользователь, так
что ни писать в базу, ни выйти из неё в систему не может. Каждый оператор уходит расширенным
протоколом Postgres — в одном тексте только один, — в транзакции только на чтение и с
таймаутом, а строк сервер отдаёт не больше, чем просили. Роль заводится при первом запросе,
её пароль лежит в shared/db-ro.env рядом с паролем базы.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import socket
import struct
import subprocess
import sys

APPS_ROOT = "/srv/apps"
NAME = re.compile(r"^[a-z][a-z0-9-]{1,30}$")
RO_ROLE = "airlok_ro"
STATEMENT_TIMEOUT_MS = 5000
SOCKET_TIMEOUT = 15
MAX_STATEMENTS = 5
MAX_SQL = 20_000
MAX_LIMIT = 10_000
# Ответ дашборду: строки сверх этого объёма отбрасываются с пометкой truncated.
MAX_RESULT_CHARS = 6 << 20
# Типы, которые встречаются чаще всего; остальные называет сама база.
TYPES = {16: "bool", 17: "bytea", 18: "char", 19: "name", 20: "int8", 21: "int2", 23: "int4", 25: "text", 26: "oid",
         114: "json", 700: "float4", 701: "float8", 1042: "bpchar", 1043: "varchar", 1082: "date", 1083: "time",
         1114: "timestamp", 1184: "timestamptz", 1186: "interval", 1700: "numeric", 2950: "uuid", 3802: "jsonb"}


class PGError(Exception):
    """Ошибка, которую вернула база: поля ErrorResponse по однобуквенным кодам."""

    def __init__(self, fields):
        super().__init__(fields.get("M", "ошибка базы"))
        self.fields = fields


class Failed(Exception):
    """Запрос не выполнить по причине вне базы; текст — для человека."""


class NoDatabase(Exception):
    pass


class Conn:
    """Минимальный клиент протокола Postgres 3.0: вход по паролю (SCRAM-SHA-256 или md5) и
    запросы в текстовом формате."""

    def __init__(self, host, port, user, password, database, timeout=SOCKET_TIMEOUT):
        # status — состояние транзакции из последнего ReadyForQuery: I — вне её, T — внутри, E — с ошибкой.
        self.status = b"I"
        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.user, self.password = user, password
        params = b"".join(k + b"\0" + v.encode() + b"\0" for k, v in (
            (b"user", user), (b"database", database), (b"application_name", "airlok-preview"), (b"client_encoding", "UTF8")))
        body = struct.pack("!i", 196608) + params + b"\0"
        self.sock.sendall(struct.pack("!i", len(body) + 4) + body)
        self._auth()

    def close(self):
        try:
            self._send(b"X", b"")
        except OSError:
            pass
        self.sock.close()

    def _send(self, kind, body):
        self.sock.sendall(kind + struct.pack("!i", len(body) + 4) + body)

    def _read(self, n):
        out = b""
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise Failed("база закрыла соединение")
            out += chunk
        return out

    def _recv(self):
        head = self._read(5)
        length = struct.unpack("!i", head[1:])[0]
        return head[:1], self._read(length - 4)

    def _auth(self):
        scram = None
        while True:
            kind, body = self._recv()
            if kind == b"E":
                raise PGError(error_fields(body))
            if kind == b"Z":
                self.status = body[:1]
                return
            if kind != b"R":
                continue
            code = struct.unpack("!i", body[:4])[0]
            if code == 0:
                continue
            if code == 3:
                self._send(b"p", self.password.encode() + b"\0")
            elif code == 5:
                inner = hashlib.md5((self.password + self.user).encode()).hexdigest()
                self._send(b"p", b"md5" + hashlib.md5(inner.encode() + body[4:8]).hexdigest().encode() + b"\0")
            elif code == 10:
                if b"SCRAM-SHA-256" not in body[4:].split(b"\0"):
                    raise Failed("база не предлагает вход SCRAM-SHA-256")
                scram = Scram(self.password)
                first = scram.client_first().encode()
                self._send(b"p", b"SCRAM-SHA-256\0" + struct.pack("!i", len(first)) + first)
            elif code == 11 and scram:
                self._send(b"p", scram.client_final(body[4:].decode()).encode())
            elif code == 12 and scram:
                scram.verify(body[4:].decode())
            else:
                raise Failed(f"вход в базу способом {code} не поддерживается")

    def simple(self, sql):
        self._send(b"Q", sql.encode() + b"\0")
        self._until_ready()

    def _until_ready(self):
        err = None
        while True:
            kind, body = self._recv()
            if kind == b"E":
                err = PGError(error_fields(body))
            elif kind == b"Z":
                self.status = body[:1]
                if err:
                    raise err
                return

    def query(self, sql, params, max_rows, max_cell, budget):
        """Один оператор расширенным протоколом: колонки, строки текстом (None — NULL), обрезано ли.
        budget — сколько ещё символов можно вернуть, в списке из одного числа."""
        self._send(b"P", b"\0" + sql.encode() + b"\0" + struct.pack("!h", 0))
        bind = b"\0\0" + struct.pack("!h", 0) + struct.pack("!h", len(params))
        for p in params:
            raw = p.encode()
            bind += struct.pack("!i", len(raw)) + raw
        self._send(b"B", bind + struct.pack("!hh", 1, 0))
        self._send(b"D", b"P\0")
        self._send(b"E", b"\0" + struct.pack("!i", max_rows))
        self._send(b"S", b"")
        columns, rows, truncated, err = [], [], False, None
        while True:
            kind, body = self._recv()
            if kind == b"T":
                columns = row_description(body)
            elif kind == b"D":
                if budget[0] <= 0:
                    truncated = True
                    continue
                row = data_row(body, max_cell)
                budget[0] -= sum(len(v) for v in row if v) + 8
                rows.append(row)
            elif kind == b"s":
                truncated = True
            elif kind == b"E":
                err = PGError(error_fields(body))
            elif kind == b"Z":
                self.status = body[:1]
                if err:
                    raise err
                return columns, rows, truncated


class Scram:
    """SCRAM-SHA-256 по RFC 5802/7677. Postgres берёт имя пользователя из стартового сообщения,
    поэтому здесь оно пустое; nonce и user задаются только в тестах."""

    def __init__(self, password, nonce=None, user=""):
        self.password = password.encode()
        self.nonce = nonce or base64.b64encode(secrets.token_bytes(18)).decode()
        self.user = user

    def client_first(self):
        self.first_bare = f"n={self.user},r={self.nonce}"
        return "n,," + self.first_bare

    def client_final(self, server_first):
        attrs = dict(part.split("=", 1) for part in server_first.split(","))
        if not attrs.get("r", "").startswith(self.nonce):
            raise Failed("база ответила на вход неверным nonce")
        salted = hashlib.pbkdf2_hmac("sha256", self.password, base64.b64decode(attrs["s"]), int(attrs["i"]))
        client_key = hmac.new(salted, b"Client Key", hashlib.sha256).digest()
        final_bare = f"c=biws,r={attrs['r']}"
        self.auth_message = f"{self.first_bare},{server_first},{final_bare}".encode()
        signature = hmac.new(hashlib.sha256(client_key).digest(), self.auth_message, hashlib.sha256).digest()
        proof = bytes(a ^ b for a, b in zip(client_key, signature))
        self.server_key = hmac.new(salted, b"Server Key", hashlib.sha256).digest()
        return final_bare + ",p=" + base64.b64encode(proof).decode()

    def verify(self, server_final):
        want = base64.b64encode(hmac.new(self.server_key, self.auth_message, hashlib.sha256).digest()).decode()
        if not hmac.compare_digest(server_final.strip(), "v=" + want):
            raise Failed("база не подтвердила вход: подпись сервера не сошлась")


def error_fields(body):
    fields = {}
    for part in body.split(b"\0"):
        if part:
            fields[chr(part[0])] = part[1:].decode("utf-8", "replace")
    return fields


def cstring(body, pos):
    end = body.index(b"\0", pos)
    return body[pos:end].decode("utf-8", "replace"), end + 1


def row_description(body):
    n = struct.unpack("!h", body[:2])[0]
    pos, out = 2, []
    for _ in range(n):
        name, pos = cstring(body, pos)
        oid = struct.unpack("!i", body[pos + 6:pos + 10])[0]
        pos += 18
        out.append({"name": name, "oid": oid})
    return out


def data_row(body, max_cell):
    n = struct.unpack("!h", body[:2])[0]
    pos, out = 2, []
    for _ in range(n):
        length = struct.unpack("!i", body[pos:pos + 4])[0]
        pos += 4
        if length < 0:
            out.append(None)
            continue
        text = body[pos:pos + length].decode("utf-8", "replace")
        pos += length
        out.append(text if len(text) <= max_cell else text[:max_cell] + "…")
    return out


def name_types(conn, results):
    """Имена типов колонок: частые — из таблицы, остальные спрашиваем у базы одним запросом."""
    unknown = sorted({c["oid"] for r in results for c in r["columns"] if c["oid"] not in TYPES})
    names = dict(TYPES)
    if unknown:
        _, rows, _ = conn.query("SELECT oid::int, format_type(oid, NULL) FROM pg_type WHERE oid = ANY($1::oid[])",
                                ["{" + ",".join(map(str, unknown)) + "}"], len(unknown), 200, [1 << 20])
        names.update({int(oid): name for oid, name in rows})
    for r in results:
        r["columns"] = [{"name": c["name"], "type": names.get(c["oid"], f"oid {c['oid']}")} for c in r["columns"]]


def parse_request(data):
    if not isinstance(data, dict):
        raise Failed("некорректный запрос")
    app = data.get("app")
    if not isinstance(app, str) or not NAME.match(app):
        raise Failed("некорректное имя приложения")
    stmts = data.get("statements")
    if not isinstance(stmts, list) or not 1 <= len(stmts) <= MAX_STATEMENTS:
        raise Failed("некорректный список запросов")
    out = []
    for s in stmts:
        sql = s.get("sql") if isinstance(s, dict) else None
        params = s.get("params") or []
        if not isinstance(sql, str) or not sql.strip() or len(sql) > MAX_SQL or "\0" in sql:
            raise Failed("некорректный текст запроса")
        if not isinstance(params, list) or len(params) > 20 or not all(isinstance(p, str) and "\0" not in p for p in params):
            raise Failed("некорректные параметры запроса")
        out.append((sql, params))
    limits = []
    for key in ("max_rows", "max_cell"):
        v = data.get(key)
        if not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= MAX_LIMIT:
            raise Failed(f"некорректное {key}")
        limits.append(v)
    return app, out, limits[0], limits[1]


def env_file(path):
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                key, sep, value = line.rstrip("\n").partition("=")
                if sep:
                    out[key] = value
    except FileNotFoundError:
        return None
    return out


def compose(app_dir, *args, stdin=None):
    return subprocess.run(["docker", "compose", "-f", os.path.join(app_dir, "compose.yml"), "--project-directory", app_dir, *args],
                          input=stdin, capture_output=True, text=True, timeout=30)


def db_address(app_dir):
    ps = compose(app_dir, "ps", "-q", "db")
    cid = ps.stdout.strip()
    if ps.returncode or not cid:
        raise Failed("база приложения не запущена")
    r = subprocess.run(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}", cid],
                       capture_output=True, text=True, timeout=30)
    ips = r.stdout.split()
    if r.returncode or not ips:
        raise Failed("не нашли адрес контейнера базы")
    return ips[0]


def ensure_role(app_dir, db):
    """Роль airlok_ro с новым паролем; суперпользователь базы приложения меняет её через psql в
    контейнере. Текст запроса свой, без ввода человека: пароль — hex."""
    password = secrets.token_hex(24)
    sql = f"""
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{RO_ROLE}') THEN CREATE ROLE {RO_ROLE}; END IF;
END $$;
ALTER ROLE {RO_ROLE} WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS CONNECTION LIMIT 3 PASSWORD '{password}';
GRANT pg_read_all_data TO {RO_ROLE};
ALTER ROLE {RO_ROLE} SET default_transaction_read_only = on;
ALTER ROLE {RO_ROLE} SET statement_timeout = {STATEMENT_TIMEOUT_MS};
"""
    r = compose(app_dir, "exec", "-T", "db", "psql", "-v", "ON_ERROR_STOP=1", "-q", "-U", db["POSTGRES_USER"], "-d", db["POSTGRES_DB"], stdin=sql)
    if r.returncode:
        raise Failed("не удалось завести роль только на чтение: " + (r.stderr.strip() or r.stdout.strip())[-500:])
    path = os.path.join(app_dir, "shared", "db-ro.env")
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(f"PASSWORD={password}\n")
    os.replace(path + ".tmp", path)
    return password


def connect(app_dir, db):
    host = db_address(app_dir)
    ro = env_file(os.path.join(app_dir, "shared", "db-ro.env")) or {}
    password = ro.get("PASSWORD") or ensure_role(app_dir, db)
    try:
        return Conn(host, 5432, RO_ROLE, password, db["POSTGRES_DB"])
    except PGError as e:
        # 28P01 — неверный пароль, 28000 — роли нет: база пересоздана, заводим роль заново.
        if e.fields.get("C") not in ("28P01", "28000"):
            raise
    return Conn(host, 5432, RO_ROLE, ensure_role(app_dir, db), db["POSTGRES_DB"])


def run(data):
    app, stmts, max_rows, max_cell = parse_request(data)
    app_dir = os.path.join(APPS_ROOT, app)
    db = env_file(os.path.join(app_dir, "shared", "db.env"))
    if db is None or not os.path.isfile(os.path.join(app_dir, "compose.yml")):
        raise NoDatabase()
    if not db.get("POSTGRES_USER") or not db.get("POSTGRES_DB"):
        raise Failed("в shared/db.env нет POSTGRES_USER или POSTGRES_DB")
    try:
        conn = connect(app_dir, db)
    except OSError as e:
        raise Failed(f"не подключиться к базе: {e}") from None
    try:
        return {"results": execute(conn, stmts, max_rows, max_cell)}
    finally:
        conn.close()


def execute(conn, stmts, max_rows, max_cell):
    """Операторы по очереди в одной транзакции только на чтение; её откатывают в конце."""
    conn.simple(f"SET statement_timeout = {STATEMENT_TIMEOUT_MS}")
    conn.simple("BEGIN READ ONLY")
    # После первого запроса транзакцию уже не перевести в режим записи.
    conn.simple("SELECT 1")
    budget = [MAX_RESULT_CHARS]
    results = []
    for sql, params in stmts:
        columns, rows, truncated = conn.query(sql, params, max_rows, max_cell, budget)
        # COMMIT или ROLLBACK в запросе закончили бы транзакцию: следующий оператор шёл бы вне неё.
        if conn.status != b"T":
            raise PGError({"C": "25000", "M": "the statement ended the read-only transaction: COMMIT and ROLLBACK are not allowed"})
        results.append({"columns": columns, "rows": rows, "truncated": truncated})
    name_types(conn, results)
    conn.simple("ROLLBACK")
    return results


def main():
    try:
        out = run(json.load(sys.stdin))
    except PGError as e:
        position = e.fields.get("P", "")
        out = {"error": {"kind": "sql", "code": e.fields.get("C", "")[:5], "message": e.fields.get("M", "")[:2000],
                         "position": int(position) if position.isdigit() else 0}}
    except NoDatabase:
        out = {"error": {"kind": "no_database"}}
    except (Failed, ValueError, subprocess.SubprocessError, OSError) as e:
        out = {"error": {"kind": "failed", "message": str(e)[:2000]}}
    json.dump(out, sys.stdout, ensure_ascii=False)


if __name__ == "__main__":
    main()
