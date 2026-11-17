"""SQLite persistence and exports for all lead-scraper tools."""
import csv
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3


LEAD_FIELDS = [
    "source", "author_name", "author_handle", "post_url", "post_title", "post_text",
    "created_at", "collected_at", "location", "public_contact", "query",
    "intent_category", "intent_evidence", "classification_reason", "score",
    "lead_temperature", "score_breakdown",
]
MAP_FIELDS = [
    "business_name", "category", "address", "phone", "website",
    "rating", "reviews", "maps_url",
]


class LeadDatabase:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            source_post_id TEXT NOT NULL,
            post_url TEXT NOT NULL,
            post_json TEXT NOT NULL,
            created_at TEXT,
            UNIQUE(source, source_post_id)
        );
        CREATE TABLE IF NOT EXISTS leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            source_post_id TEXT NOT NULL,
            lead_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(source, source_post_id)
        );
        CREATE TABLE IF NOT EXISTS search_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            config_json TEXT,
            diagnostics_json TEXT
        );
        CREATE TABLE IF NOT EXISTS collection_errors (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER,
            source TEXT,
            error TEXT,
            created_at TEXT
        );
        CREATE TABLE IF NOT EXISTS map_leads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            maps_url TEXT NOT NULL UNIQUE,
            lead_json TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS social_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            profile_url TEXT NOT NULL,
            profile_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(platform, profile_url)
        );
        """)
        self.conn.commit()

    def start_run(self, config):
        cur = self.conn.execute(
            "INSERT INTO search_runs(started_at, config_json) VALUES (?, ?)",
            (datetime.now(timezone.utc).isoformat(), json.dumps(config, default=str)),
        )
        self.conn.commit()
        return cur.lastrowid

    def save_post(self, post):
        source = post.get("source", "")
        source_id = post.get("source_post_id") or post.get("post_url") or ""
        if not source_id:
            return False
        cur = self.conn.execute(
            """INSERT INTO posts(source, source_post_id, post_url, post_json, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(source, source_post_id) DO UPDATE SET
               post_url=excluded.post_url, post_json=excluded.post_json,
               created_at=excluded.created_at""",
            (source, source_id, post.get("post_url", ""), json.dumps(post, ensure_ascii=False, default=str),
             str(post.get("created_at", ""))),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def save_lead(self, lead):
        self.conn.execute(
            """INSERT INTO leads(source, source_post_id, lead_json, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(source, source_post_id) DO UPDATE SET
               lead_json=excluded.lead_json, updated_at=excluded.updated_at""",
            (lead["source"], lead["source_post_id"], json.dumps(lead, ensure_ascii=False, default=str),
             datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    def replace_leads(self, leads: list[dict]) -> None:
        """Rebuild the derived lead table from the current normalized post set."""
        self.conn.execute("DELETE FROM leads")
        for lead in leads:
            self.conn.execute(
                """INSERT INTO leads(source, source_post_id, lead_json, updated_at)
                   VALUES (?, ?, ?, ?)""",
                (lead["source"], lead["source_post_id"],
                 json.dumps(lead, ensure_ascii=False, default=str),
                 datetime.now(timezone.utc).isoformat()),
            )
        self.conn.commit()

    def finish_run(self, run_id, diagnostics):
        self.conn.execute(
            "UPDATE search_runs SET finished_at=?, diagnostics_json=? WHERE id=?",
            (datetime.now(timezone.utc).isoformat(), json.dumps(diagnostics, ensure_ascii=False), run_id),
        )
        for item in diagnostics:
            for error in item.get("errors", []):
                self.conn.execute(
                    "INSERT INTO collection_errors(run_id, source, error, created_at) VALUES (?, ?, ?, ?)",
                    (run_id, item.get("source"), error, datetime.now(timezone.utc).isoformat()),
                )
        self.conn.commit()

    def close(self):
        self.conn.close()

    def get_posts(self, source: str | None = None, limit: int | None = None) -> list[dict]:
        query = "SELECT post_json FROM posts"
        params: list[object] = []
        if source:
            query += " WHERE source = ?"
            params.append(source)
        query += " ORDER BY id DESC"
        if limit is not None:
            if limit < 1:
                raise ValueError("limit must be at least 1.")
            query += " LIMIT ?"
            params.append(limit)
        return [json.loads(row["post_json"]) for row in self.conn.execute(query, params)]

    def get_leads(
        self,
        temperature: str | None = None,
        minimum_score: int | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        rows = [json.loads(row["lead_json"]) for row in self.conn.execute(
            "SELECT lead_json FROM leads ORDER BY updated_at DESC"
        )]
        if temperature:
            rows = [row for row in rows if row.get("lead_temperature") == temperature]
        if minimum_score is not None:
            rows = [row for row in rows if int(row.get("score", 0)) >= minimum_score]
        if limit is not None:
            if limit < 1:
                raise ValueError("limit must be at least 1.")
            rows = rows[:limit]
        return rows

    def save_map_lead(self, lead: dict) -> bool:
        maps_url = lead.get("maps_url", "")
        if not maps_url:
            return False
        cursor = self.conn.execute(
            """INSERT INTO map_leads(maps_url, lead_json, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(maps_url) DO UPDATE SET
               lead_json=excluded.lead_json, updated_at=excluded.updated_at""",
            (maps_url, json.dumps(lead, ensure_ascii=False, default=str),
             datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def get_map_leads(self, limit: int | None = None) -> list[dict]:
        query = "SELECT lead_json FROM map_leads ORDER BY updated_at DESC"
        params: list[object] = []
        if limit is not None:
            if limit < 1:
                raise ValueError("limit must be at least 1.")
            query += " LIMIT ?"
            params.append(limit)
        return [json.loads(row["lead_json"]) for row in self.conn.execute(query, params)]

    def save_social_profile(self, platform: str, profile: dict) -> bool:
        if platform not in {"linkedin", "instagram"}:
            raise ValueError("platform must be linkedin or instagram.")
        profile_url = str(profile.get("profile_url", "")).strip()
        if not profile_url:
            return False
        cursor = self.conn.execute(
            """INSERT INTO social_profiles(platform, profile_url, profile_json, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(platform, profile_url) DO UPDATE SET
               profile_json=excluded.profile_json, updated_at=excluded.updated_at""",
            (platform, profile_url, json.dumps(profile, ensure_ascii=False, default=str),
             datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()
        return cursor.rowcount > 0

    def get_social_profiles(self, platform: str | None = None) -> list[dict]:
        query = "SELECT profile_json FROM social_profiles"
        params: list[object] = []
        if platform:
            if platform not in {"linkedin", "instagram"}:
                raise ValueError("platform must be linkedin or instagram.")
            query += " WHERE platform = ?"
            params.append(platform)
        query += " ORDER BY updated_at DESC"
        return [json.loads(row["profile_json"]) for row in self.conn.execute(query, params)]

    def get_recent_runs(self, limit: int = 10) -> list[dict]:
        if limit < 1:
            raise ValueError("limit must be at least 1.")
        rows = self.conn.execute(
            """SELECT started_at, finished_at, config_json, diagnostics_json
               FROM search_runs ORDER BY id DESC LIMIT ?""",
            (limit,),
        )
        return [
            {
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
                "config": json.loads(row["config_json"] or "{}"),
                "diagnostics": json.loads(row["diagnostics_json"] or "[]"),
            }
            for row in rows
        ]

    def get_collection_errors(self, limit: int = 20) -> list[dict]:
        if limit < 1:
            raise ValueError("limit must be at least 1.")
        rows = self.conn.execute(
            """SELECT source, error, created_at
               FROM collection_errors ORDER BY id DESC LIMIT ?""",
            (limit,),
        )
        return [dict(row) for row in rows]


def _write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


EXCEL_CELL_LIMIT = 32767


def _excel_frame(pd, rows):
    """Build a DataFrame that fits Excel's 32,767-character cell limit."""
    frame = pd.DataFrame(rows)
    for column in frame.columns:
        frame[column] = frame[column].map(
            lambda value: value[:EXCEL_CELL_LIMIT]
            if isinstance(value, str) and len(value) > EXCEL_CELL_LIMIT
            else value
        )
    return frame


def export_leads(leads, raw_posts, output_dir, excel=False):
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "leads.json").write_text(
        json.dumps({"total_leads": len(leads), "leads": leads}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out / "raw_posts.json").write_text(
        json.dumps({"total_posts": len(raw_posts), "posts": raw_posts}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_csv(out / "leads.csv", leads, LEAD_FIELDS)
    if excel:
        try:
            import pandas as pd
            _excel_frame(pd, leads).to_excel(out / "leads.xlsx", index=False)
        except ImportError as exc:
            raise RuntimeError("Excel export requires pandas and openpyxl.") from exc


def export_social_profiles(output_dir: Path, linkedin: list[dict], instagram: list[dict]) -> None:
    from .scrapers import LINKEDIN_FIELDS, INSTAGRAM_FIELDS
    output_dir.mkdir(parents=True, exist_ok=True)
    for platform, rows, fields in (
        ("linkedin", linkedin, LINKEDIN_FIELDS),
        ("instagram", instagram, INSTAGRAM_FIELDS),
    ):
        _write_csv(output_dir / f"{platform}_profiles.csv", rows, fields + ["data_quality"])
        (output_dir / f"{platform}_profiles.json").write_text(
            json.dumps({"platform": platform, "total_profiles": len(rows), "profiles": rows},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        try:
            import pandas as pd
            export_rows = []
            for row in rows:
                flattened = dict(row)
                flattened["field_status"] = json.dumps(
                    row.get("field_status", {}), ensure_ascii=False, sort_keys=True
                )
                export_rows.append(flattened)
            pd.DataFrame(export_rows, columns=fields + ["data_quality"]).to_excel(
                output_dir / f"{platform}_profiles.xlsx", index=False
            )
        except ImportError as exc:
            raise RuntimeError("Social profile Excel export requires pandas and openpyxl.") from exc


def export_social_profile_platform(output_dir: Path, platform: str, rows: list[dict]) -> None:
    from .scrapers import LINKEDIN_FIELDS, INSTAGRAM_FIELDS
    fields = LINKEDIN_FIELDS if platform == "linkedin" else INSTAGRAM_FIELDS if platform == "instagram" else None
    if fields is None:
        raise ValueError("platform must be 'linkedin' or 'instagram'.")
    filename = f"{platform}_profiles.csv"
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / filename, rows, fields + ["data_quality"])
    (output_dir / f"{platform}_profiles.json").write_text(
        json.dumps({"platform": platform, "total_profiles": len(rows), "profiles": rows},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        import pandas as pd
        export_rows = []
        for row in rows:
            flattened = dict(row)
            flattened["field_status"] = json.dumps(
                row.get("field_status", {}), ensure_ascii=False, sort_keys=True
            )
            export_rows.append(flattened)
        pd.DataFrame(export_rows, columns=fields + ["data_quality"]).to_excel(
            output_dir / f"{platform}_profiles.xlsx", index=False
        )
    except ImportError as exc:
        raise RuntimeError("Social profile Excel export requires pandas and openpyxl.") from exc


def export_maps(leads: list[dict], output_dir: str | Path) -> None:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "google_maps_leads.json").write_text(
        json.dumps({"total_leads": len(leads), "leads": leads}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _write_csv(out / "google_maps_leads.csv", leads, MAP_FIELDS)
    try:
        import pandas as pd
        pd.DataFrame(leads, columns=MAP_FIELDS).to_excel(
            out / "google_maps_leads.xlsx", index=False
        )
    except ImportError as exc:
        raise RuntimeError("Google Maps Excel export requires pandas and openpyxl.") from exc
