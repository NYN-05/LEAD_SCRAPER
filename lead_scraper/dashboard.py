"""Streamlit dashboard for the Maps, social-profile, and photography tools."""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import json
from pathlib import Path
import time
from typing import Any, Callable

from .config import project_path
from .storage import LeadDatabase

try:
    import pandas as pd
except ImportError:  # pragma: no cover - dependency is declared in requirements.txt
    pd = None


_EXECUTOR = ThreadPoolExecutor(max_workers=3, thread_name_prefix="lead-scraper")
OUTPUT_ROOT = project_path("output")
DB_PATH = project_path("data/leads.db")


def _database() -> LeadDatabase:
    return LeadDatabase(DB_PATH)


def _submit_job(st, key: str, callback: Callable[[], dict[str, Any]]) -> None:
    jobs = st.session_state.setdefault("jobs", {})
    job = jobs.get(key)
    if job and not job["future"].done():
        st.warning("This collection is already running.")
        return
    jobs[key] = {"future": _EXECUTOR.submit(callback), "started_at": st.session_state.get("refresh_nonce", 0)}


def _job_result(st, key: str) -> dict[str, Any] | None:
    job = st.session_state.setdefault("jobs", {}).get(key)
    if not job:
        return st.session_state.get(f"{key}_result")
    future: Future = job["future"]
    if not future.done():
        st.info("Collection is running in the background. Refreshing status…")
        time.sleep(0.5)
        st.rerun()
        return None
    try:
        result = future.result()
    except Exception as exc:
        result = {"error": f"{type(exc).__name__}: {exc}"}
    st.session_state[f"{key}_result"] = result
    del st.session_state["jobs"][key]
    return result


def _read_download(path: Path) -> bytes | None:
    try:
        return path.read_bytes() if path.is_file() else None
    except OSError:
        return None


