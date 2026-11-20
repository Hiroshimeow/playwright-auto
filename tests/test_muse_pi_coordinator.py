import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflows" / "job_search"))
import muse_pi_coordinator as pi


class MusePiCoordinatorTests(unittest.TestCase):
    def test_engineering_model(self):
        result = pi.command("engineering", "Inspect a bounded real bug and write a focused test.")
        self.assertIn("cliproxy-2080/gpt-6.1-sol", result)
        self.assertIn("high", result)

    def test_monitoring_model(self):
        result = pi.command("monitoring", "Review a durable task outcome and the expected next action.")
        self.assertIn("cliproxy-2080/gpt-5.6-luna", result)
        self.assertIn("medium", result)

    def test_review_model(self):
        result = pi.command("review", "Independently critique this existing repair and recommend exact tests.")
        self.assertIn("cliproxy-2080/gpt-6.1-sol", result)

    def test_unknown_kind(self):
        with self.assertRaises(ValueError):
            pi.command("new-unapproved-mode", "Review a durable task outcome and give next action.")

    def test_missing_pi_cli(self):
        with patch.object(pi, "PI", Path("Z:/never-exists/pi.js")):
            with self.assertRaises(FileNotFoundError):
                pi.command("monitoring", "Review a durable task outcome and give next action.")

    def test_dry_run_creates_no_process(self):
        with patch.object(pi.subprocess, "Popen") as popen:
            result = pi.run("planning", "Prioritize the next verified acquisition task.", dry_run=True)
        self.assertEqual(result["state"], "READY")
        popen.assert_not_called()

    def test_started_receipt_is_not_completion(self):
        with tempfile.TemporaryDirectory() as d:
            with patch.object(pi, "JOBS", Path(d)):
                with patch.object(pi.subprocess, "Popen", return_value=MagicMock(pid=1234)):
                    result=pi.run("monitoring", "Review the current task status and report the next step.")
                self.assertEqual(result["state"], "STARTED")
                self.assertTrue((Path(d)/(result["id"]+".json")).exists())


if __name__ == "__main__":
    unittest.main()
