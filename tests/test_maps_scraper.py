"""Offline tests for Google Maps URL handling, parsing and collection control."""
import unittest
from unittest.mock import patch

from lead_scraper.scrapers import MapsScraper, PlaywrightTimeoutError

PLACE_A = "https://www.google.com/maps/place/Alpha+Studio/data=!4m7!3m6!1s0x1:0x2"
PLACE_B = "https://www.google.com/maps/place/Beta+Studio/data=!4m7!3m6!1s0x3:0x4"
PLACE_C = "https://www.google.com/maps/place/Gamma+Studio/data=!4m7!3m6!1s0x5:0x6"
PLACE_D = "https://www.google.com/maps/place/Delta+Studio/data=!4m7!3m6!1s0x7:0x8"


class FakeAnchorList:
    def __init__(self, feed):
        self.feed = feed

    def evaluate_all(self, script):
        return list(self.feed.hrefs)


class FakeFeed:
    def __init__(self, hrefs, text="", on_scroll=None, fail_wait=False):
        self.hrefs = list(hrefs)
        self.text = text
        self.on_scroll = on_scroll
        self.fail_wait = fail_wait
        self.scrolls = 0

    @property
    def first(self):
        return self

    def wait_for(self, state, timeout):
        if self.fail_wait:
            raise PlaywrightTimeoutError("results feed did not appear")

    def locator(self, selector):
        return FakeAnchorList(self)

    def inner_text(self, timeout=0):
        return self.text

    def evaluate(self, script):
        if "scrollTop" in script:
            self.scrolls += 1
            if self.on_scroll is not None:
                self.on_scroll(self)


class FakeConsentButton:
    @property
    def first(self):
        return self

    def count(self):
        return 0

    def is_visible(self, timeout=0):
        return False

    def click(self, timeout=0):
        return None


class FakeSearchPage:
    def __init__(self, feed, redirect_url=None):
        self.feed = feed
        self.redirect_url = redirect_url
        self.url = "about:blank"
        self.goto_calls = []

    def goto(self, url, wait_until=None, timeout=None):
        self.goto_calls.append(url)
        self.url = self.redirect_url or url

    def locator(self, selector):
        if 'role="feed"' in selector:
            return self.feed
        return FakeConsentButton()

    def wait_for_timeout(self, milliseconds):
        return None


class FlakyPage:
    def __init__(self, failures):
        self.failures = failures
        self.calls = 0

    def goto(self, url, wait_until=None, timeout=None):
        self.calls += 1
        if self.calls <= self.failures:
            raise TimeoutError("navigation timed out")


class FakeBrowserPage:
    def set_default_timeout(self, milliseconds):
        return None


class FakeContext:
    def __init__(self):
        self.closed = False

    def new_page(self):
        return FakeBrowserPage()

    def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.closed = False

    def new_context(self, **kwargs):
        return FakeContext()

    def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self, launch_error=None):
        self.launch_error = launch_error

    def launch(self, **kwargs):
        if self.launch_error is not None:
            raise self.launch_error
        return FakeBrowser()


class FakePlaywrightManager:
    def __init__(self, launch_error=None):
        self.playwright = type(
            "Playwright", (), {"chromium": FakeChromium(launch_error)}
        )()

    def __enter__(self):
        return self.playwright

    def __exit__(self, exc_type, exc, traceback):
        return False


class StubMapsScraper(MapsScraper):
    """Bypass the browser while exercising scrape() orchestration."""

    def __init__(self, urls, parser, **kwargs):
        super().__init__(delay=(0.0, 0.0), **kwargs)
        self.urls = urls
        self.parser = parser
        self.parse_calls = []

    def _collect_urls(self, page, search_query, limit):
        return list(self.urls)

    def _parse_place(self, page, url):
        self.parse_calls.append(url)
        return self.parser(url)


def lead_for(url):
    name = url.split("/maps/place/", 1)[1].split("/", 1)[0].replace("+", " ")
    return {
        "business_name": name,
        "maps_url": url,
    }


