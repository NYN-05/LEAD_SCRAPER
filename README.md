# LEAD_SCRAPER

Unified public lead-discovery tools for Google Maps businesses, LinkedIn and
Instagram profiles, and photography-service requests from public sources.

The project does not bypass authentication, CAPTCHAs, robots restrictions,
rate limits, or other access controls. Search-result and public-page data is
unverified and should be validated before use.

## Sample outputs

The sample exports in `output/` were generated on 17 November 2026. Counts vary
between runs because public search results and archives change.

| Tool | Sample | Notes |
| --- | --- | --- |
| Google Maps | 105 unique Bengaluru photography listings | 120 candidates requested; 105 valid unique rows after extraction failures and URL deduplication |
| LinkedIn | 100 unique profiles | 99 names/headlines, 50 current companies, 27 locations, 67 public About excerpts; one URL-only record is retained and labeled `url_only_unverified` |
| Instagram | 100 unique profiles | 95 full names/bios, 96 follower/following/post-count sets, 28 public email/phone values |
| Photography | 111 qualified leads | All Hot, 111 unique post URLs, no duplicate author/title crossposts; 27 titles name a location; drawn from 2,009 stored public posts |

Social profiles are built from public search-result snippets, not logged-in
profile pages. Blank fields stay blank rather than being invented, and each
record carries `verification`, `data_quality`, and `field_status` labels.

## Architecture

```text
LEAD_SCRAPER/
├── .env
├── .env.example
├── .gitignore
├── requirements.txt
├── README.md
├── main.py
├── lead_scraper/
│   ├── __init__.py
│   ├── config.py
│   ├── dashboard.py        # Streamlit UI
│   ├── scrapers.py
│   ├── processing.py
│   └── storage.py
├── data/
│   └── leads.db
└── output/
    ├── maps/
    ├── social/
    └── photography/
```

`lead_scraper/config.py` loads the single root `.env` using an absolute path,
so commands work from any current directory. Existing shell environment
variables take precedence. Secrets are never stored in Python source files.

## Windows setup

```powershell
cd C:\Users\JHASHANK\Downloads\AKSHARA_ASSIGNMENT
py -m venv .venv
.\.venv\Scripts\Activate.ps1
py -m pip install -U pip
py -m pip install -r requirements.txt
py -m playwright install chromium
```

Copy `.env.example` to `.env` and add approved API credentials as needed.
`.env` is excluded by `.gitignore`.

### Launch the Streamlit dashboard

```powershell
py -m pip install -r requirements.txt
py -m streamlit run main.py
```

The dashboard provides Overview, Google Maps, LinkedIn/Instagram, and
Photography Leads sections. Collection jobs run in background workers, while
tables and metrics read from the shared SQLite database. Use the sidebar
refresh control to reload stored results.

## Run the tools

### Photography leads

The photography CLI runs from the project root. The default sources are
`mastodon,reddit`; Reddit uses OAuth when `REDDIT_ID`/`REDDIT_SECRET` are set
and otherwise falls back to the public PullPush Reddit archive, which needs no
login:

```powershell
py main.py
py main.py --sources reddit --max-posts 9000 --delay 1.5
py main.py --sources reddit --subreddits "weddingphotography,photography"
py main.py --sources rss --rss-feeds "https://example.com/feed.xml"
py main.py --sources bluesky --keywords "need a photographer"
py main.py --output output/photography --no-excel
```

The Reddit archive returns at most 100 recent submissions per query, so
`--max-posts` is a ceiling rather than a promise. A 100-lead sample usually
needs several thousand raw archive posts; the command above uses
`--max-posts 9000` because the archive is large. Use `--delay 1.5` or higher
to stay within the public host's rate limits.

Collectors for Bluesky, Mastodon, Reddit, Discourse, and RSS are consolidated
in `lead_scraper/scrapers.py`. Processing and scoring rules are in
`lead_scraper/processing.py`; SQLite and JSON/CSV/Excel exports are in
`lead_scraper/storage.py`.

