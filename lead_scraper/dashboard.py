"""Modern Streamlit dashboard for Maps, social-profile, and photography lead tools.

All dashboard presentation and orchestration code lives in this file. Existing
scraper, configuration, and SQLite storage integrations are intentionally kept.
"""
from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
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

# -----------------------------------------------------------------------------
# Theme
# -----------------------------------------------------------------------------

_CSS = r"""
<style>
:root {
  --bg: #101216; --surface: #171a20; --surface-2: #1d2129;
  --surface-3: #242a34; --border: #2d333e; --text: #f3f4f6;
  --muted: #929aa8; --accent: #e34d5b; --accent-hover: #f05b68;
  --green: #47c58a; --amber: #e7b45b; --blue: #78aef5;
}
html, body, [class*="css"] { font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; }
.stApp { background: var(--bg); color: var(--text); }
[data-testid="stAppViewContainer"] { background: var(--bg); }
[data-testid="stHeader"] { background: rgba(16,18,22,.88); }
.block-container { max-width: 1540px; padding: 1.7rem clamp(1rem, 3vw, 3rem) 3rem; }
#MainMenu, footer { visibility: hidden; }
h1, h2, h3 { font-family: Inter, ui-sans-serif, system-ui, sans-serif !important; letter-spacing: -.035em; color: var(--text); }
h1 { font-size: clamp(1.8rem, 3vw, 2.45rem) !important; font-weight: 800 !important; margin-bottom: .15rem !important; }
h2 { font-size: 1.35rem !important; font-weight: 700 !important; }
h3 { font-size: 1.05rem !important; font-weight: 700 !important; }
p, label, .stCaption { color: var(--muted); }
small { color: var(--muted); }
/* Top navigation */
.top-brand { display:flex; align-items:center; gap:.7rem; font-family:Inter,ui-sans-serif,system-ui,sans-serif; font-weight:800; font-size:1.05rem; letter-spacing:-.03em; }
.brand-mark { width:34px; height:34px; border-radius:11px; display:grid; place-items:center; color:white; background:linear-gradient(145deg,#f06a74,#c93648); font-weight:800; box-shadow:0 7px 22px rgba(227,77,91,.2); }
.top-meta { color:var(--muted); font-size:.78rem; letter-spacing:.04em; text-transform:uppercase; }
div[role="radiogroup"] { gap:.35rem; }
div[role="radiogroup"] label { background:transparent; border:1px solid transparent; border-radius:9px; padding:.45rem .8rem; color:#aeb5c1; }
div[role="radiogroup"] label:has(input:checked) { background:rgba(227,77,91,.12); border-color:rgba(227,77,91,.35); color:#fff; }
div[role="radiogroup"] label p { color:inherit !important; font-weight:600; }
/* Surfaces and metrics */
[data-testid="stMetric"] { background:linear-gradient(145deg,var(--surface-2),var(--surface)); border:1px solid var(--border); border-radius:14px; padding:1rem 1.15rem; min-height:112px; }
[data-testid="stMetricLabel"] { color:var(--muted) !important; font-size:.82rem; }
[data-testid="stMetricValue"] { color:var(--text) !important; font-family:Inter,ui-sans-serif,system-ui,sans-serif; font-weight:800; }
[data-testid="stMetricDelta"] { font-size:.75rem; }
.panel { background:var(--surface); border:1px solid var(--border); border-radius:15px; padding:1rem 1.1rem; }
.section-kicker { color:var(--accent); text-transform:uppercase; letter-spacing:.12em; font-size:.68rem; font-weight:700; margin-bottom:.35rem; }
/* Inputs */
.stTextInput input, .stTextArea textarea, .stNumberInput input, [data-baseweb="select"] > div {
  background:var(--surface-2) !important; color:var(--text) !important;
  border-color:var(--border) !important; border-radius:9px !important;
}
.stTextInput input:focus, .stTextArea textarea:focus { border-color:var(--accent) !important; box-shadow:0 0 0 1px var(--accent) !important; }
.stMultiSelect [data-baseweb="tag"] { background:rgba(227,77,91,.18) !important; border:1px solid rgba(227,77,91,.3); }
.stButton button, .stDownloadButton button, button[kind="primary"], [data-testid="stFormSubmitButton"] button {
  border-radius:9px !important; font-weight:600 !important; min-height:2.55rem;
  transition:all .16s ease; border:1px solid var(--border) !important;
}
button[kind="primary"], [data-testid="stFormSubmitButton"] button {
  background:var(--accent) !important; border-color:var(--accent) !important; color:white !important;
}
button[kind="primary"]:hover, [data-testid="stFormSubmitButton"] button:hover { background:var(--accent-hover) !important; border-color:var(--accent-hover) !important; }
.stButton button:not([kind="primary"]), .stDownloadButton button { background:var(--surface-2) !important; color:var(--text) !important; }
.stButton button:hover, .stDownloadButton button:hover { border-color:#596273 !important; color:white !important; }
/* Tabs, tables and status */
.stTabs [data-baseweb="tab-list"] { gap:.35rem; border-bottom:1px solid var(--border); }
.stTabs [data-baseweb="tab"] { color:var(--muted); padding:.65rem 1rem; }
.stTabs [aria-selected="true"] { color:#fff !important; border-bottom-color:var(--accent) !important; }
[data-testid="stDataFrame"] { border:1px solid var(--border); border-radius:11px; overflow:hidden; }
[data-testid="stAlert"] { border-radius:10px; }
hr { border-color:var(--border) !important; }
[data-testid="stProgressBar"] > div > div { background:var(--accent) !important; }
/* Responsive */
@media (max-width: 850px) {
  .block-container { padding:1rem .85rem 2rem; }
  .top-brand { font-size:.9rem; }
  div[role="radiogroup"] { flex-wrap:wrap; }
  div[role="radiogroup"] label { padding:.35rem .5rem; font-size:.82rem; }
}
</style>
"""


