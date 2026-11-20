import importlib.util
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "bridge", str(Path(__file__).resolve().parents[1] / "workflows/job_search/muse_cdpa_bridge.py"))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

class BridgeTest(unittest.TestCase):
    def lead(self):
        return {"title": "Build a Python Excel automation",
                "author": {"login": "Hiroshimeow"},
                "body": "JOB_URL: https://www.freelancer.com/projects/python/small-job?ref=muse",
                "labels": [{"name": "cdpa-ready"}]}

    def test_parse_url_strips_tracking(self):
        u, t = mod.parse(self.lead())
        self.assertEqual(u, "https://www.freelancer.com/projects/python/small-job")
        self.assertIn("Excel", t)

    def test_reject_other_author(self):
        item = self.lead()
        item["author"]["login"] = "attacker"
        with self.assertRaises(ValueError):
            mod.parse(item)

    def test_no_private_host(self):
        item = self.lead()
        item["body"] = "JOB_URL: https://127.0.0.1:9225/api/tasks"
        with self.assertRaises(ValueError):
            mod.parse(item)

    def test_busy_endpoint(self):
        self.assertEqual(mod.available([{"status": "RUNNING",
                                        "task_id": "real-owner",
                                        "browser_endpoint_id": "9202"}]),
                         (False, "real-owner"))
        self.assertEqual(mod.available([]), (True, ""))

    def test_label(self):
        self.assertEqual(mod.labels(self.lead()), {"cdpa-ready"})

if __name__ == "__main__":
    unittest.main()
