import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import feedparser

from income_radar import db

log = logging.getLogger(__name__)

CURATED_PATH = Path(__file__).resolve().parent / "static_curated.json"
FEEDS_PATH = Path(__file__).resolve().parent / "data" / "feeds.json"


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        log.warning("Could not load %s: %s", path, exc)
        return default


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip().lower())


def _tags_from_text(text: str) -> list[str]:
    t = _norm(text)
    vocab = [
        "writing",
        "copywriting",
        "design",
        "figma",
        "video",
        "editing",
        "code",
        "python",
        "data",
        "translation",
        "customer_support",
        "chat",
        "email",
        "async",
        "remote",
        "eur",
        "eu",
        "virtual_assistant",
        "marketing",
        "seo",
        "music",
        "audio",
    ]
    found = []
    for w in vocab:
        if w.replace("_", " ") in t or w in t:
            found.append(w)
    if "remote" not in found and ("remote" in t or "werk vanuit" in t or "thuiswerk" in t):
        found.append("remote")
    return sorted(set(found))


def _score_item(title: str, summary: str, profile: dict, weights: dict[str, float]) -> float:
    text = f"{title}\n{summary}"
    tags = _tags_from_text(text)
    score = 3.0
    skills = _norm(profile.get("skills", ""))
    for tag in tags:
        if tag.replace("_", " ") in skills or tag in skills:
            score += 2.5
        score += weights.get(tag, 0.0)
    if profile.get("async_only"):
        if any(x in text for x in ("sync", "phone call", "zoom", "teams meeting", "on-call")):
            score -= 1.5
        if "async" in tags or "chat" in tags or "email" in tags:
            score += 0.8
    if profile.get("no_video_calls") and any(
        x in text for x in ("video call", "zoom", "google meet", "camera on")
    ):
        score -= 2.0
    if profile.get("no_phone") and any(x in text for x in ("phone", "bel je", "bellen")):
        score -= 1.2
    # Prefer listings that mention EU / time zones loosely (weak signal)
    if profile.get("country") == "NL" and any(
        x in text for x in ("europe", "eu", "nl", "netherlands", "amsterdam cest", "cet")
    ):
        score += 0.4
    return max(0.0, min(20.0, score))


def row_to_profile(row) -> dict:
    if not row:
        return {}
    return {
        "skills": row["skills"] or "",
        "country": row["country"] or "NL",
        "region": row["region"] or "",
        "languages": row["languages"] or "",
        "monthly_target_eur": float(row["monthly_target_eur"] or 0),
        "no_video_calls": bool(row["no_video_calls"]),
        "no_phone": bool(row["no_phone"]),
        "async_only": bool(row["async_only"]),
        "proof_threshold_eur": float(row["proof_threshold_eur"] or 100),
    }


def load_curated_opportunities(profile_row) -> list[dict]:
    data = _load_json(CURATED_PATH, {"items": []})
    items = data.get("items") or []
    profile = row_to_profile(profile_row)
    weights = db.learning_map()
    out: list[dict] = []
    for i, it in enumerate(items):
        title = str(it.get("title") or "")
        url = str(it.get("url") or "")
        summary = str(it.get("summary") or "")
        source = str(it.get("source") or "curated")
        tags = list(it.get("tags") or []) or _tags_from_text(f"{title}\n{summary}")
        ext_id = hashlib.sha256(f"{source}|{url}".encode("utf-8")).hexdigest()[:32]
        score = float(it.get("base_score") or 0) + _score_item(title, summary, profile, weights)
        out.append(
            {
                "source": source,
                "external_id": ext_id,
                "title": title,
                "url": url,
                "summary": summary,
                "published_at": None,
                "tags": tags,
                "score": score,
            }
        )
    return out


def fetch_feeds(profile_row, max_feeds: int = 12, entries_per_feed: int = 15) -> list[dict]:
    feeds = _load_json(FEEDS_PATH, {"feeds": []})
    urls = [str(u) for u in (feeds.get("feeds") or []) if str(u).strip().startswith("http")]
    urls = urls[:max_feeds]
    profile = row_to_profile(profile_row)
    weights = db.learning_map()
    out: list[dict] = []
    for u in urls:
        try:
            parsed = feedparser.parse(
                u,
                agent="IncomeRadar/1.0 (+local; private use)",
            )
            for e in (parsed.entries or [])[:entries_per_feed]:
                title = getattr(e, "title", "") or ""
                link = getattr(e, "link", "") or ""
                summary = getattr(e, "summary", "") or getattr(e, "description", "") or ""
                if not link:
                    continue
                pub = getattr(e, "published", None) or getattr(e, "updated", None)
                ext_id = hashlib.sha256(f"{u}|{link}".encode("utf-8")).hexdigest()[:32]
                tags = _tags_from_text(f"{title}\n{summary}")
                score = _score_item(title, summary, profile, weights)
                out.append(
                    {
                        "source": f"rss:{u[:48]}",
                        "external_id": ext_id,
                        "title": title.strip()[:500],
                        "url": link.strip(),
                        "summary": re.sub(r"<[^>]+>", "", summary)[:2000],
                        "published_at": pub,
                        "tags": tags,
                        "score": score,
                    }
                )
        except Exception as exc:
            log.warning("Feed failed %s: %s", u, exc)
    return out


def refresh_all() -> dict:
    db.init_db()
    profile_row = db.get_profile_row()
    merged = load_curated_opportunities(profile_row) + fetch_feeds(profile_row)
    n = db.insert_opportunities(merged)
    return {"inserted_or_updated": n, "at": datetime.now(timezone.utc).isoformat()}