class MapsUrlTests(unittest.TestCase):
    def test_canonical_url_strips_tracking_and_fragment(self):
        href = PLACE_A + "?utm_source=search#reviews"
        self.assertEqual(MapsScraper._canonical_maps_url(href), PLACE_A)

    def test_canonical_url_resolves_relative_place_link(self):
        self.assertEqual(
            MapsScraper._canonical_maps_url("/maps/place/Relative+Studio"),
            "https://www.google.com/maps/place/Relative+Studio",
        )

    def test_canonical_url_rejects_search_and_non_google_links(self):
        self.assertEqual(
            MapsScraper._canonical_maps_url(
                "https://www.google.com/maps/search/photography"
            ),
            "",
        )
        self.assertEqual(
            MapsScraper._canonical_maps_url("https://example.com/maps/place/X"),
            "",
        )

    def test_google_redirect_website_is_unwrapped(self):
        self.assertEqual(
            MapsScraper._normalize_website(
                "https://www.google.com/url?q=https%3A%2F%2Fexample.com%2Fmenu%3Fa%3D1&sa=D"
            ),
            "https://example.com/menu?a=1",
        )

    def test_direct_and_unsafe_websites(self):
        self.assertEqual(
            MapsScraper._normalize_website("https://studio.example/"),
            "https://studio.example/",
        )
        self.assertEqual(
            MapsScraper._normalize_website("https://sites.google.com/view/studio"),
            "https://sites.google.com/view/studio",
        )
        self.assertEqual(
            MapsScraper._normalize_website("https://www.google.com/maps/place/X"), ""
        )
        self.assertEqual(MapsScraper._normalize_website("javascript:alert(1)"), "")
        self.assertEqual(MapsScraper._normalize_website(""), "")


class MapsRecordTests(unittest.TestCase):
    def test_missing_optional_fields_stay_empty(self):
        record = MapsScraper._normalize_record(
            {"business_name": "  Alpha Studio  ", "maps_url": PLACE_A}
        )
        self.assertEqual(record["business_name"], "Alpha Studio")
        self.assertEqual(record["category"], "")
        self.assertEqual(record["address"], "")
        self.assertEqual(record["phone"], "")
        self.assertEqual(record["website"], "")
        self.assertEqual(record["rating"], "")
        self.assertEqual(record["reviews"], "")
        self.assertEqual(record["maps_url"], PLACE_A)

    def test_rating_and_review_label_parsing(self):
        self.assertEqual(MapsScraper._parse_rating_value("4.6 stars 50 Reviews"), "4.6")
        self.assertEqual(MapsScraper._parse_rating_value("5"), "5")
        self.assertEqual(MapsScraper._parse_rating_value("50 reviews"), "")
        self.assertEqual(MapsScraper._parse_review_count("3,413 reviews"), "3413")
        self.assertEqual(MapsScraper._parse_review_count("4.6 (50)"), "50")
        self.assertEqual(
            MapsScraper._parse_review_count("Rated 4.6 out of 5, 50 reviews"), "50"
        )
        self.assertEqual(MapsScraper._parse_review_count("No reviews"), "")

    def test_usable_lead_does_not_require_optional_fields(self):
        record = MapsScraper._normalize_record(
            {"business_name": "Alpha Studio", "maps_url": PLACE_A}
        )
        self.assertTrue(MapsScraper._is_usable_lead(record))
        self.assertFalse(MapsScraper._is_usable_lead({**record, "business_name": ""}))
        self.assertFalse(MapsScraper._is_usable_lead({**record, "maps_url": ""}))

    def test_label_prefixes_are_removed(self):
        self.assertEqual(
            MapsScraper._strip_label("Address: 12 Main Road", "Address"),
            "12 Main Road",
        )
        self.assertEqual(MapsScraper._strip_label("Phone: +91 90000", "Phone"), "+91 90000")


