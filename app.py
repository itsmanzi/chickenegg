import os
import re
import base64
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify, render_template
from nl_corpus import get_corpus_for_language
from anthropic import (
    Anthropic,
    APIStatusError,
    AuthenticationError,
    BadRequestError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
)

app = Flask(__name__)

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
DB_BACKEND = "postgres" if DATABASE_URL else "sqlite"
DEFAULT_DB_PATH = "/tmp/metrics.db" if os.getenv("VERCEL") else os.path.join(app.root_path, "metrics.db")
DB_PATH = os.getenv("METRICS_DB_PATH", DEFAULT_DB_PATH)

_pg_connect = None
_pg_dict_row = None
if DB_BACKEND == "postgres":
    try:
        from psycopg import connect as _pg_connect
        from psycopg.rows import dict_row as _pg_dict_row
    except Exception as _pg_import_err:
        print(f"[db-init] psycopg import failed; falling back to sqlite: {_pg_import_err}")
        DB_BACKEND = "sqlite"

REQUIRED_EVENTS = {
    "scan_started",
    "scan_completed",
    "hazard_flagged",
    "step_completed",
    "job_completed",
    "email_collected",
    "cta_clicked",
    "founding_offer_clicked",
    "tool_link_clicked",
}

# Legacy frontend names are mapped into canonical KPI events.
EVENT_ALIASES = {
    "scan_button_tap": "scan_started",
    "task_done_celebration": "job_completed",
    "task_done_continue": "job_completed",
    "egg_walk_next": "step_completed",
}

ALLOWED_EVENTS = REQUIRED_EVENTS | {
    "language_changed",
    "feedback_submitted",
    "mailbox_opened",
}

# Never return DIY steps for these high-risk domains.
HARD_STOP_KEYWORDS = {
    "groepenkast",
    "meterkast",
    "zekeringkast",
    "hoofdschakelaar",
    "gasleiding",
    "gasmeter",
    "cv ketel",
    "cv-ketel",
    "ketel intern",
    "boiler internals",
    "fuse box",
    "breaker panel",
    "electrical panel",
    "main breaker",
    "live wire",
    "mains voltage",
    "gas line",
}

TEST_SOURCE_CHANNELS = ("smoke_test", "test", "dev")

# Lazy client so missing env fails on first request with a clear message, not at import.
_client = None


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def _to_backend_sql(sql):
    # Existing queries use sqlite-style "?" placeholders. Translate for psycopg.
    if DB_BACKEND == "postgres":
        return sql.replace("?", "%s")
    return sql


def _sql_day_expr(column_name="created_at"):
    # Normalize day bucketing across sqlite (TEXT timestamps) and postgres (TIMESTAMPTZ).
    if DB_BACKEND == "postgres":
        return f"to_char({column_name} AT TIME ZONE 'UTC', 'YYYY-MM-DD')"
    return f"substr({column_name},1,10)"


def _metrics_filter_sql(include_test, table_alias=""):
    """Default KPI views exclude smoke/dev traffic unless include_test=1."""
    if include_test:
        return "", []
    p = f"{table_alias}." if table_alias else ""
    placeholders = ", ".join(["?"] * len(TEST_SOURCE_CHANNELS))
    clause = (
        f" AND COALESCE({p}source_channel, '') NOT IN ({placeholders})"
        f" AND LOWER(COALESCE({p}session_id, '')) NOT LIKE ?"
    )
    return clause, [*TEST_SOURCE_CHANNELS, "smoke-%"]


class _ConnAdapter:
    def __init__(self, raw_conn):
        self._conn = raw_conn

    def execute(self, sql, params=()):
        return self._conn.execute(_to_backend_sql(sql), params)

    def commit(self):
        return self._conn.commit()

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return self._conn.__exit__(exc_type, exc_val, exc_tb)


def _db():
    if DB_BACKEND == "postgres":
        if _pg_connect is None:
            raise RuntimeError("DATABASE_URL is set but psycopg is unavailable")
        raw = _pg_connect(DATABASE_URL, row_factory=_pg_dict_row, connect_timeout=8)
        return _ConnAdapter(raw)

    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    raw = sqlite3.connect(DB_PATH, timeout=8)
    raw.row_factory = sqlite3.Row
    return _ConnAdapter(raw)