Reddit requests back off on HTTP 429 and transient connection failures instead
of rotating identities or bypassing limits. PullPush returns public submissions
only; the collector skips over-18 posts and removed/deleted bodies.

Classification is intent-based and explainable. When a post has a title, the
classifier evaluates the title rather than the body, because long Reddit bodies
often mention photography in passing (stories, novels, gear questions):

- **Hot**: the author explicitly asks to hire a photographer, requests a
  recommendation, or asks about pricing/quotes.
- **Warm**: the author describes an upcoming or planned photography need.
- **Cold**: general photography interest, hobby discussion, gear questions,
  provider promotion, or third-party testimonials. Cold posts are not saved as
  qualified leads.

Each qualified export retains the original post URL, title, text, author
handle, collection timestamp, and `intent_evidence` matched by the classifier.
`location` is filled only when the title explicitly names a place; it is left
blank rather than inferred from a subreddit or unrelated body text.
Posts are deduplicated by source identity, canonical URL, normalized
author/content fingerprint, and author/title crossposts. A source returning
HTTP 403 or 429 is recorded as an access limitation and is not retried through
proxies or alternate evasion routes. Other configured public sources continue
independently.

### LinkedIn and Instagram

Use the `social` subcommand. It preserves the former social scraper options and
writes each platform independently to CSV, JSON, Excel, and the
`social_profiles` SQLite table:

```powershell
py main.py social --self-test
py main.py social --location Bengaluru --max-results 20 --no-profile-fetch
py main.py social --linkedin-urls "https://www.linkedin.com/in/example/"
py main.py social --instagram-keywords photographer --location Bengaluru
```

Optional Brave and Google Programmable Search credentials are read from the
root `.env`; DDGS remains the fallback provider. Discovery uses Yahoo's
Bing-indexed results by default and falls back to DDGS `auto` if Yahoo
rate-limits (`--backend yahoo,auto`). Yahoo returns LinkedIn and Instagram
profile results more reliably than DuckDuckGo's meta-search `auto` selection in
this environment. The installed `ddgs` 9.x package does not expose `bing` as a
backend key; `yahoo` is the Bing-indexed engine, and `--backend bing` is
accepted as a compatibility alias for `yahoo`.

`--max-results` is a **target**, not a hard cap on search effort. The scraper
walks query variants and result pages until the target is reached, the search
budget (`--max-search-requests`) is spent, or three consecutive pages return
nothing new:

```powershell
py main.py social `
  --location Bangalore `
  --max-results 100 `
  --pages 15 `
  --max-search-requests 80 `
  --backend yahoo,auto `
  --region in-en
```

Query generation accepts comma-separated keywords
(`--linkedin-keywords "photographer, wedding photographer"`), expands common
location aliases such as Bangalore/Bengaluru, and tries unquoted queries before
quoted precision variants because quoted Instagram queries frequently return
zero results. LinkedIn country subdomains (`in.linkedin.com`,
`uk.linkedin.com`, and so on) are normalized to
`https://www.linkedin.com/in/<slug>/` so the same profile is not counted twice.

Public profile fetching is only attempted when robots.txt permits it. Each
record contains `field_status`, a JSON object whose values are `extracted` or
`unavailable`. `data_quality` distinguishes URL-only records from records with
identity/context; URL-only records are not counted as valid profiles in the
printed summary. Profile URLs are normalized and deduplicated
case-insensitively. LinkedIn About and Instagram contact fields remain blank
unless the public page or an exact-account public snippet exposes them.

The platforms may still return fewer than 100 enriched records. LinkedIn and
Instagram can require login, deny automated requests, or restrict search-engine
indexing. The scraper stops on robots failures, 403, 429, CAPTCHAs, and
redirects rather than bypassing those controls. It does not rotate proxies. The
summary reports provider errors, valid-record counts, and per-field
completeness so access limitations are explicit.

### Google Maps

```powershell
py main.py maps --keyword "photography studio" --location Bengaluru --max-results 120
```

