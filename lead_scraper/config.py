"""Central configuration and root-project environment loading."""
from dataclasses import dataclass, field
import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"

load_dotenv(PROJECT_ROOT / ".env")


def project_path(value: str | Path) -> Path:
    """Resolve relative application paths against the project root."""
    path = Path(value)
    return path if path.is_absolute() else PROJECT_ROOT / path


def _csv_env(name: str, default: str = "") -> list[str]:
    value = os.getenv(name, default)
    return [item.strip() for item in value.split(",") if item.strip()]


def _secret_env(name: str, default: str = "") -> str:
    value = os.getenv(name, default).strip()
    return "" if not value or value.startswith("YOUR_") else value


DEFAULT_KEYWORDS = [
    "looking for photographer",
    "looking for a photographer for",
    "need a photographer",
    "need a photographer for",
    "photographer needed",
    "photographer recommendations",
    "photographer recommendation",
    "recommend a photographer",
    "seeking photographer",
    "hire a photographer",
    "wedding photographer",
    "need wedding photography",
    "event photographer",
    "photographer for event",
    "corporate event photographer",
    "product photography",
    "product photographer",
    "food photographer",
    "professional photoshoot",
    "engagement photographer",
    "birthday photographer",
    "family photoshoot",
    "maternity photoshoot",
    "newborn photographer",
    "portrait photographer",
    "headshot photographer",
]


@dataclass
class Settings:
    location: str = ""
    keywords: list[str] = field(default_factory=lambda: DEFAULT_KEYWORDS.copy())
    sources: list[str] = field(default_factory=lambda: ["mastodon", "reddit"])
    bluesky_limit: int = 100
    mastodon_instances: list[str] = field(default_factory=lambda: ["mastodon.social"])
    reddit_subreddits: list[str] = field(default_factory=list)
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = "PhotographyLeadsScraper/1.0 (local research tool)"
    discourse_sites: list[str] = field(default_factory=list)
    rss_feeds: list[str] = field(default_factory=list)
    request_delay: float = 1.0
    timeout: int = 20
    max_posts_per_source: int = 100
    min_score: int = 0
    db_path: str = "data/leads.db"
    output_dir: str = "output"
    export_excel: bool = False

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            location=os.getenv("LEAD_LOCATION", ""),
            sources=_csv_env("LEAD_SOURCES", "mastodon,reddit"),
            mastodon_instances=_csv_env("MASTODON_INSTANCES", "mastodon.social"),
            reddit_subreddits=_csv_env("REDDIT_SUBREDDITS", ""),
            reddit_client_id=_secret_env("REDDIT_ID"),
            reddit_client_secret=_secret_env("REDDIT_SECRET"),
            reddit_user_agent=os.getenv("REDDIT_USER_AGENT", "PhotographyLeadsScraper/1.0"),
            discourse_sites=_csv_env("DISCOURSE_SITES", ""),
            rss_feeds=_csv_env("RSS_FEEDS", ""),
        )


@dataclass
class ScrapeConfig:
    min_delay: float = 2.0
    max_delay: float = 4.0
    retries: int = 2
    max_results: int = 100
    # ddgs 9.x exposes Bing-indexed web results through the "yahoo" engine
    # (provider "bing"); there is no "bing" backend key in this release.
    # "auto" is the fallback when Yahoo rate-limits, because it lets DDGS try
    # its remaining engines instead of abandoning discovery.
    search_backend: str = "yahoo,auto"
    search_region: str = "in-en"
    search_pages: int = 15
    max_search_requests: int = 80
    brave_api_key: str = field(default_factory=lambda: _secret_env("BRAVE_SEARCH_API_KEY"))
    google_api_key: str = field(default_factory=lambda: _secret_env("GOOGLE_CSE_API_KEY"))
    google_cse_id: str = field(default_factory=lambda: _secret_env("GOOGLE_CSE_ID"))
    targeted_lookup: bool = True
    output_dir: str = "output/social"
    fetch_public_profiles: bool = True
    request_timeout: float = 15.0
    user_agent: str = "LEAD_SCRAPER/1.0 (public profile research; respects robots.txt)"

    def __post_init__(self) -> None:
        # Compatibility alias: older commands used --backend bing, but ddgs
        # 9.x disables its Bing engine and exposes Bing-indexed results
        # through the Yahoo engine instead.
        backends = [item.strip() for item in (self.search_backend or "").split(",") if item.strip()]
        backends = ["yahoo" if item.lower() == "bing" else item for item in backends]
        self.search_backend = ",".join(dict.fromkeys(backends)) or "yahoo,auto"

    def validate(self) -> None:
        if self.min_delay < 0 or self.max_delay < self.min_delay:
            raise ValueError("Delay settings are invalid: max-delay must be >= min-delay >= 0.")
        if self.retries < 0:
            raise ValueError("Retries cannot be negative.")
        if self.max_results < 1:
            raise ValueError("Maximum results must be at least 1.")
        if self.search_pages < 1:
            raise ValueError("Search pages must be at least 1.")
        if self.max_search_requests < 1:
            raise ValueError("Maximum search requests must be at least 1.")
