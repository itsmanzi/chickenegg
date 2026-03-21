import os
import base64
import json
from flask import Flask, request, jsonify, render_template
import anthropic

app = Flask(__name__)
client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

@app.route("/")
def home():
    return render_template("index.html")


@app.route("/analyze", methods=["POST"])
def analyze():
    try:
        image_file = request.files.get("image")
        question   = request.form.get("question", "")
        language   = request.form.get("language", "nl")

        if not image_file:
            return jsonify({"success": False, "error": "No image provided"}), 400

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

        response = client.messages.create(
            model="claude-3-5-sonnet-20241022",
            max_tokens=1500,
            system=system_prompt,
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
        result = json.loads(ai_text)
        return jsonify({"success": True, "result": result})

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

        response = client.messages.create(
            model="claude-3-5-sonnet-20241022",
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
        print(f"Store click: {data.get('store','')} for '{data.get('tool','')}'")
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


if __name__ == "__main__":
    app.run(debug=True, port=5000)
