import os
import re
import base64
import json
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

# Lazy client so missing env fails on first request with a clear message, not at import.
_client = None


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
- Steps: ordered, actionable, minimal jargon; every step MUST have visual_tip (camera check).
- No made-up part numbers or torque specs unless readable in the image.
- E-bike battery swollen/dented ⇒ call-pro / specialist, never open cells.
- If the scene is ambiguous, lower confidence, fill uncertainty_note, and avoid overclaiming.
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
    return {"success": True, "result": result}, None


@app.route("/analyze", methods=["POST"])
def analyze():
    try:
        payload, err = _do_analyze()
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
        return jsonify(payload)
    except json.JSONDecodeError as e:
        return jsonify({"success": False, "error": f"AI returned invalid JSON: {str(e)}"}), 500
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
        data = request.json
        print(f"New signup: {data.get('email','')}")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/track-click", methods=["POST"])
def track_click():
    try:
        data = request.json
        tool = data.get("tool") or data.get("tools") or ""
        print(f"Store click: {data.get('store','')} for '{tool}'")
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
        event = data.get("event", "")
        meta = data.get("meta", {}) or {}
        lang = data.get("language", "")
        print(f"Event: {event} lang={lang} meta={json.dumps(meta)[:300]}")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


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
        }
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
