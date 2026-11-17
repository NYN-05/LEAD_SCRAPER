"""Dashboard smoke tests for the Google Maps section."""
import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lead_scraper.storage import LeadDatabase

try:
    from streamlit.testing.v1 import AppTest
except ImportError:  # pragma: no cover - declared dependency
    AppTest = None

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(AppTest is None, "streamlit is not installed")
class MapsDashboardTests(unittest.TestCase):
    def _maps_app(self):
        app = AppTest.from_file(str(PROJECT_ROOT / "main.py"), default_timeout=60)
        app.run()
        self.assertEqual(list(app.exception), [])
        app.radio[0].set_value("Google Maps").run()
        self.assertEqual(list(app.exception), [])
        return app

    def test_maps_section_renders_inputs_table_and_exports(self):
        app = self._maps_app()

        self.assertIn("Business keyword", {item.label for item in app.text_input})
        self.assertIn("Location", {item.label for item in app.text_input})
        self.assertIn("Maximum results", {item.label for item in app.number_input})
        self.assertIn("Start Maps collection", {item.label for item in app.button})
        self.assertGreaterEqual(len(app.dataframe), 1)
        self.assertEqual(
            {item.label for item in app.get("download_button")},
            {"Download CSV", "Download JSON", "Download XLSX"},
        )

    def test_empty_keyword_is_rejected_without_starting_a_job(self):
        app = self._maps_app()
        app.text_input[0].set_value("")

        submit = next(
            item for item in app.button if item.label == "Start Maps collection"
        )
        submit.click().run()

        self.assertEqual(list(app.exception), [])
        self.assertIn(
            "Keyword and location are required.",
            {item.value for item in app.error},
        )


@unittest.skipIf(AppTest is None, "streamlit is not installed")
class SocialDashboardTests(unittest.TestCase):
    def _social_app(self):
        app = AppTest.from_file(str(PROJECT_ROOT / "main.py"), default_timeout=60)
        app.run()
        self.assertEqual(list(app.exception), [])
        app.radio[0].set_value("LinkedIn / Instagram").run()
        self.assertEqual(list(app.exception), [])
        return app

    def test_social_section_renders_forms_tables_and_exports(self):
        app = self._social_app()

        self.assertEqual({tab.label for tab in app.tabs}, {"LinkedIn", "Instagram"})
        self.assertIn("Direct profile URLs", {item.label for item in app.text_area})
        self.assertIn("Keywords", {item.label for item in app.text_input})
        self.assertIn("Hashtag", {item.label for item in app.text_input})
        self.assertIn("Location", {item.label for item in app.text_input})
        self.assertIn("Maximum profiles", {item.label for item in app.number_input})
        self.assertLessEqual(
            {"Start Linkedin collection", "Start Instagram collection"},
            {item.label for item in app.button},
        )
        self.assertGreaterEqual(len(app.dataframe), 2)
        download_labels = [item.label for item in app.get("download_button")]
        self.assertEqual(download_labels.count("Download CSV"), 2)
        self.assertEqual(download_labels.count("Download XLSX"), 2)

    def test_empty_social_form_is_rejected_without_starting_a_job(self):
        app = self._social_app()
        app.text_area[0].set_value("")
        for index in (0, 1, 2):
            app.text_input[index].set_value("")

        submit = next(
            item for item in app.button if item.label == "Start Linkedin collection"
        )
        submit.click().run()

        self.assertEqual(list(app.exception), [])
        self.assertIn(
            "Provide a URL, keyword, hashtag, or location.",
            {item.value for item in app.error},
        )
        self.assertEqual(app.session_state.get("jobs"), {})


class SocialJobTests(unittest.TestCase):
    def _row(self):
        return {
            "username": "sample.user",
            "full_name": "Sample User",
            "bio": "Photographer",
            "followers": "10",
            "following": "20",
            "post_count": "3",
            "email_or_phone": "",
            "profile_url": "https://www.instagram.com/sample.user/",
            "data_quality": "partially_enriched_unverified",
            "field_status": {"username": "extracted"},
        }

    def test_job_persists_and_exports_persisted_records_after_empty_run(self):
        import lead_scraper.dashboard as dashboard

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            def database():
                return LeadDatabase(root / "leads.db")

            def run(rows):
                with patch.object(dashboard, "_database", side_effect=database), \
                     patch.object(dashboard, "OUTPUT_ROOT", root), \
                     patch(
                         "lead_scraper.scrapers.scrape_platform",
                         return_value=rows,
                     ):
                    return dashboard._social_job(
                        "instagram", "", "photographer", "", "Bengaluru", 10,
                        lambda message: None,
                    )

            first = run([self._row()])
            second = run([])

            self.assertEqual(first["total"], 1)
            self.assertEqual(second["total"], 1)

            connection = database()
            try:
                stored = connection.get_social_profiles("instagram")
            finally:
                connection.close()
            self.assertEqual(len(stored), 1)

            for path in (
                root / "social" / "instagram_profiles.csv",
                root / "instagram_profiles.csv",
            ):
                with path.open(encoding="utf-8-sig", newline="") as handle:
                    self.assertEqual(len(list(csv.DictReader(handle))), 1)


if __name__ == "__main__":
    unittest.main()
