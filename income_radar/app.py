import csv
import io
import json
import os
import secrets
from datetime import datetime, timezone

from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from income_radar import auth, crypto_util, db
from income_radar.engine import refresh_all, row_to_profile
from income_radar.presets import merge_presets_by_id, preset_status

app = Flask(__name__)
app.secret_key = os.getenv("INCOME_RADAR_SECRET", "") or secrets.token_hex(32)


@app.template_filter("from_json")
def _from_json_filter(s):
    try:
        v = json.loads(s or "[]")
        return v if isinstance(v, list) else []
    except Exception:
        return []


def _decrypt_row_note(enc: str) -> str:
    return crypto_util.decrypt_note(enc or "")


def _safe_next(raw: str | None) -> str:
    if not raw:
        return ""
    if not raw.startswith("/") or raw.startswith("//"):
        return ""
    return raw


@app.before_request
def _pin_gate():
    if not auth.pin_configured():
        return None
    ep = request.endpoint
    if ep in ("login", "static"):
        return None
    if ep == "health" and auth.health_endpoint_public():
        return None
    if session.get(auth.SESSION_UNLOCKED):
        return None
    if ep == "health":
        return jsonify(ok=False, error="unauthorized"), 401
    nxt = _safe_next(request.full_path) or "/"
    return redirect(url_for("login", next=nxt))


@app.route("/login", methods=["GET", "POST"])
def login():
    if not auth.pin_configured():
        return redirect(url_for("dashboard"))
    expected = os.getenv("INCOME_RADAR_PIN", "").strip()
    if request.method == "POST":
        pin = request.form.get("pin") or ""
        nxt = _safe_next(request.form.get("next"))
        if auth.verify_pin(pin, expected):
            session.clear()
            session[auth.SESSION_UNLOCKED] = True
            return redirect(nxt or url_for("dashboard"))
        return render_template(
            "pin_login.html",
            error="Incorrect PIN.",
            next=nxt,
        )
    nxt = _safe_next(request.args.get("next"))
    return render_template("pin_login.html", error=None, next=nxt)


@app.post("/logout")
def logout():
    session.clear()
    if auth.pin_configured():
        return redirect(url_for("login"))
    return redirect(url_for("dashboard"))


def _attachment_csv(filename: str, header: list[str], rows: list[list]):
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in rows:
        writer.writerow(row)
    return Response(
        buf.getvalue().encode("utf-8"),
        mimetype="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/export/income.csv")
def export_income_csv():
    db.init_db()
    rows = db.income_rows(50_000)
    data = []
    for r in rows:
        data.append(
            [
                r["id"],
                r["logged_at"],
                r["amount_eur"],
                r["source_label"],
                r["user_verified"],
                _decrypt_row_note(r["notes_enc"] or ""),
            ]
        )
    return _attachment_csv(
        "income_radar_income.csv",
        ["id", "logged_at", "amount_eur", "source_label", "user_verified", "notes"],
        data,
    )


@app.get("/export/opportunities.csv")
def export_opportunities_csv():
    db.init_db()
    rows = db.opportunity_rows(50_000)
    data = []
    for r in rows:
        summ = (r["summary"] or "").replace("\n", " ").replace("\r", "")
        pipe_notes = _decrypt_row_note(r["pipe_notes_enc"] or "")
        data.append(
            [
                r["id"],
                r["source"],
                r["title"],
                r["url"],
                summ[:8000],
                r["published_at"] or "",
                r["tags"] or "[]",
                r["score"],
                r["fetched_at"],
                r["pipe_stage"] or "",
                pipe_notes,
            ]
        )
    return _attachment_csv(
        "income_radar_opportunities.csv",
        [
            "id",
            "source",
            "title",
            "url",
            "summary",
            "published_at",
            "tags_json",
            "score",
            "fetched_at",
            "pipeline_stage",
            "pipeline_notes",
        ],
        data,
    )


@app.get("/export/learning.csv")
def export_learning_csv():
    weights = db.learning_map()
    rows = [[k, round(v, 6)] for k, v in sorted(weights.items(), key=lambda x: -x[1])]
    return _attachment_csv("income_radar_learning.csv", ["tag", "weight"], rows)


@app.post("/presets/add")
def presets_add():
    ids = request.form.getlist("preset_id")
    added, total = merge_presets_by_id(ids)
    return redirect(
        url_for(
            "dashboard",
            feed_merge_added=added,
            feed_merge_total=total,
        )
    )


@app.get("/")
def dashboard():
    db.init_db()
    profile = db.get_profile_row()
    prof = row_to_profile(profile)
    verified = db.verified_income_total()
    threshold = float(prof.get("proof_threshold_eur") or 100)
    proof_ok = verified >= threshold
    opportunities = db.opportunity_rows(250)
    income_rows = db.income_rows(100)
    weights = db.learning_map()
    feed_presets = preset_status()
    try:
        f_added = int(request.args.get("feed_merge_added", "-1"))
        f_total = int(request.args.get("feed_merge_total", "-1"))
    except ValueError:
        f_added, f_total = -1, -1
    return render_template(
        "income_dashboard.html",
        profile=dict(profile),
        verified_income=verified,
        proof_threshold=threshold,
        proof_ok=proof_ok,
        monthly_target=float(prof.get("monthly_target_eur") or 5000),
        opportunities=opportunities,
        income_rows=income_rows,
        learning_weights=sorted(weights.items(), key=lambda x: -x[1])[:24],
        decrypt_note=_decrypt_row_note,
        feed_presets=feed_presets,
        pin_enabled=auth.pin_configured(),
        feed_merge_added=f_added,
        feed_merge_total=f_total,
    )


@app.post("/profile")
def save_profile():
    db.upsert_profile(request.form)
    return redirect(url_for("dashboard"))


@app.post("/refresh")
def refresh():
    info = refresh_all()
    return redirect(url_for("dashboard", refreshed=info.get("inserted_or_updated", 0)))


@app.post("/pipeline")
def pipeline():
    oid = int(request.form.get("opportunity_id", "0"))
    stage = (request.form.get("stage") or "new").strip()
    notes = request.form.get("notes") or ""
    if oid > 0 and stage in ("new", "queued", "applied", "won", "lost", "skipped"):
        db.upsert_pipeline(oid, stage, notes)
    return redirect(url_for("dashboard"))


@app.post("/income")
def income():
    try:
        amount = float(request.form.get("amount_eur") or 0)
    except ValueError:
        amount = 0.0
    label = (request.form.get("source_label") or "").strip()
    notes = request.form.get("notes") or ""
    if amount > 0 and label:
        db.add_income(amount, label, notes)
    return redirect(url_for("dashboard"))


@app.get("/health")
def health():
    return jsonify(ok=True, ts=datetime.now(timezone.utc).isoformat())


@app.get("/export-learning")
def export_learning():
    return jsonify(weights=db.learning_map())
