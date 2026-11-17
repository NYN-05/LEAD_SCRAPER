import json
import tempfile
import unittest
from pathlib import Path

from lead_scraper.scrapers import (
    is_aggregated_linkedin,
    normalize_profile_records,
    parse_linkedin,
)
from lead_scraper.storage import LeadDatabase, export_social_profiles


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


if __name__ == "__main__":
    unittest.main()
