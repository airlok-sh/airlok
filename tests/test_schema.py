"""Схема airlok.yml (schema/airlok.schema.json): python3 -m unittest discover tests

Схему видят редакторы и ИИ-агенты, поэтому она должна принимать каждый пример из README,
навыков и examples/ и отклонять опечатки. Проверяется небольшим валидатором — только тем
подмножеством JSON Schema, что есть в схеме.
"""
import glob
import json
import os
import re
import unittest

import yaml

ROOT = os.path.join(os.path.dirname(__file__), "..")
with open(os.path.join(ROOT, "schema", "airlok.schema.json"), encoding="utf-8") as f:
    SCHEMA = json.load(f)
TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def errors(value, schema, path="airlok.yml"):
    if "$ref" in schema:
        schema = {**SCHEMA["definitions"][schema["$ref"].rsplit("/", 1)[1]], **{k: v for k, v in schema.items() if k != "$ref"}}
    out = []
    t = schema.get("type")
    if t == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
        return [f"{path}: ждали целое число"]
    if t in TYPES and (not isinstance(value, TYPES[t]) or (t != "boolean" and isinstance(value, bool))):
        return [f"{path}: ждали {t}"]
    if "const" in schema and value != schema["const"]:
        out.append(f"{path}: ждали {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{path}: {value!r} не из {schema['enum']}")
    if "not" in schema and not errors(value, schema["not"], path):
        out.append(f"{path}: {value!r} запрещено")
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            out.append(f"{path}: {value!r} не подходит под {schema['pattern']}")
        if len(value) > schema.get("maxLength", 1 << 30):
            out.append(f"{path}: длиннее {schema['maxLength']}")
    if isinstance(value, int) and not isinstance(value, bool):
        if value < schema.get("minimum", value) or value > schema.get("maximum", value):
            out.append(f"{path}: {value} вне [{schema.get('minimum')}, {schema.get('maximum')}]")
    if isinstance(value, list):
        if not schema.get("minItems", 0) <= len(value) <= schema.get("maxItems", 1 << 30):
            out.append(f"{path}: число элементов вне пределов")
        for i, item in enumerate(value):
            out += errors(item, schema.get("items", {}), f"{path}[{i}]")
    if isinstance(value, dict):
        if len(value) > schema.get("maxProperties", 1 << 30):
            out.append(f"{path}: больше {schema['maxProperties']} полей")
        out += [f"{path}: нет поля {k}" for k in schema.get("required", []) if k not in value]
        props = schema.get("properties", {})
        for k, v in value.items():
            if "propertyNames" in schema:
                out += errors(k, schema["propertyNames"], f"{path}.{k} (имя)")
            if k in props:
                out += errors(v, props[k], f"{path}.{k}")
            elif schema.get("additionalProperties") is False:
                out.append(f"{path}: неизвестное поле {k}")
            elif isinstance(schema.get("additionalProperties"), dict):
                out += errors(v, schema["additionalProperties"], f"{path}.{k}")
    if "oneOf" in schema:
        if sum(1 for s in schema["oneOf"] if not errors(value, s, path)) != 1:
            out.append(f"{path}: не подходит ни под один вариант")
    return out


def doc_examples():
    """Примеры airlok.yml из документации: блоки ```yaml с version или env в начале."""
    files = [os.path.join(ROOT, p) for p in ("README.md", "recipes/README.md", "cli/README.md")]
    files += glob.glob(os.path.join(ROOT, "skills", "*", "SKILL.md"))
    for path in files:
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for block in re.findall(r"```yaml\n(.*?)```", f.read(), re.S):
                data = yaml.safe_load(block)
                if isinstance(data, dict) and ("version" in data or "env" in data):
                    yield os.path.relpath(path, ROOT), {"version": 1, **data}


class SchemaTest(unittest.TestCase):
    def test_accepts_documented_examples(self):
        found = 0
        for name, data in doc_examples():
            found += 1
            self.assertEqual(errors(data, SCHEMA), [], name)
        self.assertGreaterEqual(found, 4, "примеры из README и навыков должны находиться")

    def test_accepts_examples(self):
        paths = glob.glob(os.path.join(ROOT, "examples", "*", "airlok.yml"))
        self.assertTrue(paths)
        for path in paths:
            with open(path, encoding="utf-8") as f:
                self.assertEqual(errors(yaml.safe_load(f), SCHEMA), [], path)

    def test_rejects_mistakes(self):
        for data in ({"capabilites": {}},
                     {"version": 2},
                     {"run": {"health": "healthz"}},
                     {"run": {"health": "//evil.example/x"}},
                     {"run": {"command": "a\nb"}},
                     {"runtime": {"node": "18"}},
                     {"capabilities": {"uploads": {"enabled": True, "allowed": []}}},
                     {"env": {"lower": "x"}},
                     {"env": {"PORT": "порт"}},
                     {"env": {"A": {"requred": True}}},
                     {"env": {"A": {"build": "yes"}}},
                     {"resources": {"memory_mb": 64}}):
            data = {"version": 1, **data}
            self.assertTrue(errors(data, SCHEMA), data)

    def test_env_forms(self):
        for env in (["OPENAI_API_KEY"], {"A": None}, {"A": "описание"}, {"A": {"secret": True, "build": True, "default": "1"}}):
            self.assertEqual(errors({"version": 1, "env": env}, SCHEMA), [], env)


if __name__ == "__main__":
    unittest.main()
