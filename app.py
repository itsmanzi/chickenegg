import os
import base64
import json
from flask import Flask, request, jsonify, render_template
import anthropic

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
# Vision-capable; override if your key uses a different ID (see Anthropic console).
VISION_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-3-5-haiku-latest")
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


def _normalize_result(raw):
    if not isinstance(raw, dict):
        raw = {}

    steps_raw = (
        raw.get("steps")
        or raw.get("instructions")
        or raw.get("how_to")
        or raw.get("step_by_step")
        or raw.get("repair_steps")
        or raw.get("steps_to_fix")
        or raw.get("recommendation")
        or []
    )
    steps = []
    if isinstance(steps_raw, list):
        for step in steps_raw:
            if isinstance(step, dict):
                txt = _clean_str(
                    step.get("text")
                    or step.get("step")
                    or step.get("instruction")
                    or step.get("visual_tip")
                )
                if txt:
                    steps.append(txt)
            else:
                txt = _clean_str(step)
                if txt:
                    steps.append(txt)
    else:
        steps = _to_list(steps_raw)

    if not steps:
        fallback = _clean_str(
            raw.get("what_to_do")
            or raw.get("fix_plan")
            or raw.get("recommendation")
            or raw.get("task")
            or raw.get("what_i_see")
        )
        if fallback:
            steps = [fallback]

    tools = _to_list(raw.get("tools_needed") or raw.get("tools"))
    materials = _to_list(raw.get("materials_needed") or raw.get("materials") or raw.get("parts_needed"))

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
        "safety_tip": _clean_str(raw.get("safety_tip") or raw.get("safety") or raw.get("warning"), "Work slowly and wear protection."),
        "pro_tip": _clean_str(raw.get("pro_tip") or raw.get("tip"), ""),
    }
    return normalized


def _messages_create_with_fallback(system, messages, max_tokens, preferred_model):
    candidates = [
        preferred_model,
        os.getenv("ANTHROPIC_MODEL", "").strip(),
        "claude-3-5-haiku-latest",
        "claude-3-5-sonnet-latest",
        "claude-3-haiku-20240307",
    ]
    tried = set()
    last_error = None
    for model_name in candidates:
        model_name = (model_name or "").strip()
        if not model_name or model_name in tried:
            continue
        tried.add(model_name)
        try:
            return client.messages.create(
                model=model_name,
                max_tokens=max_tokens,
                system=system,
                messages=messages,
            )
        except Exception as e:
            last_error = e
            msg = str(e).lower()
            # If model is unavailable, try next candidate.
            if "404" in msg or "not found" in msg or "model" in msg:
                continue
            # Non-model errors should surface immediately.
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


def _do_analyze():
    image_file = request.files.get("image")
    question   = request.form.get("question", "")
    language   = request.form.get("language", "nl")

    if not image_file:
        return None, ("No image provided", 400)

    image_base64 = base64.b64encode(image_file.read()).decode("utf-8")
    mime_type    = image_file.mimetype or "image/jpeg"

    lang_instruction = "Respond entirely in Dutch (Nederlands). Use Dutch product names (e.g. 'kraan', 'moersleutel', 'Teflon tape')." if language == "nl" else "Respond in English."

    system_prompt = f"""
You are an expert Dutch home repair assistant. {lang_instruction}

Analyse the image and return ONLY a valid JSON object — no markdown, no code fences, no explanation.

JSON structure:
{{
  "what_i_see": "short description of what is broken",
  "task": "concise task name e.g. Lekkende kraan repareren",
  "difficulty": "easy | medium | hard",
  "estimated_cost": "e.g. EUR5-EUR15",
  "time_needed": "e.g. 30 minuten",
  "hazard_level": "safe | caution | warning | danger",
  "hazard_note": "one sentence hazard note or empty string",
  "when_to_call_pro": "specific condition when user must stop and call a professional, or empty string",
  "tools_needed": ["tool1", "tool2"],
  "materials_needed": ["material1", "material2"],
  "steps": [
    {{"text": "step description", "visual_tip": "one sentence describing exactly what the user should see or look for at this step — e.g. 'The valve handle should point perpendicular to the pipe when closed'"}},
    ...
  ],
  "safety_tip": "one key safety tip",
  "pro_tip": "one practical pro tip"
}}

Rules:
- Be action-oriented. Steps should tell the user EXACTLY what to do, not just explain.
- hazard_level 'danger' only for gas, live electricity, or structural risk. Be specific in when_to_call_pro.
- visual_tip must describe what success looks like at that step — something the user can verify visually.
- Only list tools and materials genuinely needed. Never hallucinate.
- Return ONLY the JSON. No extra text.
"""

    response = _messages_create_with_fallback(
        system=system_prompt,
        max_tokens=1500,
        preferred_model=VISION_MODEL,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": image_base64}},
                {"type": "text", "text": f"User note: {question}" if question else "Analyseer dit probleem."},
            ],
        }],
    )

    ai_text = response.content[0].text.strip()
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
        image_file = request.files.get("image")
        step       = request.form.get("step", "")
        task       = request.form.get("task", "")
        language   = request.form.get("language", "nl")

        if not image_file:
            return jsonify({"success": False, "error": "No image provided"}), 400

        image_base64 = base64.b64encode(image_file.read()).decode("utf-8")
        mime_type    = image_file.mimetype or "image/jpeg"
        lang_instruction = "Respond in Dutch." if language == "nl" else "Respond in English."

        response = _messages_create_with_fallback(
            preferred_model=CHECK_PROGRESS_MODEL,
            max_tokens=300,
            system=f"You are a home repair safety checker. {lang_instruction} Return ONLY JSON: {{\"danger_level\": \"safe|caution|danger|emergency\", \"progress_feedback\": \"one practical sentence about what you see\"}}",
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": image_base64}},
                    {"type": "text", "text": f"Task: {task}. Current step: {step}"},
                ],
            }],
        )

        ai_text = response.content[0].text.strip()
        if ai_text.startswith("```"):
            ai_text = ai_text.split("```")[1]
            if ai_text.startswith("json"):
                ai_text = ai_text[4:]
        result = json.loads(ai_text.strip())
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


if __name__ == "__main__":
    app.run(debug=True, port=5000)
