"""
Chicken Egg — investor MVP API.
POST /analyze with multipart image → strict JSON (vision or mock, never empty).
"""
from __future__ import annotations

import base64
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from anthropic import Anthropic
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------
VISION_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")


class CollectEmailBody(BaseModel):
    email: str = Field(default="")
    source: str = Field(default="mvp-react")
    language: str = Field(default="en")


class AnalyzeResponse(BaseModel):
    success: bool = True
    object: str = ""
    problem: str = ""
    danger_level: str = "low"  # low | medium | high
    warnings: list[str] = Field(default_factory=list)
    tools_needed: list[str] = Field(default_factory=list)
    steps: list[str] = Field(default_factory=list)
    extra_tips: list[str] = Field(default_factory=list)
    demo_mode: bool = False


MOCK_AIRFRYER: dict[str, Any] = {
    "object": "Air fryer (basket style)",
    "problem": "Grease buildup and burned crumbs reduce airflow and taste; deep clean needed.",
    "danger_level": "low",
    "warnings": [
        "Unplug before cleaning. Basket and tray must be fully cool.",
        "Never immerse the main heating unit or power cord in water.",
    ],
    "tools_needed": [
        "Soft sponge",
        "Dish soap",
        "Microfiber cloth",
        "Old toothbrush (optional)",
    ],
    "steps": [
        "Unplug the air fryer and let all parts cool completely.",
        "Remove the basket and tray. Shake loose crumbs into the bin.",
        "Wash basket and tray in warm soapy water; scrub gently — no steel wool on non-stick.",
        "Wipe the inside chamber with a damp cloth — no dripping water near the element.",
        "Dry everything fully, reassemble, run empty 2 minutes to burn off residual moisture.",
    ],
    "extra_tips": [
        "For stubborn grease, soak basket 10 minutes before scrubbing.",
        "Line the basket with parchment rated for air fryers to reduce future buildup.",
    ],
}

MOCK_BIKE_CHAIN: dict[str, Any] = {
    "object": "Bicycle chain (dry / squeaky)",
    "problem": "Chain is dry or lightly rusted; causes noise and faster wear.",
    "danger_level": "low",
    "warnings": [
        "Use bike-specific lube — mechanical oil attracts grit on chains.",
    ],
    "tools_needed": ["Bike chain lube", "Clean rag", "Degreaser (optional)"],
    "steps": [
        "Shift to middle gear so the chain runs straight.",
        "Wipe the chain with a dry rag to remove surface grit.",
        "If very dirty, apply degreaser, backpedal, wipe until rag stays cleaner.",
        "Apply one drop per roller while backpedaling slowly — wipe excess lube after.",
    ],
    "extra_tips": [
        "NL shops: chain lube at Decathlon or your local fietsenmaker.",
    ],
}


def _norm_danger(raw: str) -> str:
    s = (raw or "low").strip().lower()
    if s in ("high", "danger", "critical", "severe"):
        return "high"
    if s in ("medium", "caution", "warning", "moderate"):
        return "medium"
    return "low"


def _as_str_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, str):
        lines = [x.strip() for x in v.replace("\r", "\n").split("\n") if x.strip()]
        return lines if lines else ([v.strip()] if v.strip() else [])
    if isinstance(v, list):
        out: list[str] = []
        for item in v:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                t = (
                    item.get("text")
                    or item.get("step")
                    or item.get("tip")
                    or item.get("warning")
                    or item.get("tool")
                )
                if isinstance(t, str) and t.strip():
                    out.append(t.strip())
        return out
    return []


def _mock_from_hint(name: str) -> dict[str, Any]:
    low = (name or "").lower()
    if any(k in low for k in ("bike", "fiets", "chain", "ketting")):
        return dict(MOCK_BIKE_CHAIN)
    return dict(MOCK_AIRFRYER)


def _strict_from_ai_blob(raw: dict[str, Any]) -> dict[str, Any]:
    """Map loose model JSON to investor schema."""
    obj = (
        raw.get("object")
        or raw.get("what_i_see")
        or raw.get("task")
        or raw.get("item_name")
        or "Identified object"
    )
    if not isinstance(obj, str):
        obj = str(obj)

    problem = raw.get("problem") or raw.get("summary") or raw.get("task") or ""
    if not isinstance(problem, str):
        problem = str(problem)

    dl = _norm_danger(str(raw.get("danger_level") or raw.get("hazard_level") or "low"))

    warnings = _as_str_list(raw.get("warnings"))
    if raw.get("hazard_note") and isinstance(raw["hazard_note"], str):
        h = raw["hazard_note"].strip()
        if h and h not in warnings:
            warnings.insert(0, h)
    if raw.get("safety_tip") and isinstance(raw["safety_tip"], str):
        st = raw["safety_tip"].strip()
        if st and st not in warnings:
            warnings.append(st)

    tools = _as_str_list(raw.get("tools_needed") or raw.get("tools"))
    steps_raw = raw.get("steps") or []
    steps: list[str] = []
    if isinstance(steps_raw, list):
        for s in steps_raw:
            if isinstance(s, str) and s.strip():
                steps.append(re.sub(r"^(step|stap)\s*\d+[:\-.]?\s*", "", s, flags=re.I).strip())
            elif isinstance(s, dict):
                t = s.get("text") or s.get("step") or ""
                if isinstance(t, str) and t.strip():
                    steps.append(
                        re.sub(r"^(step|stap)\s*\d+[:\-.]?\s*", "", t, flags=re.I).strip()
                    )
    elif isinstance(steps_raw, str) and steps_raw.strip():
        steps = _as_str_list(steps_raw)

    tips = _as_str_list(raw.get("extra_tips") or raw.get("pro_tip"))
    if isinstance(raw.get("pro_tip"), str):
        pt = raw["pro_tip"].strip()
        if pt and pt not in tips:
            tips.append(pt)

    return {
        "object": obj.strip() or "Object",
        "problem": problem.strip() or "Review steps below.",
        "danger_level": dl,
        "warnings": warnings,
        "tools_needed": tools,
        "steps": [x for x in steps if x],
        "extra_tips": tips,
    }