The sample export used the command above: 120 candidates produced 105 valid
unique listings. The extra 20 candidates leave room for listings that fail
extraction or duplicate an existing Maps URL, so a default 100-candidate run
can still finish below 100 valid rows. The tool reports the shortfall instead
of padding the export.

When no limit argument is supplied, each tool requests up to 100 records:
Maps uses `--max-results 100`, social discovery uses `--max-results 100`
per platform, and photography collection uses `--max-posts 100` per source.
These are collection targets; platform restrictions, unavailable sources, and
validation may result in fewer valid records. Explicit limit arguments override
the defaults.

The Maps collector uses Playwright, scrolls result feeds, extracts public
listing fields, deduplicates canonical Maps URLs, retries transient navigation
failures, and retains per-listing diagnostics. Google `/url?q=...` redirects
are resolved to the real business website and tracking parameters are removed,
so exports do not contain relative Google redirect links. Valid listings are
persisted in the `map_leads` SQLite table and exported to CSV, JSON, and Excel.
The Streamlit dashboard keeps Maps, social profiles, and photography exports
in separate `output\maps`, `output\social`, and `output\photography`
directories so one tool cannot overwrite another tool's files. Root-level
`output\google_maps_leads.*` and `output\leads.*` copies are kept in sync for
compatibility with the assignment paths.

## CLI arguments

The following tables document every command-line argument. Explicit values
override the defaults.

### Task 1: Google Maps

| Argument          | Purpose                                          | Default                  |
| ----------------- | ------------------------------------------------ | ------------------------ |
| `--keyword`     | Business/search keyword                          | `"photography studio"` |
| `--location`    | Search location                                  | `"Bengaluru"`          |
| `--max-results` | Maximum Maps listings to collect                 | `100`                  |
| `--headed`      | Show the browser window instead of headless mode | Disabled                 |

```powershell
py main.py maps
py main.py maps --keyword "wedding photographer" --location "Mumbai" --max-results 50
```

### Task 2: LinkedIn and Instagram

| Argument                  | Purpose                                           | Default              |
| ------------------------- | ------------------------------------------------- | -------------------- |
| `--input-json`          | Load search/profile inputs from a JSON file       | None                 |
| `--linkedin-urls`       | Comma-separated LinkedIn profile URLs             | `""`               |
| `--instagram-urls`      | Comma-separated Instagram profile URLs            | `""`               |
| `--linkedin-keywords`   | LinkedIn discovery keywords                       | `"Data Scientist"` |
| `--instagram-keywords`  | Instagram discovery keywords                      | `"photographer"`   |
| `--hashtag`             | Hashtag filter                                    | `""`               |
| `--location`            | Search location for both platforms                | `"Bengaluru"`      |
| `--max-results`         | Maximum profiles per platform                     | `100`              |
| `--min-delay`           | Minimum delay between search requests, seconds    | `2.0`              |
| `--max-delay`           | Maximum delay between search requests, seconds    | `4.0`              |
| `--retries`             | Number of transient search retries                | `2`                |
| `--backend`             | Comma-separated DDGS backends in priority order   | `"yahoo,auto"`     |
| `--pages`               | Result pages requested per query                  | `15`               |
| `--max-search-requests` | Search-request budget per platform                | `80`               |
| `--region`              | DDGS region code                                  | `"in-en"`          |
| `--output-dir`          | Profile output directory                          | `"output/social"`  |
| `--no-targeted-lookup`  | Disable individual lookup for supplied URLs       | Disabled             |
| `--no-profile-fetch`    | Use search-result snippets only                   | Disabled             |
| `--request-timeout`     | Public profile request timeout, seconds           | `15.0`             |
| `--self-test`           | Run offline parser tests and exit                 | Disabled             |
| `--check-search`        | Run a search-provider connectivity check and exit | Disabled             |
| `--log-level`           | Logging level                                     | `"INFO"`           |

