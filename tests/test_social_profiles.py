import json
import tempfile
import unittest
from pathlib import Path

from lead_scraper.scrapers import (
    is_aggregated_linkedin,
    normalize_profile_records,
    parse_instagram,
    parse_linkedin,
)
from lead_scraper.storage import (
    LeadDatabase,
    export_social_profile_from_database,
    export_social_profiles,
)


class SocialProfileTests(unittest.TestCase):
    def test_normalization_deduplicates_urls_and_marks_field_status(self):
        rows = [
            {
                "name": "Jane Doe",
                "headline": "Engineer",
                "profile_url": "https://www.linkedin.com/in/jane-doe/?trk=foo",
            },
            {
                "name": "Duplicate",
                "profile_url": "https://linkedin.com/in/jane-doe/",
            },
        ]

        normalized = normalize_profile_records("linkedin", rows)

        self.assertEqual(len(normalized), 1)
        self.assertEqual(normalized[0]["profile_url"], "https://www.linkedin.com/in/jane-doe/")
        self.assertEqual(normalized[0]["field_status"]["name"], "extracted")
        self.assertEqual(normalized[0]["field_status"]["about"], "unavailable")
        self.assertNotIn("data_source", normalized[0]["field_status"])

    def test_linkedin_single_profile_url_in_snippet_is_not_rejected(self):
        record = parse_linkedin(
            {
                "title": "Jane Doe - Engineer at Example Corp | LinkedIn",
                "body": "https://www.linkedin.com/in/jane-doe/ Based in Bengaluru",
            },
            "https://www.linkedin.com/in/jane-doe/",
        )

        self.assertEqual(record["name"], "Jane Doe")
        self.assertEqual(record["current_company"], "Example Corp")
        self.assertEqual(record["location"], "Bengaluru")

    def test_linkedin_snippet_experience_and_about_are_used_when_public(self):
        record = parse_linkedin(
            {
                "title": "Jane Doe - Data Scientist at Example Corp | LinkedIn",
                "body": (
                    "I build production machine learning systems for retail teams "
                    "and love mentoring. · Experience: Example Corp · Education: "
                    "Sample University · Location: Bengaluru · 500+ connections"
                ),
            },
            "https://www.linkedin.com/in/jane-doe/",
        )

        self.assertEqual(record["headline"], "Data Scientist")
        self.assertEqual(record["current_company"], "Example Corp")
        self.assertEqual(record["location"], "Bengaluru")
        self.assertTrue(record["about"].startswith("I build production machine learning"))

    def test_linkedin_industrial_experience_prose_is_not_a_company(self):
        record = parse_linkedin(
            {
                "title": "Ramesh K - Marketing Analytics Professional | Lead Data Scientist",
                "body": (
                    "Marketing Analytics Professional | Lead Data Scientist · "
                    "Industrial Experience: Technology, FMCG, Retail (CPG), Media "
                    "Tools: BASE SAS, SQL, R"
                ),
            },
            "https://www.linkedin.com/in/ramesh-k/",
        )

        self.assertEqual(record["current_company"], "")

    def test_aggregated_title_is_recovered_when_slug_matches_first_name(self):
        record = parse_linkedin(
            {
                "title": (
                    "Keerthana Rangaraj - Data Scientist | Computer Vision, ML "
                    "...Suman Datta - Principal Data and Applied Scientist | LinkedIn"
                ),
                "body": "",
            },
            "https://in.linkedin.com/in/keerthana-rangaraj-126470188/",
        )

        self.assertEqual(record["name"], "Keerthana Rangaraj")
        self.assertEqual(record["headline"], "Data Scientist | Computer Vision, ML")
        self.assertNotEqual(record["verification"], "rejected_aggregated_or_ambiguous_snippet")

    def test_aggregated_title_is_rejected_when_slug_does_not_match(self):
        title = (
            "Suman Datta - Principal Data and Applied Scientist "
            "...Akash Radhakrishna Pillai - MSc Data & Business Analytics | LinkedIn"
        )
        self.assertTrue(is_aggregated_linkedin(title, "", "https://www.linkedin.com/in/unrelated-slug/"))
        record = parse_linkedin({"title": title, "body": ""}, "https://www.linkedin.com/in/unrelated-slug/")
        self.assertEqual(record["verification"], "rejected_aggregated_or_ambiguous_snippet")

    def test_role_segments_do_not_make_a_title_aggregated(self):
        title = "Simran Naik - Data Scientist - I at Allen Digital | LinkedIn"
        self.assertFalse(is_aggregated_linkedin(title, "", "https://www.linkedin.com/in/simran-naik-402386187/"))
        record = parse_linkedin(
            {"title": title, "body": ""},
            "https://www.linkedin.com/in/simran-naik-402386187/",
        )
        self.assertEqual(record["name"], "Simran Naik")
        self.assertEqual(record["headline"], "Data Scientist")

    def test_social_profiles_are_saved_and_exported_per_platform(self):
        linkedin = [{
            "name": "Jane Doe",
            "headline": "Engineer",
            "current_company": "",
            "location": "",
            "about": "",
            "profile_url": "https://www.linkedin.com/in/jane-doe/",
            "data_quality": "partially_enriched_unverified",
            "field_status": {"name": "extracted"},
        }]
        instagram = [{
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
        }]

        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "output"
            export_social_profiles(output_dir, linkedin, instagram)
            self.assertTrue((output_dir / "linkedin_profiles.json").is_file())
            self.assertTrue((output_dir / "instagram_profiles.json").is_file())
            self.assertTrue((output_dir / "linkedin_profiles.xlsx").is_file())
            self.assertTrue((output_dir / "instagram_profiles.xlsx").is_file())
            self.assertEqual(json.loads((output_dir / "linkedin_profiles.json").read_text())["total_profiles"], 1)

            database = LeadDatabase(Path(directory) / "profiles.db")
            try:
                self.assertTrue(database.save_social_profile("linkedin", linkedin[0]))
                self.assertTrue(database.save_social_profile("linkedin", linkedin[0]))
                self.assertEqual(len(database.get_social_profiles("linkedin")), 1)
            finally:
                database.close()


