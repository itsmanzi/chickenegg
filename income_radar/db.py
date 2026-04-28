import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from income_radar import crypto_util

SCHEMA_VERSION = 1


def data_dir() -> Path:
    root = Path(__file__).resolve().parent
    d = root / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path() -> Path:
    override = (os.getenv("INCOME_RADAR_DB") or "").strip()
    if override:
        return Path(override)
    return data_dir() / "income_radar.db"


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(db_path())
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    con = connect()
    try:
        con.executescript(
            """
            PRAGMA journal_mode = WAL;
            CREATE TABLE IF NOT EXISTS meta (
              key TEXT PRIMARY KEY,
              value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS profile (
              id INTEGER PRIMARY KEY CHECK (id = 1),
              skills TEXT DEFAULT '',
              country TEXT DEFAULT 'NL',
              region TEXT DEFAULT '',
              languages TEXT DEFAULT 'en,nl',
              monthly_target_eur REAL DEFAULT 5000,
              no_video_calls INTEGER DEFAULT 1,
              no_phone INTEGER DEFAULT 1,
              async_only INTEGER DEFAULT 1,
              proof_threshold_eur REAL DEFAULT 100,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS opportunities (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              source TEXT NOT NULL,
              external_id TEXT NOT NULL,
              title TEXT NOT NULL,
              url TEXT NOT NULL,
              summary TEXT DEFAULT '',
              published_at TEXT,
              tags TEXT DEFAULT '[]',
              score REAL DEFAULT 0,
              fetched_at TEXT NOT NULL,
              UNIQUE(source, external_id)
            );
            CREATE TABLE IF NOT EXISTS pipeline (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              opportunity_id INTEGER NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
              stage TEXT NOT NULL DEFAULT 'new',
              notes_enc TEXT DEFAULT '',
              updated_at TEXT NOT NULL,
              UNIQUE(opportunity_id)
            );
            CREATE TABLE IF NOT EXISTS income_log (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              logged_at TEXT NOT NULL,
              amount_eur REAL NOT NULL,
              source_label TEXT NOT NULL,
              user_verified INTEGER NOT NULL DEFAULT 1,
              notes_enc TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS learning_weights (
              tag TEXT PRIMARY KEY,
              weight REAL NOT NULL DEFAULT 0
            );
            """
        )
        row = con.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        if not row:
            con.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
        now = datetime.now(timezone.utc).isoformat()
        pr = con.execute("SELECT id FROM profile WHERE id=1").fetchone()
        if not pr:
            con.execute(
                """
                INSERT INTO profile(
                  id, skills, country, region, languages, monthly_target_eur,
                  no_video_calls, no_phone, async_only, proof_threshold_eur,
                  created_at, updated_at
                ) VALUES (
                  1, '', 'NL', '', 'en,nl', 5000, 1, 1, 1, 100, ?, ?
                )
                """,
                (now, now),
            )
        con.commit()
    finally:
        con.close()


def get_profile_row() -> sqlite3.Row:
    init_db()
    con = connect()
    try:
        return con.execute("SELECT * FROM profile WHERE id=1").fetchone()
    finally:
        con.close()


def upsert_profile(form: dict) -> None:
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    con = connect()
    try:
        con.execute(
            """
            UPDATE profile SET
              skills = ?, country = ?, region = ?, languages = ?,
              monthly_target_eur = ?, no_video_calls = ?, no_phone = ?, async_only = ?,
              proof_threshold_eur = ?, updated_at = ?
            WHERE id = 1
            """,
            (
                form.get("skills", "").strip(),
                form.get("country", "NL").strip() or "NL",
                form.get("region", "").strip(),
                form.get("languages", "en,nl").strip(),
                float(form.get("monthly_target_eur") or 5000),
                1 if form.get("no_video_calls") in ("1", "on", True, "true") else 0,
                1 if form.get("no_phone") in ("1", "on", True, "true") else 0,
                1 if form.get("async_only") in ("1", "on", True, "true") else 0,
                float(form.get("proof_threshold_eur") or 100),
                now,
            ),
        )
        con.commit()
    finally:
        con.close()


