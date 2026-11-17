import json
import tempfile
import unittest
from pathlib import Path

from lead_scraper.processing import classify_post, deduplicate_posts, normalize_post
from lead_scraper.config import Settings
from lead_scraper.scrapers import MastodonSource, RedditSource
from lead_scraper.storage import LeadDatabase, export_leads


class PhotographyLeadTests(unittest.TestCase):
    def test_classification_requires_actual_buying_intent(self):
        hot = classify_post({
            "post_text": "Looking to hire a wedding photographer in Bengaluru. Any recommendations?",
        })
        warm = classify_post({
            "post_text": "Our wedding is next month and we need photography coverage.",
        })
        cold = classify_post({
            "post_text": "Photography tips and camera settings discussion.",
        })
        promotion = classify_post({
            "post_text": "Book me for photography sessions and DM for bookings.",
        })

        self.assertEqual(hot["lead_temperature"], "Hot")
        self.assertTrue(hot["qualified"])
        self.assertEqual(warm["lead_temperature"], "Warm")
        self.assertTrue(warm["qualified"])
        self.assertEqual(cold["lead_temperature"], "Cold")
        self.assertFalse(cold["qualified"])
        self.assertFalse(promotion["qualified"])

    def test_classification_rejects_promotions_and_past_event_recaps(self):
        promotion = classify_post({
            "post_text": (
                "If you have a need for product photography, DM me and I can "
                "help you out."
            ),
        })
        recap = classify_post({
            "post_text": (
                "I spent a recent Saturday photographing an event. Here is my "
                "favourite photo from the set."
            ),
        })
        portfolio = classify_post({
            "post_text": (
                "Event Photographer Riyadh. Refined interiors and a visual "
                "celebration of elegance!"
            ),
        })

        self.assertFalse(promotion["qualified"])
        self.assertFalse(recap["qualified"])
        self.assertFalse(portfolio["qualified"])

    def test_classification_rejects_gear_questions_and_provider_hunting(self):
        gear = classify_post({
            "post_title": "Canon 600D for concert photography, need recommendations!",
        })
        lens = classify_post({
            "post_title": "Wedding Photographer Lens Recommendations?",
        })
        clients = classify_post({
            "post_title": "Looking for photography clients without random inquiries",
        })
        testimonial = classify_post({
            "post_title": "Wedding Photographer Recommendation! DawnPoint Studios",
        })
        partner = classify_post({
            "post_title": (
                "Made a live guest photo-sharing app for weddings — looking to "
                "partner with photographers and coordinators"
            ),
        })
        missing = classify_post({
            "post_title": "First marathon photo missing — looking for the photographer",
        })

        self.assertFalse(gear["qualified"])
        self.assertFalse(lens["qualified"])
        self.assertFalse(clients["qualified"])
        self.assertFalse(testimonial["qualified"])
        self.assertFalse(partner["qualified"])
        self.assertFalse(missing["qualified"])

    def test_crossposted_request_is_deduplicated_by_author_and_title(self):
        first = normalize_post({
            "source": "reddit",
            "source_post_id": "a1",
            "post_url": "https://www.reddit.com/r/a/comments/a1/post/",
            "author_handle": "same_user",
            "post_title": "Need a photographer for an event",
            "post_text": "Need a photographer for an event next week.",
        })
        crosspost = normalize_post({
            "source": "reddit",
            "source_post_id": "b2",
            "post_url": "https://www.reddit.com/r/b/comments/b2/post/",
            "author_handle": "same_user",
            "post_title": "Need a photographer for an event",
            "post_text": "Need a photographer for an event next week. DM me.",
        })

        self.assertEqual(deduplicate_posts([first, crosspost]), [first])

    def test_normalization_adds_collection_timestamp_and_dedupes_content(self):
        first = normalize_post({
            "source": "Mastodon",
            "source_post_id": "1",
            "post_url": "https://example.test/@person/1",
            "post_text": "Need a photographer for my wedding.",
        })
        duplicate = normalize_post({
            "source": "Mastodon",
            "source_post_id": "2",
            "post_url": "https://example.test/@person/2",
            "post_text": "Need a photographer for my wedding.",
        })
        self.assertEqual(first["source"], "mastodon")
        self.assertTrue(first["collected_at"])
        self.assertEqual(first["intent_evidence"], "")
        self.assertEqual(first["post_text"], duplicate["post_text"])
        self.assertEqual(deduplicate_posts([first, duplicate]), [first])

    def test_location_is_taken_only_from_an_explicit_place(self):
        def location(title):
            return normalize_post({"source": "reddit", "post_title": title})["location"]

        self.assertEqual(location("Looking for a photographer in Hoi An"), "Hoi An")
        self.assertEqual(
            location("Maternity photos - seeking photographer in Athens, Greece"),
            "Athens, Greece",
        )
        self.assertEqual(
            location("Seeking Affordable Bay Area Maternity Photographer"),
            "Bay Area",
        )
        self.assertEqual(
            location("Photographer needed in Elmwood Berkeley - Oct 11"),
            "Elmwood Berkeley",
        )
        self.assertEqual(location("Looking for a wedding photographer in January"), "")
        self.assertEqual(location("Proposal at Night Lights - October 17th"), "")

    def test_database_and_exports_preserve_evidence(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = LeadDatabase(Path(temp_dir) / "leads.db")
            lead = {
                "source": "reddit",
                "source_post_id": "abc",
                "post_url": "https://reddit.com/r/test/comments/abc",
                "post_title": "Need a photographer for an event",
                "post_text": "Need a photographer for an event.",
                "intent_evidence": "need a photographer",
                "lead_temperature": "Hot",
                "score": 80,
            }
            db.save_lead(lead)
            self.assertEqual(db.get_leads(), [lead])
            db.replace_leads([{**lead, "lead_temperature": "Warm"}])
            self.assertEqual(db.get_leads()[0]["lead_temperature"], "Warm")
            export_leads([lead], [lead], Path(temp_dir), excel=False)
            exported = json.loads((Path(temp_dir) / "leads.json").read_text())
            self.assertEqual(exported["leads"][0]["post_url"], lead["post_url"])
            self.assertEqual(exported["leads"][0]["post_title"], lead["post_title"])
            self.assertEqual(exported["leads"][0]["intent_evidence"], "need a photographer")
            db.close()

    def test_save_post_upserts_title_for_existing_record(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            db = LeadDatabase(Path(temp_dir) / "leads.db")
            first = {
                "source": "reddit",
                "source_post_id": "abc",
                "post_url": "https://reddit.com/r/test/comments/abc",
                "post_text": "Need a photographer.",
                "created_at": "1",
            }
            updated = {
                **first,
                "post_title": "Need a photographer for an event",
                "post_text": "Need a photographer for an event.",
            }
            db.save_post(first)
            db.save_post(updated)
            posts = db.get_posts()
            self.assertEqual(len(posts), 1)
            self.assertEqual(posts[0]["post_title"], updated["post_title"])
            db.close()

    def test_reddit_source_uses_public_archive_without_credentials(self):
        settings = Settings(
            keywords=["need a photographer"],
            max_posts_per_source=10,
            request_delay=0,
        )
        source = RedditSource(settings)
        payload = {
            "data": [
                {
                    "id": "abc123",
                    "subreddit": "bangalore",
                    "title": "Need a photographer for an event",
                    "selftext": "Budget is flexible.",
                    "author": "sample_user",
                    "permalink": "/r/bangalore/comments/abc123/need_a_photographer/",
                    "created_utc": 1791887997.0,
                    "over_18": False,
                },
                {
                    "id": "nsfw1",
                    "subreddit": "sample",
                    "title": "Need a photographer",
                    "selftext": "",
                    "author": "sample_user",
                    "permalink": "/r/sample/comments/nsfw1/post/",
                    "created_utc": 1791887997.0,
                    "over_18": True,
                },
            ]
        }
        source.get_json = lambda url, params=None, headers=None: payload

        posts = source.fetch()

        self.assertEqual(len(posts), 1)
        self.assertEqual(source.stats["mode"], "public_pullpush_archive")
        self.assertEqual(
            posts[0]["post_url"],
            "https://www.reddit.com/r/bangalore/comments/abc123/need_a_photographer/",
        )
        self.assertIn("Need a photographer", posts[0]["post_text"])
        self.assertEqual(posts[0]["created_at"], 1791887997.0)

    def test_reddit_source_pages_public_archive(self):
        settings = Settings(
            keywords=["need a photographer"],
            max_posts_per_source=200,
            request_delay=0,
        )
        source = RedditSource(settings)
        calls = []

        def fake_get_json(url, params=None, headers=None):
            params = dict(params or {})
            calls.append(params)
            if "before" in params:
                items = [{
                    "id": "older",
                    "subreddit": "sample",
                    "title": "Looking for a photographer",
                    "selftext": "",
                    "author": "sample_user",
                    "permalink": "/r/sample/comments/older/post/",
                    "created_utc": 500.0,
                    "over_18": False,
                }]
            else:
                items = [{
                    "id": f"p{i}",
                    "subreddit": "sample",
                    "title": "Need a photographer for an event",
                    "selftext": "",
                    "author": "sample_user",
                    "permalink": f"/r/sample/comments/p{i}/post/",
                    "created_utc": 1000.0 + i,
                    "over_18": False,
                } for i in range(100)]
            return {"data": items}

        source.get_json = fake_get_json
        posts = source.fetch()

        self.assertEqual(len(posts), 101)
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["before"], 999)

    def test_mastodon_source_collects_search_and_tag_statuses(self):
        settings = Settings(
            keywords=["need a photographer"],
            mastodon_instances=["mastodon.example"],
            max_posts_per_source=10,
            request_delay=0,
        )
        source = MastodonSource(settings)
        source.get_json = lambda url, params=None, headers=None: (
            {"statuses": [{
                "id": "1", "url": "https://mastodon.example/@a/1",
                "account": {"display_name": "A", "acct": "a"},
                "content": "Need a photographer for my wedding.",
                "created_at": "2026-11-17T00:00:00Z",
            }]} if "/search" in url else []
        )
        posts = source.fetch()
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["query"], "need a photographer")
        self.assertEqual(posts[0]["post_url"], "https://mastodon.example/@a/1")


if __name__ == "__main__":
    unittest.main()
