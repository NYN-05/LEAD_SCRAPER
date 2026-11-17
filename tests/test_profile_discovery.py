import unittest

from lead_scraper.config import ScrapeConfig
from lead_scraper.scrapers import (
    ProfileQuery,
    build_queries,
    canonical_url,
    discover_profiles,
)


class FakeSearchManager:
    """Minimal stand-in for SearchManager that returns canned pages."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def search(self, query, limit=20, page=1):
        self.calls.append({"query": query, "limit": limit, "page": page})
        return list(self.pages.get(page, []))


def profile_hit(url):
    return {"href": url, "title": "Sample Person - Photographer | LinkedIn", "body": ""}


class CanonicalUrlTests(unittest.TestCase):
    def test_linkedin_country_subdomains_are_normalized(self):
        self.assertEqual(
            canonical_url("linkedin", "https://in.linkedin.com/in/sample-user/"),
            "https://www.linkedin.com/in/sample-user/",
        )
        self.assertEqual(
            canonical_url("linkedin", "https://uk.linkedin.com/in/sample-user?trk=abc"),
            "https://www.linkedin.com/in/sample-user/",
        )
        self.assertEqual(
            canonical_url("linkedin", "https://mv.linkedin.com/in/sample-user"),
            "https://www.linkedin.com/in/sample-user/",
        )

    def test_linkedin_non_profile_paths_are_rejected(self):
        self.assertEqual(canonical_url("linkedin", "https://www.linkedin.com/company/example/"), "")
        self.assertEqual(canonical_url("linkedin", "https://www.linkedin.com/in/"), "")

    def test_instagram_mirror_hosts_are_rejected(self):
        self.assertEqual(
            canonical_url("instagram", "https://www.instagram.com/sample.user/"),
            "https://www.instagram.com/sample.user/",
        )
        self.assertEqual(
            canonical_url("instagram", "https://www-fallback.instagram.com/popular/bangalore/"),
            "",
        )
        self.assertEqual(canonical_url("instagram", "https://www.instagram.com/reel/abc123/"), "")


class BackendConfigTests(unittest.TestCase):
    def test_bing_alias_maps_to_yahoo(self):
        config = ScrapeConfig(search_backend="bing,yahoo")
        self.assertEqual(config.search_backend, "yahoo")

    def test_invalid_empty_backend_falls_back_to_default(self):
        config = ScrapeConfig(search_backend="")
        self.assertEqual(config.search_backend, "yahoo,auto")


class QueryBuilderTests(unittest.TestCase):
    def setUp(self):
        self.query = ProfileQuery(keywords="photographer", location="Bangalore", max_results=100)

    def test_unquoted_queries_come_before_quoted_variants(self):
        queries = build_queries("instagram", self.query)
        first_quoted = next(i for i, q in enumerate(queries) if '"' in q)
        self.assertTrue(all('"' not in query for query in queries[:first_quoted]))
        self.assertIn("site:instagram.com photographer Bangalore", queries)

    def test_location_alias_is_generated(self):
        queries = build_queries("linkedin", self.query)
        self.assertTrue(any("Bengaluru" in query for query in queries))
        self.assertIn("site:in.linkedin.com/in photographer Bangalore", queries)

    def test_multiple_keywords_are_expanded(self):
        query = ProfileQuery(keywords="photographer, wedding photographer", location="Bangalore")
        queries = build_queries("instagram", query)
        self.assertTrue(any("wedding photographer" in query for query in queries))
        self.assertTrue(any("photographer Bangalore" in query for query in queries))


class DiscoveryTests(unittest.TestCase):
    def config(self, **overrides):
        values = {
            "min_delay": 0,
            "max_delay": 0,
            "search_pages": 5,
            "max_search_requests": 20,
            "search_backend": "yahoo",
        }
        values.update(overrides)
        return ScrapeConfig(**values)

    def test_discovery_paginates_and_deduplicates(self):
        manager = FakeSearchManager({
            1: [
                profile_hit("https://in.linkedin.com/in/alpha"),
                profile_hit("https://uk.linkedin.com/in/beta"),
            ],
            2: [
                profile_hit("https://www.linkedin.com/in/gamma"),
                profile_hit("https://in.linkedin.com/in/alpha"),
            ],
            3: [profile_hit("https://www.linkedin.com/in/gamma")],
            4: [],
            5: [],
        })
        query = ProfileQuery(keywords="photographer", location="Bangalore", max_results=3)

        found = discover_profiles("linkedin", manager, query, self.config())

        urls = [url for url, _ in found]
        self.assertEqual(
            urls,
            [
                "https://www.linkedin.com/in/alpha/",
                "https://www.linkedin.com/in/beta/",
                "https://www.linkedin.com/in/gamma/",
            ],
        )
        self.assertEqual(len(manager.calls), 2)
        self.assertEqual([call["page"] for call in manager.calls], [1, 2])

    def test_discovery_stops_at_max_results(self):
        manager = FakeSearchManager({
            page: [profile_hit(f"https://www.instagram.com/user{page}_{index}/") for index in range(4)]
            for page in range(1, 6)
        })
        query = ProfileQuery(keywords="photographer", location="Bangalore", max_results=6)

        found = discover_profiles("instagram", manager, query, self.config())

        self.assertEqual(len(found), 6)
        self.assertEqual(len(manager.calls), 2)

    def test_search_budget_is_respected(self):
        manager = FakeSearchManager({
            page: [profile_hit(f"https://www.instagram.com/user{page}/")]
            for page in range(1, 6)
        })
        query = ProfileQuery(keywords="photographer", location="Bangalore", max_results=100)

        found = discover_profiles(
            "instagram", manager, query, self.config(max_search_requests=3)
        )

        self.assertEqual(len(manager.calls), 3)
        self.assertEqual(len(found), 3)


if __name__ == "__main__":
    unittest.main()