class MapsCollectionTests(unittest.TestCase):
    def setUp(self):
        self.scraper = MapsScraper(delay=(0.0, 0.0))

    def test_collect_urls_deduplicates_and_honours_limit(self):
        feed = FakeFeed([PLACE_A, PLACE_A + "?utm=1", PLACE_B, PLACE_C])
        page = FakeSearchPage(feed)

        urls = self.scraper._collect_urls(page, "photography studio Bengaluru", 3)

        self.assertEqual(urls, [PLACE_A, PLACE_B, PLACE_C])
        self.assertEqual(feed.scrolls, 0)

    def test_collect_urls_scrolls_for_dynamically_loaded_results(self):
        def load_more(feed):
            if feed.scrolls == 1:
                feed.hrefs.extend([PLACE_C, PLACE_D])

        feed = FakeFeed([PLACE_A, PLACE_B], on_scroll=load_more)
        page = FakeSearchPage(feed)

        urls = self.scraper._collect_urls(page, "photography studio Bengaluru", 4)

        self.assertEqual(urls, [PLACE_A, PLACE_B, PLACE_C, PLACE_D])
        self.assertEqual(feed.scrolls, 1)

    def test_collect_urls_stops_at_end_of_list_marker(self):
        feed = FakeFeed(
            [PLACE_A],
            text="You've reached the end of the list.",
        )
        page = FakeSearchPage(feed)

        urls = self.scraper._collect_urls(page, "photography studio Bengaluru", 10)

        self.assertEqual(urls, [PLACE_A])
        self.assertEqual(feed.scrolls, 0)

    def test_collect_urls_accepts_single_place_redirect(self):
        feed = FakeFeed([])
        page = FakeSearchPage(feed, redirect_url=PLACE_A)

        urls = self.scraper._collect_urls(page, "photography studio Bengaluru", 10)

        self.assertEqual(urls, [PLACE_A])
        self.assertEqual(feed.scrolls, 0)

    def test_collect_urls_returns_empty_when_feed_is_missing(self):
        feed = FakeFeed([], fail_wait=True)
        page = FakeSearchPage(feed)

        self.assertEqual(
            self.scraper._collect_urls(page, "photography studio Bengaluru", 10), []
        )

    def test_goto_retries_transient_navigation_failure(self):
        page = FlakyPage(failures=1)
        self.scraper._goto(page, "https://www.google.com/maps", 1000, attempts=2)
        self.assertEqual(page.calls, 2)

    def test_goto_raises_after_retry_budget_is_exhausted(self):
        page = FlakyPage(failures=5)
        with self.assertRaises(RuntimeError):
            self.scraper._goto(page, "https://www.google.com/maps", 1000, attempts=2)
        self.assertEqual(page.calls, 2)

    def test_invalid_inputs_are_rejected_before_browser_launch(self):
        for keyword, location, maximum in (
            ("", "Bengaluru", 10),
            ("photography studio", "", 10),
            ("photography studio", "Bengaluru", 0),
        ):
            with self.subTest(keyword=keyword, location=location, maximum=maximum):
                with self.assertRaises(ValueError):
                    self.scraper.scrape(keyword, location, maximum)


class MapsScrapeOrchestrationTests(unittest.TestCase):
    def _run(self, scraper, maximum, launch_error=None):
        manager = FakePlaywrightManager(launch_error=launch_error)
        with patch("lead_scraper.scrapers.sync_playwright", return_value=manager):
            return scraper.scrape("photography studio", "Bengaluru", maximum)

    def test_duplicate_urls_are_parsed_once_and_limit_is_honoured(self):
        scraper = StubMapsScraper(
            [PLACE_A, PLACE_A, PLACE_B, PLACE_C], lead_for
        )
        rows = self._run(scraper, 2)
        self.assertEqual([row["maps_url"] for row in rows], [PLACE_A, PLACE_B])
        self.assertEqual(scraper.parse_calls, [PLACE_A, PLACE_B])

    def test_single_place_failure_is_recorded_and_collection_continues(self):
        def parser(url):
            if url == PLACE_A:
                raise TimeoutError("place page timed out")
            return lead_for(url)

        scraper = StubMapsScraper([PLACE_A, PLACE_B], parser, max_attempts=1)
        rows = self._run(scraper, 5)

        self.assertEqual([row["maps_url"] for row in rows], [PLACE_B])
        self.assertEqual(len(scraper.errors), 1)
        self.assertEqual(scraper.errors[0]["stage"], "place_extraction")
        self.assertEqual(scraper.errors[0]["url"], PLACE_A)

    def test_transient_place_failure_is_retried_once(self):
        attempts = {"count": 0}

        def parser(url):
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise TimeoutError("temporary timeout")
            return lead_for(url)

        scraper = StubMapsScraper([PLACE_A], parser, max_attempts=2)
        rows = self._run(scraper, 1)

        self.assertEqual([row["maps_url"] for row in rows], [PLACE_A])
        self.assertEqual(attempts["count"], 2)
        self.assertEqual(scraper.errors, [])

    def test_browser_launch_failure_is_recorded(self):
        scraper = StubMapsScraper([PLACE_A], lead_for)
        rows = self._run(scraper, 1, launch_error=RuntimeError("browser missing"))

        self.assertEqual(rows, [])
        self.assertEqual(scraper.errors[0]["stage"], "browser")
        self.assertIn("browser missing", scraper.errors[0]["error"])

    def test_incomplete_record_is_skipped_with_a_validation_error(self):
        scraper = StubMapsScraper([PLACE_A], lambda url: {"maps_url": url})
        rows = self._run(scraper, 1)

        self.assertEqual(rows, [])
        self.assertEqual(scraper.errors[0]["stage"], "validation")


if __name__ == "__main__":
    unittest.main()
