import csv
import tempfile
import unittest
from pathlib import Path

from lead_scraper.storage import MAP_FIELDS, LeadDatabase, export_maps_from_database


class MapsStorageTests(unittest.TestCase):
    def test_exports_persisted_unique_maps_records_after_an_empty_run(self):
        lead = {
            "business_name": "Example Studio",
            "category": "Photography studio",
            "address": "1 Example Road, Bengaluru",
            "phone": "+91 9000000000",
            "website": "https://example.test",
            "rating": "4.8",
            "reviews": "12",
            "maps_url": "https://www.google.com/maps/place/Example+Studio",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = LeadDatabase(root / "leads.db")
            try:
                database.save_map_lead(lead)
                database.save_map_lead({**lead, "rating": "4.9"})
                exported = export_maps_from_database(database, root / "exports")
            finally:
                database.close()

            self.assertEqual(exported, [{**lead, "rating": "4.9"}])
            with (root / "exports" / "google_maps_leads.csv").open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows, [{key: str(value) for key, value in exported[0].items()}])

    def test_minimal_record_exports_all_columns_with_empty_optional_values(self):
        lead = {
            "business_name": "Minimal Studio",
            "maps_url": "https://www.google.com/maps/place/Minimal+Studio",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = LeadDatabase(root / "leads.db")
            try:
                database.save_map_lead(lead)
                export_maps_from_database(database, root / "exports")
            finally:
                database.close()

            with (root / "exports" / "google_maps_leads.csv").open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                reader = csv.DictReader(handle)
                self.assertEqual(reader.fieldnames, MAP_FIELDS)
                row = next(reader)
            for field in ("category", "address", "phone", "website", "rating", "reviews"):
                self.assertEqual(row[field], "")

            import pandas as pd

            frame = pd.read_excel(
                root / "exports" / "google_maps_leads.xlsx", dtype=str
            )
            self.assertEqual(list(frame.columns), MAP_FIELDS)
            self.assertEqual(frame.loc[0, "business_name"], "Minimal Studio")
            self.assertTrue(pd.isna(frame.loc[0, "phone"]))

    def test_collection_errors_are_persisted_with_the_maps_source(self):
        with tempfile.TemporaryDirectory() as directory:
            database = LeadDatabase(Path(directory) / "leads.db")
            try:
                database.save_collection_error(
                    "maps", '{"stage": "place_extraction", "error": "timeout"}'
                )
                errors = database.get_collection_errors()
            finally:
                database.close()

        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["source"], "maps")
        self.assertIn("timeout", errors[0]["error"])


if __name__ == "__main__":
    unittest.main()
