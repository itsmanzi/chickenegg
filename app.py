import os
import re
import base64
import json
import sqlite3
import threading
import time
import hmac
import hashlib
from datetime import datetime, timedelta, timezone
from flask import Flask, request, jsonify, render_template, Response, stream_with_context, make_response, send_from_directory
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


def _load_local_dotenv():
    """Load env files from app root into os.environ. Shell vars always win (never overwritten).

    Files (in order; later files override earlier for the same key): `.env`, `.env.local`, `secrets.env`.
    These files are optional; production (e.g. Vercel) uses dashboard env instead.
    """
    root = os.path.dirname(os.path.abspath(__file__))
    merged = {}

    def absorb(path):
        try:
            with open(path, encoding="utf-8") as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#"):
                        continue
                    if line.startswith("export "):
                        line = line[7:].strip()
                    if "=" not in line:
                        continue
                    key, _, val = line.partition("=")
                    key = key.strip()
                    val = val.strip()
                    if len(val) >= 2 and val[0] == val[-1] and val[0] in "'\"":
                        val = val[1:-1]
                    if key:
                        merged[key] = val
        except OSError:
            pass

    for name in (".env", ".env.local", "secrets.env"):
        path = os.path.join(root, name)
        if os.path.isfile(path):
            absorb(path)

    for key, val in merged.items():
        if key not in os.environ:
            os.environ[key] = val


_load_local_dotenv()
app = Flask(__name__)

# ── DASHBOARD AUTH ──────────────────────────────────────────────────────────
# Set DASHBOARD_TOKEN in production. If unset, dashboards stay open (dev only).
DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "").strip()


def _check_dashboard_auth():
    """Return (ok, error_response). Protects /dashboard and /metrics* routes."""
    if not DASHBOARD_TOKEN:
        return True, None
    tok = (
        request.args.get("token")
        or request.headers.get("X-Dashboard-Token")
        or ""
    ).strip()
    if not hmac.compare_digest(tok, DASHBOARD_TOKEN):
        return False, (jsonify({"error": "Unauthorized"}), 401)
    return True, None


# ── /analyze RATE LIMITER (in-memory, per-IP) ───────────────────────────────
_analyze_rate_lock = threading.Lock()
_analyze_rate_buckets: dict = {}  # ip -> [timestamps]
RATE_LIMIT_ANALYZE = int(os.getenv("RATE_LIMIT_ANALYZE", "20"))  # reqs per minute per IP
MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_BYTES", str(10 * 1024 * 1024)))  # 10 MB default


def _analyze_rate_check(ip: str, limit: int = RATE_LIMIT_ANALYZE, window: int = 60) -> bool:
    now = time.monotonic()
    with _analyze_rate_lock:
        bucket = [t for t in _analyze_rate_buckets.get(ip, []) if now - t < window]
        if len(bucket) >= limit:
            _analyze_rate_buckets[ip] = bucket
            return False
        bucket.append(now)
        _analyze_rate_buckets[ip] = bucket
        return True


