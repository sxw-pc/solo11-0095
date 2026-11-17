import tempfile
import unittest
from pathlib import Path

from support import LAKE, read_json

from lakedel.fileio import read_data_files
from lakedel.models import DataFileSpec
from lakedel.schema import parse_schema
from lakedel.util import sha256_bytes


def data_schema():
    return parse_schema(read_json(LAKE / "table.json")["schema"])


def make_spec(
    root: Path,
    name: str,
    content: str,
    fmt: str = "csv",
    row_count: int = 0,
    file_id: str | None = None,
    sha: str | None = None,
    sequence: int = 1,
):
    path = root / name
    payload = content.encode("utf-8")
    path.write_bytes(payload)
    schema = data_schema()
    return DataFileSpec(
        file_id or name.replace(".", "_"),
        name,
        fmt,
        row_count,
        sha or sha256_bytes(payload),
        sequence,
        schema.schema_hash,
    )


class FileIOTests(unittest.TestCase):
    def test_reads_csv_and_jsonl_in_stable_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = make_spec(
                root, "z.csv",
                "_row_id,_row_pos,category,region,amount,event_date\n"
                "R2,1,beta,south,20,2024-01-02\n"
                "R1,0,alpha,north,10,2024-01-01\n",
                row_count=2, file_id="z",
            )
            second = make_spec(
                root, "a.jsonl",
                '{"_row_id":"R3","_row_pos":0,"category":"gamma",'
                '"region":"west","amount":30,"event_date":"2024-01-03"}\n',
                fmt="jsonl", row_count=1, file_id="a",
            )
            rows, findings = read_data_files(root, (first, second), data_schema())
            self.assertEqual(findings, [])
            self.assertEqual(
                [(row.file_id, row.row_pos, row.row_id) for row in rows],
                [("a", 0, "R3"), ("z", 0, "R1"), ("z", 1, "R2")],
            )
            self.assertEqual(rows[-1].values["amount"], 20)

    def test_csv_invalid_rows_blank_and_duplicate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = make_spec(
                root, "bad.csv",
                "_row_id,_row_pos,category,region,amount,event_date,extra\n"
                "A,0,a,n,1,2024-01-01,x\n"
                "\n"
                "B,0,b,n,2,2024-01-02,x\n"
                "C,nope,c,n,3,2024-01-03,x\n"
                "D,2,d,n,bad,2024-02-30,x\n",
                row_count=4, file_id="bad",
            )
            rows, findings = read_data_files(root, (spec,), data_schema())
            codes = [item.code for item in findings]
            self.assertIn("EXTRA_COLUMN", codes)
            self.assertIn("BLANK_LINE", codes)
            self.assertIn("DUPLICATE_ROW_POSITION", codes)
            self.assertGreaterEqual(codes.count("INVALID_VALUE"), 3)
            self.assertEqual(len(rows), 2)
            self.assertEqual([row.row_pos for row in rows], [0, 2])

    def test_missing_header_and_hash_and_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = make_spec(
                root, "missing.csv",
                "_row_id,category,region,amount,event_date\n"
                "R1,a,n,1,2024-01-01\n",
                row_count=99, file_id="missing", sha="0" * 64,
            )
            rows, findings = read_data_files(root, (spec,), data_schema())
            codes = [item.code for item in findings]
            self.assertIn("FILE_HASH_MISMATCH", codes)
            self.assertIn("MISSING_COLUMN", codes)
            self.assertIn("ROW_COUNT_MISMATCH", codes)
            self.assertEqual(rows, ())

    def test_duplicate_row_id_across_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = (
                "_row_id,_row_pos,category,region,amount,event_date\n"
                "R1,0,a,n,1,2024-01-01\n"
            )
            first = make_spec(root, "one.csv", content, row_count=1, file_id="one")
            second = make_spec(root, "two.csv", content, row_count=1, file_id="two")
            rows, findings = read_data_files(root, (first, second), data_schema())
            self.assertEqual(len(rows), 2)
            self.assertIn("DUPLICATE_ROW_ID", [item.code for item in findings])

    def test_jsonl_invalid_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = make_spec(
                root, "data.jsonl",
                '{"_row_id":"A","_row_pos":0,"category":"a",'
                '"region":"n","amount":1,"event_date":null}\n'
                "\n"
                "not-json\n"
                '{"_row_id":"B","_row_pos":0,"category":"b",'
                '"region":"n","amount":2,"event_date":null}\n'
                '{"_row_id":"C","_row_pos":1,"category":"c",'
                '"region":"n","amount":"bad","event_date":null}\n',
                fmt="jsonl", row_count=4, file_id="json",
            )
            rows, findings = read_data_files(root, (spec,), data_schema())
            codes = [item.code for item in findings]
            self.assertIn("BLANK_LINE", codes)
            self.assertIn("INVALID_JSON_LINE", codes)
            self.assertIn("DUPLICATE_ROW_POSITION", codes)
            self.assertIn("INVALID_VALUE", codes)
            self.assertEqual([row.row_id for row in rows], ["A", "C"])


if __name__ == "__main__":
    unittest.main()
