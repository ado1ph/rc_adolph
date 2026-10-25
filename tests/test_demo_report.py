import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

if sys.version_info >= (3, 12):
    from notifier.production.demo import scenario_report


@unittest.skipUnless(sys.version_info >= (3, 12), "Production demo requires Python 3.12")
class DemoReportTests(unittest.TestCase):
    def test_scenario_report_contains_only_the_visualization_contract(self):
        result = {
            "snapshot": "do-not-export",
            "attempts": [
                {"number": 1, "outcome": "retrying", "http_status": 503,
                 "error": "http_503", "duration_ms": 2},
                {"number": 2, "outcome": "retrying", "http_status": 503,
                 "error": "http_503", "duration_ms": 2},
                {"number": 3, "outcome": "succeeded", "http_status": 200,
                 "error": None, "duration_ms": 3},
            ],
        }
        report = scenario_report(
            "retry_after_503", "idempotent", ["pending", "succeeded"],
            result, 3, 1, True,
        )
        self.assertEqual(set(report), {
            "name", "mode", "observed_statuses", "attempts", "supplier_calls",
            "supplier_effects", "idempotent_receipt_reused",
        })
        self.assertEqual(report["attempts"], [
            {"number": 1, "outcome": "retrying", "http_status": 503, "error": "http_503"},
            {"number": 2, "outcome": "retrying", "http_status": 503, "error": "http_503"},
            {"number": 3, "outcome": "succeeded", "http_status": 200, "error": None},
        ])
        self.assertEqual(report["supplier_calls"], 3)
        self.assertEqual(report["supplier_effects"], 1)
        self.assertNotIn("do-not-export", json.dumps(report))

    def test_cli_refuses_to_overwrite_an_existing_report(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text("original report")
            result = subprocess.run(
                [sys.executable, "-m", "notifier.production.demo", "--output", str(path)],
                capture_output=True, text=True, timeout=5,
                env={**os.environ, "NOTIFY_DATABASE_URL": "postgresql://do-not-read:secret@invalid/db"},
            )
            self.assertEqual(result.returncode, 1)
            self.assertFalse(json.loads(result.stdout)["ok"])
            self.assertIn("already exists", json.loads(result.stdout)["error"])
            self.assertNotIn("secret", result.stdout + result.stderr)
            self.assertEqual(path.read_text(), "original report")

    def test_cli_rejects_a_missing_report_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing" / "report.json"
            result = subprocess.run(
                [sys.executable, "-m", "notifier.production.demo", "--output", str(path)],
                capture_output=True, text=True, timeout=5,
            )
            self.assertEqual(result.returncode, 1)
            self.assertFalse(json.loads(result.stdout)["ok"])
            self.assertIn("does not exist", json.loads(result.stdout)["error"])
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
