import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "workflows/job_search"))
import muse_cdpa_followup as m

URL = "https://www.freelancer.com/projects/python/mock"

def issue():
    return {
        "number": 24, "title": "Python automation for Excel import",
        "author": {"login": "Hiroshimeow"},
        "body": "JOB_URL: " + URL,
        "labels": [{"name": "muse-lead"}, {"name": "cdpa-dispatched"}],
    }


class FollowUpTest(unittest.TestCase):
    def test_no_result_no_send(self):
        with patch.object(m, "stage_result", return_value=None):
            out = m.status([issue()])
        self.assertEqual(out[0]["status"], "AWAITING_OUTCOME")

    def test_applied_old_means_one_watch(self):
        with patch.object(m, "stage_result", return_value={
            "stage": "applied", "updated_at": "2026-10-12T00:00:00+00:00", "receipt": "https://example.com/receipt"
        }):
            out = m.status([issue()])
        self.assertEqual(out[0]["status"], "WATCH_READY")
        self.assertEqual(out[0]["number"], 1)

    def test_award_requires_receipt(self):
        with patch.object(m, "stage_result", return_value={"stage": "awarded", "receipt": ""}):
            out = m.status([issue()])
        self.assertEqual(out[0]["status"], "AWARD_MISSING_RECEIPT")

    def test_award_with_receipt_ready_to_work(self):
        with patch.object(m, "stage_result", return_value={
            "stage": "awarded", "receipt": "https://example.com/award"
        }):
            out = m.status([issue()])
        self.assertEqual(out[0]["status"], "WORK_READY")

    def test_paid_label(self):
        with patch.object(m, "stage_result", return_value={"stage": "paid", "receipt": "paid-confirmed"}):
            out = m.status([issue()])
        self.assertEqual(out[0]["label"], "cdpa-paid")

    def test_watch_limit(self):
        x = issue()
        x["labels"].extend({"name": "cdpa-watch-" + str(n)} for n in range(1, 5))
        with patch.object(m, "stage_result", return_value={
            "stage": "applied", "updated_at": "2026-10-12T00:00:00+00:00", "receipt": "https://example.com/receipt"
        }):
            out = m.status([x])
        self.assertEqual(out[0]["status"], "WATCH_LIMIT")

if __name__ == "__main__":
    unittest.main()
