import json
from pathlib import Path

from income_radar.db import data_dir

PRESETS_PATH = Path(__file__).resolve().parent / "async_feed_presets.json"
FEEDS_PATH = data_dir() / "feeds.json"


def load_preset_definitions() -> list[dict]:
    if not PRESETS_PATH.exists():
        return []
    try:
        data = json.loads(PRESETS_PATH.read_text(encoding="utf-8"))
        items = data.get("presets") or []
        return [x for x in items if isinstance(x, dict) and x.get("id") and x.get("url")]
    except Exception:
        return []


def load_feed_urls() -> list[str]:
    FEEDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not FEEDS_PATH.exists():
        return []
    try:
        data = json.loads(FEEDS_PATH.read_text(encoding="utf-8"))
        raw = data.get("feeds") or []
        out: list[str] = []
        for u in raw:
            s = str(u).strip()
            if s.startswith("http"):
                out.append(s)
        return list(dict.fromkeys(out))
    except Exception:
        return []


def save_feed_urls(urls: list[str]) -> None:
    FEEDS_PATH.parent.mkdir(parents=True, exist_ok=True)
    clean: list[str] = []
    for u in urls:
        s = str(u).strip()
        if s.startswith("http"):
            clean.append(s)
    clean = list(dict.fromkeys(clean))
    FEEDS_PATH.write_text(json.dumps({"feeds": clean}, indent=2), encoding="utf-8")


def merge_presets_by_id(preset_ids: list[str]) -> tuple[int, int]:
    """Returns (new_urls_added, total_feeds_after)."""
    wanted = {str(i).strip() for i in preset_ids if str(i).strip()}
    defs = {p["id"]: str(p["url"]).strip() for p in load_preset_definitions()}
    urls_to_add = [defs[i] for i in wanted if i in defs and defs[i].startswith("http")]
    existing = load_feed_urls()
    merged = list(dict.fromkeys(existing + urls_to_add))
    added = len(merged) - len(existing)
    save_feed_urls(merged)
    return added, len(merged)


def preset_status() -> list[dict]:
    """Each preset dict plus `in_feeds` bool."""
    have = set(load_feed_urls())
    out: list[dict] = []
    for p in load_preset_definitions():
        u = str(p.get("url") or "").strip()
        row = dict(p)
        row["in_feeds"] = u in have
        out.append(row)
    return out
