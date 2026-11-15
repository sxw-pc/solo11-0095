import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from support import LAKE, ROOT, copy_lake

from lakedel.cli import main


def invoke(args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(args)
    return code, out.getvalue(), err.getvalue()


class CliTests(unittest.TestCase):
    def test_doctor_json(self):
        code, output, _ = invoke(["doctor", "--workspace", str(LAKE), "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["rows"], 14)
        self.assertEqual(payload["delete_operations"], 10)
        self.assertEqual(payload["expected_visible"], 8)
        self.assertIn("STALE_SEQUENCE", payload["warning_codes"])

    def test_audit_explain_verify_flow(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "audit"
            db = out / "ledger.sqlite"
            code, output, _ = invoke([
                "audit", "--workspace", str(LAKE), "--out", str(out),
                "--db", str(db), "--format", "json,md",
            ])
            self.assertEqual(code, 0, output)
            self.assertTrue((out / "report.json").is_file())
            self.assertTrue((out / "report.md").is_file())
            code, output, _ = invoke([
                "explain", "--workspace", str(LAKE), "--row-id", "R0004"
            ])
            self.assertEqual(code, 0)
            payload = json.loads(output)
            self.assertTrue(payload["visible"])
            self.assertEqual(payload["reason_code"], "VISIBLE_NO_DELETE")
            code, output, _ = invoke(["verify-ledger", "--db", str(db)])
            self.assertEqual(code, 0)
            self.assertTrue(json.loads(output)["ok"])

    def test_explain_by_file_and_position(self):
        code, output, _ = invoke([
            "explain", "--workspace", str(LAKE),
            "--file-id", "base_001", "--row-pos", "1",
        ])
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertFalse(payload["visible"])
        self.assertEqual(payload["position_hits"], ["pos_010"])

    def test_audit_returns_two_on_expected_diff(self):
        with tempfile.TemporaryDirectory() as tmp:
            lake = copy_lake(Path(tmp) / "lake")
            expected = lake / "expected" / "visible_snap_001.csv"
            text = expected.read_text(encoding="utf-8")
            expected.write_text(
                text.replace("base_001,0,R0001,false", "base_001,0,R0001,true"),
                encoding="utf-8",
            )
            code, output, _ = invoke([
                "audit", "--workspace", str(lake),
                "--out", str(Path(tmp) / "audit"), "--format", "json",
            ])
            self.assertEqual(code, 2)
            self.assertIn("diffs: 1", output)

    def test_input_errors_return_three(self):
        code, _, _ = invoke(["doctor", "--workspace", "no-such-workspace"])
        self.assertEqual(code, 3)
        code, _, _ = invoke(["audit", "--workspace", str(LAKE), "--format", "pdf"])
        self.assertEqual(code, 3)
        code, _, _ = invoke([
            "explain", "--workspace", str(LAKE), "--row-id", "missing"
        ])
        self.assertEqual(code, 3)
        code, _, _ = invoke(["verify-ledger", "--db", "missing.sqlite"])
        self.assertEqual(code, 3)

    def test_module_demo_runs_without_installation(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [sys.executable, "-m", "lakedel", "demo", "--out", str(Path(tmp) / "demo")],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("行删镜演示审计完成", proc.stdout)
            self.assertTrue((Path(tmp) / "demo" / "report.html").is_file())

    def test_no_command_prints_help(self):
        code, output, _ = invoke([])
        self.assertEqual(code, 0)
        self.assertIn("doctor", output)
        self.assertIn("verify-ledger", output)


if __name__ == "__main__":
    unittest.main()