```powershell
py main.py social
py main.py social --max-results 50 --location Mumbai
```

### Task 3: Photography leads

| Argument                 | Purpose                            | Default                    |
| ------------------------ | ---------------------------------- | -------------------------- |
| `--location`           | Target location filter             | None / root `.env` value |
| `--keywords`           | One or more search phrases         | Root configured keywords   |
| `--sources`            | Comma-separated public sources     | `mastodon,reddit`        |
| `--mastodon-instances` | Comma-separated Mastodon servers   | `mastodon.social`        |
| `--subreddits`         | Comma-separated Reddit subreddits  | All/default                |
| `--discourse-sites`    | Comma-separated Discourse URLs     | None                       |
| `--rss-feeds`          | Comma-separated RSS/Atom feed URLs | None                       |
| `--max-posts`          | Maximum posts per source           | `100`                    |
| `--min-score`          | Minimum lead score                 | `0`                      |
| `--delay`              | Request delay, seconds             | `1.0`                    |
| `--db`                 | SQLite database path               | `data/leads.db`          |
| `--output`             | Output directory                   | `output/photography`     |
| `--excel`              | Enable Excel export                | Enabled                    |
| `--no-excel`           | Disable Excel export               | Disabled                   |

```powershell
py main.py
py main.py --max-posts 50 --sources mastodon,rss
```

The `100` values are collection targets. Restrictions, duplicate removal,
unavailable sources, and validation may result in fewer valid records.

## Outputs and data safety

- `data/leads.db` retains the SQLite schema and normalizes stored posts on each
  run, so classifier changes apply to the complete stored sample.
- `output/photography/leads.json`, `leads.csv`, `leads.xlsx`, and
  `raw_posts.json` are the canonical photography exports.
- `output/leads.json`, `leads.csv`, and `leads.xlsx` mirror the photography
  exports at the root for compatibility. Use `--no-excel` only when the
  spreadsheet is not needed.
- `output/social/linkedin_profiles.*` and `instagram_profiles.*` contain the
  canonical platform-separated profile exports. The same files are also kept
  in sync at `output/linkedin_profiles.*` and `output/instagram_profiles.*`
  for compatibility with existing scripts and the assignment file paths.
- `output/maps/google_maps_leads.*` contain validated Maps listings, mirrored
  at `output/google_maps_leads.*`.

No external API scraping is claimed to succeed unless it is run and returns
results in the current environment.

## Assignment deliverables

| Task                               | Implementation                                                              | Main outputs                                                                  |
| ---------------------------------- | --------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| 1. Google Maps leads               | `py main.py maps` (`MapsScraper` in `lead_scraper/scrapers.py`)       | `output/maps/google_maps_leads.csv/.json/.xlsx`                             |
| 2. LinkedIn + Instagram profiles   | `py main.py social` (`scrape_platform` in `lead_scraper/scrapers.py`) | `output/social/linkedin_profiles.*`, `output/social/instagram_profiles.*` |
| 3. Photography buying-intent leads | `py main.py` (`run_photography` in `main.py`)                         | `output/photography/leads.csv/.json/.xlsx`                                  |
| Dashboard                          | `py -m streamlit run main.py`                                             | Reads the same SQLite database and output folders                             |

The `100` values are collection targets. Restrictions, duplicate removal,
unavailable sources, and validation may result in fewer valid records; the
printed summaries and `data_quality`/`field_status` columns report that
shortfall instead of hiding it.

## Verification commands

```powershell
py -m unittest discover -s tests -v

@'
import csv
checks = (
    ("output/maps/google_maps_leads.csv", "maps_url"),
    ("output/social/linkedin_profiles.csv", "profile_url"),
    ("output/social/instagram_profiles.csv", "profile_url"),
    ("output/photography/leads.csv", "post_url"),
)
for path, key in checks:
    rows = list(csv.DictReader(open(path, encoding="utf-8-sig")))
    print(path, "rows:", len(rows), "unique:", len({row[key] for row in rows}))
'@ | py -
```
