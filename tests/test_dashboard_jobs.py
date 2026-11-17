import threading
import unittest

from lead_scraper.dashboard import _job_result, _submit_job


class FakeStreamlit:
    def __init__(self):
        self.session_state = {}


class DashboardJobTests(unittest.TestCase):
    def test_background_job_reports_actual_worker_stage_before_completion(self):
        streamlit = FakeStreamlit()
        start = threading.Event()
        release = threading.Event()

        def work(report):
            start.set()
            release.wait(timeout=5)
            report("Collecting records")
            report("Writing exports")
            return {"rows": []}

        _submit_job(streamlit, "maps", work)
        job = streamlit.session_state["jobs"]["maps"]
        self.assertEqual(job["stage"], "Queued")

        self.assertTrue(start.wait(timeout=5))
        release.set()
        self.assertEqual(job["future"].result(timeout=5), {"rows": []})
        self.assertEqual(job["stage"], "Writing exports")
        self.assertEqual(_job_result(streamlit, "maps"), {"rows": []})


if __name__ == "__main__":
    unittest.main()