def _dbg_d78afc(hypothesis_id, location, message, data=None):
    """Session d78afc: append one NDJSON line to workspace log. No secrets/PII."""
    try:
        row = {
            "sessionId": "d78afc",
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data or {},
            "timestamp": int(time.time() * 1000),
            "runId": "pre",
        }
        path = os.path.join(app.root_path, "debug-d78afc.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


@app.route("/api/debug-d78afc", methods=["POST"])
def api_debug_d78afc():
    """Browser session d78afc: append one NDJSON line (same file as _dbg_d78afc)."""
    try:
        j = request.get_json(silent=True) or {}
        data = j.get("data")
        if not isinstance(data, dict):
            data = {}
        _dbg_d78afc(
            str(j.get("hypothesisId") or "?"),
            str(j.get("location") or "?"),
            str(j.get("message") or ""),
            data,
        )
    except Exception:
        pass
    return jsonify({"ok": True})


def _ce_debug_ndjson(hypothesis_id, location, message, data):
    """Session debug log (NDJSON append). Do not log secrets or PII."""
    try:
        payload = {
            "sessionId": "e91706",
            "hypothesisId": hypothesis_id,
            "location": location,
            "message": message,
            "data": data,
            "timestamp": int(time.time() * 1000),
            "runId": "pre",
        }
        base = "/tmp" if os.getenv("VERCEL") else app.root_path
        path = os.path.join(base, "debug-e91706.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception:
        pass


# Bol.com affiliate partner site ID — replace default with your ID from affiliate.bol.com (or set env BOL_PARTNER_ID).
BOL_PARTNER_ID = (os.getenv("BOL_PARTNER_ID") or "1516197").strip() or "1516197"

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
    "coach_mark_completed",
    "coach_mark_shown",
    "coach_sequence_skipped",
    "scan_guide_opened",
    "scan_guide_soft_prompt_shown",
    "primary_cta_tapped",
    "hard_stop_bypassed",
    "repair_survey_submitted",
    "paywall_options_tapped",
    "paywall_dismissed_home",
}
FREE_SCAN_LIMIT = int(os.getenv("FREE_SCAN_LIMIT", "3"))
LICENSE_VERIFY_WINDOW_SEC = 60
LICENSE_VERIFY_MAX_PER_WINDOW = 20
_license_verify_hits = {}


def _truthy_env(name):
    v = (os.environ.get(name) or "").strip().lower()
    return v in ("1", "true", "yes", "on")


def _is_scan_meter_exempt(fingerprint):
    """Opt-in unlimited scans for development / owner testing (never on by default).

    Set any of these on the server (e.g. Vercel env), never commit secrets:

    - CE_DISABLE_SCAN_LIMIT=1  → bypass meter for every client on this deployment
    - CE_DEV_FINGERPRINT_ALLOWLIST=fp1,fp2  → bypass for matching device_fingerprint values
    - CE_DEV_BYPASS_SECRET=<long random>  → bypass when request header X-CE-Dev-Bypass matches
    - CE_PRO_EMAILS=email1,email2  → bypass when request body contains a matching pro_email field
    """
    if _truthy_env("CE_DISABLE_SCAN_LIMIT"):
        return True
    allow = (os.environ.get("CE_DEV_FINGERPRINT_ALLOWLIST") or "").strip()
    if fingerprint and allow:
        fp_norm = fingerprint.strip().lower()
        parts = {p.strip().lower() for p in allow.split(",") if p.strip()}
        if fp_norm in parts:
            return True
    secret = (os.environ.get("CE_DEV_BYPASS_SECRET") or "").strip()
    if secret and request.headers.get("X-CE-Dev-Bypass", "").strip() == secret:
        return True
    # CE_PRO_EMAILS: check if the request carries a pro_email that matches
    pro_emails_env = (os.environ.get("CE_PRO_EMAILS") or "").strip().lower()
    if pro_emails_env:
        allowed_emails = {e.strip() for e in pro_emails_env.split(",") if e.strip()}
        # Check JSON body, form, and query args
        candidate = None
        try:
            if request.is_json:
                body = request.get_json(silent=True) or {}
                candidate = (body.get("pro_email") or body.get("email") or "").strip().lower()
            if not candidate:
                candidate = (
                    request.form.get("pro_email")
                    or request.form.get("email")
                    or request.args.get("pro_email")
                    or request.args.get("email")
                    or ""
                ).strip().lower()
        except Exception:
            pass
        if candidate and candidate in allowed_emails:
            return True
    return False


# Never return DIY steps for these high-risk domains.
HARD_STOP_KEYWORDS = {
    "groepenkast",
    "meterkast",
    "zekeringkast",
    "zekering",
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scan_sessions (
                    fingerprint TEXT PRIMARY KEY,
                    scans_used INTEGER NOT NULL DEFAULT 0,
                    pro INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMPTZ NOT NULL,
                    updated_at TIMESTAMPTZ NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pro_licenses (
                    id BIGSERIAL PRIMARY KEY,
                    email TEXT NOT NULL,
                    product TEXT,
                    sale_id TEXT UNIQUE,
                    created_at TIMESTAMPTZ NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS scan_sessions (
                    fingerprint TEXT PRIMARY KEY,
                    scans_used INTEGER NOT NULL DEFAULT 0,
                    pro INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pro_licenses (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL,
                    product TEXT,
                    sale_id TEXT UNIQUE,
                    created_at TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1
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
        conn.execute("CREATE INDEX IF NOT EXISTS idx_scan_sessions_updated_at ON scan_sessions(updated_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pro_licenses_email ON pro_licenses(email)")
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


def _request_field(name):
    if request.is_json:
        body = request.get_json(silent=True) or {}
        return body.get(name)
    return request.form.get(name)


def _extract_scan_fingerprint():
    fp = _clean_small_str(
        _request_field("device_fingerprint")
        or _request_field("fingerprint")
        or _request_field("session_fingerprint")
        or _request_field("session_id"),
        160,
    )
    return fp


def _request_any_json_form():
    if request.is_json:
        return request.get_json(silent=True) or {}
    out = {}
    try:
        out.update(request.form.to_dict(flat=True))
    except Exception:
        pass
    return out


def _plan_from_product(product):
    p = _clean_small_str(product, 120).lower()
    if not p:
        return ""
    if "year" in p or "annual" in p:
        return "yearly"
    if "month" in p:
        return "monthly"
    return "scanpack"


def _get_scan_session(fingerprint):
    with _db() as conn:
        row = conn.execute(
            "SELECT scans_used, pro FROM scan_sessions WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        if not row:
            now = _utc_now_iso()
            conn.execute(
                """
                INSERT INTO scan_sessions (fingerprint, scans_used, pro, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (fingerprint, 0, 0, now, now),
            )
            conn.commit()
            return {"scans_used": 0, "limit": FREE_SCAN_LIMIT, "pro": False}
        return {
            "scans_used": int(row["scans_used"] or 0),
            "limit": FREE_SCAN_LIMIT,
            "pro": bool(row["pro"]),
        }


def _increment_scan_session(fingerprint):
    with _db() as conn:
        row = conn.execute(
            "SELECT scans_used, pro FROM scan_sessions WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()
        now = _utc_now_iso()
        if not row:
            conn.execute(
                """
                INSERT INTO scan_sessions (fingerprint, scans_used, pro, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (fingerprint, 1, 0, now, now),
            )
            conn.commit()
            return {"scans_used": 1, "limit": FREE_SCAN_LIMIT, "pro": False}
        scans_used = int(row["scans_used"] or 0) + 1
        pro = bool(row["pro"])
        conn.execute(
            "UPDATE scan_sessions SET scans_used = ?, updated_at = ? WHERE fingerprint = ?",
            (scans_used, now, fingerprint),
        )
        conn.commit()
        return {"scans_used": scans_used, "limit": FREE_SCAN_LIMIT, "pro": pro}


def _check_scan_limit_or_402():
    fp = _extract_scan_fingerprint()
    if not fp:
        return None, ("device_fingerprint is required", 400)
    if _is_scan_meter_exempt(fp):
        # Do not increment DB usage for exempt traffic (keeps prod metrics meaningful).
        return fp, {"scans_used": 0, "limit": FREE_SCAN_LIMIT, "pro": True}, None
    state = _get_scan_session(fp)
    if state["scans_used"] >= state["limit"] and not state["pro"]:
        return fp, None, (
            jsonify(
                {
                    "success": False,
                    "error": "free_scan_limit_reached",
                    "paywall": True,
                    "scans_used": state["scans_used"],
                    "limit": state["limit"],
                    "pro": state["pro"],
                }
            ),
            402,
        )
    return fp, state, None


def _consume_scan_after_success(fingerprint, scan_state):
    if not fingerprint:
        return scan_state or {"scans_used": 0, "limit": FREE_SCAN_LIMIT, "pro": False}
    if scan_state and scan_state.get("pro"):
        return scan_state
    if _is_scan_meter_exempt(fingerprint):
        return {"scans_used": 0, "limit": FREE_SCAN_LIMIT, "pro": True}
    return _increment_scan_session(fingerprint)


def _verify_gumroad_webhook_auth():
    token = (os.getenv("GUMROAD_WEBHOOK_TOKEN") or "").strip()
    secret = (os.getenv("GUMROAD_WEBHOOK_SECRET") or "").strip()
    if not token and not secret:
        # Without shared secrets, accepting webhooks would let anyone POST Pro activations.
        # Local/dev only: set ALLOW_UNAUTHENTICATED_GUMROAD_WEBHOOK=1 if you intentionally omit Gumroad signing.
        return _truthy_env("ALLOW_UNAUTHENTICATED_GUMROAD_WEBHOOK")

    if token:
        hdr_token = (
            (request.headers.get("X-Webhook-Token") or "").strip()
            or (request.headers.get("X-Gumroad-Token") or "").strip()
        )
        auth = (request.headers.get("Authorization") or "").strip()
        if auth.lower().startswith("bearer "):
            hdr_token = hdr_token or auth.split(" ", 1)[1].strip()
        if hmac.compare_digest(hdr_token, token):
            return True

    if secret:
        sig = (
            (request.headers.get("X-Gumroad-Signature") or "").strip()
            or (request.headers.get("X-Webhook-Signature") or "").strip()
        )
        if sig:
            raw = request.get_data(cache=True) or b""
            digest = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
            sig_norm = sig.lower().replace("sha256=", "").strip()
            if hmac.compare_digest(sig_norm, digest.lower()):
                return True

    return False


def _allow_license_verify_request():
    now = time.time()
    ip = _request_ip() or "unknown"
    bucket = _license_verify_hits.get(ip, [])
    bucket = [ts for ts in bucket if now - ts <= LICENSE_VERIFY_WINDOW_SEC]
    if len(bucket) >= LICENSE_VERIFY_MAX_PER_WINDOW:
        _license_verify_hits[ip] = bucket
        return False
    bucket.append(now)
    _license_verify_hits[ip] = bucket
    if len(_license_verify_hits) > 5000:
        # Best-effort memory cap for long-running processes.
        for key in list(_license_verify_hits.keys())[:1000]:
            if not _license_verify_hits.get(key):
                _license_verify_hits.pop(key, None)
    return True


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


def _insert_analyze_success_events(payload, form_src, source_tag="analyze"):
    """Persist scan_completed (+ hazard_flagged when needed). form_src supports .get like request.form."""
    try:
        result = (payload or {}).get("result") or {}
        lang = _clean_small_str(form_src.get("language"), 12)
        sess = _clean_small_str(form_src.get("session_id"), 80)
        uid = _clean_small_str(form_src.get("user_id"), 80)
        jid = _clean_small_str(form_src.get("job_id"), 80)
        ch = _clean_small_str(form_src.get("source_channel"), 40)
        ua = _clean_small_str(request.headers.get("User-Agent"), 260)
        ip = _request_ip()
        _insert_event(
            {
                "event_raw": "scan_completed",
                "event_name": "scan_completed",
                "language": lang,
                "session_id": sess,
                "user_id": uid,
                "job_id": jid,
                "task_category": _clean_small_str(result.get("job_category"), 60),
                "hazard_level": _clean_small_str(result.get("hazard_level"), 20).lower(),
                "source_channel": ch,
                "meta_json": json.dumps({"from": source_tag}, ensure_ascii=False),
                "ip": ip,
                "user_agent": ua,
            }
        )
        hz = _clean_small_str(result.get("hazard_level"), 20).lower()
        if hz in {"caution", "warning", "danger", "emergency"}:
            _insert_event(
                {
                    "event_raw": "hazard_flagged",
                    "event_name": "hazard_flagged",
                    "language": lang,
                    "session_id": sess,
                    "user_id": uid,
                    "job_id": jid,
                    "task_category": _clean_small_str(result.get("job_category"), 60),
                    "hazard_level": hz,
                    "source_channel": ch,
                    "meta_json": json.dumps({"from": source_tag}, ensure_ascii=False),
                    "ip": ip,
                    "user_agent": ua,
                }
            )
    except Exception:
        pass


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
        key = (os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY") or "").strip()
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY is not set (or set CLAUDE_API_KEY, or add it to .env in the app folder)")
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


def _normalize_rental_hint_value(v):
    x = _clean_str(v, "").lower().replace("-", "_").replace(" ", "_").strip()
    if x in ("landlord_likely", "tenant_likely", "unclear", "not_applicable"):
        return x
    if not x or x in ("n/a", "na", "none", "null", "not_relevant", "not_applicable"):
        return "not_applicable"
    if any(k in x for k in ("landlord", "owner", "verhuurder", "huisbaas")):
        return "landlord_likely"
    if any(k in x for k in ("tenant", "renter", "huurder")):
        return "tenant_likely"
    if any(k in x for k in ("unclear", "unknown", "unsure", "depends", "mixed", "onzeker", "onduidelijk", "twijfel")):
        return "unclear"
    return "not_applicable"


def _extract_rental_fields(raw):
    hint_keys = (
        "rental_liability_hint",
        "rental_hint",
        "liability_hint",
        "tenant_vs_landlord",
        "tenant_landlord_hint",
    )
    note_keys = (
        "rental_liability_note",
        "rental_note",
        "liability_note",
        "tenant_landlord_note",
    )
    raw_hint = ""
    for k in hint_keys:
        raw_hint = _clean_str(raw.get(k), "")
        if raw_hint:
            break
    raw_note = ""
    for k in note_keys:
        raw_note = _clean_str(raw.get(k), "")
        if raw_note:
            break
    hint = _normalize_rental_hint_value(raw_hint)
    # Fallback: if hint is missing but note strongly implies one side.
    if hint == "not_applicable" and raw_note:
        note_low = raw_note.lower()
        has_landlord = any(k in note_low for k in ("landlord", "verhuurder", "huisbaas", "owner"))
        has_tenant = any(k in note_low for k in ("tenant", "renter", "huurder"))
        if has_landlord and has_tenant:
            hint = "unclear"
        elif has_landlord:
            hint = "landlord_likely"
        elif has_tenant:
            hint = "tenant_likely"
    return hint, raw_note


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

    _rental_h, _rental_note = _extract_rental_fields(raw)

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
        "rental_liability_hint": _rental_h,
        "rental_liability_note": _rental_note,
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
            rationale = "You may proceed carefully if you follow checks, use the right tools, and stop if anything feels unsafe."
        else:
            rationale = "This looks suitable for careful DIY if you follow the steps and safety guidance."

    why_matters = _clean_str(raw.get("why_safety_matters"), "")

    if not key_obs and normalized.get("material_readout"):
        key_obs = [normalized["material_readout"]]
    if not poss and normalized.get("xray_readout"):
        poss = [normalized["xray_readout"]]

    normalized["key_observations"] = key_obs[:4]
    normalized["possible_issues"] = poss[:4]
    normalized["severity_ui"] = sev
    normalized["decision_outcome"] = dec
    normalized["decision_rationale"] = rationale[:480]
    normalized["why_safety_matters"] = why_matters[:320]
    normalized["when_to_stop"] = when_stop[:3]
    return normalized


def safe_normalize(raw_result):
    try:
        return _normalize_result(raw_result)
    except Exception as e:
        print(f"Normalization error: {e}")
        fallback_step = "Retake a closer photo in brighter light, then scan again."
        return {
            "what_i_see": "Unable to analyse this image",
            "task": "Retake photo and retry analysis",
            "difficulty": "easy",
            "estimated_cost": "",
            "time_needed": "2 minutes",
            "hazard_level": "caution",
            "hazard_note": "Automatic analysis failed; verify the object before any repair action.",
            "decision_outcome": "do_not_proceed",
            "decision_rationale": "We could not complete the analysis. Please try again with a clearer photo.",
            "hard_stop_triggered": False,
            "needs_retake": True,
            "retake_guidance": "Try a closer photo with better lighting.",
            "confidence": "low",
            "steps": [fallback_step],
            "step_details": [{"text": fallback_step, "visual_tip": "Fill the frame with the damaged area only."}],
            "tools_needed": [],
            "materials_needed": [],
            "quick_checks": [],
            "confidence_tier": "caution",
            "safety_tip": "Do not proceed until the item is clearly identified.",
            "pro_tip": "Take one wide context photo and one close-up for better accuracy.",
            "rental_liability_note": "",
            "rental_liability_hint": "not_applicable",
        }


def _model_fallback_chain(preferred_model):
    env_override = (os.getenv("ANTHROPIC_MODEL") or "").strip()
    ordered = [
        preferred_model,
        env_override,
        DEFAULT_VISION_MODEL,
        "claude-haiku-4-5-20251001",
        "claude-sonnet-4-5-20250929",
        "claude-3-5-sonnet-20241022",
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

@app.route("/agreement")
def agreement_page():
    return render_template("agreement.html")


@app.route("/safety-stop")
def safety_stop_page():
    tier = (request.args.get("tier") or "danger").lower().strip()
    if tier not in ("caution", "danger", "emergency"):
        tier = "danger"
    message = _clean_small_str(request.args.get("m"), 2000)
    return render_template("safety_stop.html", tier=tier, message=message)


@app.route("/pricing")
def pricing_page():
    return render_template("pricing.html")


@app.route("/")
def home():
    # One-time helper: open /?reveal_fp=<CE_FINGERPRINT_REVEAL_TOKEN> on a device to copy its
    # device_fingerprint for CE_DEV_FINGERPRINT_ALLOWLIST (phone-friendly; remove token after use).
    _fp_reveal = (os.environ.get("CE_FINGERPRINT_REVEAL_TOKEN") or "").strip()
    _reveal_arg = _clean_small_str(request.args.get("reveal_fp"), 200).strip()
    ce_show_device_fingerprint = bool(_fp_reveal) and _reveal_arg == _fp_reveal
    _vercel_sha = (os.environ.get("VERCEL_GIT_COMMIT_SHA") or "").strip()
    html = render_template(
        "index.html",
        BOL_PARTNER_ID=BOL_PARTNER_ID,
        ce_show_device_fingerprint=ce_show_device_fingerprint,
        ce_deploy_sha=_vercel_sha[:7] if len(_vercel_sha) >= 7 else _vercel_sha,
    )
    resp = make_response(html)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    if _vercel_sha:
        resp.headers["X-CE-Deploy-SHA"] = _vercel_sha[:7]
    return resp


@app.route("/favicon.ico")
def favicon():
    return send_from_directory("static", "icon-192.png", mimetype="image/png")


@app.route("/robots.txt")
def robots():
    body = (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /metrics\n"
        "Disallow: /metrics-detail\n"
        "Disallow: /dashboard\n"
        "Disallow: /api/\n"
    )
    return Response(body, mimetype="text/plain")


@app.route("/app-build")
def app_build():
    """Optional: frontend uses this to show deploy version in UI."""
    return jsonify({"success": True, "version": os.getenv("APP_VERSION", "dev")})


# Marketing floor + DB count for "we refused risky DIY" trust messaging on the landing UI.
PUBLIC_HAZARD_REFUSAL_BASE = int(os.getenv("PUBLIC_HAZARD_REFUSAL_BASE", "800"))


@app.route("/api/trust-stats")
def trust_stats():
    """Rough count of hazard-flagged events plus a configurable base (for empty dev DBs)."""
    extra = 0
    try:
        with _db() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM events WHERE event_name = ?",
                ("hazard_flagged",),
            ).fetchone()
            extra = int(row[0] if row is not None else 0)
    except Exception:
        extra = 0
    return jsonify({"refused_total": PUBLIC_HAZARD_REFUSAL_BASE + extra})


def _file_to_image_block(upload):
    raw = upload.read()
    if not raw:
        return None
    b64 = base64.b64encode(raw).decode("utf-8")
    mime = upload.mimetype or "image/jpeg"
    return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}}


def _immutable_analyze_snapshot():
    """Materialize multipart to in-memory FileStorage objects (safe for background threads)."""
    from io import BytesIO

    from werkzeug.datastructures import FileStorage, ImmutableMultiDict

    pairs = []
    for key in ("image", "image_2", "image_3"):
        uf = request.files.get(key)
        if uf:
            raw = uf.read()
            fn = uf.filename or f"{key}.jpg"
            ct = uf.mimetype or "image/jpeg"
            pairs.append((key, FileStorage(stream=BytesIO(raw), filename=fn, content_type=ct)))
    files = ImmutableMultiDict(pairs)
    form = ImmutableMultiDict(list(request.form.items(multi=True)))
    return files, form


def _analyze_stream_error_payload(exc):
    """JSON body for SSE when /analyze fails inside the worker thread."""
    if isinstance(exc, json.JSONDecodeError):
        return {"success": False, "error": f"AI returned invalid JSON: {str(exc)}"}
    if isinstance(exc, AuthenticationError):
        return {"success": False, "error": "AI authentication failed"}
    if isinstance(exc, (RateLimitError, PermissionDeniedError)):
        return {"success": False, "error": "AI service temporarily unavailable"}
    if isinstance(exc, (APIStatusError, BadRequestError, NotFoundError)):
        return {"success": False, "error": "AI upstream request failed"}
    return {"success": False, "error": (str(exc) or "error")[:500]}


def _do_analyze(req_files=None, req_form=None):
    try:
        _get_client()
    except RuntimeError as e:
        return None, (str(e), 503)

    files = req_files if req_files is not None else request.files
    form = req_form if req_form is not None else request.form

    # Per-IP rate limit (keeps API costs bounded against abuse / runaway loops)
    try:
        _ip_for_rate = _request_ip()
    except Exception:
        _ip_for_rate = ""
    if _ip_for_rate and not _analyze_rate_check(_ip_for_rate):
        return None, ("Rate limit exceeded — please wait a moment", 429)

    image_file = files.get("image")
    image_2 = files.get("image_2")
    image_3 = files.get("image_3")
    question = (form.get("question", "") or "")[:500]  # cap question length
    language = form.get("language", "nl")

    if not image_file:
        return None, ("No image provided", 400)

    # Reject oversized images before paying the API tokens
    for _img in (image_file, image_2, image_3):
        if _img is None:
            continue
        try:
            _img.stream.seek(0, 2)
            _sz = _img.stream.tell()
            _img.stream.seek(0)
        except Exception:
            _sz = 0
        if _sz > MAX_IMAGE_BYTES:
            return None, ("Image too large — maximum 10 MB per photo", 413)

    view_defs = [
        (image_file, "VIEW 1 — CONTEXT: Whole object + a bit of surroundings (orientation)."),
        (image_2, "VIEW 2 — MATERIALS & TYPEPLATE: brands, stickers, couplings, pipe material, wire entry."),
        (image_3, "VIEW 3 — X-RAY / PROBLEM ZONE: close-up; separate dirt/limescale from real damage or leak origin."),
    ]

    def _snapshot_kb(upl):
        if not upl or not getattr(upl, "stream", None):
            return 0
        try:
            upl.stream.seek(0)
            n = len(upl.stream.read())
            upl.stream.seek(0)
            return n // 1024
        except Exception:
            try:
                upl.stream.seek(0)
            except Exception:
                pass
            return 0

    payload_kb_approx = sum(_snapshot_kb(u) for u, _ in view_defs if u)

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
  "task": "under ~10 words, verb-led, confident action title the user would tap on",
  "difficulty": "easy | medium | hard",
  "estimated_cost": "range or empty",
  "time_needed": "duration",
  "hazard_level": "safe | caution | warning | danger",
  "hazard_note": "or empty",
  "when_to_call_pro": "licensed-work triggers",
  "rental_liability_hint": "landlord_likely | tenant_likely | unclear | not_applicable",
  "rental_liability_note": "one short reason for the hint; empty only when not_applicable",
  "tools_needed": [],
  "materials_needed": [],
  "steps": [{{"text": "...", "visual_tip": "what the user should see in frame when this step is done"}}],
  "safety_tip": "...",
  "pro_tip": "one sharp insider tip, or empty if none"
}}

Hard rules:
- severity_ui MUST align with hazard_level and decision_outcome (danger or call-pro ⇒ potentially_dangerous + do_not_proceed unless you are 100% sure it is only informational).
- decision_outcome do_not_proceed: user must not DIY; tell them to stop or call a pro.
- key_observations = facts seen; possible_issues = what could be wrong — keep separate.
- Steps: ordered, actionable, minimal jargon; every step MUST have visual_tip (camera check).
- Always include rental_liability_hint using the allowed enum values.
- Use landlord_likely only when landlord responsibility is more likely than tenant responsibility.
- Use tenant_likely for small routine upkeep and user-serviceable household fixes.
- Use unclear when evidence is mixed or contract-dependent.
- Use not_applicable for non-rental contexts or when tenancy relevance is clearly absent.
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

    t_api0 = time.time()
    usage_extra = {}
    _dbg_d78afc(
        "H2",
        "app.py:_do_analyze:anthropic_start",
        "vision call",
        {"payload_kb": payload_kb_approx, "n_views": n_views},
    )
    response = _messages_create_with_fallback(
        system=system_prompt,
        max_tokens=2800,
        preferred_model=VISION_MODEL,
        messages=[{"role": "user", "content": user_content}],
    )
    try:
        u = getattr(response, "usage", None)
        if u is not None:
            it = getattr(u, "input_tokens", None)
            ot = getattr(u, "output_tokens", None)
            usage_extra = {k: v for k, v in (("input_tokens", it), ("output_tokens", ot)) if v is not None}
    except Exception:
        pass
    _dbg_d78afc(
        "H2",
        "app.py:_do_analyze:anthropic_end",
        "vision done",
        {"duration_ms": int((time.time() - t_api0) * 1000), **usage_extra},
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
    result = safe_normalize(parsed)
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
        result["hard_stop_reasons"] = []  # don't expose internal filter keywords
    else:
        result["hard_stop_triggered"] = False
        result["hard_stop_reasons"] = []
    steps = result.get("steps") or []
    step_details = result.get("step_details") or []
    _ce_debug_ndjson(
        "H5",
        "app.py:_do_analyze:final",
        "normalized result",
        {
            "steps_len": len(steps) if isinstance(steps, list) else -1,
            "step_details_len": len(step_details) if isinstance(step_details, list) else -1,
            "decision_outcome": str(result.get("decision_outcome") or ""),
            "hazard_level": str(result.get("hazard_level") or ""),
        },
    )
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
        result["hard_stop_reasons"] = []  # don't expose internal filter keywords
    else:
        result["hard_stop_triggered"] = False
        result["hard_stop_reasons"] = []

    return {"success": True, "result": result}, None


@app.route("/analyze", methods=["POST"])
def analyze():
    _t0 = time.time()
    _dbg_d78afc("H2", "app.py:analyze:entry", "POST /analyze", {})
    want_stream = (request.headers.get("X-CE-Stream") or "").strip() == "1"
    try:
        fp, scan_state, limit_err = _check_scan_limit_or_402()
        if limit_err:
            if isinstance(limit_err[0], str):
                _dbg_d78afc("H2", "app.py:analyze:limit", "reject string", {"ms": int((time.time() - _t0) * 1000)})
                return jsonify({"success": False, "error": limit_err[0]}), limit_err[1]
            _dbg_d78afc("H2", "app.py:analyze:limit", "reject 402", {"ms": int((time.time() - _t0) * 1000)})
            return limit_err[0], limit_err[1]

        if want_stream:
            snap_files, snap_form = _immutable_analyze_snapshot()
            if not snap_files.get("image"):
                return jsonify({"success": False, "error": "No image provided"}), 400

            def generate():
                holder = {}

                def work():
                    try:
                        holder["pair"] = _do_analyze(snap_files, snap_form)
                    except Exception as e:
                        holder["exc"] = e

                t = threading.Thread(target=work, daemon=True)
                t.start()
                last_keep = time.time()
                while t.is_alive():
                    now = time.time()
                    if now - last_keep >= 4:
                        yield ": keepalive\n\n"
                        last_keep = now
                    time.sleep(0.25)
                t.join()
                if holder.get("exc") is not None:
                    body = _analyze_stream_error_payload(holder["exc"])
                    yield f"data: {json.dumps(body)}\n\n"
                    yield "data: [DONE]\n\n"
                    return
                payload, err = holder.get("pair", (None, None))
                if err:
                    yield f"data: {json.dumps({'success': False, 'error': err[0]})}\n\n"
                    yield "data: [DONE]\n\n"
                    return
                scan_state2 = _consume_scan_after_success(fp, scan_state)
                payload["scan_session"] = scan_state2
                _insert_analyze_success_events(payload, snap_form, "analyze")
                _dbg_d78afc("H2", "app.py:analyze:success", "sse payload", {"ms": int((time.time() - _t0) * 1000)})
                yield f"data: {json.dumps(payload)}\n\n"
                yield "data: [DONE]\n\n"

            return Response(
                stream_with_context(generate()),
                mimetype="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )

        payload, err = _do_analyze()
        if err:
            _dbg_d78afc(
                "H2",
                "app.py:analyze:do_analyze_err",
                "upstream failed",
                {"err": str(err[0])[:120], "status": err[1], "ms": int((time.time() - _t0) * 1000)},
            )
            return jsonify({"success": False, "error": err[0]}), err[1]
        scan_state = _consume_scan_after_success(fp, scan_state)
        payload["scan_session"] = scan_state
        _insert_analyze_success_events(payload, request.form, "analyze")
        _dbg_d78afc("H2", "app.py:analyze:success", "jsonify ok", {"ms": int((time.time() - _t0) * 1000)})
        return jsonify(payload)
    except json.JSONDecodeError as e:
        _dbg_d78afc("H2", "app.py:analyze:exc", "JSONDecodeError", {"ms": int((time.time() - _t0) * 1000)})
        return jsonify({"success": False, "error": f"AI returned invalid JSON: {str(e)}"}), 500
    except AuthenticationError:
        _dbg_d78afc("H2", "app.py:analyze:exc", "AuthenticationError", {"ms": int((time.time() - _t0) * 1000)})
        return jsonify({"success": False, "error": "AI authentication failed"}), 503
    except (RateLimitError, PermissionDeniedError):
        _dbg_d78afc("H2", "app.py:analyze:exc", "RateLimit/Permission", {"ms": int((time.time() - _t0) * 1000)})
        return jsonify({"success": False, "error": "AI service temporarily unavailable"}), 503
    except (APIStatusError, BadRequestError, NotFoundError):
        _dbg_d78afc("H2", "app.py:analyze:exc", "APIStatus/BadRequest/NotFound", {"ms": int((time.time() - _t0) * 1000)})
        return jsonify({"success": False, "error": "AI upstream request failed"}), 502
    except Exception as e:
        _dbg_d78afc("H2", "app.py:analyze:exc", "Exception", {"err": str(e)[:160], "ms": int((time.time() - _t0) * 1000)})
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/analyze-stage1", methods=["POST"])
def analyze_stage1():
    """Fast first-pass response for perceived speed; full result comes from /analyze."""
    _t0 = time.time()
    _dbg_d78afc("H2", "app.py:analyze_stage1:entry", "POST /analyze-stage1", {})
    try:
        payload, err = _do_stage1_quick_analyze()
        if err:
            _dbg_d78afc("H2", "app.py:analyze_stage1:err", "stage1 failed", {"err": str(err[0])[:120], "ms": int((time.time() - _t0) * 1000)})
            return jsonify({"success": False, "error": err[0]}), err[1]
        _dbg_d78afc("H2", "app.py:analyze_stage1:ok", "success", {"ms": int((time.time() - _t0) * 1000)})
        return jsonify(payload)
    except json.JSONDecodeError as e:
        _dbg_d78afc("H2", "app.py:analyze_stage1:exc", "JSONDecodeError", {"ms": int((time.time() - _t0) * 1000)})
        return jsonify({"success": False, "error": f"AI returned invalid JSON: {str(e)}"}), 500
    except Exception as e:
        _dbg_d78afc("H2", "app.py:analyze_stage1:exc", "Exception", {"err": str(e)[:160], "ms": int((time.time() - _t0) * 1000)})
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/analyze-live", methods=["POST"])
def analyze_live():
    """Same vision analysis as /analyze; frontend expects multi-view hints in `live`."""
    try:
        fp, scan_state, limit_err = _check_scan_limit_or_402()
        if limit_err:
            if isinstance(limit_err[0], str):
                return jsonify({"success": False, "error": limit_err[0]}), limit_err[1]
            return limit_err[0], limit_err[1]
        payload, err = _do_analyze()
        if err:
            return jsonify({"success": False, "error": err[0]}), err[1]
        scan_state = _consume_scan_after_success(fp, scan_state)
        payload["live"] = {"needs_more_views": False, "next_prompt": ""}
        payload["scan_session"] = scan_state
        _insert_analyze_success_events(payload, request.form, "analyze_live")
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

Goal: Return one SHORT generic object label (1–3 words) for what is most central in frame, plus confidence.

Rules:
- Return ONLY valid JSON with this exact shape:
  {{"label":"<label>","confidence":"high|medium|low"}}
- Use the user's language (Dutch if instructed, otherwise English).
- Generic nouns only — no brands, no materials, no conditions.
- Prefer a broader generic label if uncertain (e.g. "object").
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
        product = data.get("product") or tool or ""
        event_name = _clean_small_str(
            data.get("event") or data.get("event_name") or "tool_link_clicked",
            80,
        )
        if event_name == "tool_link_clicked":
            print(f"Store click: {data.get('store','')} for '{product or tool}'")
        else:
            print(f"Track click: {event_name} value={data.get('value','')!r}")
        _insert_event(
            {
                "event_raw": event_name,
                "event_name": event_name,
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
                        "product": _clean_small_str(product, 120),
                        "value": _clean_small_str(data.get("value"), 120),
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


@app.route("/api/session/scan", methods=["POST"])
def api_session_scan():
    try:
        fp = _extract_scan_fingerprint()
        if not fp:
            return jsonify({"success": False, "error": "device_fingerprint is required"}), 400
        if _is_scan_meter_exempt(fp):
            return jsonify(
                {
                    "success": True,
                    "scans_used": 0,
                    "limit": FREE_SCAN_LIMIT,
                    "pro": True,
                }
            )
        state = _get_scan_session(fp)
        return jsonify(
            {
                "success": True,
                "scans_used": state["scans_used"],
                "limit": state["limit"],
                "pro": state["pro"],
            }
        )
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/webhooks/gumroad", methods=["POST"])
def gumroad_webhook():
    try:
        if not _verify_gumroad_webhook_auth():
            return jsonify({"success": False, "error": "unauthorized"}), 401
        data = _request_any_json_form()
        email = _clean_small_str(
            data.get("email")
            or data.get("buyer_email")
            or data.get("purchaser_email"),
            200,
        ).lower()
        product = _clean_small_str(
            data.get("product_permalink")
            or data.get("permalink")
            or data.get("product")
            or data.get("product_name"),
            160,
        )
        sale_id = _clean_small_str(
            data.get("sale_id") or data.get("id") or data.get("order_id"),
            120,
        )
        if not email or not sale_id:
            return jsonify({"success": False, "error": "email and sale_id are required"}), 400
        now = _utc_now_iso()
        with _db() as conn:
            existing = conn.execute(
                "SELECT id FROM pro_licenses WHERE sale_id = ?",
                (sale_id,),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE pro_licenses
                    SET email = ?, product = ?, active = 1
                    WHERE sale_id = ?
                    """,
                    (email, product, sale_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO pro_licenses (email, product, sale_id, created_at, active)
                    VALUES (?, ?, ?, ?, 1)
                    """,
                    (email, product, sale_id, now),
                )
            conn.commit()
        return jsonify({"success": True}), 200
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/license/verify", methods=["GET"])
def api_license_verify():
    try:
        if not _allow_license_verify_request():
            return jsonify({"success": False, "error": "rate_limited"}), 429
        email = _clean_small_str(request.args.get("email"), 200).lower()
        if not email:
            return jsonify({"success": False, "error": "email is required"}), 400

        # Check CE_PRO_EMAILS env var (comma-separated owner/tester bypass)
        pro_env = (os.environ.get("CE_PRO_EMAILS") or "").strip().lower()
        env_pro = email in {e.strip() for e in pro_env.split(",") if e.strip()} if pro_env else False

        is_pro = env_pro
        plan = "lifetime" if env_pro else ""

        if not is_pro:
            with _db() as conn:
                row = conn.execute(
                    """
                    SELECT product
                    FROM pro_licenses
                    WHERE email = ? AND active = 1
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (email,),
                ).fetchone()
            if row:
                is_pro = True
                plan = _plan_from_product(row["product"])

        if is_pro:
            # Upgrade the calling device's scan session to pro so the scan gate passes
            # GET request — fingerprint comes via query param
            fp = _clean_small_str(
                request.args.get("device_fingerprint")
                or request.args.get("fingerprint")
                or request.args.get("session_id"),
                160,
            ) or _extract_scan_fingerprint()
            if fp:
                now = _utc_now_iso()
                with _db() as conn:
                    exists = conn.execute(
                        "SELECT 1 FROM scan_sessions WHERE fingerprint = ?", (fp,)
                    ).fetchone()
                    if exists:
                        conn.execute(
                            "UPDATE scan_sessions SET pro = 1, updated_at = ? WHERE fingerprint = ?",
                            (now, fp),
                        )
                    else:
                        conn.execute(
                            """INSERT INTO scan_sessions (fingerprint, scans_used, pro, created_at, updated_at)
                               VALUES (?, 0, 1, ?, ?)""",
                            (fp, now, now),
                        )
                    conn.commit()
            return jsonify({"success": True, "pro": True, "plan": plan})

        return jsonify({"success": True, "pro": False, "plan": ""})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/admin/grant-pro", methods=["POST"])
def admin_grant_pro():
    """Manually grant pro to an email (owner use only, requires DASHBOARD_TOKEN)."""
    try:
        token = (os.getenv("DASHBOARD_TOKEN") or "").strip()
        if not token:
            return jsonify({"success": False, "error": "not_configured"}), 403
        auth = (request.headers.get("Authorization") or "").strip()
        provided = auth.replace("Bearer ", "").replace("bearer ", "").strip()
        if not hmac.compare_digest(provided, token):
            return jsonify({"success": False, "error": "unauthorized"}), 401
        data = request.get_json(silent=True) or {}
        email = _clean_small_str(data.get("email"), 200).lower()
        if not email:
            return jsonify({"success": False, "error": "email required"}), 400
        now = _utc_now_iso()
        with _db() as conn:
            existing = conn.execute(
                "SELECT id FROM pro_licenses WHERE email = ? AND sale_id = 'manual'", (email,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE pro_licenses SET active = 1, product = 'manual', updated_at = ? WHERE email = ? AND sale_id = 'manual'",
                    (now, email),
                )
            else:
                conn.execute(
                    "INSERT INTO pro_licenses (email, product, sale_id, created_at, active) VALUES (?, 'manual', 'manual', ?, 1)",
                    (email, now),
                )
            conn.commit()
        return jsonify({"success": True, "email": email, "plan": "lifetime"})
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


@app.route("/api/debug-session", methods=["POST"])
def api_debug_session():
    """Append client debug payloads (session e91706). No secrets/PII."""
    try:
        data = request.get_json(silent=True) or {}
        base = "/tmp" if os.getenv("VERCEL") else app.root_path
        path = os.path.join(base, "debug-e91706.jsonl")
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False) + "\n")
    except Exception:
        pass
    return jsonify({"ok": True})


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
    ok, err = _check_dashboard_auth()
    if not ok:
        return err
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
    ok, err = _check_dashboard_auth()
    if not ok:
        return err
    return render_template("dashboard.html")


@app.route("/metrics-detail")
def metrics_detail():
    """Detailed KPI payload for live dashboard and funding updates."""
    ok, err = _check_dashboard_auth()
    if not ok:
        return err
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
    key_ok = bool((os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY") or "").strip())
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