def verified_income_total() -> float:
    init_db()
    con = connect()
    try:
        row = con.execute(
            "SELECT COALESCE(SUM(amount_eur),0) AS t FROM income_log WHERE user_verified = 1"
        ).fetchone()
        return float(row["t"] or 0)
    finally:
        con.close()


def add_income(amount_eur: float, source_label: str, notes: str) -> None:
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    con = connect()
    try:
        con.execute(
            """
            INSERT INTO income_log(logged_at, amount_eur, source_label, user_verified, notes_enc)
            VALUES(?, ?, ?, 1, ?)
            """,
            (now, amount_eur, source_label.strip() or "unspecified", crypto_util.encrypt_note(notes or "")),
        )
        con.commit()
    finally:
        con.close()


def learning_map() -> dict[str, float]:
    init_db()
    con = connect()
    try:
        rows = con.execute("SELECT tag, weight FROM learning_weights").fetchall()
        return {r["tag"]: float(r["weight"]) for r in rows}
    finally:
        con.close()


def bump_learning_from_won_tags(con: sqlite3.Connection | None, tags: list[str]) -> None:
    if not tags:
        return
    close_after = False
    if con is None:
        init_db()
        con = connect()
        close_after = True
    try:
        for t in tags:
            t = t.strip().lower()[:64]
            if not t:
                continue
            con.execute(
                """
                INSERT INTO learning_weights(tag, weight) VALUES(?, 0.15)
                ON CONFLICT(tag) DO UPDATE SET weight = weight + 0.15
                """,
                (t,),
            )
        if close_after:
            con.commit()
    finally:
        if close_after and con is not None:
            con.close()


def income_rows(limit: int = 200) -> list[sqlite3.Row]:
    init_db()
    con = connect()
    try:
        return con.execute(
            "SELECT * FROM income_log ORDER BY logged_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    finally:
        con.close()


def opportunity_rows(limit: int = 200) -> list[sqlite3.Row]:
    init_db()
    con = connect()
    try:
        return con.execute(
            """
            SELECT o.*, p.stage AS pipe_stage, p.id AS pipe_id, p.notes_enc AS pipe_notes_enc
            FROM opportunities o
            LEFT JOIN pipeline p ON p.opportunity_id = o.id
            ORDER BY o.score DESC, o.fetched_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    finally:
        con.close()


def upsert_pipeline(opportunity_id: int, stage: str, notes: str) -> None:
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    enc = crypto_util.encrypt_note(notes or "")
    con = connect()
    try:
        con.execute(
            """
            INSERT INTO pipeline(opportunity_id, stage, notes_enc, updated_at)
            VALUES(?, ?, ?, ?)
            ON CONFLICT(opportunity_id) DO UPDATE SET
              stage = excluded.stage,
              notes_enc = excluded.notes_enc,
              updated_at = excluded.updated_at
            """,
            (opportunity_id, stage, enc, now),
        )
        if stage == "won":
            o = con.execute("SELECT tags FROM opportunities WHERE id=?", (opportunity_id,)).fetchone()
            if o:
                try:
                    tags = json.loads(o["tags"] or "[]")
                    if isinstance(tags, list):
                        bump_learning_from_won_tags(con, [str(x) for x in tags])
                except json.JSONDecodeError:
                    pass
        con.commit()
    finally:
        con.close()


def insert_opportunities(rows: list[dict]) -> int:
    """Insert or update opportunities; returns rows processed."""
    if not rows:
        return 0
    init_db()
    now = datetime.now(timezone.utc).isoformat()
    con = connect()
    try:
        for r in rows:
            con.execute(
                """
                INSERT INTO opportunities(
                  source, external_id, title, url, summary, published_at, tags, score, fetched_at
                ) VALUES(?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source, external_id) DO UPDATE SET
                  title = excluded.title,
                  url = excluded.url,
                  summary = excluded.summary,
                  published_at = excluded.published_at,
                  tags = excluded.tags,
                  score = excluded.score,
                  fetched_at = excluded.fetched_at
                """,
                (
                    r["source"],
                    r["external_id"],
                    r["title"],
                    r["url"],
                    r.get("summary", ""),
                    r.get("published_at"),
                    json.dumps(r.get("tags") or []),
                    float(r.get("score") or 0),
                    now,
                ),
            )
        con.commit()
        return len(rows)
    finally:
        con.close()
