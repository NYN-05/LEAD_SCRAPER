"""Root application entry point for all lead-scraping tools."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from lead_scraper.config import Settings, project_path
from lead_scraper.processing import classify_post, deduplicate_posts, normalize_post, score_lead
from lead_scraper.scrapers import (
    PHOTOGRAPHY_COLLECTORS,
    MapsScraper,
    main as social_main,
)
from lead_scraper.storage import LeadDatabase, export_leads, export_maps_from_database


def build_photography_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover public photography-service buying-intent posts."
    )
    parser.add_argument("--location", default=None, help="Target location, e.g. Bengaluru")
    parser.add_argument("--keywords", nargs="+", default=None, help="Search phrases")
    parser.add_argument("--sources", default=None, help="Comma-separated: bluesky,mastodon,reddit,discourse,rss")
    parser.add_argument("--mastodon-instances", default=None, help="Comma-separated instance hostnames")
    parser.add_argument("--subreddits", default=None, help="Comma-separated subreddit names; default: all")
    parser.add_argument("--discourse-sites", default=None, help="Comma-separated public Discourse base URLs")
    parser.add_argument("--rss-feeds", default=None, help="Comma-separated RSS/Atom feed URLs")
    parser.add_argument("--max-posts", type=int, default=100)
    parser.add_argument("--min-score", type=int, default=0)
    parser.add_argument("--delay", type=float, default=1.0)
    parser.add_argument("--db", default="data/leads.db")
    parser.add_argument("--output", default="output/photography")
    parser.add_argument("--excel", dest="excel", action="store_true", default=True,
                        help="Write the Excel export (default).")
    parser.add_argument("--no-excel", dest="excel", action="store_false",
                        help="Skip the Excel export.")
    return parser


def run_photography(argv: list[str] | None = None) -> int:
    args = build_photography_parser().parse_args(argv)
    settings = Settings.from_env()
    if args.location is not None:
        settings.location = args.location
    if args.keywords is not None:
        settings.keywords = args.keywords
    if args.sources is not None:
        settings.sources = [x.strip().lower() for x in args.sources.split(",") if x.strip()]
    if args.mastodon_instances is not None:
        settings.mastodon_instances = [x.strip() for x in args.mastodon_instances.split(",") if x.strip()]
    if args.subreddits is not None:
        settings.reddit_subreddits = [x.strip() for x in args.subreddits.split(",") if x.strip()]
    if args.discourse_sites is not None:
        settings.discourse_sites = [x.strip() for x in args.discourse_sites.split(",") if x.strip()]
    if args.rss_feeds is not None:
        settings.rss_feeds = [x.strip() for x in args.rss_feeds.split(",") if x.strip()]
    settings.max_posts_per_source = args.max_posts
    settings.min_score = args.min_score
    settings.request_delay = args.delay
    settings.db_path = str(project_path(args.db))
    settings.output_dir = str(project_path(args.output))
    settings.export_excel = args.excel

    unknown = set(settings.sources) - set(PHOTOGRAPHY_COLLECTORS)
    if unknown:
        raise SystemExit(f"Unknown sources: {', '.join(sorted(unknown))}")

    database = LeadDatabase(settings.db_path)
    run_id = database.start_run({
        "location": settings.location,
        "keywords": settings.keywords,
        "sources": settings.sources,
        "max_posts_per_source": settings.max_posts_per_source,
    })
    raw_posts, diagnostics, leads = [], [], []
    try:
        for source_name in settings.sources:
            collector = PHOTOGRAPHY_COLLECTORS[source_name](settings)
            raw_posts.extend(collector.collect())
            diagnostics.append(collector.diagnostics())

        normalized_posts = []
        for raw in raw_posts:
            normalized = normalize_post(raw, settings.location)
            if not normalized["post_text"]:
                continue
            normalized_posts.append(normalized)
        unique_posts = deduplicate_posts(normalized_posts)
        for normalized in unique_posts:
            database.save_post(normalized)

        for post in unique_posts:
            classification = classify_post(post)
            if not classification["qualified"]:
                continue
            scoring = score_lead(post, classification, settings.location)
            if scoring["score"] < settings.min_score:
                continue
            lead = {**post, **classification, **scoring}
            leads.append(lead)
        all_posts = deduplicate_posts([
            normalize_post(post, settings.location)
            for post in database.get_posts()
        ])
        all_leads = []
        for post in all_posts:
            classification = classify_post(post)
            if not classification["qualified"]:
                continue
            scoring = score_lead(post, classification, settings.location)
            if scoring["score"] < settings.min_score:
                continue
            all_leads.append({**post, **classification, **scoring})
        database.replace_leads(all_leads)
        export_leads(all_leads, all_posts, settings.output_dir, settings.export_excel)
        # Keep the historical output/ root files in sync so existing scripts
        # and the documented paths keep working.
        root_output = project_path("output")
        if Path(settings.output_dir).resolve() != root_output.resolve():
            export_leads(all_leads, all_posts, root_output, settings.export_excel)
        database.finish_run(run_id, diagnostics)
    finally:
        database.close()

    print("\nPHOTOGRAPHY LEADS SCRAPER")
    print("=" * 48)
    print(f"Location: {settings.location or 'Global / not restricted'}")
    print(f"Sources enabled: {', '.join(settings.sources)}")
    print(f"Raw posts retrieved: {len(raw_posts)}")
    print(f"Unique posts saved: {len(all_posts)}")
    print(f"Qualified leads: {len(all_leads)}")
    print(f"Hot: {sum(x['lead_temperature'] == 'Hot' for x in all_leads)}")
    print(f"Warm: {sum(x['lead_temperature'] == 'Warm' for x in all_leads)}")
    print(f"Cold: {sum(x['lead_temperature'] == 'Cold' for x in all_leads)}")
    print("\nSOURCE DIAGNOSTICS")
    for item in diagnostics:
        print(f"- {item['source']}: {item['status']} | retrieved={item['retrieved']} | requests={item['requests']}")
        for error in item["errors"]:
            print(f"    ERROR: {error}")
    print(f"\nJSON: {Path(settings.output_dir) / 'leads.json'}")
    print(f"CSV:  {Path(settings.output_dir) / 'leads.csv'}")
    print(f"Raw:  {Path(settings.output_dir) / 'raw_posts.json'}")
    return 0


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be a whole number") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def run_maps(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Collect public Google Maps business listings.")
    parser.add_argument("--keyword", default="photography studio")
    parser.add_argument("--location", default="Bengaluru")
    parser.add_argument("--max-results", type=_positive_int, default=100)
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args(argv)

    keyword = args.keyword.strip()
    location = args.location.strip()
    if not keyword or not location:
        print("error: --keyword and --location must not be empty.", file=sys.stderr)
        return 2

    def progress(message: str) -> None:
        print(f"[maps] {message}", file=sys.stderr)

    try:
        scraper = MapsScraper(headless=not args.headed, progress=progress)
        leads = scraper.scrape(keyword, location, args.max_results)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    database = LeadDatabase(project_path("data/leads.db"))
    try:
        for lead in leads:
            database.save_map_lead(lead)
        for error in scraper.errors:
            database.save_collection_error(
                "maps", json.dumps(error, ensure_ascii=False)
            )
        stored = export_maps_from_database(database, project_path("output/maps"))
        # Keep the historical output/ root files in sync for compatibility.
        export_maps_from_database(database, project_path("output"))
        total = len(stored)
    finally:
        database.close()

    print("\nGOOGLE MAPS LEAD SCRAPER")
    print("=" * 48)
    print(f"Keyword: {keyword}")
    print(f"Location: {location}")
    print(f"Requested: {args.max_results}")
    print(f"Collected this run: {len(leads)}")
    print(f"Total stored (all runs): {total}")
    print(f"Diagnostics: {len(scraper.errors)}")
    print(f"CSV: {project_path('output/maps/google_maps_leads.csv')}")
    print(f"Excel: {project_path('output/maps/google_maps_leads.xlsx')}")
    print(json.dumps(leads, indent=2, ensure_ascii=False))
    if scraper.errors:
        print("\nExtraction diagnostics:")
        print(json.dumps(scraper.errors, indent=2, ensure_ascii=False))
    # A failed run that produced no rows is an error; a genuinely empty search
    # (no diagnostics) is a valid successful run.
    return 0 if leads or not scraper.errors else 1


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "dashboard":
        from lead_scraper.dashboard import run_dashboard
        run_dashboard()
        return 0
    if args and args[0] == "social":
        return social_main(args[1:])
    if args and args[0] == "maps":
        return run_maps(args[1:])
    return run_photography(args)


def _is_streamlit_runtime() -> bool:
    if "streamlit" in sys.modules:
        return True
    try:
        from streamlit.runtime import exists
    except ImportError:
        return False
    return exists()


if __name__ == "__main__":
    if _is_streamlit_runtime():
        from lead_scraper.dashboard import run_streamlit_dashboard
        run_streamlit_dashboard()
    else:
        raise SystemExit(main())