def _database() -> LeadDatabase:
    return LeadDatabase(DB_PATH)


def _safe_count(value: Any) -> int:
    try:
        return len(value or [])
    except TypeError:
        return 0


def _submit_job(
    st, key: str, callback: Callable[[Callable[[str], None]], dict[str, Any]]
) -> None:
    jobs = st.session_state.setdefault("jobs", {})
    job = jobs.get(key)
    if job and not job["future"].done():
        st.warning("This collection is already running.")
        return
    job = {"stage": "Queued", "started_at": time.time()}

    def report(stage: str) -> None:
        job["stage"] = stage

    job["future"] = _EXECUTOR.submit(callback, report)
    jobs[key] = job
    st.session_state.pop(f"{key}_result", None)


def _job_result(st, key: str) -> dict[str, Any] | None:
    """Return a completed job; show honest elapsed-time status while it runs."""
    job = st.session_state.setdefault("jobs", {}).get(key)
    if not job:
        return st.session_state.get(f"{key}_result")
    future: Future = job["future"]
    if not future.done():
        elapsed = max(0, int(time.time() - job["started_at"]))
        minutes, seconds = divmod(elapsed, 60)
        with st.container(border=True):
            st.markdown("**Collection in progress**")
            st.caption(f"The scraper is running in the background · elapsed {minutes:02d}:{seconds:02d}")
            st.caption(f"Current stage: {job.get('stage', 'Running')}")
            if st.button("Refresh collection status", key=f"refresh-{key}"):
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
    st.markdown('<div class="section-kicker">Export data</div>', unsafe_allow_html=True)
    columns = st.columns(3)
    for column, extension, mime in (
        (columns[0], "csv", "text/csv"),
        (columns[1], "json", "application/json"),
        (columns[2], "xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ):
        data = _read_download(directory / f"{stem}.{extension}")
        if data is not None:
            column.download_button(
                f"Download {extension.upper()}", data=data,
                file_name=f"{stem}.{extension}", mime=mime,
                key=f"download-{directory.name}-{stem}-{extension}",
                width="stretch",
            )
        else:
            column.caption(f"{extension.upper()} export not available")


def _filtered_table(st, rows: list[dict], key: str, default_sort: str | None = None) -> None:
    """Search, filter selected columns, sort, and display records safely."""
    if pd is None:
        st.error("Pandas is required to display dashboard tables.")
        return
    if not rows:
        st.info("No records available yet. Start a collection to populate this table.")
        return

    frame = pd.DataFrame(rows).copy()
    frame = frame.drop(columns=[c for c in frame.columns if str(c).startswith("_")], errors="ignore")
    with st.container(border=True):
        st.markdown('<div class="section-kicker">Find and refine records</div>', unsafe_allow_html=True)
        search_col, filter_col = st.columns([1.5, 1])
        search = search_col.text_input("Search all fields", key=f"{key}-search", placeholder="Search names, locations, URLs, keywords…")
        columns = list(frame.columns)
        with filter_col:
            filter_columns = st.multiselect(
                "Filter by column", columns, default=[], key=f"{key}-filter-columns",
                help="Choose columns to filter by exact values.",
            )
        if search:
            mask = frame.astype(str).apply(
                lambda column: column.str.contains(search, case=False, na=False, regex=False)
            ).any(axis=1)
            frame = frame.loc[mask]
        if filter_columns:
            with st.expander("Filter values", expanded=True):
                filter_cols = st.columns(min(3, len(filter_columns)))
                for index, name in enumerate(filter_columns):
                    values = sorted(frame[name].dropna().astype(str).unique().tolist())
                    if values:
                        selected = filter_cols[index % len(filter_cols)].multiselect(
                            name.replace("_", " ").title(), values,
                            key=f"{key}-value-{name}",
                        )
                        if selected:
                            frame = frame[frame[name].astype(str).isin(selected)]
        toolbar_left, toolbar_right = st.columns([1, 1])
        sortable = list(frame.columns)
        if sortable:
            default_index = sortable.index(default_sort) if default_sort in sortable else 0
            selected_sort = toolbar_left.selectbox("Sort by", sortable, index=default_index, key=f"{key}-sort")
            direction = toolbar_right.selectbox("Order", ["Ascending", "Descending"], key=f"{key}-direction")
            frame = frame.sort_values(selected_sort, ascending=direction == "Ascending", kind="stable", na_position="last")
        st.caption(f"Showing **{len(frame):,}** of **{len(rows):,}** records")
        st.dataframe(frame, width="stretch", hide_index=True, height=min(560, max(220, 38 * min(len(frame), 14) + 42)))


def _maps_job(
    keyword: str, location: str, maximum: int, report: Callable[[str], None]
) -> dict[str, Any]:
    from .scrapers import MapsScraper
    from .storage import export_maps_from_database

    scraper = MapsScraper(progress=report)
    report("Collecting Maps listings")
    rows = scraper.scrape(keyword, location, maximum)
    report("Saving unique listings")
    database = _database()
    try:
        for row in rows:
            database.save_map_lead(row)
        for error in scraper.errors:
            database.save_collection_error(
                "maps", json.dumps(error, ensure_ascii=False)
            )
        report("Writing persistent CSV/Excel exports")
        directory = OUTPUT_ROOT / "maps"
        export_maps_from_database(database, directory)
        export_maps_from_database(database, OUTPUT_ROOT)
        total = len(database.get_map_leads())
    finally:
        database.close()
    return {"rows": rows, "errors": scraper.errors, "total": total}


def _social_job(
    platform: str, urls: str, keywords: str, hashtag: str, location: str,
    maximum: int, report: Callable[[str], None],
) -> dict[str, Any]:
    from .config import ScrapeConfig
    from .scrapers import ProfileQuery, SearchManager, scrape_platform
    from .storage import export_social_profile_from_database

    config = ScrapeConfig(
        max_results=maximum, output_dir=str(OUTPUT_ROOT / "social"),
        min_delay=1.0, max_delay=2.0,
    )
    config.validate()
    report(f"Discovering {platform.title()} profiles")
    query = ProfileQuery(
        profile_urls=[item.strip() for item in urls.replace(",", "\n").splitlines() if item.strip()],
        keywords=keywords, hashtag=hashtag, location=location, max_results=maximum,
    )
    manager = SearchManager(config)
    rows = scrape_platform(platform, manager, query, config)
    database = _database()
    try:
        report("Saving unique profiles")
        for row in rows:
            database.save_social_profile(platform, row)
        report("Writing persistent profile exports")
        directory = OUTPUT_ROOT / "social"
        stored = export_social_profile_from_database(database, directory, platform)
        export_social_profile_from_database(database, OUTPUT_ROOT, platform)
        total = len(stored)
    finally:
        database.close()
    return {"rows": rows, "issues": manager.issues, "total": total}


def _photography_job(
    location: str, keywords: list[str], sources: list[str], maximum: int,
    report: Callable[[str], None],
) -> dict[str, Any]:
    from main import run_photography

    args = [
        "--location", location, "--keywords", *keywords,
        "--sources", ",".join(sources), "--max-posts", str(maximum),
        "--delay", "1", "--db", str(DB_PATH),
        "--output", str(OUTPUT_ROOT / "photography"), "--excel",
    ]
    report("Collecting public intent posts")
    run_photography(args)
    report("Reading classified lead records")
    database = _database()
    try:
        return {
            "leads": database.get_leads(), "posts": database.get_posts(),
            "errors": database.get_collection_errors(),
        }
    finally:
        database.close()


def _page_header(title: str, description: str, eyebrow: str) -> None:
    st = __import__("streamlit")
    st.markdown(f'<div class="section-kicker">{eyebrow}</div>', unsafe_allow_html=True)
    st.title(title)
    st.caption(description)


def _overview(st) -> None:
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

    _page_header("Overview", "A live view of your public-data collection workspace.", "Workspace / Overview")
    total = len(maps) + len(linkedin) + len(instagram) + len(leads)
    metrics = st.columns(4)
    for column, label, value, note in (
        (metrics[0], "Total records", total, "Across all sources"),
        (metrics[1], "Google Maps", len(maps), "Business listings"),
        (metrics[2], "Social profiles", len(linkedin) + len(instagram), "LinkedIn + Instagram"),
        (metrics[3], "Photography leads", len(leads), "Classified opportunities"),
    ):
        with column:
            st.metric(label, f"{value:,}")
            st.caption(note)

    st.write("")
    left, right = st.columns([1.12, 1])
    with left:
        st.markdown("### Lead classification")
        temperatures = [str(lead.get("lead_temperature") or "Unknown").title() for lead in leads]
        counts = pd.Series(temperatures).value_counts() if pd is not None and temperatures else None
        if counts is not None and not counts.empty:
            order = [name for name in ["Hot", "Warm", "Cold", "Unknown"] if name in counts.index]
            counts = counts.reindex(order)
            st.bar_chart(counts, color="#e34d5b", height=250)
            hot, warm, cold = (temperatures.count(name) for name in ("Hot", "Warm", "Cold"))
            c1, c2, c3 = st.columns(3)
            c1.metric("Hot", hot)
            c2.metric("Warm", warm)
            c3.metric("Cold", cold)
        else:
            st.info("No photography classifications have been collected yet.")
    with right:
        st.markdown("### Source distribution")
        distribution = pd.Series({"Google Maps": len(maps), "LinkedIn": len(linkedin), "Instagram": len(instagram), "Photography": len(leads)}) if pd is not None else None
        if distribution is not None and int(distribution.sum()) > 0:
            st.bar_chart(distribution, color="#78aef5", height=250)
        else:
            st.info("Collected records will appear here.")

    st.markdown("### Recent activity")
    if runs:
        rows = [{
            "Started": run.get("started_at"),
            "Finished": run.get("finished_at") or "Running",
            "Sources": ", ".join(run.get("config", {}).get("sources", [])),
        } for run in runs]
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    else:
        st.info("No collection runs recorded.")

    st.markdown("### Collection status and errors")
    if errors:
        st.warning(f"{len(errors)} collection issue(s) recorded. Review provider responses and retry when appropriate.")
        st.dataframe(pd.DataFrame(errors), width="stretch", hide_index=True)
    else:
        st.success("No collection errors recorded.")


def _maps_section(st) -> None:
    _page_header("Google Maps leads", "Discover photography studios and related businesses by keyword and location.", "Lead sources / Maps")
    with st.container(border=True):
        st.markdown("**Search configuration**")
        with st.form("maps-form"):
            c1, c2 = st.columns([1.2, 1])
            keyword = c1.text_input("Business keyword", "photography studio", help="What type of business should be discovered?")
            location = c2.text_input("Location", "Bengaluru, Karnataka")
            maximum = st.number_input("Maximum results", min_value=1, max_value=200, value=100)
            submitted = st.form_submit_button("Start Maps collection", type="primary", width="stretch")
    if submitted:
        if not keyword.strip() or not location.strip():
            st.error("Keyword and location are required.")
        else:
            _submit_job(st, "maps", lambda report: _maps_job(keyword, location, int(maximum), report))
    result = _job_result(st, "maps")
    if result and result.get("error"):
        st.error(result["error"])
    elif result and "rows" in result:
        collected = len(result.get("rows") or [])
        total = int(result.get("total", collected))
        if collected:
            st.success(
                f"Collection completed · {collected:,} listing(s) collected this run · "
                f"{total:,} stored in SQLite."
            )
        elif result.get("errors"):
            st.error("No listings could be collected. Existing records were preserved.")
        else:
            st.warning("No listings matched this search. Existing records were preserved.")
        if result.get("errors"):
            with st.expander(f"{len(result['errors'])} listing issue(s) recorded"):
                if pd is not None:
                    st.dataframe(
                        pd.DataFrame(result["errors"]),
                        width="stretch",
                        hide_index=True,
                    )
                else:
                    st.json(result["errors"])
    database = _database()
    try:
        rows = database.get_map_leads()
    finally:
        database.close()
    st.markdown("### Business directory")
    st.caption("Records are read from SQLite, so a failed or empty scrape never removes earlier listings.")
    _filtered_table(st, rows, "maps-table", "business_name")
    _downloads(st, OUTPUT_ROOT / "maps", "google_maps_leads")


def _social_section(st) -> None:
    _page_header("Social profiles", "Find publicly available professional and creator profiles.", "Lead sources / Social")
    linkedin_tab, instagram_tab = st.tabs(["LinkedIn", "Instagram"])
    for platform, tab in (("linkedin", linkedin_tab), ("instagram", instagram_tab)):
        with tab:
            st.caption("Only public, permitted results are collected. Login, CAPTCHA, robots, and access restrictions are not bypassed.")
            with st.container(border=True):
                st.markdown(f"**{platform.title()} search configuration**")
                with st.form(f"{platform}-form"):
                    urls = st.text_area("Direct profile URLs", placeholder="Paste one profile URL per line", height=90)
                    c1, c2 = st.columns(2)
                    keywords = c1.text_input("Keywords", "photographer")
                    hashtag = c2.text_input("Hashtag", "" if platform == "linkedin" else "photography")
                    c3, c4 = st.columns([1, .65])
                    location = c3.text_input("Location", "Bengaluru")
                    maximum = c4.number_input("Maximum profiles", min_value=1, max_value=200, value=100, key=f"{platform}-max")
                    submitted = st.form_submit_button(f"Start {platform.title()} collection", type="primary", width="stretch")
            job_key = f"social-{platform}"
            if submitted:
                if not any((urls.strip(), keywords.strip(), hashtag.strip(), location.strip())):
                    st.error("Provide a URL, keyword, hashtag, or location.")
                else:
                    _submit_job(st, job_key, lambda report, p=platform: _social_job(p, urls, keywords, hashtag, location, int(maximum), report))
            result = _job_result(st, job_key)
            if result and result.get("error"):
                st.error(result["error"])
            elif result and "rows" in result:
                collected = len(result.get("rows") or [])
                total = int(result.get("total", collected))
                if collected:
                    st.success(
                        f"Collection completed · {collected:,} {platform} profile(s) "
                        f"collected this run · {total:,} stored in SQLite."
                    )
                elif result.get("issues"):
                    st.error(
                        "No profiles could be collected. Existing records were preserved."
                    )
                else:
                    st.warning(
                        "No profiles matched this search. Existing records were preserved."
                    )
                if result.get("issues"):
                    st.warning(f"{len(result['issues'])} provider issue(s) were recorded.")
            database = _database()
            try:
                rows = database.get_social_profiles(platform)
            finally:
                database.close()
            st.markdown("### Profile directory")
            _filtered_table(st, rows, f"{platform}-table", "profile_url")
            _downloads(st, OUTPUT_ROOT / "social", f"{platform}_profiles")


def _photography_section(st) -> None:
    _page_header("Photography leads", "Identify people actively looking to hire photographers across public communities.", "Lead sources / Photography")
    with st.container(border=True):
        st.markdown("**Collection configuration**")
        with st.form("photography-form"):
            keywords_text = st.text_area(
                "Search keywords",
                "looking for photographer,need a photographer,photographer recommendations,photographer needed,hire a photographer",
                height=90,
                help="Separate multiple search phrases with commas.",
            )
            c1, c2 = st.columns([1, 1.2])
            location = c1.text_input("Location", "", placeholder="Optional location")
            sources = c2.multiselect("Public sources", ["mastodon", "bluesky", "reddit", "discourse", "rss"], default=["mastodon", "reddit"])
            maximum = st.number_input("Maximum posts per source", min_value=1, max_value=10000, value=2000,
                help="The Reddit archive is large; 1000+ posts are usually needed for a 100-lead sample.")
            submitted = st.form_submit_button("Start photography collection", type="primary", width="stretch")
    if submitted:
        keywords = [item.strip() for item in keywords_text.split(",") if item.strip()]
        if not keywords or not sources:
            st.error("Add at least one keyword and one source.")
        else:
            _submit_job(st, "photography", lambda report: _photography_job(location, keywords, sources, int(maximum), report))
    result = _job_result(st, "photography")
    if result and result.get("error"):
        st.error(result["error"])
    elif result and "leads" in result:
        st.success(f"Collection completed · {len(result['leads']):,} photography lead(s) available.")

    database = _database()
    try:
        leads = database.get_leads()
    finally:
        database.close()
    temperatures = [str(lead.get("lead_temperature") or "Unknown").title() for lead in leads]
    counts = {name: temperatures.count(name) for name in ("Hot", "Warm", "Cold")}
    st.markdown("### Lead qualification")
    metric_cols = st.columns(3)
    for column, label, value, color, description in (
        (metric_cols[0], "Hot leads", counts["Hot"], "#47c58a", "High-priority opportunities"),
        (metric_cols[1], "Warm leads", counts["Warm"], "#e7b45b", "Potential opportunities"),
        (metric_cols[2], "Cold leads", counts["Cold"], "#78aef5", "Lower-priority opportunities"),
    ):
        with column:
            st.metric(label, f"{value:,}")
            st.caption(description)
    if leads and pd is not None:
        st.markdown("### Classification distribution")
        st.bar_chart(pd.Series(temperatures).value_counts(), height=190)
    st.markdown("### Lead records")
    _filtered_table(st, leads, "photography-table", "score")
    _downloads(st, OUTPUT_ROOT / "photography", "leads")


def _top_navigation(st) -> str:
    brand, navigation, refresh = st.columns([1.15, 4.5, 1.15], vertical_alignment="center")
    with brand:
        st.markdown('<div class="top-brand"><span class="brand-mark">L</span><span>LEADFLOW</span></div>', unsafe_allow_html=True)
        st.caption("PUBLIC DATA WORKSPACE")
    with navigation:
        sections = ["Overview", "Google Maps", "LinkedIn / Instagram", "Photography Leads"]
        selected = st.radio("Main navigation", sections, horizontal=True, label_visibility="collapsed", key="main-navigation")
    with refresh:
        if st.button("↻  Refresh data", width="stretch", help="Reload stored database records"):
            st.session_state["refresh_nonce"] = st.session_state.get("refresh_nonce", 0) + 1
            st.rerun()
    st.divider()
    return selected


def run_streamlit_dashboard() -> None:
    import streamlit as st

    st.set_page_config(page_title="Leadflow · Lead Intelligence", page_icon="📊", layout="wide", initial_sidebar_state="collapsed")
    st.markdown(_CSS, unsafe_allow_html=True)
    st.session_state.setdefault("refresh_nonce", 0)
    section = _top_navigation(st)
    if section == "Overview":
        _overview(st)
    elif section == "Google Maps":
        _maps_section(st)
    elif section == "LinkedIn / Instagram":
        _social_section(st)
    else:
        _photography_section(st)


def run_dashboard(run_tool: Callable[..., Any] | None = None, **kwargs: Any) -> Any:
    """Compatibility entry point; Streamlit is the dashboard UI."""
    if run_tool is not None:
        if not callable(run_tool):
            raise TypeError("run_tool must be callable.")
        return run_tool(**kwargs)
    return run_streamlit_dashboard()
