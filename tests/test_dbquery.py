"""Запросы к базе приложения на своём сервере: python3 -m unittest discover tests

Разбор сообщений протокола Postgres и вход SCRAM проверяются без базы; живую проверку делает
dbquery.py на сервере.
"""
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "recipes"))

import dbquery  # noqa: E402


def request(**over):
    return {"app": "shop", "statements": [{"sql": "SELECT 1", "params": []}], "max_rows": 50, "max_cell": 300, **over}


class RequestTest(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(dbquery.parse_request(request()), ("shop", [("SELECT 1", [])], 50, 300))

    def test_rejects(self):
        for bad in (request(app="../etc"), request(app="Shop"), request(statements=[]), request(statements=[{"sql": " "}]),
                    request(statements=[{"sql": "select\0"}]), request(statements=[{"sql": "x", "params": [1]}]),
                    request(statements=[{"sql": "x"}] * 6), request(max_rows=0), request(max_rows=True),
                    request(max_cell=10_001), "SELECT 1"):
            with self.assertRaises(dbquery.Failed, msg=bad):
                dbquery.parse_request(bad)


class ProtocolTest(unittest.TestCase):
    def test_row_description(self):
        field = lambda name, oid: name.encode() + b"\0" + struct.pack("!ihihih", 0, 0, oid, 4, -1, 0)  # noqa: E731
        body = struct.pack("!h", 2) + field("id", 23) + field("имя", 25)
        self.assertEqual(dbquery.row_description(body), [{"name": "id", "oid": 23}, {"name": "имя", "oid": 25}])

    def test_data_row(self):
        cell = lambda s: struct.pack("!i", len(s.encode())) + s.encode()  # noqa: E731
        body = struct.pack("!h", 3) + cell("Анна") + struct.pack("!i", -1) + cell("я" * 10)
        self.assertEqual(dbquery.data_row(body, 5), ["Анна", None, "яяяяя…"], "NULL — не пустая строка, длинное обрезано")

    def test_error_fields(self):
        body = b"SERROR\0C42601\0Msyntax error\0P8\0\0"
        self.assertEqual(dbquery.error_fields(body), {"S": "ERROR", "C": "42601", "M": "syntax error", "P": "8"})


class ScramTest(unittest.TestCase):
    def test_rfc7677_example(self):
        s = dbquery.Scram("pencil", nonce="rOprNGfwEbeRWgbNEkqO", user="user")
        self.assertEqual(s.client_first(), "n,,n=user,r=rOprNGfwEbeRWgbNEkqO")
        final = s.client_final("r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0,s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096")
        self.assertEqual(final, "c=biws,r=rOprNGfwEbeRWgbNEkqO%hvYDpWUa2RaTCAfuxFIlj)hNlF$k0,p=dHzbZapWIk4jUhN+Ute9ytag9zjfMHgsqmmiz7AndVQ=")
        s.verify("v=6rriTRBi23WpRR/wtup+mMhUZUn/dB5nLTJRsjl95G4=")
        with self.assertRaises(dbquery.Failed):
            s.verify("v=AAAA")

    def test_foreign_nonce(self):
        s = dbquery.Scram("pencil", nonce="abc")
        s.client_first()
        with self.assertRaises(dbquery.Failed):
            s.client_final("r=xyz,s=W22ZaJ0SNY7soEsUEjb6gQ==,i=4096")


if __name__ == "__main__":
    unittest.main()