class LinkedInSegmentTests(unittest.TestCase):
    URL = "https://www.linkedin.com/in/jane-doe/"

    def test_title_location_segment_is_not_stored_as_company(self):
        record = parse_linkedin(
            {
                "title": "Jane Doe - Data Scientist - Bengaluru, Karnataka | LinkedIn",
                "body": "",
            },
            self.URL,
        )

        self.assertEqual(record["name"], "Jane Doe")
        self.assertEqual(record["headline"], "Data Scientist")
        self.assertEqual(record["current_company"], "")
        self.assertEqual(record["location"], "Bengaluru, Karnataka")

    def test_trailing_city_is_split_from_the_company(self):
        record = parse_linkedin(
            {
                "title": "Jane Doe - Senior Data Scientist at Tesco, Bengaluru ...",
                "body": "",
            },
            self.URL,
        )

        self.assertEqual(record["headline"], "Senior Data Scientist")
        self.assertEqual(record["current_company"], "Tesco")
        self.assertEqual(record["location"], "Bengaluru")

    def test_experience_label_with_city_is_split(self):
        record = parse_linkedin(
            {
                "title": "Jane Doe - Data Scientist | LinkedIn",
                "body": "Experience: Optum, Bengaluru · Education: Sample University",
            },
            self.URL,
        )

        self.assertEqual(record["current_company"], "Optum")
        self.assertEqual(record["location"], "Bengaluru")

    def test_legal_company_suffix_is_not_treated_as_a_location(self):
        record = parse_linkedin(
            {"title": "Jane Doe - Engineer at Example, Inc. | LinkedIn", "body": ""},
            self.URL,
        )

        self.assertEqual(record["current_company"], "Example, Inc.")

    def test_postal_code_is_not_stored_as_location(self):
        record = parse_linkedin(
            {
                "title": "Jane Doe - Data Scientist | LinkedIn",
                "body": "Location: 562130 · 500+ connections",
            },
            self.URL,
        )
        self.assertEqual(record["location"], "")

    def test_postal_code_prefix_is_removed_from_location(self):
        record = parse_linkedin(
            {
                "title": "Jane Doe - Data Scientist | LinkedIn",
                "body": "Location: 560001, Bengaluru · 500+ connections",
            },
            self.URL,
        )
        self.assertEqual(record["location"], "Bengaluru")

    def test_punctuation_only_headline_is_empty(self):
        record = parse_linkedin(
            {"title": "Jane Doe - -- | LinkedIn", "body": ""}, self.URL
        )
        self.assertEqual(record["headline"], "")

    def test_person_name_is_not_stored_as_company(self):
        record = parse_linkedin(
            {"title": "Jane Doe - Photography at Jane Doe | LinkedIn", "body": ""},
            self.URL,
        )
        self.assertEqual(record["headline"], "Photography")
        self.assertEqual(record["current_company"], "")