def _init_metrics_db():
    with _db() as conn:
        if DB_BACKEND == "postgres":
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id BIGSERIAL PRIMARY KEY,
                    created_at TIMESTAMPTZ NOT NULL,
                    event_raw TEXT NOT NULL,
                    event_name TEXT NOT NULL,
                    language TEXT,
                    session_id TEXT,
                    user_id TEXT,
                    job_id TEXT,
                    task_category TEXT,
                    hazard_level TEXT,
                    source_channel TEXT,
                    meta_json TEXT,
                    ip TEXT,
                    user_agent TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS emails (
                    id BIGSERIAL PRIMARY KEY,
                    created_at TIMESTAMPTZ NOT NULL,
                    email TEXT NOT NULL,
                    language TEXT,
                    source_channel TEXT,
                    session_id TEXT,
                    user_id TEXT,
                    job_id TEXT,
                    ip TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outcomes (
                    id BIGSERIAL PRIMARY KEY,
                    created_at TIMESTAMPTZ NOT NULL,
                    success INTEGER NOT NULL,
                    rating TEXT,
                    reason TEXT,
                    language TEXT,
                    session_id TEXT,
                    user_id TEXT,
                    job_id TEXT,
                    source_channel TEXT,
                    task_category TEXT,
                    task_text TEXT,
                    what_i_see TEXT,
                    hazard_level TEXT,
                    steps_json TEXT,
                    tools_json TEXT,
                    materials_json TEXT
                )
                """
            )
        else:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    event_raw TEXT NOT NULL,
                    event_name TEXT NOT NULL,
                    language TEXT,
                    session_id TEXT,
                    user_id TEXT,
                    job_id TEXT,
                    task_category TEXT,
                    hazard_level TEXT,
                    source_channel TEXT,
                    meta_json TEXT,
                    ip TEXT,
                    user_agent TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS emails (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    email TEXT NOT NULL,
                    language TEXT,
                    source_channel TEXT,
                    session_id TEXT,
                    user_id TEXT,
                    job_id TEXT,
                    ip TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS outcomes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    success INTEGER NOT NULL,
                    rating TEXT,
                    reason TEXT,
                    language TEXT,
                    session_id TEXT,
                    user_id TEXT,
                    job_id TEXT,
                    source_channel TEXT,
                    task_category TEXT,
                    task_text TEXT,
                    what_i_see TEXT,
                    hazard_level TEXT,
                    steps_json TEXT,
                    tools_json TEXT,
                    materials_json TEXT
                )
                """
            )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_created_at ON events(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_event_name ON events(event_name)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_emails_created_at ON emails(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_outcomes_created_at ON outcomes(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_outcomes_success ON outcomes(success)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_outcomes_category ON outcomes(task_category)")
        conn.commit()


def _clean_small_str(v, max_len=120):
    if v is None:
        return ""
    s = str(v).strip()
    if not s:
        return ""
    return s[:max_len]


def _safe_meta(meta):
    if not isinstance(meta, dict):
        return {}
    out = {}
    for k, v in meta.items():
        kk = _clean_small_str(k, 60)
        if not kk:
            continue
        if isinstance(v, (dict, list)):
            out[kk] = v
        elif isinstance(v, (int, float, bool)) or v is None:
            out[kk] = v
        else:
            out[kk] = _clean_small_str(v, 240)
    return out


def _request_ip():
    xff = request.headers.get("X-Forwarded-For", "").strip()
    if xff:
        return xff.split(",")[0].strip()[:80]
    return _clean_small_str(request.remote_addr, 80)


def _resolve_event_payload(data):
    raw_event = _clean_small_str((data or {}).get("event"), 80)
    if not raw_event:
        return None, "event is required"
    event_name = EVENT_ALIASES.get(raw_event, raw_event)
    if event_name not in ALLOWED_EVENTS:
        return None, f"event '{raw_event}' is not allowed"

    meta = _safe_meta((data or {}).get("meta") or {})
    payload = {
        "event_raw": raw_event,
        "event_name": event_name,
        "language": _clean_small_str((data or {}).get("language") or meta.get("language"), 12),
        "session_id": _clean_small_str((data or {}).get("session_id") or meta.get("session_id"), 80),
        "user_id": _clean_small_str((data or {}).get("user_id") or meta.get("user_id"), 80),
        "job_id": _clean_small_str((data or {}).get("job_id") or meta.get("job_id"), 80),
        "task_category": _clean_small_str((data or {}).get("task_category") or meta.get("task_category"), 60),
        "hazard_level": _clean_small_str((data or {}).get("hazard_level") or meta.get("hazard_level"), 20).lower(),
        "source_channel": _clean_small_str((data or {}).get("source_channel") or meta.get("source_channel"), 40),
        "meta_json": json.dumps(meta, ensure_ascii=False)[:8000],
        "ip": _request_ip(),
        "user_agent": _clean_small_str(request.headers.get("User-Agent"), 260),
    }
    return payload, None


def _insert_event(payload):
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO events (
                created_at, event_raw, event_name, language, session_id, user_id, job_id,
                task_category, hazard_level, source_channel, meta_json, ip, user_agent
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _utc_now_iso(),
                payload.get("event_raw", ""),
                payload.get("event_name", ""),
                payload.get("language", ""),
                payload.get("session_id", ""),
                payload.get("user_id", ""),
                payload.get("job_id", ""),
                payload.get("task_category", ""),
                payload.get("hazard_level", ""),
                payload.get("source_channel", ""),
                payload.get("meta_json", "{}"),
                payload.get("ip", ""),
                payload.get("user_agent", ""),
            ),
        )
        conn.commit()


def _insert_outcome(payload):
    with _db() as conn:
        conn.execute(
            """
            INSERT INTO outcomes (
                created_at, success, rating, reason, language, session_id, user_id, job_id, source_channel,
                task_category, task_text, what_i_see, hazard_level, steps_json, tools_json, materials_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _utc_now_iso(),
                1 if payload.get("success") else 0,
                _clean_small_str(payload.get("rating"), 40),
                _clean_small_str(payload.get("reason"), 240),
                _clean_small_str(payload.get("language"), 12),
                _clean_small_str(payload.get("session_id"), 80),
                _clean_small_str(payload.get("user_id"), 80),
                _clean_small_str(payload.get("job_id"), 80),
                _clean_small_str(payload.get("source_channel"), 40),
                _clean_small_str(payload.get("task_category"), 60),
                _clean_small_str(payload.get("task_text"), 240),
                _clean_small_str(payload.get("what_i_see"), 240),
                _clean_small_str(payload.get("hazard_level"), 20).lower(),
                json.dumps(payload.get("steps") or [], ensure_ascii=False)[:12000],
                json.dumps(payload.get("tools") or [], ensure_ascii=False)[:6000],
                json.dumps(payload.get("materials") or [], ensure_ascii=False)[:6000],
            ),
        )
        conn.commit()


def _build_success_pattern_memory(question, language):
    """Return compact proven-pattern hints from successful past outcomes."""
    q = _clean_small_str(question, 240).lower()
    token_candidates = [t for t in re.split(r"[^a-zA-Z0-9À-ÿ]+", q) if len(t) >= 4][:4]
    try:
        with _db() as conn:
            # Prefer matching successful outcomes by question tokens.
            rows = []
            if token_candidates:
                where = " OR ".join(["LOWER(task_text) LIKE ?" for _ in token_candidates])
                args = [f"%{tok}%" for tok in token_candidates]
                rows = conn.execute(
                    f"""
                    SELECT task_category, task_text, steps_json, tools_json, materials_json, COUNT(*) AS c, MAX(created_at) AS last_seen
                    FROM outcomes
                    WHERE success = 1
                      AND COALESCE(language,'') IN (?, '')
                      AND ({where})
                    GROUP BY task_category, task_text, steps_json, tools_json, materials_json
                    ORDER BY c DESC, last_seen DESC
                    LIMIT 4
                    """,
                    [language, *args],
                ).fetchall()
            # Fallback to top successful patterns in same language.
            if not rows:
                rows = conn.execute(
                    """
                    SELECT task_category, task_text, steps_json, tools_json, materials_json, COUNT(*) AS c, MAX(created_at) AS last_seen
                    FROM outcomes
                    WHERE success = 1
                      AND COALESCE(language,'') IN (?, '')
                    GROUP BY task_category, task_text, steps_json, tools_json, materials_json
                    ORDER BY c DESC, last_seen DESC
                    LIMIT 4
                    """,
                    (language,),
                ).fetchall()
    except Exception:
        return ""

    hints = []
    for r in rows:
        try:
            steps = json.loads(r["steps_json"] or "[]")
        except Exception:
            steps = []
        try:
            tools = json.loads(r["tools_json"] or "[]")
        except Exception:
            tools = []
        task_txt = _clean_small_str(r["task_text"], 90)
        cat = _clean_small_str(r["task_category"], 40)
        step_preview = ", ".join([_clean_small_str(s, 60) for s in steps[:2] if s]) or "n/a"
        tool_preview = ", ".join([_clean_small_str(t, 40) for t in tools[:3] if t]) or "n/a"
        hints.append(
            f"- [{cat}] {task_txt} | winning_steps: {step_preview} | common_tools: {tool_preview} | wins: {int(r['c'] or 1)}"
        )
    if not hints:
        return ""
    return "\n".join(hints)


try:
    _init_metrics_db()
except Exception as _db_boot_err:
    # Keep app boot resilient; endpoints return explicit errors if DB is unavailable later.
    print(f"[metrics-db-init] {type(_db_boot_err).__name__}: {_db_boot_err}")


def _get_client():
    global _client
    if _client is None:
        key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set")
        _client = Anthropic(api_key=key)
    return _client


# Vision-capable; Haiku 3.5 snapshots were retired — default to Haiku 4.5 (see Anthropic console).
DEFAULT_VISION_MODEL = "claude-haiku-4-5-20251001"
VISION_MODEL = os.getenv("ANTHROPIC_MODEL", DEFAULT_VISION_MODEL)
CHECK_PROGRESS_MODEL = os.getenv("ANTHROPIC_CHECK_MODEL", VISION_MODEL)


def _clean_str(v, fallback=""):
    if v is None:
        return fallback
    s = str(v).strip()
    return s if s else fallback


def _item_to_str(item):
    if item is None:
        return ""
    if isinstance(item, dict):
        return _clean_str(
            item.get("name")
            or item.get("tool")
            or item.get("item")
            or item.get("text")
            or item.get("description")
        )
    return _clean_str(item)


def _to_list(v):
    if isinstance(v, list):
        out = []
        for item in v:
            s = _item_to_str(item)
            if s:
                out.append(s)
        return out
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return []
        parts = [p.strip(" -•\t\r\n") for p in s.replace("\r", "\n").split("\n")]
        parts = [p for p in parts if p]
        return parts if parts else [s]
    return []


def _strip_step_prefix(txt):
    s = _clean_str(txt, "")
    if not s:
        return ""
    return re.sub(r"^(step|stap)\s*\d+[:\-.]?\s*", "", s, flags=re.I).strip()


def _confidence_bucket_and_flags(raw_confidence_tier, uncertainty_note, what_i_see):
    tier = _clean_str(raw_confidence_tier, "").lower().replace("_", "-")
    if "call" in tier or "not-diy" in tier:
        bucket = "low"
    elif "caution" in tier or "careful" in tier:
        bucket = "medium"
    elif tier:
        bucket = "high"
    else:
        bucket = "medium"

    uncertainty = _clean_str(uncertainty_note, "")
    seen_txt = _clean_str(what_i_see, "").lower()
    looks_unknown = ("unknown" in seen_txt) or ("onbekend" in seen_txt)
    needs_retake = bucket == "low" or bool(uncertainty) or looks_unknown
    return bucket, needs_retake


def _hard_stop_match_text(result, user_question):
    fields = []
    if isinstance(result, dict):
        fields.extend(
            [
                _clean_str(result.get("job_category"), ""),
                _clean_str(result.get("what_i_see"), ""),
                _clean_str(result.get("task"), ""),
                _clean_str(result.get("hazard_note"), ""),
                _clean_str(result.get("when_to_call_pro"), ""),
                " ".join(_to_list(result.get("steps") or [])),
                " ".join(_to_list(result.get("tools_needed") or [])),
                " ".join(_to_list(result.get("materials_needed") or [])),
            ]
        )
    fields.append(_clean_str(user_question, ""))
    return " ".join([f for f in fields if f]).lower()


def _apply_server_hard_stop(result, user_question, language):
    """Final backend safety layer: blocks risky DIY classes regardless of model output."""
    if not isinstance(result, dict):
        return result, []

    blob = _hard_stop_match_text(result, user_question)
    triggers = [kw for kw in HARD_STOP_KEYWORDS if kw in blob]
    if not triggers:
        return result, []

    is_nl = (language or "").lower() == "nl"
    hard_stop_result = dict(result)
    if is_nl:
        hard_stop_result.update(
            {
                "confidence_tier": "call-pro",
                "confidence": "low",
                "needs_retake": False,
                "job_category": result.get("job_category") or "other",
                "hazard_level": "danger",
                "hazard_note": "Hoog risico gedetecteerd. Stop direct en schakel een erkende professional in.",
                "when_to_call_pro": "Altijd bij groepenkast, hoofdspanning, gasleiding of ketel-internals.",
                "task": "Stop en bel een professional",
                "steps": [
                    "Stop onmiddellijk met DIY-werk aan dit onderdeel.",
                    "Schakel indien veilig de hoofdtoevoer uit en raak geen interne delen aan.",
                    "Neem contact op met een erkende elektricien/installateur.",
                ],
                "safety_tip": "Veiligheid eerst: geen DIY op hoofdspanning of gascomponenten.",
                "pro_tip": "Maak duidelijke foto's voor de vakman zodat diagnose sneller gaat.",
                "retake_guidance": "",
            }
        )
    else:
        hard_stop_result.update(
            {
                "confidence_tier": "call-pro",
                "confidence": "low",
                "needs_retake": False,
                "job_category": result.get("job_category") or "other",
                "hazard_level": "danger",
                "hazard_note": "High-risk scenario detected. Stop and contact a licensed professional.",
                "when_to_call_pro": "Always for fuse/electrical panels, mains voltage, gas lines, or boiler internals.",
                "task": "Stop and call a professional",
                "steps": [
                    "Stop DIY work on this component immediately.",
                    "If safe, isolate supply and do not touch internal parts.",
                    "Contact a licensed electrician/installer.",
                ],
                "safety_tip": "Safety first: no DIY work on mains electrical or gas systems.",
                "pro_tip": "Share clear photos with your professional to speed diagnosis.",
                "retake_guidance": "",
            }
        )
    return hard_stop_result, triggers


def _normalize_result(raw):
    if not isinstance(raw, dict):
        raw = {}

    steps_raw = (
        raw.get("steps")
        or raw.get("step_details")
        or raw.get("instructions")
        or raw.get("how_to")
        or raw.get("step_by_step")
        or raw.get("repair_steps")
        or raw.get("steps_to_fix")
        or raw.get("recommendation")
        or []
    )
    steps = []
    step_details = []
    if isinstance(steps_raw, list):
        for step in steps_raw:
            if isinstance(step, dict):
                txt = _clean_str(
                    step.get("text")
                    or step.get("step")
                    or step.get("instruction")
                )
                txt = _strip_step_prefix(txt)
                vt = _clean_str(step.get("visual_tip") or step.get("look_for") or step.get("verify"))
                if txt:
                    steps.append(txt)
                    step_details.append({"text": txt, "visual_tip": vt})
            else:
                txt = _strip_step_prefix(step)
                if txt:
                    steps.append(txt)
                    step_details.append({"text": txt, "visual_tip": ""})
    else:
        for line in _to_list(steps_raw):
            txt = _strip_step_prefix(line)
            if txt:
                steps.append(txt)
                step_details.append({"text": txt, "visual_tip": ""})

    if not steps:
        fallback = _clean_str(
            raw.get("what_to_do")
            or raw.get("fix_plan")
            or raw.get("recommendation")
            or raw.get("task")
            or raw.get("what_i_see")
        )
        if fallback:
            fb = _strip_step_prefix(fallback)
            steps = [fb]
            step_details = [{"text": fb, "visual_tip": ""}]

    tools = _to_list(raw.get("tools_needed") or raw.get("tools"))
    materials = _to_list(raw.get("materials_needed") or raw.get("materials") or raw.get("parts_needed"))

    qc_raw = raw.get("quick_checks") or raw.get("before_you_start") or []
    quick_checks = []
    if isinstance(qc_raw, list):
        for x in qc_raw:
            q = _clean_str(
                x
                if isinstance(x, str)
                else (
                    (x.get("text") or x.get("check"))
                    if isinstance(x, dict)
                    else str(x)
                ),
                "",
            )
            if q:
                quick_checks.append(q)
    else:
        quick_checks = _to_list(qc_raw)
    quick_checks = quick_checks[:4]

    cat = _clean_str(
        raw.get("job_category")
        or raw.get("category")
        or raw.get("domain"),
        "",
    )
    uncertainty = _clean_str(
        raw.get("uncertainty_note")
        or raw.get("image_limitation")
        or raw.get("confidence_caveat"),
        "",
    )
    conf = _clean_str(raw.get("confidence_tier") or raw.get("confidence"), "")
    low = conf.lower().replace("_", "-")
    if not conf:
        hz = _clean_str(raw.get("hazard_level"), "safe").lower()
        if hz in ("danger",):
            conf = "call-pro"
        elif hz in ("warning", "caution"):
            conf = "caution"
        else:
            conf = "DIY-safe"
    elif "call" in low or low.startswith("call-") or "not-diy" in low:
        conf = "call-pro"
    elif "caution" in low or "careful" in low:
        conf = "caution"
    else:
        conf = "DIY-safe"

    normalized = {
        "what_i_see": _clean_str(raw.get("what_i_see") or raw.get("problem") or raw.get("issue"), "Unknown item"),
        "task": _clean_str(raw.get("task") or raw.get("what_to_do") or raw.get("fix"), "Fix task"),
        "difficulty": _clean_str(raw.get("difficulty"), "medium"),
        "estimated_cost": _clean_str(raw.get("estimated_cost"), ""),
        "time_needed": _clean_str(raw.get("time_needed"), ""),
        "hazard_level": _clean_str(raw.get("hazard_level"), "safe").lower(),
        "hazard_note": _clean_str(raw.get("hazard_note"), ""),
        "when_to_call_pro": _clean_str(raw.get("when_to_call_pro"), ""),
        "tools_needed": tools,
        "materials_needed": materials,
        "steps": steps,
        "step_details": step_details,
        "job_category": cat,
        "uncertainty_note": uncertainty,
        "quick_checks": quick_checks[:4],
        "confidence_tier": conf,
        "safety_tip": _clean_str(raw.get("safety_tip") or raw.get("safety") or raw.get("warning"), "Work slowly and wear protection."),
        "pro_tip": _clean_str(raw.get("pro_tip") or raw.get("tip"), ""),
        "xray_readout": _clean_str(raw.get("xray_readout") or raw.get("defect_vs_cleaning"), ""),
        "material_readout": _clean_str(raw.get("material_readout") or raw.get("materials_spotted"), ""),
    }
    confidence_bucket, needs_retake = _confidence_bucket_and_flags(
        normalized.get("confidence_tier"),
        normalized.get("uncertainty_note"),
        normalized.get("what_i_see"),
    )
    normalized["confidence"] = confidence_bucket
    normalized["needs_retake"] = bool(needs_retake)
    if needs_retake:
        normalized["retake_guidance"] = (
            "Maak nog een foto van dichterbij met betere verlichting."
            if confidence_bucket == "low"
            else "Maak nog een extra detailfoto voor hogere zekerheid."
        )
    else:
        normalized["retake_guidance"] = ""

    def _ve_bullets(src_dict, key, max_n):
        raw_list = src_dict.get(key) or []
        out = []
        if isinstance(raw_list, list):
            for x in raw_list[: max_n + 2]:
                s = _clean_str(
                    x
                    if isinstance(x, str)
                    else ((x.get("text") or x.get("item")) if isinstance(x, dict) else str(x)),
                    "",
                )
                if s:
                    out.append(s)
        return out[:max_n]

    key_obs = _ve_bullets(raw, "key_observations", 5)
    poss = _ve_bullets(raw, "possible_issues", 5)
    when_stop = _ve_bullets(raw, "when_to_stop", 4)

    sev_raw = _clean_str(raw.get("severity_ui"), "").lower().replace("-", "_").replace(" ", "_")
    if sev_raw in ("safe", "cosmetic", "safecosmetic", "green"):
        sev = "safe_cosmetic"
    elif sev_raw in ("attention", "needs_attention", "caution", "yellow"):
        sev = "needs_attention"
    elif sev_raw in ("dangerous", "potentially_dangerous", "danger", "red"):
        sev = "potentially_dangerous"
    elif sev_raw in ("safe_cosmetic", "needs_attention", "potentially_dangerous"):
        sev = sev_raw
    else:
        sev = ""

    hz_n = (normalized.get("hazard_level") or "safe").lower()
    ct_n = (normalized.get("confidence_tier") or "").lower()
    if not sev:
        if hz_n in ("danger", "emergency") or "call" in ct_n:
            sev = "potentially_dangerous"
        elif hz_n in ("warning", "caution") or confidence_bucket == "low":
            sev = "needs_attention"
        else:
            sev = "safe_cosmetic"

    dec_raw = _clean_str(raw.get("decision_outcome"), "").lower().replace("-", "_").replace(" ", "_")
    if dec_raw in ("safe", "safe_to_proceed", "ok", "green"):
        dec = "safe_to_proceed"
    elif dec_raw in ("caution", "proceed_with_caution", "careful", "yellow"):
        dec = "proceed_with_caution"
    elif dec_raw in ("stop", "do_not_proceed", "dont", "no", "red"):
        dec = "do_not_proceed"
    elif dec_raw in ("safe_to_proceed", "proceed_with_caution", "do_not_proceed"):
        dec = dec_raw
    else:
        dec = ""

    if not dec:
        if sev == "potentially_dangerous" or "call" in ct_n:
            dec = "do_not_proceed"
        elif sev == "needs_attention":
            dec = "proceed_with_caution"
        else:
            dec = "safe_to_proceed"

    rationale = _clean_str(raw.get("decision_rationale"), "")
    if not rationale:
        if dec == "do_not_proceed":
            rationale = _clean_str(
                normalized.get("hazard_note") or normalized.get("when_to_call_pro"),
                "Do not continue without qualified help if you see risk signs.",
            )
        elif dec == "proceed_with_caution":
            rationale = (
                "You may proceed carefully if you follow checks, use the right tools, and stop if anything feels unsafe."
            )
        else:
            rationale = "This looks suitable for careful DIY if you follow the steps and safety guidance."

    why_matters = _clean_str(raw.get("why_safety_matters"), "")

    if not key_obs and normalized.get("material_readout"):
        key_obs = [normalized["material_readout"]]
    if not poss and normalized.get("xray_readout"):
        poss = [normalized["xray_readout"]]
    if not when_stop and normalized.get("when_to_call_pro"):
        for part in re.split(r"[.;•]\s*", normalized["when_to_call_pro"]):
            p = _clean_str(part, "")
            if p and len(when_stop) < 3:
                when_stop.append(p)

    normalized["key_observations"] = key_obs[:4]
    normalized["possible_issues"] = poss[:4]
    normalized["severity_ui"] = sev
    normalized["decision_outcome"] = dec
    normalized["decision_rationale"] = rationale[:480]
    normalized["why_safety_matters"] = why_matters[:320]
    normalized["when_to_stop"] = when_stop[:3]

    rl_raw = _clean_str(
        raw.get("rental_liability_hint") or raw.get("huur_verhuur_hint") or raw.get("landlord_tenant_hint"),
        "",
    ).lower()
    rl_raw = rl_raw.replace("-", "_").replace(" ", "_")
    alias_map = {
        "verhuurder": "landlord_likely",
        "landlord": "landlord_likely",
        "huurder": "tenant_likely",
        "tenant": "tenant_likely",
        "nvt": "not_applicable",
        "n_a": "not_applicable",
        "na": "not_applicable",
        "none": "not_applicable",
        "onzeker": "unclear",
        "unknown": "unclear",
    }
    if rl_raw in alias_map:
        rl_raw = alias_map[rl_raw]
    if rl_raw not in ("not_applicable", "landlord_likely", "tenant_likely", "unclear"):
        rl_raw = "not_applicable"
    rl_note = _clean_str(raw.get("rental_liability_note") or raw.get("huur_verhuur_toelichting"), "",)[:280]
    if rl_raw == "not_applicable":
        rl_note = ""
    if hz_n in ("danger", "emergency"):
        rl_raw = "not_applicable"
        rl_note = ""
    normalized["rental_liability_hint"] = rl_raw
    normalized["rental_liability_note"] = rl_note

    return normalized


def _model_fallback_chain(preferred_model):
    env_override = (os.getenv("ANTHROPIC_MODEL") or "").strip()
    ordered = [
        preferred_model,
        env_override,
        DEFAULT_VISION_MODEL,
        "claude-sonnet-4-5-20250929",
        "claude-3-5-haiku-20241022",
        "claude-3-5-sonnet-20241022",
        "claude-3-5-haiku-latest",
        "claude-3-haiku-20240307",
    ]
    seen = set()
    out = []
    for m in ordered:
        m = (m or "").strip()
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


def _extract_message_text(response):
    """Claude 4.x can return multiple blocks (e.g. non-text first). Join all text blocks."""
    parts = []
    for block in getattr(response, "content", None) or []:
        btype = getattr(block, "type", None)
        if btype == "text":
            parts.append(getattr(block, "text", "") or "")
    out = "".join(parts).strip()
    if out:
        return out
    if getattr(response, "content", None):
        first = response.content[0]
        txt = getattr(first, "text", None)
        if txt:
            return str(txt).strip()
    raise ValueError("AI returned no text content")


def _is_model_selection_error(exc):
    if isinstance(exc, NotFoundError):
        return True
    if isinstance(exc, BadRequestError):
        msg = str(exc).lower()
        return any(
            s in msg
            for s in ("model", "model_id", "invalid model", "unknown model", "does not exist")
        )
    if isinstance(exc, APIStatusError) and getattr(exc, "status_code", None) == 404:
        return True
    msg = str(exc).lower()
    return "model:" in msg and ("not found" in msg or "invalid" in msg)


def _messages_create_with_fallback(system, messages, max_tokens, preferred_model):
    cli = _get_client()
    last_error = None
    for model_name in _model_fallback_chain(preferred_model):
        try:
            return cli.messages.create(
                model=model_name,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
            )
        except (AuthenticationError, PermissionDeniedError, RateLimitError):
            raise
        except Exception as e:
            last_error = e
            if _is_model_selection_error(e):
                continue
            raise
    if last_error:
        raise last_error
    raise RuntimeError("No valid Anthropic model could be selected.")

@app.route("/")
def home():
    return render_template("index.html")


@app.route("/app-build")
def app_build():
    """Optional: frontend uses this to show deploy version in UI."""
    return jsonify({"success": True, "version": os.getenv("APP_VERSION", "dev")})


def _file_to_image_block(upload):
    raw = upload.read()
    if not raw:
        return None
    b64 = base64.b64encode(raw).decode("utf-8")
    mime = upload.mimetype or "image/jpeg"
    return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}}


def _do_analyze():
    try:
        _get_client()
    except RuntimeError as e:
        return None, (str(e), 503)

    image_file = request.files.get("image")
    image_2 = request.files.get("image_2")
    image_3 = request.files.get("image_3")
    question = request.form.get("question", "")
    language = request.form.get("language", "nl")

    if not image_file:
        return None, ("No image provided", 400)

    view_defs = [
        (image_file, "VIEW 1 — CONTEXT: Whole object + a bit of surroundings (orientation)."),
        (image_2, "VIEW 2 — MATERIALS & TYPEPLATE: brands, stickers, couplings, pipe material, wire entry."),
        (image_3, "VIEW 3 — X-RAY / PROBLEM ZONE: close-up; separate dirt/limescale from real damage or leak origin."),
    ]
    blocks = []
    n_views = 0
    for upload, caption in view_defs:
        if not upload:
            continue
        img = _file_to_image_block(upload)
        if not img:
            continue
        blocks.append({"type": "text", "text": caption})
        blocks.append(img)
        n_views += 1

    lang_instruction = "Respond entirely in Dutch (Nederlands). Use Dutch product names (e.g. 'kraan', 'moersleutel', 'Teflon tape')." if language == "nl" else "Respond in English."
    corpus = get_corpus_for_language(language)
    pattern_memory = _build_success_pattern_memory(question, language)
    tri_note = ""
    if n_views >= 3:
        tri_note = (
            "THREE views were taken in order. Merge them: view 1 context, view 2 materials/model, view 3 decides "
            "cleaning/adjustment vs mechanical failure."
        )
    elif n_views == 2:
        tri_note = "Two views: merge wide shot + detail."

    system_prompt = f"""
You are the vision brain behind Chicken Egg: a camera-first app where people photograph real objects at home (or bike)
and get safe, ordered steps to fix, clean, assemble, or simply understand what they are looking at.

Your tone: calm expert who notices details — impressive when the photo allows it, never performative or salesy.

Domain: whole-home repairs — plumbing, fixtures, furniture, witgoed, walls/mounting (gips/kalkzand/beton/spouw),
bicycles and e-bikes, typical NL/EU housing + retail (Gamma, Praxis, Karwei, IKEA, Bol).

Pattern recognition targets: NL appliances (Miele/Bosch/Siemens/AEG/Beko), CV ketels, sanitaire knel/koper/PVC,
EU Schuko low-voltage only (never advise groepenkast / mains work), fiets/e-bike wear and connectors.

INTERNAL REFERENCE (use facts; do not paste this block into JSON answers):
{corpus}

{lang_instruction}

{tri_note}

PROVEN FIELD PATTERNS FROM COMPLETED SUCCESSFUL JOBS (use as priors only if image context matches):
{pattern_memory if pattern_memory else "- none yet"}

Return ONLY valid JSON — no markdown, no code fences.

JSON structure:
{{
  "job_category": "plumbing | electrical_low_voltage | furniture | appliance | walls_surface | bicycle_ebike | other",
  "confidence_tier": "DIY-safe | caution | call-pro",
  "material_readout": "one short sentence: materials/brands/fittings visible — or empty",
  "xray_readout": "one short sentence: dirt vs damage vs adjustment; what the close-up shows — or best single-view",
  "uncertainty_note": "empty or one sentence if ambiguous",
  "quick_checks": ["max 2 short pre-flight checks or empty"],
  "what_i_see": "2–3 short sentences. Sentence 1 = sharp, specific hook (visible brand/type/material/setting). Avoid hedgy filler.",
  "key_observations": ["2–4 short bullets: visible facts only, no diagnosis drama"],
  "possible_issues": ["2–4 short bullets: plausible problems or ambiguities"],
  "severity_ui": "safe_cosmetic | needs_attention | potentially_dangerous",
  "decision_outcome": "safe_to_proceed | proceed_with_caution | do_not_proceed",
  "decision_rationale": "one clear sentence: why that decision",
  "why_safety_matters": "empty or one sentence explaining risk if user ignores guidance",
  "when_to_stop": ["1–3 short stop conditions, e.g. smell gas / sparks / major leak"],
  "rental_liability_hint": "not_applicable | landlord_likely | tenant_likely | unclear",
  "rental_liability_note": "one short sentence: plain-language observation for renters (NL if Dutch user context) — empty if not_applicable",
  "task": "under ~10 words, verb-led, confident action title the user would tap on",
  "difficulty": "easy | medium | hard",
  "estimated_cost": "range or empty",
  "time_needed": "duration",
  "hazard_level": "safe | caution | warning | danger",
  "hazard_note": "or empty",
  "when_to_call_pro": "licensed-work triggers",
  "tools_needed": [],
  "materials_needed": [],
  "steps": [{{"text": "...", "visual_tip": "what the user should see in frame when this step is done"}}],
  "safety_tip": "...",
  "pro_tip": "one sharp insider tip, or empty if none"
}}

Hard rules:
- severity_ui MUST align with hazard_level and decision_outcome (danger or call-pro ⇒ potentially_dangerous + do_not_proceed unless purely informational).
- decision_outcome do_not_proceed: user must not DIY; tell them to stop or call a pro.
- key_observations = facts seen; possible_issues = what could be wrong — keep separate.
- Steps: ordered, actionable, minimal jargon; every step MUST have visual_tip (camera check).
- No made-up part numbers or torque specs unless readable in the image.
- E-bike battery swollen/dented ⇒ call-pro / specialist, never open cells.
- If the scene is ambiguous, lower confidence, fill uncertainty_note, and avoid overclaiming.
- rental_liability_hint: ONLY for rental-relevant issues (moisture, mold, stains, minor leaks, wall/ceiling damage, window condensation patterns). Use not_applicable for bikes, appliances with no rental context, outdoor-only, or purely cosmetic owned-home DIY. This is NOT legal advice — phrase as \"based on what is visible\" / \"lijkt op basis van de foto\".
- Return ONLY the JSON object.
"""

    q = (question or "").strip()
    suffix = f" ({n_views} beeld(en))." if language == "nl" else f" ({n_views} image(s))."
    default_ask = (
        "Chicken Egg: identificeer scherp wat er op de foto staat en geef het beste fix-/schoonmaak-/montageplan."
        if language == "nl"
        else "Chicken Egg: identify what's in the photo and give the best fix, clean, or assembly plan."
    )
    user_tail = {"type": "text", "text": (f"User note: {q}" if q else default_ask) + suffix}
    user_content = blocks + [user_tail]

    response = _messages_create_with_fallback(
        system=system_prompt,
        max_tokens=2800,
        preferred_model=VISION_MODEL,
        messages=[{"role": "user", "content": user_content}],
    )

    ai_text = _extract_message_text(response)
    if ai_text.startswith("```"):
        ai_text = ai_text.split("```")[1]
        if ai_text.startswith("json"):
            ai_text = ai_text[4:]
    ai_text = ai_text.strip()
    try:
        parsed = json.loads(ai_text)
    except Exception:
        # Try to recover if the model wrapped JSON with extra text.
        start = ai_text.find("{")
        end = ai_text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        parsed = json.loads(ai_text[start : end + 1])
    result = _normalize_result(parsed)
    # Localize guidance and enforce final server-side hard-stop policy.
    if result.get("needs_retake"):
        if language == "nl":
            result["retake_guidance"] = (
                "Ik kan dit nog niet betrouwbaar beoordelen. Maak een scherpere foto van dichterbij met beter licht."
            )
        else:
            result["retake_guidance"] = (
                "I cannot assess this reliably yet. Please retake a closer, sharper photo in better lighting."
            )
    result, hard_stop_triggers = _apply_server_hard_stop(result, question, language)
    if hard_stop_triggers:
        result["hard_stop_triggered"] = True
        result["hard_stop_reasons"] = hard_stop_triggers[:6]
        result["severity_ui"] = "potentially_dangerous"
        result["decision_outcome"] = "do_not_proceed"
        result["rental_liability_hint"] = "not_applicable"
        result["rental_liability_note"] = ""
        hn = _clean_str(result.get("hazard_note"), "")
        result["decision_rationale"] = hn or (
            "Server safety filter: this topic requires a licensed professional."
            if language != "nl"
            else "Server-veiligheidsfilter: dit onderwerp vereist een erkende professional."
        )
    else:
        result["hard_stop_triggered"] = False
        result["hard_stop_reasons"] = []
    return {"success": True, "result": result}, None


def _do_stage1_quick_analyze():
    """Fast first-pass summary to improve perceived speed before full analysis finishes."""
    try:
        _get_client()
    except RuntimeError as e:
        return None, (str(e), 503)

    image_file = request.files.get("image")
    question = request.form.get("question", "")
    language = request.form.get("language", "nl")
    if not image_file:
        return None, ("No image provided", 400)

    img = _file_to_image_block(image_file)
    if not img:
        return None, ("Invalid image data", 400)

    lang_instruction = "Respond in Dutch." if language == "nl" else "Respond in English."
    prompt = f"""You are Stage-1 fast scene triage for a camera-first repair assistant.
{lang_instruction}

Return ONLY valid JSON:
{{
  "quick_label": "very short object/task label (2-5 words)",
  "risk_level": "safe|caution|danger",
  "confidence": "high|medium|low",
  "needs_retake": true,
  "retake_guidance": "one short sentence, empty if not needed",
  "quick_action": "single short immediate next action for user"
}}

Rules:
- Be concise and practical.
- If blurry/unclear, set confidence low and needs_retake true.
- For high-risk electrical mains/gas hints, set risk_level danger.
- No markdown, no extra text; JSON only."""

    user_tail = {
        "type": "text",
        "text": f"User note: {_clean_small_str(question, 200) or 'Quick first-pass triage only.'}",
    }
    response = _messages_create_with_fallback(
        system=prompt,
        max_tokens=220,
        preferred_model=CHECK_PROGRESS_MODEL,
        messages=[{"role": "user", "content": [img, user_tail]}],
    )
    ai_text = _extract_message_text(response).strip()
    if ai_text.startswith("```"):
        ai_text = ai_text.split("```")[1]
        if ai_text.startswith("json"):
            ai_text = ai_text[4:]
    ai_text = ai_text.strip()
    try:
        parsed = json.loads(ai_text)
    except Exception:
        start = ai_text.find("{")
        end = ai_text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise
        parsed = json.loads(ai_text[start : end + 1])

    result = {
        "quick_label": _clean_small_str(parsed.get("quick_label"), 60) or ("Object check" if language != "nl" else "Object check"),
        "risk_level": _clean_small_str(parsed.get("risk_level"), 20).lower() or "caution",
        "confidence": _clean_small_str(parsed.get("confidence"), 20).lower() or "medium",
        "needs_retake": bool(parsed.get("needs_retake", False)),
        "retake_guidance": _clean_small_str(parsed.get("retake_guidance"), 180),
        "quick_action": _clean_small_str(parsed.get("quick_action"), 180),
    }
    if result["risk_level"] not in {"safe", "caution", "danger"}:
        result["risk_level"] = "caution"
    if result["confidence"] not in {"high", "medium", "low"}:
        result["confidence"] = "medium"
    if not result["quick_action"]:
        result["quick_action"] = (
            "Take one sharper close-up."
            if language != "nl"
            else "Maak een scherpere close-up."
        )
    if result["needs_retake"] and not result["retake_guidance"]:
        result["retake_guidance"] = (
            "I need a sharper, closer photo with better light."
            if language != "nl"
            else "Ik heb een scherpere foto van dichterbij met beter licht nodig."
        )

    # Keep safety posture consistent with final hard-stop policy.
    hard_stop_probe = {
        "what_i_see": result["quick_label"],
        "task": result["quick_action"],
        "hazard_note": result["retake_guidance"],
    }
    safe_result, triggers = _apply_server_hard_stop(hard_stop_probe, question, language)
    if triggers:
        result["risk_level"] = "danger"
        result["confidence"] = "low"
        result["needs_retake"] = False
        result["quick_action"] = safe_result.get("task") or result["quick_action"]
        result["retake_guidance"] = safe_result.get("hazard_note") or result["retake_guidance"]
        result["hard_stop_triggered"] = True
        result["hard_stop_reasons"] = triggers[:6]
    else:
        result["hard_stop_triggered"] = False
        result["hard_stop_reasons"] = []

    return {"success": True, "result": result}, None


@app.route("/analyze", methods=["POST"])
def analyze():
    try:
        payload, err = _do_analyze()
        if err:
            return jsonify({"success": False, "error": err[0]}), err[1]
        try:
            result = (payload or {}).get("result") or {}
            _insert_event(
                {
                    "event_raw": "scan_completed",
                    "event_name": "scan_completed",
                    "language": _clean_small_str(request.form.get("language"), 12),
                    "session_id": _clean_small_str(request.form.get("session_id"), 80),
                    "user_id": _clean_small_str(request.form.get("user_id"), 80),
                    "job_id": _clean_small_str(request.form.get("job_id"), 80),
                    "task_category": _clean_small_str(result.get("job_category"), 60),
                    "hazard_level": _clean_small_str(result.get("hazard_level"), 20).lower(),
                    "source_channel": _clean_small_str(request.form.get("source_channel"), 40),
                    "meta_json": json.dumps({"from": "analyze"}, ensure_ascii=False),
                    "ip": _request_ip(),
                    "user_agent": _clean_small_str(request.headers.get("User-Agent"), 260),
                }
            )
            hz = _clean_small_str(result.get("hazard_level"), 20).lower()
            if hz in {"caution", "warning", "danger", "emergency"}:
                _insert_event(
                    {
                        "event_raw": "hazard_flagged",
                        "event_name": "hazard_flagged",
                        "language": _clean_small_str(request.form.get("language"), 12),
                        "session_id": _clean_small_str(request.form.get("session_id"), 80),
                        "user_id": _clean_small_str(request.form.get("user_id"), 80),
                        "job_id": _clean_small_str(request.form.get("job_id"), 80),
                        "task_category": _clean_small_str(result.get("job_category"), 60),
                        "hazard_level": hz,
                        "source_channel": _clean_small_str(request.form.get("source_channel"), 40),
                        "meta_json": json.dumps({"from": "analyze"}, ensure_ascii=False),
                        "ip": _request_ip(),
                        "user_agent": _clean_small_str(request.headers.get("User-Agent"), 260),
                    }
                )
        except Exception:
            pass
        return jsonify(payload)
    except json.JSONDecodeError as e:
        return jsonify({"success": False, "error": f"AI returned invalid JSON: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/analyze-stage1", methods=["POST"])
def analyze_stage1():
    """Fast first-pass response for perceived speed; full result comes from /analyze."""
    try:
        payload, err = _do_stage1_quick_analyze()
        if err:
            return jsonify({"success": False, "error": err[0]}), err[1]
        return jsonify(payload)
    except json.JSONDecodeError as e:
        return jsonify({"success": False, "error": f"AI returned invalid JSON: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/analyze-live", methods=["POST"])
def analyze_live():
    """Same vision analysis as /analyze; frontend expects multi-view hints in `live`."""
    try:
        payload, err = _do_analyze()
        if err:
            return jsonify({"success": False, "error": err[0]}), err[1]
        payload["live"] = {"needs_more_views": False, "next_prompt": ""}
        try:
            result = (payload or {}).get("result") or {}
            _insert_event(
                {
                    "event_raw": "scan_completed",
                    "event_name": "scan_completed",
                    "language": _clean_small_str(request.form.get("language"), 12),
                    "session_id": _clean_small_str(request.form.get("session_id"), 80),
                    "user_id": _clean_small_str(request.form.get("user_id"), 80),
                    "job_id": _clean_small_str(request.form.get("job_id"), 80),
                    "task_category": _clean_small_str(result.get("job_category"), 60),
                    "hazard_level": _clean_small_str(result.get("hazard_level"), 20).lower(),
                    "source_channel": _clean_small_str(request.form.get("source_channel"), 40),
                    "meta_json": json.dumps({"from": "analyze_live"}, ensure_ascii=False),
                    "ip": _request_ip(),
                    "user_agent": _clean_small_str(request.headers.get("User-Agent"), 260),
                }
            )
            hz = _clean_small_str(result.get("hazard_level"), 20).lower()
            if hz in {"caution", "warning", "danger", "emergency"}:
                _insert_event(
                    {
                        "event_raw": "hazard_flagged",
                        "event_name": "hazard_flagged",
                        "language": _clean_small_str(request.form.get("language"), 12),
                        "session_id": _clean_small_str(request.form.get("session_id"), 80),
                        "user_id": _clean_small_str(request.form.get("user_id"), 80),
                        "job_id": _clean_small_str(request.form.get("job_id"), 80),
                        "task_category": _clean_small_str(result.get("job_category"), 60),
                        "hazard_level": hz,
                        "source_channel": _clean_small_str(request.form.get("source_channel"), 40),
                        "meta_json": json.dumps({"from": "analyze_live"}, ensure_ascii=False),
                        "ip": _request_ip(),
                        "user_agent": _clean_small_str(request.headers.get("User-Agent"), 260),
                    }
                )
        except Exception:
            pass
        return jsonify(payload)
    except json.JSONDecodeError as e:
        return jsonify({"success": False, "error": f"AI returned invalid JSON: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/live-label", methods=["POST"])
def live_label():
    """Fast object-only label for live camera HUD (short, readable)."""
    try:
        try:
            _get_client()
        except RuntimeError as e:
            return jsonify({"success": False, "error": str(e)}), 503

        image_file = request.files.get("image")
        language = _clean_str(request.form.get("language"), "en").lower()
        if not image_file:
            return jsonify({"success": False, "error": "No image provided"}), 400

        image_base64 = base64.b64encode(image_file.read()).decode("utf-8")
        mime_type = image_file.mimetype or "image/jpeg"

        lang_instruction = "Respond in Dutch." if language == "nl" else "Respond in English."
        prompt = f"""You are a fast live camera labeler for a home-fix app.
{lang_instruction}

Goal: Return one SHORT object label for what is most central in frame, plus confidence.

Allowed labels (English): chair, tv, window, laptop, bottle, cable, pan, airfryer, sink, faucet, door, hinge, shelf, pipe, radiator, object
Allowed labels (Dutch): stoel, tv, raam, laptop, fles, kabel, pan, airfryer, wasbak, kraan, deur, scharnier, plank, pijp, radiator, object

Rules:
- Return ONLY valid JSON with this exact shape:
  {{"label":"<label>","confidence":"high|medium|low"}}
- No brands.
- Prefer generic if uncertain.
- Never output materials/conditions.
"""

        response = _messages_create_with_fallback(
            preferred_model=CHECK_PROGRESS_MODEL,
            max_tokens=60,
            system=prompt,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": mime_type,
                                "data": image_base64,
                            },
                        },
                        {"type": "text", "text": "Center object label + confidence JSON only."},
                    ],
                }
            ],
        )

        ai_text = _extract_message_text(response)
        if ai_text.startswith("```"):
            ai_text = ai_text.split("```")[1]
            if ai_text.startswith("json"):
                ai_text = ai_text[4:]
        ai_text = ai_text.strip()
        try:
            parsed = json.loads(ai_text)
        except Exception:
            start = ai_text.find("{")
            end = ai_text.rfind("}")
            if start != -1 and end != -1 and end > start:
                parsed = json.loads(ai_text[start : end + 1])
            else:
                parsed = {"label": ai_text, "confidence": "low"}

        label = _clean_str(parsed.get("label"), "object").lower()
        label = re.sub(r"[^a-zA-ZÀ-ÿ0-9_\-\s]", "", label).strip()
        if not label:
            label = "object"
        if " " in label:
            label = label.split(" ")[0]
        conf = _clean_str(parsed.get("confidence"), "low").lower()
        if conf not in {"high", "medium", "low"}:
            conf = "low"

        return jsonify({"success": True, "label": label, "confidence": conf})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/check-progress", methods=["POST"])
def check_progress():
    try:
        try:
            _get_client()
        except RuntimeError as e:
            return jsonify({"success": False, "error": str(e)}), 503

        image_file = request.files.get("image")
        step       = request.form.get("step", "")
        task       = request.form.get("task", "")
        language   = request.form.get("language", "nl")

        if not image_file:
            return jsonify({"success": False, "error": "No image provided"}), 400

        image_base64 = base64.b64encode(image_file.read()).decode("utf-8")
        mime_type    = image_file.mimetype or "image/jpeg"
        lang_instruction = "Respond in Dutch." if language == "nl" else "Respond in English."

        pr_sys = f"""You are Chicken Egg's progress coach: users send a photo mid-repair while following step-by-step guidance.
{lang_instruction}
Compare the photo to the CURRENT STEP. Does the work-in-progress plausibly match what they should be doing right now?

Return ONLY JSON:
{{
  "danger_level": "safe | caution | danger | emergency",
  "step_match": "yes | no | unclear",
  "progress_feedback": "one clear sentence — what you see and whether it matches the step",
  "next_hint": "empty string, OR one short hint if step_match is no or unclear (what to adjust or photograph)"
}}

Rules:
- emergency: fire, major gas, flood, exposed live conductors user could touch.
- danger: unsafe situation that should stop work (not yet emergency).
- step_match "yes" only if the photo is consistent with completing this step safely; "unclear" if image is too tight/blurry.
- Be direct. No part numbers unless visible in the photo."""
        response = _messages_create_with_fallback(
            preferred_model=CHECK_PROGRESS_MODEL,
            max_tokens=400,
            system=pr_sys,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": image_base64}},
                    {"type": "text", "text": f"Task: {task}\nCurrent step (full text): {step}"},
                ],
            }],
        )

        ai_text = _extract_message_text(response)
        if ai_text.startswith("```"):
            ai_text = ai_text.split("```")[1]
            if ai_text.startswith("json"):
                ai_text = ai_text[4:]
        ai_text = ai_text.strip()
        try:
            result = json.loads(ai_text)
        except Exception:
            start = ai_text.find("{")
            end = ai_text.rfind("}")
            if start == -1 or end == -1 or end <= start:
                raise
            result = json.loads(ai_text[start : end + 1])
        if "danger_level" not in result and isinstance(result, dict):
            dl = result.get("risk") or result.get("level")
            if dl:
                result = {**result, "danger_level": dl}
        return jsonify({"success": True, "result": result})

    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/collect-email", methods=["POST"])
def collect_email():
    try:
        data = request.json or {}
        email = _clean_small_str(data.get("email"), 180).lower()
        if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            return jsonify({"success": False, "error": "invalid email"}), 400
        with _db() as conn:
            conn.execute(
                """
                INSERT INTO emails (
                    created_at, email, language, source_channel, session_id, user_id, job_id, ip
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _utc_now_iso(),
                    email,
                    _clean_small_str(data.get("language"), 12),
                    _clean_small_str(data.get("source_channel"), 40),
                    _clean_small_str(data.get("session_id"), 80),
                    _clean_small_str(data.get("user_id"), 80),
                    _clean_small_str(data.get("job_id"), 80),
                    _request_ip(),
                ),
            )
            conn.commit()
        _insert_event(
            {
                "event_raw": "email_collected",
                "event_name": "email_collected",
                "language": _clean_small_str(data.get("language"), 12),
                "session_id": _clean_small_str(data.get("session_id"), 80),
                "user_id": _clean_small_str(data.get("user_id"), 80),
                "job_id": _clean_small_str(data.get("job_id"), 80),
                "task_category": _clean_small_str(data.get("task_category"), 60),
                "hazard_level": _clean_small_str(data.get("hazard_level"), 20).lower(),
                "source_channel": _clean_small_str(data.get("source_channel"), 40),
                "meta_json": json.dumps({"email_domain": email.split("@")[-1]}, ensure_ascii=False),
                "ip": _request_ip(),
                "user_agent": _clean_small_str(request.headers.get("User-Agent"), 260),
            }
        )
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/track-click", methods=["POST"])
def track_click():
    try:
        data = request.json or {}
        tool = data.get("tool") or data.get("tools") or ""
        print(f"Store click: {data.get('store','')} for '{tool}'")
        _insert_event(
            {
                "event_raw": "tool_link_clicked",
                "event_name": "tool_link_clicked",
                "language": _clean_small_str(data.get("language"), 12),
                "session_id": _clean_small_str(data.get("session_id"), 80),
                "user_id": _clean_small_str(data.get("user_id"), 80),
                "job_id": _clean_small_str(data.get("job_id"), 80),
                "task_category": _clean_small_str(data.get("task_category"), 60),
                "hazard_level": _clean_small_str(data.get("hazard_level"), 20).lower(),
                "source_channel": _clean_small_str(data.get("source_channel"), 40),
                "meta_json": json.dumps(
                    {
                        "store": _clean_small_str(data.get("store"), 40),
                        "tool": _clean_small_str(tool, 120),
                    },
                    ensure_ascii=False,
                ),
                "ip": _request_ip(),
                "user_agent": _clean_small_str(request.headers.get("User-Agent"), 260),
            }
        )
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/collect-feedback", methods=["POST"])
def collect_feedback():
    try:
        data = request.json or {}
        print(f"Feedback: rating={data.get('rating')} notes={data.get('notes','')[:80]!r}")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/track-event", methods=["POST"])
def track_event():
    try:
        data = request.json or {}
        payload, err = _resolve_event_payload(data)
        if err:
            return jsonify({"success": False, "error": err}), 400
        _insert_event(payload)
        return jsonify({"success": True, "event": payload["event_name"]})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/submit-outcome", methods=["POST"])
def submit_outcome():
    """Store post-job outcome to build proprietary winning-pattern memory."""
    try:
        data = request.json or {}
        result = data.get("result") or {}
        success = bool(data.get("success", False))

        payload = {
            "success": success,
            "rating": _clean_small_str(data.get("rating"), 40),
            "reason": _clean_small_str(data.get("reason"), 240),
            "language": _clean_small_str(data.get("language"), 12),
            "session_id": _clean_small_str(data.get("session_id"), 80),
            "user_id": _clean_small_str(data.get("user_id"), 80),
            "job_id": _clean_small_str(data.get("job_id"), 80),
            "source_channel": _clean_small_str(data.get("source_channel"), 40),
            "task_category": _clean_small_str(result.get("job_category") or data.get("task_category"), 60),
            "task_text": _clean_small_str(result.get("task") or data.get("task_text"), 240),
            "what_i_see": _clean_small_str(result.get("what_i_see"), 240),
            "hazard_level": _clean_small_str(result.get("hazard_level") or data.get("hazard_level"), 20).lower(),
            "steps": result.get("steps") if isinstance(result.get("steps"), list) else [],
            "tools": result.get("tools_needed") if isinstance(result.get("tools_needed"), list) else [],
            "materials": result.get("materials_needed") if isinstance(result.get("materials_needed"), list) else [],
        }
        _insert_outcome(payload)

        # Also track standardized completion event for KPI consistency.
        _insert_event(
            {
                "event_raw": "job_completed",
                "event_name": "job_completed",
                "language": payload["language"],
                "session_id": payload["session_id"],
                "user_id": payload["user_id"],
                "job_id": payload["job_id"],
                "task_category": payload["task_category"],
                "hazard_level": payload["hazard_level"],
                "source_channel": payload["source_channel"],
                "meta_json": json.dumps(
                    {
                        "success": success,
                        "rating": payload["rating"],
                        "reason": payload["reason"],
                    },
                    ensure_ascii=False,
                ),
                "ip": _request_ip(),
                "user_agent": _clean_small_str(request.headers.get("User-Agent"), 260),
            }
        )
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/metrics")
def metrics():
    """Simple KPI snapshot for product and investor updates."""
    try:
        try:
            days = int(request.args.get("days", 7))
        except Exception:
            days = 7
        include_test = str(request.args.get("include_test", "0")).strip().lower() in {"1", "true", "yes"}
        days = max(1, min(days, 90))
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

        day_expr = _sql_day_expr("created_at")
        events_filter_sql, events_filter_params = _metrics_filter_sql(include_test)
        with _db() as conn:
            rows = conn.execute(
                f"""
                SELECT event_name, COUNT(*) AS c
                FROM events
                WHERE created_at >= ? {events_filter_sql}
                GROUP BY event_name
                """,
                (since, *events_filter_params),
            ).fetchall()
            counts = {r["event_name"]: int(r["c"]) for r in rows}

            unique_scan_users = conn.execute(
                f"""
                SELECT COUNT(DISTINCT COALESCE(NULLIF(user_id,''), NULLIF(session_id,''), NULLIF(ip,''))) AS c
                FROM events
                WHERE created_at >= ? AND event_name = 'scan_completed' {events_filter_sql}
                """,
                (since, *events_filter_params),
            ).fetchone()["c"]

            wa_users = conn.execute(
                f"""
                SELECT COUNT(DISTINCT COALESCE(NULLIF(user_id,''), NULLIF(session_id,''), NULLIF(ip,''))) AS c
                FROM events
                WHERE created_at >= ? {events_filter_sql}
                """,
                (since, *events_filter_params),
            ).fetchone()["c"]

            # Repeat proxy: users with scan_completed on >=2 distinct dates in window.
            repeat_users = conn.execute(
                f"""
                SELECT COUNT(*) AS c FROM (
                    SELECT COALESCE(NULLIF(user_id,''), NULLIF(session_id,''), NULLIF(ip,'')) AS u,
                           COUNT(DISTINCT {day_expr}) AS d
                    FROM events
                    WHERE created_at >= ? AND event_name = 'scan_completed' {events_filter_sql}
                    GROUP BY u
                    HAVING COUNT(DISTINCT {day_expr}) >= 2
                )
                """,
                (since, *events_filter_params),
            ).fetchone()["c"]

        scans = counts.get("scan_completed", 0)
        jobs = counts.get("job_completed", 0)
        emails = counts.get("email_collected", 0)
        ctas = counts.get("cta_clicked", 0)
        hazards = counts.get("hazard_flagged", 0)
        tool_clicks = counts.get("tool_link_clicked", 0)

        def pct(n, d):
            return round((100.0 * n / d), 2) if d else 0.0

        return jsonify(
            {
                "success": True,
                "window_days": days,
                "include_test": include_test,
                "counts": counts,
                "kpis": {
                    "weekly_active_users_proxy": int(wa_users or 0),
                    "unique_scan_users": int(unique_scan_users or 0),
                    "scan_completion_rate_pct": pct(scans, counts.get("scan_started", 0)),
                    "job_completion_rate_pct": pct(jobs, scans),
                    "scan_to_email_cvr_pct": pct(emails, scans),
                    "cta_click_rate_pct": pct(ctas, scans),
                    "hazard_flag_rate_pct": pct(hazards, scans),
                    "tool_click_rate_pct": pct(tool_clicks, scans),
                    "repeat_user_rate_proxy_pct": pct(int(repeat_users or 0), int(unique_scan_users or 0)),
                },
                "required_events": sorted(REQUIRED_EVENTS),
            }
        )
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/dashboard")
def dashboard():
    """Founder-facing live dashboard UI."""
    return render_template("dashboard.html")


@app.route("/metrics-detail")
def metrics_detail():
    """Detailed KPI payload for live dashboard and funding updates."""
    try:
        try:
            days = int(request.args.get("days", 14))
        except Exception:
            days = 14
        include_test = str(request.args.get("include_test", "0")).strip().lower() in {"1", "true", "yes"}
        days = max(3, min(days, 90))
        now_utc = datetime.now(timezone.utc)
        since = (now_utc - timedelta(days=days)).isoformat()
        prev_since = (now_utc - timedelta(days=days * 2)).isoformat()

        def pct(n, d):
            return round((100.0 * n / d), 2) if d else 0.0

        day_expr = _sql_day_expr("created_at")
        events_filter_sql, events_filter_params = _metrics_filter_sql(include_test)
        emails_filter_sql, emails_filter_params = _metrics_filter_sql(include_test)
        with _db() as conn:
            rows = conn.execute(
                f"""
                SELECT event_name, COUNT(*) AS c
                FROM events
                WHERE created_at >= ? {events_filter_sql}
                GROUP BY event_name
                """,
                (since, *events_filter_params),
            ).fetchall()
            counts = {r["event_name"]: int(r["c"]) for r in rows}

            day_rows = conn.execute(
                f"""
                SELECT {day_expr} AS day, event_name, COUNT(*) AS c
                FROM events
                WHERE created_at >= ? {events_filter_sql}
                  AND event_name IN ('scan_started','scan_completed','job_completed','email_collected','founding_offer_clicked')
                GROUP BY day, event_name
                ORDER BY day ASC
                """,
                (since, *events_filter_params),
            ).fetchall()

            top_categories = conn.execute(
                f"""
                SELECT task_category, COUNT(*) AS c
                FROM events
                WHERE created_at >= ? {events_filter_sql}
                  AND event_name = 'scan_completed'
                  AND COALESCE(task_category, '') <> ''
                GROUP BY task_category
                ORDER BY c DESC
                LIMIT 8
                """,
                (since, *events_filter_params),
            ).fetchall()

            top_channels = conn.execute(
                f"""
                SELECT source_channel, COUNT(*) AS c
                FROM events
                WHERE created_at >= ? {events_filter_sql}
                  AND COALESCE(source_channel, '') <> ''
                GROUP BY source_channel
                ORDER BY c DESC
                LIMIT 8
                """,
                (since, *events_filter_params),
            ).fetchall()

            language_mix = conn.execute(
                f"""
                SELECT language, COUNT(*) AS c
                FROM events
                WHERE created_at >= ? {events_filter_sql}
                  AND COALESCE(language, '') <> ''
                GROUP BY language
                ORDER BY c DESC
                """,
                (since, *events_filter_params),
            ).fetchall()

            recent_events = conn.execute(
                f"""
                SELECT created_at, event_name, task_category, hazard_level, source_channel, language, session_id
                FROM events
                WHERE 1=1 {events_filter_sql}
                ORDER BY id DESC
                LIMIT 25
                """,
                (*events_filter_params,),
            ).fetchall()

            email_by_day = conn.execute(
                f"""
                SELECT {day_expr} AS day, COUNT(*) AS c
                FROM emails
                WHERE created_at >= ? {emails_filter_sql}
                GROUP BY day
                ORDER BY day ASC
                """,
                (since, *emails_filter_params),
            ).fetchall()

            unique_scan_users = int(
                conn.execute(
                    f"""
                    SELECT COUNT(DISTINCT COALESCE(NULLIF(user_id,''), NULLIF(session_id,''), NULLIF(ip,''))) AS c
                    FROM events
                    WHERE created_at >= ? AND event_name = 'scan_completed' {events_filter_sql}
                    """,
                    (since, *events_filter_params),
                ).fetchone()["c"]
                or 0
            )

            repeat_users = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*) AS c FROM (
                        SELECT COALESCE(NULLIF(user_id,''), NULLIF(session_id,''), NULLIF(ip,'')) AS u,
                               COUNT(DISTINCT {day_expr}) AS d
                        FROM events
                        WHERE created_at >= ? AND event_name = 'scan_completed' {events_filter_sql}
                        GROUP BY u
                        HAVING COUNT(DISTINCT {day_expr}) >= 2
                    )
                    """,
                    (since, *events_filter_params),
                ).fetchone()["c"]
                or 0
            )

            # Current vs previous same-length window for momentum view.
            current_scans = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*) AS c
                    FROM events
                    WHERE created_at >= ? AND event_name = 'scan_completed' {events_filter_sql}
                    """,
                    (since, *events_filter_params),
                ).fetchone()["c"]
                or 0
            )
            prev_scans = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*) AS c
                    FROM events
                    WHERE created_at >= ? AND created_at < ? AND event_name = 'scan_completed' {events_filter_sql}
                    """,
                    (prev_since, since, *events_filter_params),
                ).fetchone()["c"]
                or 0
            )
            current_emails = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*) AS c
                    FROM events
                    WHERE created_at >= ? AND event_name = 'email_collected' {events_filter_sql}
                    """,
                    (since, *events_filter_params),
                ).fetchone()["c"]
                or 0
            )
            prev_emails = int(
                conn.execute(
                    f"""
                    SELECT COUNT(*) AS c
                    FROM events
                    WHERE created_at >= ? AND created_at < ? AND event_name = 'email_collected' {events_filter_sql}
                    """,
                    (prev_since, since, *events_filter_params),
                ).fetchone()["c"]
                or 0
            )

        scans_started = counts.get("scan_started", 0)
        scans_completed = counts.get("scan_completed", 0)
        jobs_completed = counts.get("job_completed", 0)
        emails_collected = counts.get("email_collected", 0)
        offer_clicks = counts.get("founding_offer_clicked", 0)
        hazards = counts.get("hazard_flagged", 0)

        scan_start_base = scans_started if scans_started > 0 else scans_completed

        trend_map = {}
        for row in day_rows:
            day = row["day"]
            if day not in trend_map:
                trend_map[day] = {
                    "day": day,
                    "scan_started": 0,
                    "scan_completed": 0,
                    "job_completed": 0,
                    "email_collected": 0,
                    "founding_offer_clicked": 0,
                }
            trend_map[day][row["event_name"]] = int(row["c"])

        trend = [trend_map[k] for k in sorted(trend_map.keys())]
        total_events = sum(counts.values())
        scans_per_day = round(scans_completed / days, 2) if days else float(scans_completed)

        # Lightweight "founder execution score" to track momentum.
        # Weighting favors meaningful product usage and conversion quality.
        scan_to_job = pct(jobs_completed, scans_completed)
        scan_to_email = pct(emails_collected, scans_completed)
        repeat_rate = pct(repeat_users, unique_scan_users)
        scan_volume_score = min(100.0, scans_per_day * 10.0)  # 10 scans/day == full score
        live_score = round(
            0.40 * scan_to_job
            + 0.25 * scan_to_email
            + 0.20 * repeat_rate
            + 0.15 * scan_volume_score,
            2,
        )

        def growth(cur, prev):
            if prev <= 0:
                return 100.0 if cur > 0 else 0.0
            return round(((cur - prev) / prev) * 100.0, 2)

        return jsonify(
            {
                "success": True,
                "as_of_utc": now_utc.isoformat(),
                "window_days": days,
                "include_test": include_test,
                "required_events": sorted(REQUIRED_EVENTS),
                "counts": counts,
                "kpis": {
                    "live_score": live_score,
                    "unique_scan_users": unique_scan_users,
                    "repeat_user_rate_proxy_pct": repeat_rate,
                    "scan_start_to_complete_pct": pct(scans_completed, scan_start_base),
                    "scan_to_job_completion_pct": scan_to_job,
                    "scan_to_email_cvr_pct": scan_to_email,
                    "scan_to_offer_click_pct": pct(offer_clicks, scans_completed),
                    "hazard_flag_rate_pct": pct(hazards, scans_completed),
                    "scans_per_day": scans_per_day,
                },
                "momentum": {
                    "scan_growth_pct": growth(current_scans, prev_scans),
                    "email_growth_pct": growth(current_emails, prev_emails),
                    "current_window_scans": current_scans,
                    "previous_window_scans": prev_scans,
                    "current_window_emails": current_emails,
                    "previous_window_emails": prev_emails,
                },
                "funnel": [
                    {"stage": "scan_started", "count": scans_started},
                    {"stage": "scan_completed", "count": scans_completed},
                    {"stage": "job_completed", "count": jobs_completed},
                    {"stage": "email_collected", "count": emails_collected},
                    {"stage": "founding_offer_clicked", "count": offer_clicks},
                ],
                "trend_daily": trend,
                "email_daily": [{"day": r["day"], "count": int(r["c"])} for r in email_by_day],
                "top_task_categories": [{"name": r["task_category"], "count": int(r["c"])} for r in top_categories],
                "top_source_channels": [{"name": r["source_channel"], "count": int(r["c"])} for r in top_channels],
                "language_mix": [{"language": r["language"], "count": int(r["c"])} for r in language_mix],
                "event_coverage": {
                    event_name: bool(counts.get(event_name, 0))
                    for event_name in sorted(REQUIRED_EVENTS)
                },
                "total_events_in_window": int(total_events),
                "recent_events": [
                    {
                        "created_at": r["created_at"],
                        "event_name": r["event_name"],
                        "task_category": r["task_category"] or "",
                        "hazard_level": r["hazard_level"] or "",
                        "source_channel": r["source_channel"] or "",
                        "language": r["language"] or "",
                        "session_id": (r["session_id"] or "")[:18],
                    }
                    for r in recent_events
                ],
            }
        )
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/privacy")
def privacy():
    """Dutch-first privacy statement (AVG-oriented); EN copy on same page."""
    return render_template("privacy.html")


@app.route("/health")
def health():
    """Quick prod sanity check: no secrets returned."""
    key_ok = bool(os.getenv("ANTHROPIC_API_KEY", "").strip())
    return jsonify(
        {
            "ok": True,
            "anthropic_api_key_configured": key_ok,
            "vision_model": VISION_MODEL,
            "check_model": CHECK_PROGRESS_MODEL,
            "default_vision_if_env_unset": DEFAULT_VISION_MODEL,
            "db_backend": DB_BACKEND,
        }
    )


if __name__ == "__main__":
    _port = int(os.getenv("PORT", "5000"))
    _debug = os.getenv("FLASK_DEBUG", "1").strip().lower() in ("1", "true", "yes")
    app.run(debug=_debug, port=_port)