def _extract_json_object(text: str) -> dict[str, Any]:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("```", 2)[1]
        if t.startswith("json"):
            t = t[4:]
    t = t.strip()
    try:
        data = json.loads(t)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", t)
        if m:
            try:
                data = json.loads(m.group())
                return data if isinstance(data, dict) else {}
            except json.JSONDecodeError:
                pass
    return {}


def _analyze_with_anthropic(image_bytes: bytes, content_type: str) -> dict[str, Any]:
    key = (os.getenv("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        raise RuntimeError("no_api_key")

    client = Anthropic(api_key=key)
    b64 = base64.standard_b64encode(image_bytes).decode("ascii")
    mime = content_type if content_type and "/" in content_type else "image/jpeg"

    system = """You are Chicken Egg — the camera-first app: users photograph real objects to fix, clean, build, or understand them at home.
You must feel like a sharp on-site technician: name specifics when visible (brand, material, part type), never vague mush.

Look at the image. Identify the main object and deliver a tight, impressive plan.

Return ONLY valid JSON with exactly these keys (no markdown):
{
  "object": "short, specific label — include brand/model hints if visible",
  "problem": "max 2 sentences — what's wrong, what to do, or why it matters",
  "danger_level": "low" | "medium" | "high",
  "warnings": ["safety strings, can be empty if truly low risk"],
  "tools_needed": ["tools and supplies"],
  "steps": ["ordered, actionable strings — each one a single clear action"],
  "extra_tips": ["0–2 sharp pro tips, or empty list"]
}

Rules:
- Object line = your "wow" recognition moment when the photo allows it.
- If electrical mains / gas / structural — danger_level high and tell them to call a licensed pro in warnings.
- Steps: no fluff; user could follow with hands busy.
- Match language to the user's likely locale if hinted (e.g. Dutch keywords → Dutch); else concise English.
- Never claim you see live video — this is a still photo."""

    user_content = [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime,
                "data": b64,
            },
        },
        {
            "type": "text",
            "text": "Chicken Egg scan — analyze this still image only. Return only the JSON object, no other text.",
        },
    ]

    msg = client.messages.create(
        model=VISION_MODEL,
        max_tokens=2048,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    blocks = getattr(msg, "content", None) or []
    text = ""
    for b in blocks:
        if getattr(b, "type", None) == "text":
            text += getattr(b, "text", "") or ""
    raw = _extract_json_object(text)
    return _strict_from_ai_blob(raw)


app = FastAPI(title="Chicken Egg MVP", version="1.0.0")

_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins or ["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"ok": True, "service": "chicken-egg-mvp"}


@app.post("/collect-email")
async def collect_email(body: CollectEmailBody):
    """Lightweight lead capture for the React MVP demo (appends to local file)."""
    email = (body.email or "").strip()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Invalid email")
    line = f"{datetime.now(timezone.utc).isoformat()}\t{email}\t{body.source}\t{body.language}\n"
    log_path = Path(__file__).resolve().parent / "mvp_emails.log"
    try:
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line)
    except OSError:
        pass
    return {"success": True}


@app.post("/analyze", response_model=AnalyzeResponse)
async def analyze(file: UploadFile = File(...)):
    """
    Multipart image field name must be `file` (browser) — FastAPI uses first file.
    Accepts typical image/* types.
    """
    raw_bytes = await file.read()
    if not raw_bytes:
        raise HTTPException(status_code=400, detail="Empty image")

    filename = (file.filename or "").lower()
    force_mock = os.getenv("FORCE_MOCK", "").lower() in ("1", "true", "yes")

    payload: dict[str, Any]
    demo = False
    if force_mock:
        payload = _mock_from_hint(filename)
        demo = True
    else:
        try:
            ct = file.content_type or "image/jpeg"
            payload = _analyze_with_anthropic(raw_bytes, ct)
            if not payload.get("steps"):
                raise ValueError("empty_steps")
        except Exception:
            payload = _mock_from_hint(filename)
            demo = True

    # Final guardrails
    if not payload.get("steps"):
        payload = dict(MOCK_AIRFRYER)
        demo = True

    return AnalyzeResponse(
        success=True,
        object=str(payload.get("object") or "Object"),
        problem=str(payload.get("problem") or ""),
        danger_level=_norm_danger(str(payload.get("danger_level") or "low")),
        warnings=_as_str_list(payload.get("warnings")),
        tools_needed=_as_str_list(payload.get("tools_needed")),
        steps=_as_str_list(payload.get("steps")),
        extra_tips=_as_str_list(payload.get("extra_tips")),
        demo_mode=demo,
    )
