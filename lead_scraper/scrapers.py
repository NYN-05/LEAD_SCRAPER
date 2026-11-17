# Consolidated scraper implementations.

#!/usr/bin/env python3
"""LinkedIn + Instagram public-profile lead scraper.

Discovery source: DuckDuckGo search results through the ``ddgs`` package.
The scraper does not log into social platforms, bypass access controls, or
scrape private content. Search snippets are incomplete and are never treated
as authoritative profile records.

Examples (PowerShell):
    py -m pip install ddgs requests beautifulsoup4
    py linkedin_instagram_scraper.py --location Bengaluru --max-results 100

    py linkedin_instagram_scraper.py `
      --linkedin-keywords "data scientist" `
      --instagram-keywords photographer `
      --location Bengaluru --max-results 100

    py linkedin_instagram_scraper.py `
      --linkedin-urls "https://www.linkedin.com/in/example/" `
      --instagram-urls "https://www.instagram.com/example/" `
      --targeted-lookup

    py linkedin_instagram_scraper.py --input-json profile_inputs.json

Input JSON format:
    {
      "linkedin_urls": ["https://www.linkedin.com/in/example/"],
      "instagram_urls": ["https://www.instagram.com/example/"],
      "linkedin_keywords": "data scientist",
      "instagram_keywords": "photographer",
      "location": "Bengaluru"
    }

Outputs are written to ./output/social by default:
    linkedin_profiles.csv, instagram_profiles.csv

Privacy and compliance:
- Collect public profile information only; respect platform terms and local law.
- No login automation, CAPTCHA bypass, private endpoint use, or rotating proxies
  to evade blocks. Public profile HTML is fetched only when robots.txt permits it.
  HTTP 403/429 responses are treated as stop signals. Delays are for responsible
  request pacing only.
- Contact information is retained only when it appears in the matched public
  Instagram bio snippet. Do not use it for unsolicited or unlawful outreach.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import re
import sys
import time
import feedparser
from dataclasses import asdict, dataclass, field
from urllib.robotparser import RobotFileParser
from urllib.parse import urljoin
from datetime import datetime, timezone
from pathlib import Path

from .config import ScrapeConfig, project_path
from .storage import LeadDatabase, export_social_profiles

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright
except ImportError:
    PlaywrightTimeoutError = TimeoutError
    sync_playwright = None
from typing import Any, Iterable
from urllib.parse import unquote, urlparse, parse_qs
from urllib.parse import quote_plus, urljoin, urlunparse

try:
    from ddgs import DDGS
except ImportError:  # Give a useful error only when a search is requested.
    DDGS = None  # type: ignore[assignment,misc]

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    requests = None  # type: ignore[assignment]
    BeautifulSoup = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Constants and data models
# ---------------------------------------------------------------------------

EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
PHONE_RE = re.compile(r"(?<!\w)(?:\+\d{1,3}[\s().-]?)?(?:\d[\s().-]?){7,14}\d(?!\w)")
IG_RESERVED = {
    "p", "reel", "reels", "explore", "tags", "tv", "stories", "accounts",
    "about", "legal", "popular", "direct", "web", "developer", "challenge",
}
# Prevent splitting at the comma inside counts such as 2,900 Followers.
IG_STATS_START = re.compile(
    r"(?<![\d,.])(?=\b[\d,.]+(?:\s?[KMB])?\s+Followers,\s*"
    r"[\d,.]+(?:\s?[KMB])?\s+Following,\s*"
    r"[\d,.]+(?:\s?[KMB])?\s+Posts\s*[-–—])",
    re.I,
)

LINKEDIN_FIELDS = [
    "name", "headline", "current_company", "location", "about", "profile_url",
    "data_source", "verification", "profile_page_access", "source_title", "source_snippet", "search_provider", "collected_at",
    "field_status",
]
INSTAGRAM_FIELDS = [
    "username", "full_name", "bio", "followers", "following", "post_count",
    "email_or_phone", "profile_url", "data_source", "verification", "profile_page_access",
    "source_title", "source_snippet", "search_provider", "collected_at",
    "field_status",
]
LINKEDIN_PROFILE_FIELDS = ["name", "headline", "current_company", "location", "about", "profile_url"]
INSTAGRAM_PROFILE_FIELDS = [
    "username", "full_name", "bio", "followers", "following", "post_count",
    "email_or_phone", "profile_url",
]


@dataclass
class ProfileQuery:
    profile_urls: list[str] = field(default_factory=list)
    keywords: str = ""
    hashtag: str = ""
    location: str = ""
    max_results: int = 100


@dataclass
class SearchIssue:
    timestamp: str
    category: str
    backend: str
    query: str
    error_type: str
    message: str
    attempt: int


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_text(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def digits(value: str) -> str:
    return re.sub(r"\D", "", value or "")


def canonical_url(platform: str, href: str) -> str:
    """Validate and normalize a public LinkedIn or Instagram profile URL."""
    try:
        parsed = urlparse(unquote((href or "").strip()))
        host = (parsed.hostname or "").lower().removeprefix("www.")
        parts = [part for part in parsed.path.split("/") if part]
    except (ValueError, AttributeError):
        return ""

    if platform == "linkedin":
        # Search engines commonly return country subdomains such as
        # in.linkedin.com, uk.linkedin.com, and mv.linkedin.com. Normalize
        # every LinkedIn profile host to the canonical www host so the same
        # person is deduplicated regardless of which subdomain appeared.
        if not (host == "linkedin.com" or host.endswith(".linkedin.com")):
            return ""
        if len(parts) < 2 or parts[0].lower() != "in":
            return ""
        slug = parts[1].strip().casefold()
        if not re.fullmatch(r"[A-Za-z0-9_%.-]{2,150}", slug):
            return ""
        return f"https://www.linkedin.com/in/{slug}/"

    if platform == "instagram":
        # Accept only the canonical host and its www prefix. Mirrors such as
        # www-fallback.instagram.com must not be treated as profiles.
        if host != "instagram.com" or not parts:
            return ""
        username = parts[0].strip().casefold()
        if username.lower() in IG_RESERVED or not re.fullmatch(r"[A-Za-z0-9._]{1,30}", username):
            return ""
        return f"https://www.instagram.com/{username}/"

    raise ValueError("Platform must be 'linkedin' or 'instagram'.")


def profile_identifier(platform: str, profile_url: str) -> str:
    parts = [part for part in urlparse(profile_url).path.split("/") if part]
    return parts[-1] if parts else ""


def normalize_profile_records(platform: str, records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Canonicalize, deduplicate, and label extracted fields without inventing values."""
    if platform not in {"linkedin", "instagram"}:
        raise ValueError("Platform must be linkedin or instagram.")
    fields = LINKEDIN_PROFILE_FIELDS if platform == "linkedin" else INSTAGRAM_PROFILE_FIELDS
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in records:
        profile_url = canonical_url(platform, clean_text(source.get("profile_url")))
        if not profile_url or profile_url.casefold() in seen:
            continue
        seen.add(profile_url.casefold())
        record = dict(source)
        record["profile_url"] = profile_url
        status = dict(record.get("field_status") or {})
        for field_name in fields:
            if field_name == "profile_url":
                status[field_name] = "extracted"
            elif clean_text(record.get(field_name)):
                status[field_name] = status.get(field_name, "extracted")
            else:
                status[field_name] = "unavailable"
        record["field_status"] = status
        normalized.append(record)
    return normalized