def _downloads(st, directory: Path, stem: str) -> None:
    columns = st.columns(3)
    for column, extension, mime in (
        (columns[0], "csv", "text/csv"),
        (columns[1], "json", "application/json"),
        (columns[2], "xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ):
        data = _read_download(directory / f"{stem}.{extension}")
        if data is not None:
            column.download_button(
                f"Download {extension.upper()}",
                data=data,
                file_name=f"{stem}.{extension}",
                mime=mime,
                key=f"download-{directory.name}-{stem}-{extension}",
                use_container_width=True,
            )
        else:
            column.caption(f"{extension.upper()} not available")


def _filtered_table(st, rows: list[dict], key: str, default_sort: str | None = None) -> None:
    if pd is None:
        st.error("Pandas is required to display dashboard tables.")
        return
    if not rows:
        st.info("No records available yet.")
        return
    frame = pd.DataFrame(rows)
    search = st.text_input("Search results", key=f"{key}-search", placeholder="Filter any visible field")
    if search:
        mask = frame.astype(str).apply(
            lambda column: column.str.contains(search, case=False, na=False)
        ).any(axis=1)
        frame = frame.loc[mask]
    sortable = list(frame.columns)
    selected_sort = st.selectbox(
        "Sort by", sortable, index=sortable.index(default_sort) if default_sort in sortable else 0,
        key=f"{key}-sort",
    )
    frame = frame.sort_values(selected_sort, kind="stable", na_position="last")
    st.caption(f"{len(frame)} record(s)")
    st.dataframe(frame, use_container_width=True, hide_index=True)


def _maps_job(keyword: str, location: str, maximum: int) -> dict[str, Any]:
    from .scrapers import MapsScraper
    from .storage import export_maps

    scraper = MapsScraper()
    rows = scraper.scrape(keyword, location, maximum)
    database = _database()
    try:
        for row in rows:
            database.save_map_lead(row)
    finally:
        database.close()
    directory = OUTPUT_ROOT / "maps"
    export_maps(rows, directory)
    export_maps(rows, OUTPUT_ROOT)
    return {"rows": rows, "errors": scraper.errors}


def _social_job(platform: str, urls: str, keywords: str, hashtag: str, location: str, maximum: int) -> dict[str, Any]:
    from .config import ScrapeConfig
    from .scrapers import ProfileQuery, SearchManager, scrape_platform
    from .storage import export_social_profile_platform

    config = ScrapeConfig(
        max_results=maximum,
        output_dir=str(OUTPUT_ROOT / "social"),
        min_delay=1.0,
        max_delay=2.0,
    )
    config.validate()
    query = ProfileQuery(
        profile_urls=[item.strip() for item in urls.replace(",", "\n").splitlines() if item.strip()],
        keywords=keywords,
        hashtag=hashtag,
        location=location,
        max_results=maximum,
    )
    manager = SearchManager(config)
    rows = scrape_platform(platform, manager, query, config)
    directory = OUTPUT_ROOT / "social"
    export_social_profile_platform(directory, platform, rows)
    export_social_profile_platform(OUTPUT_ROOT, platform, rows)
    database = _database()
    try:
        for row in rows:
            database.save_social_profile(platform, row)
    finally:
        database.close()
    return {"rows": rows, "issues": manager.issues}


def _photography_job(location: str, keywords: list[str], sources: list[str], maximum: int) -> dict[str, Any]:
    from main import run_photography

    args = [
        "--location", location,
        "--keywords", *keywords,
        "--sources", ",".join(sources),
        "--max-posts", str(maximum),
        "--delay", "1",
        "--db", str(DB_PATH),
        "--output", str(OUTPUT_ROOT / "photography"),
        "--excel",
    ]
    run_photography(args)
    database = _database()
    try:
        return {
            "leads": database.get_leads(),
            "posts": database.get_posts(),
            "errors": database.get_collection_errors(),
        }
    finally:
        database.close()


def _overview(st) -> None:
    st.title("Lead scraper overview")
    st.caption("Public-data collection workspace · live SQLite-backed results")
    database = _database()
    try:
        maps = database.get_map_leads()
        linkedin = database.get_social_profiles("linkedin")
        instagram = database.get_social_profiles("instagram")
        leads = database.get_leads()
        runs = database.get_recent_runs()
        errors = database.get_collection_errors()
    finally:
        database.close()

    metrics = st.columns(4)
    for column, label, value in (
        (metrics[0], "Google Maps leads", len(maps)),
        (metrics[1], "LinkedIn profiles", len(linkedin)),
        (metrics[2], "Instagram profiles", len(instagram)),
        (metrics[3], "Photography leads", len(leads)),
    ):
        column.metric(label, value)

    left, right = st.columns([1.2, 1])
    with left:
        st.subheader("Lead classification")
        if leads:
            counts = pd.Series([lead.get("lead_temperature", "Unknown") for lead in leads]).value_counts()
            st.bar_chart(counts)
        else:
            st.info("No photography classifications have been collected.")
    with right:
        st.subheader("Recent activity")
        if runs:
            rows = [
                {"started": run["started_at"], "finished": run["finished_at"] or "Running",
                 "sources": ", ".join(run["config"].get("sources", []))}
                for run in runs
            ]
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        else:
            st.info("No collection runs recorded.")

    st.subheader("Collection status and errors")
    if errors:
        st.dataframe(pd.DataFrame(errors), use_container_width=True, hide_index=True)
    else:
        st.success("No collection errors recorded.")


def _maps_section(st) -> None:
    st.title("Google Maps scraper")
    with st.form("maps-form"):
        keyword = st.text_input("Keyword", "photography studio")
        location = st.text_input("Location", "Bengaluru")
        maximum = st.number_input("Maximum results", min_value=1, max_value=200, value=100)
        submitted = st.form_submit_button("Start Maps scrape", type="primary", use_container_width=True)
    if submitted:
        if not keyword.strip() or not location.strip():
            st.error("Keyword and location are required.")
        else:
            _submit_job(st, "maps", lambda: _maps_job(keyword, location, int(maximum)))
    result = _job_result(st, "maps")
    if result and result.get("error"):
        st.error(result["error"])
    if result and "rows" in result:
        st.success(f"Collected {len(result['rows'])} Maps lead(s).")
        if result.get("errors"):
            st.warning(f"{len(result['errors'])} listing issue(s) were recorded.")
    database = _database()
    try:
        rows = database.get_map_leads()
    finally:
        database.close()
    _filtered_table(st, rows, "maps-table", "business_name")
    _downloads(st, OUTPUT_ROOT / "maps", "google_maps_leads")


def _social_section(st) -> None:
    st.title("LinkedIn and Instagram profiles")
    linkedin_tab, instagram_tab = st.tabs(["LinkedIn", "Instagram"])
    for platform, tab in (("linkedin", linkedin_tab), ("instagram", instagram_tab)):
        with tab:
            st.caption("Only public, permitted results are collected. Login, CAPTCHA, robots, and access restrictions are not bypassed.")
            with st.form(f"{platform}-form"):
                urls = st.text_area("Direct profile URLs", placeholder="One URL per line")
                keywords = st.text_input("Keywords", "photographer" if platform == "instagram" else "photographer")
                hashtag = st.text_input("Hashtag", "" if platform == "linkedin" else "photography")
                location = st.text_input("Location", "Bengaluru")
                maximum = st.number_input("Maximum profiles", min_value=1, max_value=200, value=100, key=f"{platform}-max")
                submitted = st.form_submit_button(f"Start {platform.title()} scrape", type="primary", use_container_width=True)
            job_key = f"social-{platform}"
            if submitted:
                if not any((urls.strip(), keywords.strip(), hashtag.strip(), location.strip())):
                    st.error("Provide a URL, keyword, hashtag, or location.")
                else:
                    _submit_job(st, job_key, lambda p=platform: _social_job(p, urls, keywords, hashtag, location, int(maximum)))
            result = _job_result(st, job_key)
            if result and result.get("error"):
                st.error(result["error"])
            if result and "rows" in result:
                st.success(f"Collected {len(result['rows'])} {platform} profile(s).")
                if result.get("issues"):
                    st.warning(f"{len(result['issues'])} provider issue(s) were recorded.")
            database = _database()
            try:
                rows = database.get_social_profiles(platform)
            finally:
                database.close()
            _filtered_table(st, rows, f"{platform}-table", "profile_url")
            _downloads(st, OUTPUT_ROOT / "social", f"{platform}_profiles")


def _photography_section(st) -> None:
    st.title("Photography leads scraper")
    with st.form("photography-form"):
        keywords_text = st.text_input(
            "Keywords",
            "looking for photographer,need a photographer,photographer recommendations,"
            "photographer needed,hire a photographer",
        )
        location = st.text_input("Location", "")
        sources = st.multiselect(
            "Public sources",
            ["mastodon", "bluesky", "reddit", "discourse", "rss"],
            default=["mastodon", "reddit"],
        )
        maximum = st.number_input(
            "Maximum posts per source",
            min_value=1,
            max_value=10000,
            value=2000,
            help="The Reddit archive is large; 1000+ posts are usually needed for a 100-lead sample.",
        )
        submitted = st.form_submit_button("Start photography scrape", type="primary", use_container_width=True)
    if submitted:
        keywords = [item.strip() for item in keywords_text.split(",") if item.strip()]
        if not keywords or not sources:
            st.error("Add at least one keyword and one source.")
        else:
            _submit_job(st, "photography", lambda: _photography_job(location, keywords, sources, int(maximum)))
    result = _job_result(st, "photography")
    if result and result.get("error"):
        st.error(result["error"])
    database = _database()
    try:
        leads = database.get_leads()
    finally:
        database.close()
    counts = st.columns(3)
    for column, label in zip(counts, ("Hot", "Warm", "Cold")):
        column.metric(label, sum(lead.get("lead_temperature") == label for lead in leads))
    _filtered_table(st, leads, "photography-table", "score")
    _downloads(st, OUTPUT_ROOT / "photography", "leads")


def run_streamlit_dashboard() -> None:
    import streamlit as st

    st.set_page_config(page_title="LEAD_SCRAPER", page_icon="📊", layout="wide")
    st.markdown(
        """
        <style>
        .block-container { max-width: 1440px; padding-top: 2rem; }
        [data-testid="stMetric"] { border: 1px solid #dfe5ec; padding: 1rem; border-radius: .5rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.session_state.setdefault("refresh_nonce", 0)
    st.sidebar.title("LEAD_SCRAPER")
    st.sidebar.caption("Public lead discovery workspace")
    section = st.sidebar.radio("Navigate", ["Overview", "Google Maps", "LinkedIn / Instagram", "Photography Leads"])
    if st.sidebar.button("Refresh stored data", use_container_width=True):
        st.session_state["refresh_nonce"] += 1
        st.rerun()
    if section == "Overview":
        _overview(st)
    elif section == "Google Maps":
        _maps_section(st)
    elif section == "LinkedIn / Instagram":
        _social_section(st)
    else:
        _photography_section(st)


def run_dashboard(run_tool: Callable[..., Any] | None = None, **kwargs: Any) -> Any:
    """Compatibility entry point; Streamlit is now the only dashboard UI."""
    if run_tool is not None:
        if not callable(run_tool):
            raise TypeError("run_tool must be callable.")
        return run_tool(**kwargs)
    return run_streamlit_dashboard()
