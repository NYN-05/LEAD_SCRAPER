"""CLI behavior tests for the Google Maps command."""
import unittest
from unittest.mock import patch

from main import run_maps


class FailingScraper:
    def __init__(self, **kwargs):
        self.errors = [
            {"stage": "browser", "url": "", "error": "RuntimeError: browser failed"}
        ]

    def scrape(self, keyword, location, maximum):
        return []


class EmptyScraper:
    def __init__(self, **kwargs):
        self.errors = []

    def scrape(self, keyword, location, maximum):
        return []


class MapsCliTests(unittest.TestCase):
    def test_empty_keyword_returns_a_usage_error(self):
        self.assertEqual(
            run_maps(["--keyword", "", "--location", "Bengaluru"]), 2
        )

    def test_zero_max_results_is_rejected_by_argparse(self):
        with self.assertRaises(SystemExit) as raised:
            run_maps(
                [
                    "--keyword", "photography studio",
                    "--location", "Bengaluru",
                    "--max-results", "0",
                ]
            )
        self.assertEqual(raised.exception.code, 2)

    def test_failed_run_returns_exit_code_one_but_keeps_exports(self):
        with patch("main.MapsScraper", FailingScraper), patch(
            "main.LeadDatabase"
        ) as database, patch(
            "main.export_maps_from_database", return_value=[]
        ) as exporter:
            code = run_maps(
                ["--keyword", "photography studio", "--location", "Bengaluru"]
            )

        self.assertEqual(code, 1)
        database.return_value.save_collection_error.assert_called_once()
        self.assertEqual(exporter.call_count, 2)
        database.return_value.close.assert_called_once()

    def test_empty_search_without_errors_returns_success(self):
        with patch("main.MapsScraper", EmptyScraper), patch(
            "main.LeadDatabase"
        ), patch("main.export_maps_from_database", return_value=[]):
            code = run_maps(
                ["--keyword", "no such studio", "--location", "Bengaluru"]
            )

        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