def unique_strings(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        value = clean_text(value)
        key = value.casefold()
        if value and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def classify_error(exc: Exception) -> str:
    message = str(exc).lower()
    name = type(exc).__name__.lower()
    if "timeout" in message or "timeout" in name:
        return "timeout"
    if any(term in message for term in ("rate", "429", "too many requests")):
        return "rate_limited"
    if any(term in message for term in ("403", "forbidden", "blocked", "captcha")):
        return "blocked_or_forbidden"
    if "no results" in message or "no result" in message:
        return "no_results"
    if any(term in message for term in ("connection", "network", "dns", "proxy")):
        return "network_error"
    return "provider_error"


# ---------------------------------------------------------------------------
# Search provider manager
# ---------------------------------------------------------------------------

class SearchManager:
    """Search provider manager with independent API adapters and DDGS fallback.

    Brave Search API and Google Programmable Search are independent providers
    when credentials are configured. DDGS is the fallback. A DDGS backend name
    is only a backend selector inside that library, not proof of an independent
    provider. Rate limits and access denials are recorded; no provider is
    hammered or bypassed.
    """

    def __init__(self, config: ScrapeConfig) -> None:
        self.config = config
        self.issues: list[dict[str, Any]] = []
        self.search_count = 0
        self.success_count = 0
        self.provider_stats: dict[str, dict[str, int]] = {}
        self._last_request: dict[str, float] = {}
        self._consecutive_failures: dict[str, int] = {}
        self._disabled_providers: set[str] = set()
        self.session = requests.Session() if requests is not None else None

    def _pace(self, provider: str) -> None:
        target = random.uniform(self.config.min_delay, self.config.max_delay)
        previous = self._last_request.get(provider, 0.0)
        elapsed = time.monotonic() - previous
        if elapsed < target:
            time.sleep(target - elapsed)
        self._last_request[provider] = time.monotonic()

    def _record_issue(self, provider: str, query: str, exc: Exception, attempt: int = 1) -> None:
        category = classify_error(exc)
        self.issues.append(asdict(SearchIssue(
            timestamp=utc_now(), category=category, backend=provider,
            query=query[:300], error_type=type(exc).__name__,
            message=str(exc)[:500], attempt=attempt,
        )))

    def _stats(self, provider: str, *, success: bool = False) -> None:
        stats = self.provider_stats.setdefault(provider, {"requests": 0, "successes": 0, "results": 0, "failures": 0})
        stats["requests"] += 1
        if success:
            stats["successes"] += 1

    def _brave_search(self, query: str, limit: int, page: int = 1) -> list[dict[str, Any]]:
        if not self.config.brave_api_key or self.session is None:
            return []
        provider = "brave_search_api"
        self._pace(provider)
        self._stats(provider)
        response = self.session.get(
            "https://api.search.brave.com/res/v1/web/search",
            headers={"Accept": "application/json", "X-Subscription-Token": self.config.brave_api_key},
            params={"q": query, "count": min(max(limit, 1), 20),
                    "offset": max(0, (page - 1) * 20)},
            timeout=self.config.request_timeout,
        )
        if response.status_code in (403, 429):
            raise RuntimeError(f"Brave Search API returned HTTP {response.status_code}; provider request stopped.")
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("web", {}).get("results", [])
        self.provider_stats[provider]["successes"] += 1
        self.provider_stats[provider]["results"] += len(rows)
        return [{"title": row.get("title", ""), "href": row.get("url", ""),
                 "body": row.get("description", ""), "provider": provider} for row in rows]

    def _google_search(self, query: str, limit: int, page: int = 1) -> list[dict[str, Any]]:
        if not (self.config.google_api_key and self.config.google_cse_id) or self.session is None:
            return []
        provider = "google_programmable_search"
        self._pace(provider)
        self._stats(provider)
        response = self.session.get(
            "https://customsearch.googleapis.com/customsearch/v1",
            params={"key": self.config.google_api_key, "cx": self.config.google_cse_id,
                    "q": query, "num": min(max(limit, 1), 10),
                    "start": max(1, (page - 1) * 10 + 1)},
            timeout=self.config.request_timeout,
        )
        if response.status_code in (403, 429):
            raise RuntimeError(f"Google Programmable Search returned HTTP {response.status_code}; provider request stopped.")
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("items", [])
        self.provider_stats[provider]["successes"] += 1
        self.provider_stats[provider]["results"] += len(rows)
        return [{"title": row.get("title", ""), "href": row.get("link", ""),
                 "body": row.get("snippet", ""), "provider": provider} for row in rows]

    def _ddgs_search(self, query: str, limit: int, page: int = 1) -> list[dict[str, Any]]:
        if DDGS is None:
            raise RuntimeError("Missing dependency 'ddgs'. Install with: py -m pip install -U ddgs")
        backends = [item.strip() for item in (self.config.search_backend or "auto").split(",") if item.strip()]
        last_exc: Exception | None = None
        for attempt in range(1, self.config.retries + 2):
            for backend in backends:
                provider = f"ddgs:{backend}"
                if provider in self._disabled_providers:
                    continue
                self._pace(provider)
                self._stats(provider)
                try:
                    with DDGS() as engine:
                        rows = list(engine.text(
                            query,
                            backend=backend,
                            max_results=max(1, limit),
                            page=max(1, page),
                            region=self.config.search_region,
                        ) or [])
                    self.provider_stats[provider]["successes"] += 1
                    self.provider_stats[provider]["results"] += len(rows)
                    self._consecutive_failures[provider] = 0
                    if rows:
                        return [{"title": row.get("title", ""), "href": row.get("href", ""),
                                 "body": row.get("body", ""), "provider": provider} for row in rows]
                    # The backend responded but had no results for this page.
                    # Try the next configured backend before retrying.
                except Exception as exc:
                    last_exc = exc
                    self._record_issue(provider, query, exc, attempt)
                    self.provider_stats[provider]["failures"] += 1
                    failures = self._consecutive_failures.get(provider, 0) + 1
                    self._consecutive_failures[provider] = failures
                    category = classify_error(exc)
                    # Do not retry access-denied or rate-limit responses. Try
                    # the next backend instead, and stop using this one.
                    if category in {"rate_limited", "blocked_or_forbidden"}:
                        self._disabled_providers.add(provider)
                        continue
                    if failures >= 5:
                        logging.warning("DDGS backend %s has failed repeatedly; pausing it.", backend)
                        self._disabled_providers.add(provider)
                        continue
            if attempt <= self.config.retries and not all(
                f"ddgs:{backend}" in self._disabled_providers for backend in backends
            ):
                time.sleep(min(2 ** attempt, 8) + random.uniform(0.2, 0.8))
        if last_exc:
            logging.debug("DDGS search failed: %s", last_exc)
        return []

    def search(self, query: str, limit: int = 20, page: int = 1) -> list[dict[str, Any]]:
        """Query configured independent APIs, then DDGS; merge and deduplicate hits."""
        self.search_count += 1
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        providers: list[tuple[str, Any]] = []
        # API providers have their own offset support, but are only worth
        # calling on the first page; DDGS handles page-by-page discovery.
        if page == 1:
            if self.config.brave_api_key:
                providers.append(("brave_search_api", self._brave_search))
            if self.config.google_api_key and self.config.google_cse_id:
                providers.append(("google_programmable_search", self._google_search))
        providers.append(("ddgs", self._ddgs_search))

        for provider, search_fn in providers:
            try:
                hits = search_fn(query, limit, page)
                if hits:
                    self.success_count += 1
                for hit in hits:
                    href = clean_text(hit.get("href"))
                    key = href.split("?")[0].rstrip("/").casefold()
                    if href and key not in seen:
                        seen.add(key)
                        merged.append(hit)
                if len(merged) >= limit:
                    break
            except Exception as exc:
                self._record_issue(provider, query, exc)
                self.provider_stats.setdefault(provider, {"requests": 1, "successes": 0, "results": 0, "failures": 0})
                self.provider_stats[provider]["failures"] += 1
                logging.warning("Search provider %s failed: %s", provider, exc)
        return merged[:limit]


# ---------------------------------------------------------------------------
# Public profile-page fetcher (robots.txt + conservative pacing)
# ---------------------------------------------------------------------------

class PublicProfileFetcher:
    """Fetch only public profile pages allowed by robots.txt.

    This deliberately does not rotate proxies or retry access denials. A 403,
    429, CAPTCHA, or robots disallow is treated as a stop signal, not something
    to route around.
    """

    def __init__(self, config: ScrapeConfig) -> None:
        self.config = config
        self.session = requests.Session() if requests is not None else None
        if self.session is not None:
            self.session.headers.update({
                "User-Agent": config.user_agent,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "en-US,en;q=0.8",
            })
        self._robots: dict[str, RobotFileParser | None] = {}
        self._last_request: dict[str, float] = {}
        self._disallowed_hosts: set[str] = set()
        self._unverified_hosts: set[str] = set()
        self.issues: list[dict[str, Any]] = []

    def _issue(self, url: str, category: str, message: str) -> None:
        self.issues.append({
            "timestamp": utc_now(), "category": category, "url": url,
            "message": message[:500],
        })

    def _pace(self, host: str) -> None:
        previous = self._last_request.get(host, 0.0)
        delay = random.uniform(self.config.min_delay, self.config.max_delay)
        remaining = delay - (time.monotonic() - previous)
        if remaining > 0:
            time.sleep(remaining)
        self._last_request[host] = time.monotonic()

    def _robots_parser(self, base: str) -> RobotFileParser | None:
        if base in self._robots:
            return self._robots[base]
        if self.session is None:
            self._robots[base] = None
            return None
        robots_url = urljoin(base, "/robots.txt")
        try:
            response = self.session.get(robots_url, timeout=self.config.request_timeout)
            if response.status_code != 200:
                self._robots[base] = None
                return None
            parser = RobotFileParser()
            parser.set_url(robots_url)
            parser.parse(response.text.splitlines())
            self._robots[base] = parser
            return parser
        except Exception as exc:
            self._issue(robots_url, "robots_unavailable", f"Could not verify robots.txt: {exc}")
            self._robots[base] = None
            return None

    def fetch(self, profile_url: str) -> dict[str, Any] | None:
        if self.session is None or BeautifulSoup is None:
            self._issue(profile_url, "missing_dependency", "Install requests and beautifulsoup4 for public HTML extraction.")
            return None
        parsed = urlparse(profile_url)
        base = f"{parsed.scheme}://{parsed.netloc}"
        host = parsed.netloc.lower()
        if host in self._disallowed_hosts:
            return None
        if host in self._unverified_hosts:
            return None
        robots = self._robots_parser(base)
        if robots is None:
            self._unverified_hosts.add(host)
            self._issue(profile_url, "robots_not_verified", "Profile fetch skipped because robots.txt could not be checked.")
            return None
        if not robots.can_fetch(self.config.user_agent, profile_url):
            self._disallowed_hosts.add(host)
            self._issue(profile_url, "robots_disallowed", "robots.txt does not allow profile pages on this host.")
            return None
        self._pace(host)
        try:
            response = self.session.get(profile_url, timeout=self.config.request_timeout, allow_redirects=True)
            if response.status_code in (403, 429):
                self._issue(profile_url, "access_denied_or_rate_limited", f"HTTP {response.status_code}; stopping this profile fetch.")
                return None
            if response.status_code >= 400:
                self._issue(profile_url, "http_error", f"HTTP {response.status_code}")
                return None
            platform = "linkedin" if "linkedin.com" in host else "instagram"
            final_url = canonical_url(platform, response.url)
            if not final_url or final_url.casefold() != profile_url.casefold():
                self._issue(profile_url, "redirect_rejected", "Redirect did not remain on the exact requested public profile URL.")
                return None
            soup = BeautifulSoup(response.text, "html.parser")
            metadata: dict[str, Any] = {"_html_title": clean_text(soup.title.get_text(" ", strip=True) if soup.title else ""), "_page_url": final_url}
            for tag in soup.find_all("meta"):
                key = clean_text(tag.get("property") or tag.get("name") or tag.get("itemprop") or "").lower()
                value = clean_text(tag.get("content"))
                if key and value:
                    metadata[key] = value
            jsonld: list[Any] = []
            for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
                try:
                    jsonld.append(json.loads(tag.string or tag.get_text()))
                except (json.JSONDecodeError, TypeError):
                    continue
            metadata["_jsonld"] = jsonld
            return metadata
        except Exception as exc:
            self._issue(profile_url, classify_error(exc), f"Public profile fetch failed: {exc}")
            return None


def _jsonld_objects(value: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if isinstance(value, dict):
        found.append(value)
        graph = value.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                found.extend(_jsonld_objects(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_jsonld_objects(item))
    return found

def _meta_value(page: dict[str, Any] | None, *keys: str) -> str:
    if not page:
        return ""
    for key in keys:
        if page.get(key):
            return clean_text(page[key])
    return ""

def merge_linkedin_page(record: dict[str, Any], page: dict[str, Any] | None) -> dict[str, Any]:
    if not page:
        return record
    title = _meta_value(page, "og:title", "twitter:title", "_html_title")
    description = _meta_value(page, "og:description", "description", "twitter:description")
    objects = [obj for item in page.get("_jsonld", []) for obj in _jsonld_objects(item)]
    person = next((obj for obj in objects if str(obj.get("@type", "")).lower() in {"person", "profilepage"}), {})
    name = clean_text(person.get("name")) or title
    name = re.sub(r"\s*(?:\||·|-)\s*LinkedIn.*$", "", name, flags=re.I).strip()
    if name and len(name.split()) <= 7 and not re.search(r"linkedin|sign in|log in", name, re.I):
        record["name"] = name
    headline = clean_text(person.get("jobTitle"))
    if headline:
        record["headline"] = headline
    works_for = person.get("worksFor")
    if isinstance(works_for, dict):
        works_for = works_for.get("name", "")
    elif isinstance(works_for, list):
        works_for = ", ".join(clean_text(x.get("name") if isinstance(x, dict) else x) for x in works_for)
    if works_for:
        record["current_company"] = clean_text(works_for)
    address = person.get("address")
    if isinstance(address, dict):
        location = ", ".join(filter(None, [clean_text(address.get("addressLocality")), clean_text(address.get("addressRegion")), clean_text(address.get("addressCountry"))]))
        if location:
            record["location"] = location
    # The page description is used as an About candidate only when the public
    # page exposes it; provenance remains explicitly unverified.
    if description and not re.search(r"sign in|join linkedin|access denied", description, re.I):
        record["about"] = description
    record["data_source"] = "public_profile_html+search_result" if record.get("source_snippet") else "public_profile_html"
    record["profile_page_access"] = "public_html_accessible"
    if any(record.get(k) for k in ("name", "headline", "current_company", "location", "about")):
        record["verification"] = "public_page_metadata_unverified"
    return record

def merge_instagram_page(record: dict[str, Any], page: dict[str, Any] | None) -> dict[str, Any]:
    if not page:
        return record
    title = _meta_value(page, "og:title", "twitter:title", "_html_title")
    description = _meta_value(page, "og:description", "description", "twitter:description")
    username = record.get("username", "")
    # Only accept page metadata when it identifies the exact requested account.
    identity_text = f"{title} {description}".casefold()
    segment = description if username and f"@{username}".casefold() in identity_text else ""
    if segment:
        match = re.search(r"([\d,.]+(?:\s?[KMB])?)\s+Followers,\s*([\d,.]+(?:\s?[KMB])?)\s+Following,\s*([\d,.]+(?:\s?[KMB])?)\s+Posts", segment, re.I)
        if match:
            record["followers"] = match.group(1).replace(" ", "")
            record["following"] = match.group(2).replace(" ", "")
            record["post_count"] = match.group(3).replace(" ", "")
        name_match = re.search(r"^(.+?)\s*\(@" + re.escape(username) + r"\)", title, re.I)
        if name_match:
            record["full_name"] = clean_text(name_match.group(1).strip(" -|·"))
        bio_match = re.search(r"Posts\s*[-–—]\s*(?:See Instagram photos and videos from\s+[^:]+:\s*)?[\"“]?(.*)$", segment, re.I)
        candidate_bio = clean_text(bio_match.group(1).rstrip('\"”')) if bio_match else segment
        if candidate_bio:
            record["bio"] = candidate_bio
        emails = EMAIL_RE.findall(record["bio"])
        phones = [x for x in PHONE_RE.findall(record["bio"]) if 10 <= len(digits(x)) <= 15]
        record["email_or_phone"] = ", ".join(unique_strings(emails + phones))
    record["data_source"] = "public_profile_html+search_result" if record.get("source_snippet") else "public_profile_html"
    record["profile_page_access"] = "public_html_accessible"
    if any(record.get(k) for k in ("full_name", "bio", "followers", "following", "post_count")):
        record["verification"] = "public_page_metadata_unverified"
    return record


# ---------------------------------------------------------------------------
# Discovery and query expansion
# ---------------------------------------------------------------------------

LOCATION_ALIASES = {
    "bangalore": ["Bengaluru"],
    "bengaluru": ["Bangalore"],
    "mumbai": ["Bombay"],
    "bombay": ["Mumbai"],
    "delhi": ["New Delhi"],
    "new delhi": ["Delhi"],
    "chennai": ["Madras"],
    "madras": ["Chennai"],
    "kolkata": ["Calcutta"],
    "calcutta": ["Kolkata"],
    "pune": ["Poona"],
    "poona": ["Pune"],
}


def _split_keywords(value: str) -> list[str]:
    """Split a comma/semicolon/newline separated keyword string."""
    return [item.strip() for item in re.split(r"[,;\n]+", value or "") if item.strip()]


def location_variants(location: str) -> list[str]:
    """Return the supplied location plus common local aliases."""
    location = clean_text(location)
    if not location:
        return [""]
    variants = [location]
    variants.extend(LOCATION_ALIASES.get(location.casefold(), []))
    for key, aliases in LOCATION_ALIASES.items():
        if key in location.casefold():
            variants.extend(aliases)
            break
    return unique_strings(variants)


def build_queries(platform: str, query: ProfileQuery) -> list[str]:
    """Build a broad set of discovery queries.

    Search engines are inconsistent about quoted phrases and site: filters.
    Unquoted variants are emitted first because they return far more profile
    results for Instagram; quoted variants follow as precision fallbacks.
    """
    if platform not in {"linkedin", "instagram"}:
        raise ValueError("Platform must be linkedin or instagram.")
    domain = "linkedin.com/in" if platform == "linkedin" else "instagram.com"
    keywords = _split_keywords(query.keywords)
    locations = location_variants(query.location)
    hashtag = clean_text(query.hashtag).lstrip("#")

    if not keywords and not hashtag and not any(locations):
        raise ValueError(f"{platform}: supply profile URLs, keywords, hashtag, or location.")

    unquoted: list[str] = []
    quoted: list[str] = []
    for keyword in keywords:
        for location in locations:
            if location:
                unquoted.extend([
                    f"site:{domain} {keyword} {location}",
                    f"site:{domain} {location} {keyword}",
                ])
                if platform == "linkedin":
                    unquoted.append(f"site:in.linkedin.com/in {keyword} {location}")
                else:
                    unquoted.append(f"site:instagram.com {keyword} {location} profile")
                quoted.append(f"site:{domain} \"{keyword}\" \"{location}\"")
            else:
                unquoted.extend([f"site:{domain} {keyword}", f"site:{domain} {keyword} profile"])
                quoted.append(f"site:{domain} \"{keyword}\"")

    if hashtag:
        for location in locations:
            suffix = f" {location}" if location else ""
            unquoted.extend([
                f"site:instagram.com {hashtag}{suffix}",
                f"site:instagram.com #{hashtag}{suffix}",
            ])
            if location:
                quoted.append(f"site:instagram.com \"#{hashtag}\" \"{location}\"")

    if not keywords and any(locations):
        for location in locations:
            if location:
                unquoted.extend([
                    f"site:{domain} {location}",
                    f"site:{domain} {location} profile",
                ])
                quoted.append(f"site:{domain} \"{location}\"")

    return unique_strings(unquoted + quoted)


def result_url(hit: dict[str, Any]) -> str:
    return clean_text(hit.get("href") or hit.get("url") or hit.get("link") or "")


def discover_profiles(
    platform: str,
    manager: SearchManager,
    query: ProfileQuery,
    config: ScrapeConfig | None = None,
) -> list[tuple[str, dict[str, Any]]]:
    """Search query pages until the target is reached or the budget is spent."""
    config = config or ScrapeConfig()
    config.validate()
    max_pages = max(1, config.search_pages)
    budget = max(1, config.max_search_requests)
    found: list[tuple[str, dict[str, Any]]] = []
    seen: set[str] = set()
    searches = 0
    queries = build_queries(platform, query)
    for search_query in queries:
        if len(found) >= query.max_results or searches >= budget:
            break
        empty_streak = 0
        for page in range(1, max_pages + 1):
            if len(found) >= query.max_results or searches >= budget:
                break
            remaining = query.max_results - len(found)
            hits = manager.search(
                search_query,
                limit=min(max(remaining * 3, 20), 50),
                page=page,
            )
            searches += 1
            added = 0
            for hit in hits:
                url = canonical_url(platform, result_url(hit))
                if url and url not in seen:
                    seen.add(url)
                    found.append((url, hit))
                    added += 1
                    if len(found) >= query.max_results:
                        break
            if added:
                empty_streak = 0
                logging.info(
                    "%s discovery: %d/%d unique profiles (%d searches)",
                    platform.title(), len(found), query.max_results, searches,
                )
            else:
                empty_streak += 1
                # Search engines sometimes return an empty page and then
                # resume on the next one; only abandon this query after a
                # short run of empty pages.
                if empty_streak >= 3:
                    break
    if len(found) < query.max_results:
        logging.warning(
            "%s discovery stopped at %d/%d profiles after %d searches; "
            "provider limits or empty result pages prevented reaching the target.",
            platform.title(), len(found), query.max_results, searches,
        )
    return found


def exact_profile_search(platform: str, profile_url: str, manager: SearchManager) -> dict[str, Any] | None:
    slug = profile_identifier(platform, profile_url)
    domain = "linkedin.com/in" if platform == "linkedin" else "instagram.com"
    query = f'site:{domain} "{slug}" "{profile_url}"'
    hits = manager.search(query, limit=10)
    exact = [hit for hit in hits if canonical_url(platform, result_url(hit)) == profile_url]
    if not exact:
        return None
    # Prefer a useful title/snippet, but exact URL is a hard requirement.
    return max(exact, key=lambda hit: len(clean_text(hit.get("title"))) + min(len(clean_text(hit.get("body"))), 1000))


# ---------------------------------------------------------------------------
# Data extraction: LinkedIn
# ---------------------------------------------------------------------------

def _linkedin_name_tokens(name: str) -> list[str]:
    tokens = [token for token in re.split(r"[^a-z]+", name.casefold()) if len(token) >= 2]
    return [token for token in tokens if token not in {"dr", "mr", "mrs", "ms", "prof", "phd"}]


def _linkedin_slug_tokens(profile_url: str) -> list[str]:
    slug = profile_identifier("linkedin", profile_url) or ""
    return [token for token in re.split(r"[-_.]+", slug.casefold()) if token and not token.isdigit()]


def linkedin_name_matches_slug(name: str, profile_url: str) -> bool:
    """Return True when the title name is supported by the profile URL slug."""
    name_tokens = _linkedin_name_tokens(name)
    slug_tokens = _linkedin_slug_tokens(profile_url)
    if not name_tokens or not slug_tokens:
        return False
    matches = 0
    strong_match = False
    for name_token in name_tokens:
        for slug_token in slug_tokens:
            if name_token == slug_token:
                matches += 1
                if len(name_token) >= 4:
                    strong_match = True
                break
            if len(name_token) >= 4 and (
                slug_token.startswith(name_token) or name_token.startswith(slug_token)
            ):
                matches += 1
                strong_match = True
                break
    return matches >= 2 or strong_match


def _looks_like_person_name(candidate: str) -> bool:
    tokens = candidate.split()
    return (
        1 <= len(tokens) <= 6
        and not re.search(
            r"\b(data|scientist|analyst|engineer|engineering|manager|management|developer|"
            r"development|consultant|architect|lead|director|specialist|designer|founder|"
            r"ceo|cto|intern|student|senior|junior|principal|staff|head|vp|president|"
            r"officer|associate|assistant|computer|vision|machine|learning|artificial|"
            r"intelligence|software|product|project|business|marketing|sales|research|"
            r"solutions|analytics|science|technology|technical)\b",
            candidate,
            re.I,
        )
        and not any(ch in candidate for ch in "|@")
    )


def _clean_linkedin_headline(value: str) -> str:
    value = re.split(r"\s*(?:\.\.\.|…)\s*", value, maxsplit=1)[0]
    value = re.sub(r"\s*\|\s*LinkedIn.*$", "", value, flags=re.I)
    return clean_text(value)


def _clean_linkedin_company(value: str) -> str:
    value = re.split(r"\s*(?:[|·•]|\.\.\.|…)\s*", value, maxsplit=1)[0]
    return clean_text(value).strip(" ,-")


def _is_plausible_company(value: str) -> bool:
    """Reject prose, skills lists, and school labels captured as companies."""
    if not value or len(value) > 60:
        return False
    if "/" in value or "@" in value or re.search(r"[|]", value):
        return False
    if re.search(
        r"\b(?:tools?|techniques?|skills?|proficient|experience|industrial|"
        r"education|university|college|campus|school|student|looking|seeking|"
        r"available|resume|curriculum)\b",
        value,
        re.I,
    ):
        return False
    if re.search(
        r"\b(?:at|data|scientist|analyst|engineer|manager|developer|lead|"
        r"director|specialist|senior|junior|principal)\b",
        value,
        re.I,
    ):
        return False
    return True


def is_aggregated_linkedin(title: str, body: str, profile_url: str = "") -> bool:
    title = title.replace("\ufffd", " - ")
    combined = clean_text(f"{title} {body}")
    suffixes = len(re.findall(r"\|\s*LinkedIn", title, re.I))
    name_headline_patterns = re.findall(
        r"([A-Z][A-Za-z.'’-]+(?:\s+[A-Z][A-Za-z.'’-]+){1,4})\s+[-\u2013\u2014]\s+", title
    )
    plausible_names = [name for name in name_headline_patterns if _looks_like_person_name(name)]
    first_name_supported = bool(
        profile_url
        and plausible_names
        and linkedin_name_matches_slug(plausible_names[0], profile_url)
    )
    # When several titles are packed into one snippet, keep the first profile only
    # if its name is independently supported by the profile URL slug.
    if len(plausible_names) > 1 and not first_name_supported:
        return True
    if suffixes > 1 and not first_name_supported:
        return True
    return len(combined) > 1500 and not first_name_supported


def parse_linkedin(hit: dict[str, Any] | None, profile_url: str) -> dict[str, Any]:
    raw_title = clean_text(hit.get("title")) if hit else ""
    body = clean_text(hit.get("body")) if hit else ""
    record: dict[str, Any] = {
        "name": "", "headline": "", "current_company": "", "location": "", "about": "",
        "profile_url": profile_url, "data_source": "public_search_result",
        "verification": "profile_url_only_no_public_snippet", "source_title": raw_title,
        "source_snippet": body, "search_provider": clean_text(hit.get("provider")) if hit else "", "collected_at": utc_now(),
    }
    if not hit:
        return record
    if is_aggregated_linkedin(raw_title, body, profile_url):
        record["verification"] = "rejected_aggregated_or_ambiguous_snippet"
        return record

    title = re.sub(r"\s*[|·]\s*LinkedIn\s*$", "", raw_title, flags=re.I).strip(" -|·")
    title = re.sub(r"\s*-\s*LinkedIn\s*$", "", title, flags=re.I).strip()
    title = title.replace("\ufffd", " - ")
    # Typical public search title: Name - Headline | LinkedIn.
    parts = [clean_text(part) for part in re.split(r"\s+[-\u2013\u2014]\s+", title) if clean_text(part)]
    if len(parts) >= 2:
        candidate = parts[0]
        if _looks_like_person_name(candidate):
            record["name"] = candidate
            record["headline"] = _clean_linkedin_headline(parts[1])
            if len(parts) >= 3:
                company = _clean_linkedin_company(_clean_linkedin_headline(parts[2]))
                if _is_plausible_company(company):
                    record["current_company"] = company
    elif len(parts) == 1:
        candidate = parts[0]
        if _looks_like_person_name(candidate):
            record["name"] = candidate

    headline = record["headline"]
    company_match = re.search(r"\s+(?:at|@)\s+(.+)$", headline, re.I)
    if not record["current_company"] and company_match:
        company = _clean_linkedin_company(company_match.group(1))
        if _is_plausible_company(company):
            record["current_company"] = company
            record["headline"] = headline[:company_match.start()].strip()

    # Public snippets often expose a compact "Experience: Company" label even
    # when the full profile page is blocked. Require a real separator so
    # "Industrial Experience:" prose is not mistaken for the label.
    experience_match = re.search(
        r"(?:^|[·•|;])\s*Experience\s*:\s*([^·•|;\n]{2,80})", body, re.I
    )
    if not record["current_company"] and experience_match:
        company = _clean_linkedin_company(experience_match.group(1))
        if _is_plausible_company(company):
            record["current_company"] = company

    location_match = re.search(
        r"(?:Location|Based in)\s*:?\s*([^·•|;\n]{2,80}?)"
        r"(?:\s*[·•|;]|\s+(?:with|and|having|where|who)\b|\.|$)",
        body,
        re.I,
    )
    if location_match:
        record["location"] = location_match.group(1).strip(" ,")

    # Search snippets are not the full LinkedIn About section. Use only prose
    # from an exact, accepted snippet; never invent or pad the field.
    about_text = re.split(r"\s*·\s*Experience\s*:", body, maxsplit=1)[0]
    about_text = re.sub(
        r"\s*·\s*(?:Education|Location)\s*:.*$", "", about_text, flags=re.I
    )
    about_text = _clean_linkedin_headline(about_text)
    if (
        record["name"]
        and len(about_text) >= 60
        and about_text.casefold() != record["headline"].casefold()
        and re.search(r"\b(?:my|our|i|we|i'm|i am|i have|experienced|passionate|skilled|proven)\b",
                      about_text, re.I)
    ):
        record["about"] = about_text[:600]
    if record["name"]:
        record["verification"] = "identity_candidate_from_search_title_unverified"
    else:
        record["verification"] = "insufficient_profile_identity"
    return record


# ---------------------------------------------------------------------------
# Data extraction: Instagram (username-isolated snippets)
# ---------------------------------------------------------------------------

def instagram_profile_segment(body: str, username: str) -> tuple[str, str]:
    body = clean_text(body)
    segments = [part.strip() for part in IG_STATS_START.split(body) if part.strip()]
    target = re.compile(r"@" + re.escape(username) + r"(?![A-Za-z0-9._])", re.I)
    if len(segments) > 1:
        matches = [segment for segment in segments if target.search(segment)]
        if len(matches) == 1:
            return matches[0], "snippet_isolated_by_exact_username"
        return "", "rejected_ambiguous_multi_profile_snippet"
    if target.search(body):
        return body, "snippet_contains_exact_username_unverified"
    return "", "profile_not_identifiable_in_search_snippet"


def parse_instagram(hit: dict[str, Any] | None, profile_url: str) -> dict[str, Any]:
    username = profile_identifier("instagram", profile_url)
    raw_title = clean_text(hit.get("title")) if hit else ""
    body = clean_text(hit.get("body")) if hit else ""
    segment, status = instagram_profile_segment(body, username) if hit else ("", "profile_url_only_no_public_snippet")
    safe = bool(segment)

    full_name = ""
    if safe:
        name_match = re.search(r"([^\n]{2,120}?)\s*\(@" + re.escape(username) + r"\)", segment, re.I)
        if name_match:
            candidate = name_match.group(1).strip(" -–—:\"'")
            # Remove a leading stats wrapper without truncating thousands counts.
            candidate = re.sub(
                r"^[\d,.]+(?:\s?[KMB])?\s+Followers,.*?\bPosts\s*[-–—]\s*", "", candidate, flags=re.I
            )
            full_name = clean_text(candidate)

    def metric(label: str) -> str:
        if not safe:
            return ""
        match = re.search(rf"([\d,.]+(?:\s?[KMB])?)\s+{label}\b", segment, re.I)
        return match.group(1).replace(" ", "") if match else ""

    bio = ""
    if safe:
        bio_match = re.search(r"on Instagram:\s*[\"“]?(.*)$", segment, re.I)
        bio = bio_match.group(1).strip().rstrip("\"”") if bio_match else segment
        # Avoid retaining the account's metric/name preamble as bio text.
        bio = re.sub(r"^.*?\bPosts\s*[-–—]\s*", "", bio, flags=re.I)

    emails = EMAIL_RE.findall(bio) if safe else []
    phones: list[str] = []
    if safe:
        for candidate in PHONE_RE.findall(bio):
            candidate = candidate.strip()
            count = len(digits(candidate))
            # Avoid extracting short numeric fragments or follower counts.
            if 10 <= count <= 15:
                phones.append(candidate)
    contacts = unique_strings(emails + phones)

    return {
        "username": username, "full_name": full_name, "bio": bio,
        "followers": metric("followers"), "following": metric("following"),
        "post_count": metric("posts"), "email_or_phone": ", ".join(contacts),
        "profile_url": profile_url, "data_source": "public_search_result",
        "verification": status, "source_title": raw_title,
        "source_snippet": body, "search_provider": clean_text(hit.get("provider")) if hit else "",
        "collected_at": utc_now(),
    }


# ---------------------------------------------------------------------------
# Validation and extraction workflow
# ---------------------------------------------------------------------------

def validate_record(platform: str, record: dict[str, Any]) -> dict[str, Any]:
    """Add a transparent quality label; never promote search snippets to verified."""
    if platform == "linkedin":
        has_identity = bool(record.get("name"))
        has_context = bool(record.get("headline") or record.get("current_company") or record.get("location"))
    else:
        has_identity = bool(record.get("username"))
        has_context = bool(record.get("full_name") or record.get("bio") or record.get("followers"))

    status = record.get("verification", "")
    if status.startswith("rejected"):
        quality = "rejected"
    elif has_identity and has_context:
        quality = "partially_enriched_unverified"
    elif has_identity:
        quality = "identity_only_unverified"
    else:
        quality = "url_only_unverified"
    record["data_quality"] = quality
    return record


def scrape_platform(platform: str, manager: SearchManager, query: ProfileQuery, config: ScrapeConfig) -> list[dict[str, Any]]:
    if platform not in {"linkedin", "instagram"}:
        raise ValueError("Platform must be linkedin or instagram.")
    results: list[dict[str, Any]] = []
    seen: set[str] = set()

    if query.profile_urls:
        candidates: list[tuple[str, dict[str, Any] | None]] = []
        for raw_url in query.profile_urls:
            normalized = canonical_url(platform, raw_url)
            if not normalized:
                logging.warning("Skipping invalid %s profile URL: %s", platform, raw_url)
                continue
            if normalized not in seen:
                seen.add(normalized)
                candidates.append((normalized, None))
    else:
        candidates = discover_profiles(platform, manager, query, config)

    parser = parse_linkedin if platform == "linkedin" else parse_instagram
    fetcher = PublicProfileFetcher(config) if config.fetch_public_profiles else None
    for profile_url, discovery_hit in candidates[:query.max_results]:
        # Try the supplied/discovered public profile page first. A direct URL
        # should not require a search-engine query just to attempt extraction.
        page = fetcher.fetch(profile_url) if fetcher else None
        hit = discovery_hit
        # If public HTML is unavailable, an exact public search lookup may
        # provide a limited snippet. This never bypasses access restrictions.
        if hit is None and page is None and config.targeted_lookup:
            hit = exact_profile_search(platform, profile_url, manager)
        record = parser(hit, profile_url)
        record = merge_linkedin_page(record, page) if platform == "linkedin" else merge_instagram_page(record, page)
        record = validate_record(platform, record)
        results.append(record)
    if fetcher:
        manager.issues.extend(fetcher.issues)
    return normalize_profile_records(platform, results)


# ---------------------------------------------------------------------------
# CLI and application entry point
# ---------------------------------------------------------------------------

def parse_csv_urls(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def load_input_json(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    file_path = Path(path)
    if not file_path.is_file():
        raise FileNotFoundError(f"Input JSON file not found: {file_path}")
    data = json.loads(file_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Input JSON must contain an object at the top level.")
    return data


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover public LinkedIn and Instagram profiles and extract available public metadata."
    )
    parser.add_argument("--input-json", help="Optional JSON file containing profile URLs and search filters.")
    parser.add_argument("--linkedin-urls", default="", help="Comma-separated LinkedIn profile URLs.")
    parser.add_argument("--instagram-urls", default="", help="Comma-separated Instagram profile URLs.")
    parser.add_argument("--linkedin-keywords", default="Data Scientist", help="LinkedIn discovery keywords.")
    parser.add_argument("--instagram-keywords", default="photographer", help="Instagram discovery keywords.")
    parser.add_argument("--hashtag", default="", help="Optional hashtag (without or with #).")
    parser.add_argument("--location", default="Bengaluru", help="Location used in both platform searches.")
    parser.add_argument("--max-results", type=int, default=100, help="Maximum profiles per platform.")
    parser.add_argument("--min-delay", type=float, default=2.0, help="Minimum seconds between search requests.")
    parser.add_argument("--max-delay", type=float, default=4.0, help="Maximum seconds between search requests.")
    parser.add_argument("--retries", type=int, default=2, help="Retries for transient search errors.")
    parser.add_argument(
        "--backend", default="yahoo,auto",
        help="Comma-separated DDGS backends in priority order (default: yahoo,auto).",
    )
    parser.add_argument("--pages", type=int, default=15, help="Result pages to request per query.")
    parser.add_argument(
        "--max-search-requests", type=int, default=80,
        help="Maximum search requests per platform (safety budget).",
    )
    parser.add_argument("--region", default="in-en", help="DDGS region, for example in-en or us-en.")
    parser.add_argument("--output-dir", default="output/social", help="Directory for CSV, JSON, and Excel outputs.")
    parser.add_argument("--no-targeted-lookup", action="store_true", help="Do not search supplied profile URLs individually.")
    parser.add_argument("--no-profile-fetch", action="store_true", help="Use search-result snippets only; do not fetch public profile HTML.")
    parser.add_argument("--request-timeout", type=float, default=15.0, help="Timeout for permitted public profile-page requests.")
    parser.add_argument("--self-test", action="store_true", help="Run offline parser/validation tests and exit.")
    parser.add_argument("--check-search", action="store_true", help="Run one live search-provider connectivity check and exit.")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def run_self_tests() -> None:
    """Small offline regression suite for the failure cases seen in prior runs."""
    assert canonical_url("linkedin", "https://www.linkedin.com/in/test-user/?trk=abc") == "https://www.linkedin.com/in/test-user/"
    assert canonical_url("linkedin", "https://in.linkedin.com/in/test-user/") == "https://www.linkedin.com/in/test-user/"
    assert canonical_url("linkedin", "https://uk.linkedin.com/in/test-user") == "https://www.linkedin.com/in/test-user/"
    assert canonical_url("instagram", "https://www.instagram.com/sample.user/?hl=en") == "https://www.instagram.com/sample.user/"
    assert canonical_url("instagram", "https://www.instagram.com/p/ABC123/") == ""
    assert canonical_url("instagram", "https://www-fallback.instagram.com/popular/") == ""

    ig_url = "https://www.instagram.com/beingphotographerbangalore/"
    ig_hit = {
        "title": "Being Photographer (@beingphotographerbangalore) | Instagram",
        "body": "2,900 Followers, 7,200 Following, 1,234 Posts - See Instagram photos and videos from Being Photographer (@beingphotographerbangalore) on Instagram: Wedding photographer contact 9620434357",
    }
    ig = parse_instagram(ig_hit, ig_url)
    assert ig["followers"] == "2,900", ig
    assert ig["following"] == "7,200", ig
    assert ig["post_count"] == "1,234", ig
    assert "9620434357" in ig["email_or_phone"], ig

    linkedin_url = "https://www.linkedin.com/in/jane-doe/"
    li = parse_linkedin({"title": "Jane Doe - Data Scientist at Example Corp | LinkedIn", "body": "Based in Bengaluru"}, linkedin_url)
    assert li["name"] == "Jane Doe", li
    assert li["current_company"] == "Example Corp", li
    aggregate_title = "Jane Doe - Data Scientist | LinkedIn | John Smith - Engineer | LinkedIn"
    aggregate = parse_linkedin({"title": aggregate_title, "body": ""}, "https://www.linkedin.com/in/unrelated-slug/")
    assert aggregate["verification"] == "rejected_aggregated_or_ambiguous_snippet", aggregate
    recovered = parse_linkedin({"title": aggregate_title, "body": ""}, "https://www.linkedin.com/in/jane-doe/")
    assert recovered["name"] == "Jane Doe" and recovered["verification"] != "rejected_aggregated_or_ambiguous_snippet", recovered

    isolated, status = instagram_profile_segment(
        "2,900 Followers, 7,200 Following, 100 Posts - Person A (@person.a) on Instagram: A bio "
        "1,200 Followers, 900 Following, 80 Posts - Person B (@person.b) on Instagram: B bio",
        "person.b",
    )
    assert "Person B" in isolated and status == "snippet_isolated_by_exact_username", (isolated, status)
    li_page = {"og:title": "Jane Doe | LinkedIn", "og:description": "Senior Engineer at Example Corp. Based in Bengaluru.", "_jsonld": [{"@type": "Person", "name": "Jane Doe", "jobTitle": "Senior Engineer", "worksFor": {"name": "Example Corp"}, "address": {"addressLocality": "Bengaluru"}}]}
    li_page_record = merge_linkedin_page(parse_linkedin(None, linkedin_url), li_page)
    assert li_page_record["name"] == "Jane Doe" and li_page_record["headline"] == "Senior Engineer", li_page_record
    ig_page = {"og:title": "Sample Person (@sample.user) • Instagram", "og:description": "2,900 Followers, 7,200 Following, 100 Posts - Sample Person (@sample.user) on Instagram: Photographer Bengaluru contact hello@example.com"}
    ig_page_record = merge_instagram_page(parse_instagram(None, "https://www.instagram.com/sample.user/"), ig_page)
    assert ig_page_record["followers"] == "2,900" and "hello@example.com" in ig_page_record["email_or_phone"], ig_page_record
    print("Self-test passed: URL validation, Instagram metric parsing, exact-account isolation, LinkedIn field extraction, public metadata merging, and aggregation rejection.")


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s | %(levelname)s | %(message)s")

    try:
        if args.self_test:
            run_self_tests()
            return 0

        if args.check_search:
            config = ScrapeConfig(
                min_delay=args.min_delay, max_delay=args.max_delay,
                retries=args.retries, search_backend=args.backend,
                search_pages=args.pages, max_search_requests=args.max_search_requests,
                search_region=args.region,
            )
            config.validate()
            manager = SearchManager(config)
            hits = manager.search("site:instagram.com photographer Bengaluru", limit=5)
            print(json.dumps({
                "backend": config.search_backend,
                "search_requests": manager.search_count,
                "successful_search_requests": manager.success_count,
                "returned_results": len(hits),
                "sample_results": [
                    {"title": clean_text(hit.get("title")), "url": result_url(hit)}
                    for hit in hits[:5]
                ],
                "provider_stats": manager.provider_stats,
                "errors": manager.issues,
            }, indent=2, ensure_ascii=False))
            return 0 if manager.success_count else 2

        inputs = load_input_json(args.input_json)
        config = ScrapeConfig(
            min_delay=args.min_delay, max_delay=args.max_delay, retries=args.retries,
            max_results=args.max_results, search_backend=args.backend,
            search_pages=args.pages, max_search_requests=args.max_search_requests,
            search_region=args.region,
            targeted_lookup=not args.no_targeted_lookup, output_dir=str(project_path(args.output_dir)),
            fetch_public_profiles=not args.no_profile_fetch, request_timeout=args.request_timeout,
        )
        config.validate()

        linkedin_query = ProfileQuery(
            profile_urls=parse_csv_urls(args.linkedin_urls) or list(inputs.get("linkedin_urls", [])),
            keywords=str(inputs.get("linkedin_keywords", args.linkedin_keywords) or ""),
            hashtag=str(inputs.get("hashtag", args.hashtag) or ""),
            location=str(inputs.get("location", args.location) or ""),
            max_results=config.max_results,
        )
        instagram_query = ProfileQuery(
            profile_urls=parse_csv_urls(args.instagram_urls) or list(inputs.get("instagram_urls", [])),
            keywords=str(inputs.get("instagram_keywords", args.instagram_keywords) or ""),
            hashtag=str(inputs.get("hashtag", args.hashtag) or ""),
            location=str(inputs.get("location", args.location) or ""),
            max_results=config.max_results,
        )

        # When a platform has explicit URLs, use only those; otherwise discover.
        started_at = utc_now()
        manager = SearchManager(config)
        logging.info("Starting LinkedIn profile discovery/extraction...")
        linkedin = scrape_platform("linkedin", manager, linkedin_query, config)
        logging.info("LinkedIn: %d profile records", len(linkedin))
        logging.info("Starting Instagram profile discovery/extraction...")
        instagram = scrape_platform("instagram", manager, instagram_query, config)
        logging.info("Instagram: %d profile records", len(instagram))

        linkedin = normalize_profile_records("linkedin", linkedin)
        instagram = normalize_profile_records("instagram", instagram)
        database = LeadDatabase(project_path("data/leads.db"))
        try:
            for row in linkedin:
                database.save_social_profile("linkedin", row)
            for row in instagram:
                database.save_social_profile("instagram", row)
        finally:
            database.close()

        def completeness(rows: list[dict[str, Any]], fields: list[str]) -> dict[str, int]:
            return {
                field_name: sum(bool(row.get(field_name)) for row in rows)
                for field_name in fields
            }

        summary = {
            "started_at": started_at,
            "completed_at": utc_now(),
            "linkedin_profiles": len(linkedin),
            "instagram_profiles": len(instagram),
            "linkedin_with_name": sum(bool(row.get("name")) for row in linkedin),
            "instagram_with_bio_or_stats": sum(bool(row.get("bio") or row.get("followers")) for row in instagram),
            "linkedin_valid_profiles": sum(
                row.get("data_quality") in {"partially_enriched_unverified", "identity_only_unverified", "public_page_verified"}
                for row in linkedin
            ),
            "instagram_valid_profiles": sum(
                row.get("data_quality") in {"partially_enriched_unverified", "identity_only_unverified", "public_page_verified"}
                for row in instagram
            ),
            "linkedin_rejected_profiles": sum(row.get("data_quality") == "rejected" for row in linkedin),
            "instagram_rejected_profiles": sum(row.get("data_quality") == "rejected" for row in instagram),
            "linkedin_field_completeness": completeness(linkedin, LINKEDIN_PROFILE_FIELDS),
            "instagram_field_completeness": completeness(instagram, INSTAGRAM_PROFILE_FIELDS),
            "linkedin_public_profile_pages_accessed": sum(row.get("profile_page_access") == "public_html_accessible" for row in linkedin),
            "instagram_public_profile_pages_accessed": sum(row.get("profile_page_access") == "public_html_accessible" for row in instagram),
            "linkedin_quality_counts": {},
            "instagram_quality_counts": {},
            "search_requests": manager.search_count,
            "successful_search_requests": manager.success_count,
            "search_error_count": len(manager.issues),
            "search_error_categories": {},
            "search_provider_stats": manager.provider_stats,
            "search_provider_note": "Brave Search API and Google Programmable Search are queried only when their API credentials are configured; DDGS is the fallback. DDGS backend labels are not assumed to be independent engines.",
            "important_note": "Search-result extraction is unverified and may be incomplete; profile counts are not a guarantee of usable leads.",
        }
        for platform_rows, key in ((linkedin, "linkedin_quality_counts"), (instagram, "instagram_quality_counts")):
            counts: dict[str, int] = {}
            for row in platform_rows:
                quality = row.get("data_quality", "unknown")
                counts[quality] = counts.get(quality, 0) + 1
            summary[key] = counts
        for issue in manager.issues:
            category = issue.get("category", "unknown")
            summary["search_error_categories"][category] = summary["search_error_categories"].get(category, 0) + 1

        output_dir = Path(config.output_dir)
        export_social_profiles(output_dir, linkedin, instagram)
        # Keep the historical output/ root files in sync so existing scripts,
        # downloads, and the assignment's documented paths keep working.
        legacy_output_dir = project_path("output")
        if output_dir.resolve() != legacy_output_dir.resolve():
            export_social_profiles(legacy_output_dir, linkedin, instagram)

        print("\nSCRAPE SUMMARY")
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        print(f"\nFiles saved to: {output_dir.resolve()}")
        if len(linkedin) < config.max_results or len(instagram) < config.max_results:
            print("\nNOTE: Search engines may return fewer profiles than requested. Do not treat missing results as proof that no profiles exist.")
        return 0

    except KeyboardInterrupt:
        logging.warning("Interrupted by user.")
        return 130
    except Exception as exc:
        logging.exception("Scraper failed: %s", exc)
        return 1


class MapsScraper:
    """Collect public business listing details from Maps result pages."""

    def __init__(
        self,
        headless: bool = True,
        delay: tuple[float, float] = (2.0, 3.5),
        navigation_timeout_ms: int = 30000,
        place_timeout_ms: int = 15000,
    ) -> None:
        if sync_playwright is None:
            raise RuntimeError("Google Maps scraping requires the 'playwright' package.")
        if delay[0] < 0 or delay[1] < delay[0]:
            raise ValueError("Invalid delay range.")
        self.headless = headless
        self.delay = delay
        self.navigation_timeout_ms = navigation_timeout_ms
        self.place_timeout_ms = place_timeout_ms
        self.errors: list[dict] = []

    def _sleep(self) -> None:
        time.sleep(random.uniform(*self.delay))

    @staticmethod
    def _canonical_maps_url(href: str, base_url: str = "https://www.google.com") -> str:
        absolute = urljoin(base_url, href)
        parsed = urlparse(absolute)
        if "google." not in (parsed.hostname or "") or "/maps/place/" not in parsed.path:
            return ""
        # Keep the place path but remove tracking/query parameters.
        return urlunparse(
            (parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", "")
        )

    @staticmethod
    def _normalize_website(href: str) -> str:
        """Resolve Google redirect links (``/url?q=...``) to the real website."""
        href = (href or "").strip()
        if not href:
            return ""
        absolute = urljoin("https://www.google.com", href)
        parsed = urlparse(absolute)
        host = (parsed.hostname or "").lower()
        if "google." in host or host.endswith("googleusercontent.com"):
            params = parse_qs(parsed.query)
            target = (params.get("q") or params.get("url") or [""])[0]
            target = unquote(target).strip()
            target_parsed = urlparse(target)
            if target_parsed.scheme not in {"http", "https"} or not target_parsed.hostname:
                return ""
            if "google." in (target_parsed.hostname or "").lower():
                return ""
            return urlunparse(
                (
                    target_parsed.scheme,
                    target_parsed.netloc,
                    target_parsed.path,
                    target_parsed.params,
                    target_parsed.query,
                    "",
                )
            )
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            return absolute
        return ""

    @classmethod
    def _normalize_record(cls, data: dict) -> dict:
        normalized = {
            key: str(data.get(key, "") or "").strip()
            for key in (
                "business_name", "category", "address", "phone", "website",
                "rating", "reviews",
            )
        }
        normalized["website"] = cls._normalize_website(normalized["website"])
        normalized["maps_url"] = cls._canonical_maps_url(
            normalized.get("maps_url", "") or data.get("maps_url", "")
        )
        return normalized

    @staticmethod
    def _safe_text(page, selector: str) -> str:
        locator = page.locator(selector).first
        try:
            if locator.count():
                return locator.inner_text(timeout=2500).strip()
        except Exception:
            pass
        return ""

    @staticmethod
    def _safe_attr(page, selector: str, attribute: str = "aria-label") -> str:
        locator = page.locator(selector).first
        try:
            if locator.count():
                return (locator.get_attribute(attribute, timeout=2500) or "").strip()
        except Exception:
            pass
        return ""

    def _accept_consent_if_present(self, page) -> None:
        for selector in (
            "button:has-text('Accept all')",
            "button:has-text('Reject all')",
        ):
            try:
                button = page.locator(selector).first
                if button.is_visible(timeout=1000):
                    button.click(timeout=2000)
                    self._sleep()
                    return
            except Exception:
                continue

    def _goto(self, page, url: str, timeout_ms: int, attempts: int = 2) -> None:
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                return
            except Exception as exc:
                last_error = exc
                if attempt + 1 < attempts:
                    self._sleep()
        raise RuntimeError(
            f"Navigation failed after {attempts} attempt(s): {url}"
        ) from last_error

    def _collect_urls(self, page, search_query: str, limit: int) -> list[str]:
        search_url = (
            "https://www.google.com/maps/search/"
            f"{quote_plus(search_query)}?hl=en"
        )
        self._goto(page, search_url, self.navigation_timeout_ms)
        self._accept_consent_if_present(page)

        feed = page.locator('div[role="feed"]').first
        try:
            feed.wait_for(state="visible", timeout=self.place_timeout_ms)
        except PlaywrightTimeoutError:
            # Google may open a single place directly instead of a results feed.
            current = self._canonical_maps_url(page.url)
            return [current] if "/maps/place/" in current else []

        seen: set[str] = set()
        urls: list[str] = []
        unchanged_rounds = 0

        while len(urls) < limit and unchanged_rounds < 5:
            previous_count = len(urls)
            anchors = feed.locator('a[href*="/maps/place/"]')
            try:
                hrefs = anchors.evaluate_all(
                    "(els) => els.map(el => el.href).filter(Boolean)"
                )
            except Exception:
                hrefs = []

            for href in hrefs:
                canonical = self._canonical_maps_url(href, page.url)
                if canonical and canonical not in seen:
                    seen.add(canonical)
                    urls.append(canonical)
                    if len(urls) >= limit:
                        break

            if len(urls) >= limit:
                break

            try:
                feed_text = feed.inner_text(timeout=2000).lower()
                if "reached the end of the list" in feed_text:
                    break
            except Exception:
                pass

            try:
                feed.evaluate("(el) => { el.scrollTop = el.scrollHeight; }")
                page.wait_for_timeout(1200)
            except Exception:
                break

            self._sleep()
            unchanged_rounds = (
                unchanged_rounds + 1 if len(urls) == previous_count else 0
            )

        return urls[:limit]

    def _parse_place(self, page, url: str) -> dict:
        self._goto(
            page,
            url + ("&hl=en" if "?" in url else "?hl=en"),
            self.navigation_timeout_ms,
        )
        page.locator('div[role="main"]').wait_for(
            state="visible", timeout=self.place_timeout_ms
        )
        self._sleep()

        name = self._safe_text(page, 'div[role="main"] h1')
        category = self._safe_text(
            page, 'button[jsaction*="category"], button.DkEaL'
        )

        address_label = self._safe_attr(
            page, 'button[data-item-id="address"]'
        )
        address = re.sub(r"^Address:\s*", "", address_label, flags=re.I).strip()

        phone_item = self._safe_attr(
            page, 'button[data-item-id^="phone:tel:"]', "data-item-id"
        )
        phone = phone_item.replace("phone:tel:", "", 1) if phone_item else ""
        if not phone:
            phone_label = self._safe_attr(
                page, 'button[data-item-id^="phone:tel:"]'
            )
            phone = re.sub(r"^Phone:\s*", "", phone_label, flags=re.I).strip()

        website = self._safe_attr(
            page, 'a[data-item-id="authority"]', "href"
        )

        rating_label = self._safe_attr(
            page, 'span[role="img"][aria-label*="star"]'
        )
        if not rating_label:
            rating_label = self._safe_text(page, "div.F7nice")

        rating_match = re.search(r"\b([1-5](?:\.\d)?)\b", rating_label)
        rating = rating_match.group(1) if rating_match else ""

        review_label = self._safe_attr(
            page, 'span[aria-label*="review"]'
        )
        header_text = self._safe_text(page, "div.F7nice")
        review_match = re.search(r"([\d,]+)\s+reviews?", review_label, re.I)
        if not review_match:
            review_match = re.search(r"\(([\d,]+)\)", header_text)
        reviews = (
            review_match.group(1).replace(",", "")
            if review_match else ""
        )

        return self._normalize_record({
            "business_name": name,
            "category": category,
            "address": address,
            "phone": phone,
            "website": website,
            "rating": rating,
            "reviews": reviews,
            "maps_url": url,
        })

    @staticmethod
    def _is_usable_lead(data: dict) -> bool:
        # Phone, website, rating and review count are optional on Maps.
        return bool(
            data.get("business_name")
            and data.get("maps_url")
            and data.get("address")
            and "/maps/place/" in data.get("maps_url", "")
        )

    def scrape(
        self,
        keyword: str,
        location: str,
        max_results: int = 100,
        require_complete: bool = True,
    ) -> list[dict]:
        """Scrape up to max_results unique listings; retain per-place errors."""
        if not keyword.strip() or not location.strip():
            raise ValueError("Both keyword and location are required.")
        if max_results < 1:
            raise ValueError("max_results must be at least 1.")

        self.errors = []
        search_query = f"{keyword.strip()} {location.strip()}"
        # Fetch extra candidates because some listings may lack core fields.
        candidate_limit = max_results * 3 if require_complete else max_results
        results: list[dict] = []
        seen: set[str] = set()

        with sync_playwright() as playwright:
            browser = None
            context = None
            try:
                browser = playwright.chromium.launch(
                    headless=self.headless,
                    timeout=self.navigation_timeout_ms,
                )
                context = browser.new_context(
                    locale="en-US",
                    user_agent=ScrapeConfig().user_agent,
                    viewport={"width": 1366, "height": 900},
                )
                page = context.new_page()
                page.set_default_timeout(self.place_timeout_ms)
                try:
                    place_urls = self._collect_urls(
                        page, search_query, candidate_limit
                    )
                except Exception as exc:
                    self.errors.append({
                        "stage": "search",
                        "url": "",
                        "error": f"{type(exc).__name__}: {exc}",
                    })
                    return []

                for place_url in place_urls:
                    if place_url in seen:
                        continue
                    seen.add(place_url)

                    try:
                        data = self._normalize_record(
                            self._parse_place(page, place_url)
                        )
                        if require_complete and not self._is_usable_lead(data):
                            self.errors.append({
                                "stage": "validation",
                                "url": place_url,
                                "error": "Missing business name, address, or Maps URL.",
                            })
                            continue

                        results.append(data)
                        if len(results) >= max_results:
                            break

                    except Exception as exc:
                        # A single broken listing must not stop the whole run.
                        self.errors.append({
                            "stage": "place_extraction",
                            "url": place_url,
                            "error": f"{type(exc).__name__}: {exc}",
                        })
                        continue

                return results
            except Exception as exc:
                self.errors.append({
                    "stage": "browser",
                    "url": "",
                    "error": f"{type(exc).__name__}: {exc}",
                })
                return results
            finally:
                if context is not None:
                    context.close()
                if browser is not None:
                    browser.close()

    def scrape_json(
        self,
        keyword: str,
        location: str,
        max_results: int = 100,
        require_complete: bool = True,
    ) -> str:
        leads = self.scrape(
            keyword=keyword,
            location=location,
            max_results=max_results,
            require_complete=require_complete,
        )
        return json.dumps(leads, indent=2, ensure_ascii=False)


class PhotographyBaseSource:
    """Shared HTTP and diagnostics utilities for public source collectors."""

    name = "base"

    def __init__(self, settings):
        self.settings = settings
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": settings.reddit_user_agent or "PhotographyLeadsScraper/1.0",
        })
        self.stats = {"status": "not_started", "requests": 0, "retrieved": 0, "errors": []}

    def get_json(self, url, params=None, headers=None):
        self.stats["requests"] += 1
        response = self.session.get(
            url, params=params, headers=headers, timeout=self.settings.timeout,
        )
        if response.status_code in (403, 429):
            raise RuntimeError(f"HTTP {response.status_code} from permitted source; request stopped without bypass.")
        response.raise_for_status()
        time.sleep(max(0.0, self.settings.request_delay))
        return response.json()

    def get_text(self, url, params=None, headers=None):
        self.stats["requests"] += 1
        response = self.session.get(
            url, params=params, headers=headers, timeout=self.settings.timeout,
        )
        if response.status_code in (403, 429):
            raise RuntimeError(f"HTTP {response.status_code} from permitted source; request stopped without bypass.")
        response.raise_for_status()
        time.sleep(max(0.0, self.settings.request_delay))
        return response.text

    def collect(self):
        self.stats["status"] = "running"
        try:
            posts = self.fetch()
            self.stats["retrieved"] = len(posts)
            self.stats["status"] = "ok" if posts else "no_results"
            return posts
        except Exception as exc:
            self.stats["status"] = "error"
            self.stats["errors"].append(f"{type(exc).__name__}: {exc}")
            return []

    def fetch(self):
        raise NotImplementedError

    def diagnostics(self):
        return {"source": self.name, **self.stats}


class BlueskySource(PhotographyBaseSource):
    name = "bluesky"
    endpoint = "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts"

    def fetch(self):
        results, seen = [], set()
        queries = self.settings.keywords or ["photographer"]
        for query in queries:
            cursor = None
            remaining = self.settings.max_posts_per_source - len(results)
            while remaining > 0:
                params = {"q": query, "limit": min(100, remaining), "sort": "latest"}
                if cursor:
                    params["cursor"] = cursor
                payload = self.get_json(self.endpoint, params=params)
                for item in payload.get("posts", []):
                    post = item.get("post", item)
                    record = post.get("record", {})
                    author = post.get("author", {})
                    uri = post.get("uri", "")
                    rkey = uri.rsplit("/", 1)[-1] if uri else ""
                    handle = author.get("handle", "")
                    url = f"https://bsky.app/profile/{handle}/post/{rkey}" if handle and rkey else ""
                    key = uri or url
                    if not key or key in seen:
                        continue
                    seen.add(key)
                    results.append({
                        "source": self.name,
                        "source_post_id": uri or key,
                        "author_name": author.get("displayName") or handle,
                        "author_handle": handle,
                        "post_url": url,
                        "post_text": record.get("text", ""),
                        "created_at": record.get("createdAt") or post.get("indexedAt"),
                        "location_text": "",
                        "public_contact": "",
                        "query": query,
                    })
                cursor = payload.get("cursor")
                remaining = self.settings.max_posts_per_source - len(results)
                if not cursor or not payload.get("posts") or remaining <= 0:
                    break
        return results


class DiscourseSource(PhotographyBaseSource):
    name = "discourse"

    def fetch(self):
        posts, seen = [], set()
        for site in self.settings.discourse_sites:
            base = site.rstrip("/")
            for query in self.settings.keywords:
                payload = self.get_json(f"{base}/search.json", params={"q": query})
                for topic in payload.get("topics", []):
                    topic_id = topic.get("id")
                    topic_url = f"{base}/t/{topic.get('slug', 'topic')}/{topic_id}"
                    if topic_url in seen:
                        continue
                    seen.add(topic_url)
                    excerpt = BeautifulSoup(topic.get("excerpt", ""), "html.parser").get_text(" ", strip=True)
                    posts.append({
                        "source": self.name,
                        "source_post_id": str(topic_id),
                        "author_name": topic.get("last_poster_username", ""),
                        "author_handle": topic.get("last_poster_username", ""),
                        "post_url": topic_url,
                        "post_text": (topic.get("title", "") + " " + excerpt).strip(),
                        "created_at": topic.get("created_at", ""),
                        "location_text": "",
                        "public_contact": "",
                        "query": query,
                    })
                    if len(posts) >= self.settings.max_posts_per_source:
                        return posts
        return posts


class MastodonSource(PhotographyBaseSource):
    name = "mastodon"
    hashtags = [
        "photography", "weddingphotography", "eventphotography",
        "productphotography", "foodphotography", "photoshoot",
    ]

    def fetch(self):
        results, seen = [], set()
        for instance in self.settings.mastodon_instances:
            host = instance.replace("https://", "").replace("http://", "").strip("/")
            searches = [query for query in self.settings.keywords if query.strip()]
            for query in searches:
                if len(results) >= self.settings.max_posts_per_source:
                    return results
                try:
                    payload = self.get_json(
                        f"https://{host}/api/v2/search",
                        params={"q": query, "type": "statuses", "limit": 40, "resolve": "false"},
                    )
                    self._append_statuses(payload.get("statuses", []), query, results, seen)
                except Exception as exc:
                    self.stats["errors"].append(f"{host} search {query}: {type(exc).__name__}: {exc}")
            for tag in self.hashtags:
                if len(results) >= self.settings.max_posts_per_source:
                    return results
                url = f"https://{host}/api/v1/timelines/tag/{tag}"
                try:
                    payload = self.get_json(url, params={"limit": 40})
                    self._append_statuses(payload, f"#{tag}", results, seen)
                except Exception as exc:
                    self.stats["errors"].append(f"{host} #{tag}: {type(exc).__name__}: {exc}")
        return results

    def _append_statuses(self, statuses, query, results, seen):
        for item in statuses:
            status_url = item.get("url", "")
            if not status_url or status_url in seen:
                continue
            seen.add(status_url)
            account = item.get("account", {})
            text = BeautifulSoup(item.get("content", ""), "html.parser").get_text(" ", strip=True)
            results.append({
                "source": self.name,
                "source_post_id": str(item.get("id", status_url)),
                "author_name": account.get("display_name") or account.get("username", ""),
                "author_handle": account.get("acct", ""),
                "post_url": status_url,
                "post_text": text,
                "created_at": item.get("created_at", ""),
                "location_text": "",
                "public_contact": "",
                "query": query,
            })
            if len(results) >= self.settings.max_posts_per_source:
                break


class RedditSource(PhotographyBaseSource):
    """Reddit search through OAuth or the public PullPush archive.

    OAuth is used when REDDIT_ID and REDDIT_SECRET are configured. Without
    credentials the collector falls back to PullPush, a public Reddit archive
    API that requires no login. Both paths use public posts only and respect
    HTTP 429 responses with backoff.
    """

    name = "reddit"
    token_url = "https://www.reddit.com/api/v1/access_token"
    search_url = "https://oauth.reddit.com"
    archive_url = "https://api.pullpush.io/reddit/search/submission/"
    archive_pages = 3

    def fetch(self):
        if not self.settings.reddit_client_id or not self.settings.reddit_client_secret:
            self.stats["mode"] = "public_pullpush_archive"
            return self._fetch_archive()
        self.stats["mode"] = "oauth"
        return self._fetch_oauth()

    def _fetch_archive(self):
        posts, seen = [], set()
        for query in self.settings.keywords:
            before = None
            for _ in range(max(1, self.archive_pages)):
                if len(posts) >= self.settings.max_posts_per_source:
                    return posts
                remaining = self.settings.max_posts_per_source - len(posts)
                params = {"q": query, "size": min(100, remaining), "sort": "desc"}
                if before is not None:
                    params["before"] = before
                try:
                    payload = self._archive_json(params)
                except Exception as exc:
                    self.stats["errors"].append(
                        f"pullpush {query}: {type(exc).__name__}: {exc}"
                    )
                    break
                items = payload.get("data", [])
                if not items:
                    break
                for item in items:
                    if len(posts) >= self.settings.max_posts_per_source:
                        return posts
                    if item.get("over_18"):
                        continue
                    title = (item.get("title") or "").strip()
                    body = (item.get("selftext") or "").strip()
                    if body in {"[removed]", "[deleted]"}:
                        body = ""
                    if not title and not body:
                        continue
                    permalink = item.get("permalink") or ""
                    post_id = str(item.get("id") or "").strip()
                    if permalink.startswith("/"):
                        post_url = "https://www.reddit.com" + permalink
                    elif permalink:
                        post_url = permalink
                    elif post_id:
                        subreddit = item.get("subreddit") or "all"
                        post_url = (
                            f"https://www.reddit.com/r/{subreddit}/comments/{post_id}/"
                        )
                    else:
                        continue
                    key = post_id or post_url
                    if key in seen:
                        continue
                    seen.add(key)
                    posts.append({
                        "source": self.name,
                        "source_post_id": post_id or post_url,
                        "author_name": item.get("author", ""),
                        "author_handle": item.get("author", ""),
                        "post_url": post_url,
                        "post_title": title,
                        "post_text": (title + "\n" + body).strip(),
                        "created_at": item.get("created_utc", ""),
                        "location_text": "",
                        "public_contact": "",
                        "query": query,
                    })
                timestamps = [
                    item.get("created_utc")
                    for item in items
                    if isinstance(item.get("created_utc"), (int, float))
                ]
                # Only continue paging when a full page came back; otherwise
                # this query has no older matches to fetch.
                if len(items) < 100 or not timestamps:
                    break
                before = int(min(timestamps)) - 1
                if before <= 0:
                    break
        return posts

    def _archive_json(self, params: dict, attempts: int = 4) -> dict:
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                return self.get_json(self.archive_url, params=params)
            except Exception as exc:
                last_error = exc
                message = str(exc)
                connection_problem = (
                    requests is not None
                    and isinstance(
                        exc,
                        (requests.exceptions.ConnectionError, requests.exceptions.Timeout),
                    )
                )
                if "429" not in message and not connection_problem:
                    raise
                if attempt + 1 >= attempts:
                    raise
                # PullPush can return HTTP 429 under normal use and the public
                # host occasionally drops connections. Back off instead of
                # rotating identities or bypassing the limit.
                time.sleep(max(2.0, self.settings.request_delay) * (attempt + 1))
        raise last_error or RuntimeError("PullPush request failed.")

    def _fetch_oauth(self):
        auth = requests.auth.HTTPBasicAuth(
            self.settings.reddit_client_id, self.settings.reddit_client_secret
        )
        token_response = self.session.post(
            self.token_url,
            data={"grant_type": "client_credentials"},
            auth=auth,
            headers={"User-Agent": self.settings.reddit_user_agent},
            timeout=self.settings.timeout,
        )
        token_response.raise_for_status()
        token = token_response.json()["access_token"]
        headers = {"Authorization": f"Bearer {token}", "User-Agent": self.settings.reddit_user_agent}
        subreddits = self.settings.reddit_subreddits or ["all"]
        posts, seen = [], set()
        for subreddit in subreddits:
            for query in self.settings.keywords:
                if len(posts) >= self.settings.max_posts_per_source:
                    return posts
                payload = self.get_json(
                    f"{self.search_url}/r/{subreddit}/search",
                    params={"q": query, "sort": "new", "limit": 100, "restrict_sr": "on"},
                    headers=headers,
                )
                for child in payload.get("data", {}).get("children", []):
                    item = child.get("data", {})
                    permalink = "https://www.reddit.com" + item.get("permalink", "")
                    if not permalink or permalink in seen:
                        continue
                    seen.add(permalink)
                    posts.append({
                        "source": self.name,
                        "source_post_id": str(item.get("id", permalink)),
                        "author_name": item.get("author", ""),
                        "author_handle": item.get("author", ""),
                        "post_url": permalink,
                        "post_title": item.get("title", ""),
                        "post_text": (item.get("title", "") + "\n" + item.get("selftext", "")).strip(),
                        "created_at": item.get("created_utc", ""),
                        "location_text": "",
                        "public_contact": "",
                        "query": query,
                    })
        return posts


class RSSSource(PhotographyBaseSource):
    name = "rss"

    def fetch(self):
        posts, seen = [], set()
        for feed_url in self.settings.rss_feeds:
            parsed = feedparser.parse(feed_url)
            if getattr(parsed, "bozo", False) and not parsed.entries:
                self.stats["errors"].append(f"Feed parse error: {feed_url}: {parsed.bozo_exception}")
                continue
            for entry in parsed.entries:
                url = entry.get("link", "")
                if not url or url in seen:
                    continue
                seen.add(url)
                text = " ".join([
                    entry.get("title", ""),
                    entry.get("summary", ""),
                ]).strip()
                posts.append({
                    "source": self.name,
                    "source_post_id": entry.get("id", url),
                    "author_name": entry.get("author", ""),
                    "author_handle": "",
                    "post_url": url,
                    "post_title": entry.get("title", ""),
                    "post_text": text,
                    "created_at": entry.get("published", entry.get("updated", "")),
                    "location_text": "",
                    "public_contact": "",
                    "query": "",
                })
                if len(posts) >= self.settings.max_posts_per_source:
                    return posts
        return posts


PHOTOGRAPHY_COLLECTORS = {
    'bluesky': BlueskySource,
    'mastodon': MastodonSource,
    'reddit': RedditSource,
    'discourse': DiscourseSource,
    'rss': RSSSource,
}


if __name__ == "__main__":
    sys.exit(main())

