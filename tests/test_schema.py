import copy
import unittest

from support import LAKE, read_json

from lakedel.errors import AuditError
from lakedel.schema import coerce_value, parse_schema, schema_hash


def schema_raw(columns=None):
    return {
        "columns": columns
        or [
            {"name": "_row_pos", "type": "long", "nullable": False},
            {"name": "name", "type": "string", "nullable": False},
            {"name": "amount", "type": "long", "nullable": True},
            {"name": "day", "type": "date", "nullable": True},
        ]
    }


class SchemaTests(unittest.TestCase):
    def test_fixture_schema_hash_is_stable(self):
        raw = read_json(LAKE / "table.json")["schema"]
        first = parse_schema(copy.deepcopy(raw))
        second = parse_schema(copy.deepcopy(raw))
        self.assertEqual(first.schema_hash, second.schema_hash)
        self.assertEqual(first.schema_hash, schema_hash(first.columns))
        self.assertEqual(len(first.schema_hash), 64)

    def test_hash_changes_with_nullability(self):
        first = parse_schema(schema_raw())
        changed = schema_raw()
        changed["columns"][2]["nullable"] = False
        second = parse_schema(changed)
        self.assertNotEqual(first.schema_hash, second.schema_hash)

    def test_duplicate_column(self):
        raw = schema_raw()
        raw["columns"].append(
            {"name": "name", "type": "string", "nullable": False}
        )
        with self.assertRaises(AuditError) as ctx:
            parse_schema(raw)
        self.assertEqual(ctx.exception.code, "DUPLICATE_COLUMN")

    def test_row_pos_is_required(self):
        raw = schema_raw(
            [{"name": "name", "type": "string", "nullable": False}]
        )
        with self.assertRaises(AuditError) as ctx:
            parse_schema(raw)
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_unknown_type(self):
        raw = schema_raw()
        raw["columns"][1]["type"] = "binary"
        with self.assertRaises(AuditError) as ctx:
            parse_schema(raw)
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_bad_column_name(self):
        raw = schema_raw()
        raw["columns"][1]["name"] = "bad-name"
        with self.assertRaises(AuditError) as ctx:
            parse_schema(raw)
        self.assertEqual(ctx.exception.code, "SCHEMA_INVALID")

    def test_coerce_long_date_and_null(self):
        schema = parse_schema(schema_raw())
        amount = schema.column("amount")
        day = schema.column("day")
        name = schema.column("name")
        self.assertEqual(coerce_value(amount, "007"), (True, 7, ""))
        self.assertEqual(coerce_value(amount, ""), (True, None, ""))
        self.assertEqual(coerce_value(day, "2024-02-29"), (True, "2024-02-29", ""))
        self.assertFalse(coerce_value(day, "2024-02-30")[0])
        self.assertFalse(coerce_value(amount, True)[0])
        self.assertFalse(coerce_value(name, 3)[0])
        self.assertEqual(coerce_value(name, "alpha"), (True, "alpha", ""))


if __name__ == "__main__":
    unittest.main()