class InstagramParserTests(unittest.TestCase):
    URL = "https://www.instagram.com/sample.user/"

    def test_full_snippet_extracts_identity_metrics_bio_and_phone(self):
        record = parse_instagram(
            {
                "title": "Sample User | Photographer (@sample.user) • Instagram photos",
                "body": (
                    "5,117 Followers, 1,504 Following, 479 Posts - Sample User | "
                    "Photographer (@sample.user) on Instagram: \"Wedding & Lifestyle "
                    "Photographer in Bengaluru | +91 7736388774\""
                ),
            },
            self.URL,
        )

        self.assertEqual(record["username"], "sample.user")
        self.assertEqual(record["full_name"], "Sample User | Photographer")
        self.assertEqual(
            record["bio"],
            "Wedding & Lifestyle Photographer in Bengaluru | +91 7736388774",
        )
        self.assertEqual(record["followers"], "5,117")
        self.assertEqual(record["following"], "1,504")
        self.assertEqual(record["post_count"], "479")
        self.assertIn("+91 7736388774", record["email_or_phone"])

    def test_page_title_boilerplate_is_not_a_display_name_or_bio(self):
        record = parse_instagram(
            {
                "title": "Bangalore Photographers (@bangalore_photographers ...",
                "body": (
                    "1,621 Followers, 1,098 Following, 121 Posts - See Instagram "
                    "photos and videos from Bangalore Photographers "
                    "(@bangalore_photographers)"
                ),
            },
            "https://www.instagram.com/bangalore_photographers/",
        )

        self.assertEqual(record["full_name"], "Bangalore Photographers")
        self.assertEqual(record["bio"], "")
        self.assertEqual(record["followers"], "1,621")
        self.assertEqual(record["following"], "1,098")
        self.assertEqual(record["post_count"], "121")

    def test_long_stats_prefix_is_fully_removed_from_display_name(self):
        record = parse_instagram(
            {
                "title": "Bangalore photographer baby maternity family (@nivetha ...",
                "body": (
                    "9,033 Followers, 1,573 Following, 1,258 Posts - See Instagram "
                    "photos and videos from Bangalore photographer baby maternity "
                    "family (@nivetha.photography)"
                ),
            },
            "https://www.instagram.com/nivetha.photography/",
        )

        self.assertEqual(
            record["full_name"], "Bangalore photographer baby maternity family"
        )
        self.assertEqual(record["bio"], "")
        self.assertEqual(record["followers"], "9,033")

    def test_email_in_bio_is_captured(self):
        record = parse_instagram(
            {
                "title": "Sample User (@sample.user) • Instagram photos",
                "body": (
                    "900 Followers, 100 Following, 12 Posts - Sample User "
                    "(@sample.user) on Instagram: \"Bookings: hello@example.com\""
                ),
            },
            self.URL,
        )

        self.assertEqual(record["email_or_phone"], "hello@example.com")

    def test_snippet_without_the_exact_username_is_not_used(self):
        record = parse_instagram(
            {
                "title": "Someone Else (@someone.else) • Instagram photos",
                "body": "900 Followers, 100 Following, 12 Posts - Someone Else (@someone.else) on Instagram: \"Bio\"",
            },
            self.URL,
        )

        self.assertEqual(record["full_name"], "")
        self.assertEqual(record["bio"], "")
        self.assertEqual(record["followers"], "")
        self.assertEqual(record["verification"], "profile_not_identifiable_in_search_snippet")

    def test_bio_stat_like_text_is_not_used_as_account_metrics(self):
        record = parse_instagram(
            {
                "title": "Sample User (@sample.user) • Instagram photos",
                "body": (
                    "Sample User (@sample.user) on Instagram: "
                    "\"500 posts and 10k followers\""
                ),
            },
            self.URL,
        )

        self.assertEqual(record["followers"], "")
        self.assertEqual(record["following"], "")
        self.assertEqual(record["post_count"], "")


class SocialExportTests(unittest.TestCase):
    def _instagram_row(self, username="sample.user"):
        return {
            "username": username,
            "full_name": "Sample User",
            "bio": "Photographer",
            "followers": "10",
            "following": "20",
            "post_count": "3",
            "email_or_phone": "",
            "profile_url": f"https://www.instagram.com/{username}/",
            "data_quality": "partially_enriched_unverified",
            "field_status": {"username": "extracted", "bio": "extracted"},
        }

    def test_csv_and_excel_field_status_are_valid_json(self):
        rows = [self._instagram_row()]
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "output"
            export_social_profiles(output_dir, [], rows)

            import csv

            with (output_dir / "instagram_profiles.csv").open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                csv_row = next(csv.DictReader(handle))
            self.assertEqual(
                json.loads(csv_row["field_status"]),
                {"bio": "extracted", "username": "extracted"},
            )

            import pandas as pd

            frame = pd.read_excel(output_dir / "instagram_profiles.xlsx", dtype=str)
            self.assertEqual(
                json.loads(frame.loc[0, "field_status"]),
                {"bio": "extracted", "username": "extracted"},
            )

    def test_exports_read_persisted_records_after_an_empty_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = LeadDatabase(root / "profiles.db")
            try:
                for username in ("sample.one", "sample.two"):
                    database.save_social_profile(
                        "instagram", self._instagram_row(username)
                    )
                exported = export_social_profile_from_database(
                    database, root / "exports", "instagram"
                )
                # A later run that collected nothing must not erase the export.
                exported_again = export_social_profile_from_database(
                    database, root / "exports", "instagram"
                )
            finally:
                database.close()

            self.assertEqual(len(exported), 2)
            self.assertEqual(len(exported_again), 2)
            import csv

            with (root / "exports" / "instagram_profiles.csv").open(
                encoding="utf-8-sig", newline=""
            ) as handle:
                self.assertEqual(len(list(csv.DictReader(handle))), 2)


if __name__ == "__main__":
    unittest.main()
