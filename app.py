"""
chickenegg MVP — Home Installation
===================================
Point your camera at anything in your home.
AI tells you exactly how to install, fix, or build it.

Setup:
    pip install flask anthropic

Run:
    python app.py

Then open in your browser: http://localhost:5000
On your phone (same WiFi): http://YOUR_PC_IP:5000
"""

import base64
import os
import time
import sqlite3
import datetime
from collections import defaultdict
from functools import wraps
from flask import Flask, request, jsonify, render_template, session, redirect

import anthropic

app = Flask(__name__)

# ── Security config ───────────────────────────────────────────────────────────
app.secret_key = os.environ.get('SECRET_KEY', 'ce-secret-xK9#mP2@nQ7')
APP_PIN        = os.environ.get('APP_PIN', '2606')        # ← your PIN (change this!)
SESSION_HOURS  = 24                                        # stay logged in for 24h
# ─────────────────────────────────────────────────────────────────────────────

# ── Rate limiter — max 30 API calls per minute per IP ─────────────────────────
_rate_log = defaultdict(list)

def is_rate_limited(ip, max_calls=30, window=60):
    now   = time.time()
    calls = [t for t in _rate_log[ip] if now - t < window]
    _rate_log[ip] = calls
    if len(calls) >= max_calls:
        return True
    _rate_log[ip].append(now)
    return False
# ─────────────────────────────────────────────────────────────────────────────

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get('auth'):
            return redirect('/pin')
        return f(*args, **kwargs)
    return decorated

# ── API key — reads from environment variable, falls back to hardcoded ───────
CLAUDE_API_KEY = os.environ.get('CLAUDE_API_KEY')
if not CLAUDE_API_KEY:
    print("⚠️  WARNING: CLAUDE_API_KEY not set in environment variables!")
    print("   Set it with: export CLAUDE_API_KEY='your-key-here'")
    print("   Or on Render: add to Environment Variables")
# ────────────────────────────────────────────────────────────────────────────

client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

# ── Scan logger — SQLite database ─────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(__file__), 'scans.db')

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS scans (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                ts          TEXT    NOT NULL,
                ip          TEXT,
                question    TEXT,
                what_i_see  TEXT,
                task        TEXT,
                difficulty  TEXT,
                time_needed TEXT,
                tools       TEXT,
                step_count  INTEGER
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS emails (
                id    INTEGER PRIMARY KEY AUTOINCREMENT,
                ts    TEXT    NOT NULL,
                email TEXT    UNIQUE,
                ip    TEXT,
                source TEXT
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS store_clicks (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       TEXT    NOT NULL,
                ip       TEXT,
                store    TEXT,
                tools    TEXT
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS questions (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       TEXT    NOT NULL,
                ip       TEXT,
                question TEXT
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS free_scans (
                id    INTEGER PRIMARY KEY AUTOINCREMENT,
                ts    TEXT    NOT NULL,
                ip    TEXT    UNIQUE,
                count INTEGER DEFAULT 0
            )
        ''')

def log_scan(ip, question, result):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute('''
                INSERT INTO scans (ts, ip, question, what_i_see, task, difficulty, time_needed, tools, step_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                datetime.datetime.utcnow().isoformat(),
                ip,
                question,
                result.get('what_i_see', ''),
                result.get('task', ''),
                result.get('difficulty', ''),
                result.get('time_needed', ''),
                ', '.join(result.get('tools_needed', [])),
                len(result.get('steps', []))
            ))
    except Exception as e:
        print(f"[chickenegg] Log error: {e}")

init_db()
# ─────────────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are chickenegg — a home installation and repair expert AI.

When given a photo, you:
1. Identify exactly what you see (the object, fixture, or situation)
2. Understand what the user wants to do with it
3. Give clear, beginner-friendly step-by-step instructions

Always respond in this exact JSON format:
{
  "what_i_see": "short description of what is in the photo",
  "task": "what needs to be done",
  "difficulty": "Easy / Medium / Hard",
  "time_needed": "e.g. 30 minutes",
  "tools_needed": ["tool 1", "tool 2", "tool 3"],
  "steps": [
    "Step 1: ...",
    "Step 2: ...",
    "Step 3: ..."
  ],
  "safety_tip": "one important safety note",
  "pro_tip": "one expert tip to make it easier"
}

Keep steps simple. Write like you're explaining to someone who has never done this before.
Be specific and practical. No fluff."""


@app.route('/pin')
def pin_page():
    if session.get('auth'):
        return redirect('/')
    return render_template('pin.html')


@app.route('/pin/verify', methods=['POST'])
def pin_verify():
    ip  = request.remote_addr
    if is_rate_limited(ip, max_calls=10, window=60):
        return jsonify({'error': 'Too many attempts. Wait a minute.'}), 429
    data = request.get_json() or {}
    if data.get('pin') == APP_PIN:
        session['auth']       = True
        session.permanent     = True
        app.permanent_session_lifetime = __import__('datetime').timedelta(hours=SESSION_HOURS)
        return jsonify({'success': True})
    return jsonify({'error': 'Wrong PIN'}), 401


@app.route('/logout')
def logout():
    session.clear()
    return redirect('/pin')


@app.route('/')
@login_required
def index():
    return render_template('index.html')


@app.route('/check-free-scans', methods=['GET'])
@login_required
def check_free_scans():
    ip = request.remote_addr
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute('SELECT count FROM free_scans WHERE ip = ?', (ip,)).fetchone()
        count = row[0] if row else 0
    remaining = max(0, 5 - count)
    return jsonify({'scans_used': count, 'scans_remaining': remaining, 'allowed': count < 5})


@app.route('/analyze', methods=['POST'])
@login_required
def analyze():
    if 'image' not in request.files:
        return jsonify({'error': 'No image uploaded'}), 400

    image_file = request.files['image']
    user_question = request.form.get('question', '').strip()

    if image_file.filename == '':
        return jsonify({'error': 'No image selected'}), 400
    
    # Check paywall
    ip = request.remote_addr
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute('SELECT count FROM free_scans WHERE ip = ?', (ip,)).fetchone()
        scan_count = row[0] if row else 0
    
    if scan_count >= 5:
        return jsonify({'error': 'paywall', 'message': 'Free scans used. Upgrade to continue.'}), 402

    # Read and encode image
    image_data = image_file.read()
    image_b64 = base64.standard_b64encode(image_data).decode('utf-8')

    # Detect media type
    filename = image_file.filename.lower()
    if filename.endswith('.png'):
        media_type = 'image/png'
    elif filename.endswith('.gif'):
        media_type = 'image/gif'
    elif filename.endswith('.webp'):
        media_type = 'image/webp'
    else:
        media_type = 'image/jpeg'

    # Build the user message
    user_text = user_question if user_question else "What is this and how do I install or fix it?"

    if is_rate_limited(request.remote_addr):
        return jsonify({'error': 'Slow down — too many requests. Wait a moment.'}), 429

    print(f"[chickenegg] Analyzing image... question: '{user_text}'")

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1500,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": user_text
                        }
                    ],
                }
            ],
        )

        raw_text = response.content[0].text.strip()

        # Parse JSON from Claude response
        import json
        import re

        # Extract JSON block if wrapped in markdown
        json_match = re.search(r'\{[\s\S]*\}', raw_text)
        if json_match:
            result = json.loads(json_match.group())
        else:
            result = json.loads(raw_text)

        log_scan(request.remote_addr, user_text, result)
        
        # Increment free scan counter
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute('''
                INSERT INTO free_scans (ts, ip, count) VALUES (?, ?, 1)
                ON CONFLICT(ip) DO UPDATE SET count = count + 1, ts = excluded.ts
            ''', (datetime.datetime.utcnow().isoformat(), request.remote_addr))
        
        print("[chickenegg] Analysis complete.")
        return jsonify({'success': True, 'result': result})

    except Exception as e:
        print(f"[chickenegg] Error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/check-progress', methods=['POST'])
@login_required
def check_progress():
    if 'image' not in request.files:
        return jsonify({'error': 'No image uploaded'}), 400

    image_file = request.files['image']
    step_text   = request.form.get('step', '').strip()
    task_text   = request.form.get('task', '').strip()

    image_data  = image_file.read()
    image_b64   = base64.standard_b64encode(image_data).decode('utf-8')

    filename = image_file.filename.lower()
    if filename.endswith('.png'):
        media_type = 'image/png'
    elif filename.endswith('.webp'):
        media_type = 'image/webp'
    else:
        media_type = 'image/jpeg'

    progress_prompt = f"""You are a home repair safety inspector for the chickenegg app.

Task the user is working on: {task_text}
Step they just completed: {step_text}

Analyse the photo they took of their progress and respond in this EXACT JSON format:
{{
  "looks_correct": true or false,
  "danger_level": "safe" or "caution" or "danger" or "emergency",
  "progress_feedback": "brief honest assessment of what you see",
  "warning_message": "specific warning if danger_level is not safe — leave empty string if safe",
  "can_continue": true or false,
  "recommendation": "what they should do next — one sentence"
}}

Danger level guide:
- safe: everything looks correct and safe to continue
- caution: minor issue, worth noting but can continue carefully
- danger: something looks wrong or potentially unsafe — strong warning needed, stop and fix first
- emergency: immediately dangerous (exposed live wires, gas leak signs, flooding, structural collapse risk) — stop everything

Be honest and specific. If you see exposed wiring, flooding, gas fittings without shutoff, or structural damage — flag it immediately. Do not downplay risks."""

    import json, re
    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_b64,
                        },
                    },
                    {"type": "text", "text": progress_prompt}
                ],
            }],
        )

        raw_text    = response.content[0].text.strip()
        json_match  = re.search(r'\{[\s\S]*\}', raw_text)
        result      = json.loads(json_match.group() if json_match else raw_text)

        print(f"[chickenegg] Progress check: danger_level={result.get('danger_level','?')}")
        return jsonify({'success': True, 'result': result})

    except Exception as e:
        print(f"[chickenegg] Progress check error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/collect-email', methods=['POST'])
def collect_email():
    data  = request.get_json() or {}
    email = data.get('email', '').strip().lower()
    if not email or '@' not in email:
        return jsonify({'error': 'Invalid email'}), 400
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute('''
                INSERT OR IGNORE INTO emails (ts, email, ip, source)
                VALUES (?, ?, ?, ?)
            ''', (datetime.datetime.utcnow().isoformat(), email, request.remote_addr, data.get('source', 'app')))
        print(f"[chickenegg] Email captured: {email}")
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/track-click', methods=['POST'])
@login_required
def track_click():
    data = request.get_json() or {}
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute('''
                INSERT INTO store_clicks (ts, ip, store, tools)
                VALUES (?, ?, ?, ?)
            ''', (datetime.datetime.utcnow().isoformat(), request.remote_addr, data.get('store',''), data.get('tools','')))
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/stats')
@login_required
def stats():
    with sqlite3.connect(DB_PATH) as conn:
        total        = conn.execute('SELECT COUNT(*) FROM scans').fetchone()[0]
        total_emails = conn.execute('SELECT COUNT(*) FROM emails').fetchone()[0]
        total_clicks = conn.execute('SELECT COUNT(*) FROM store_clicks').fetchone()[0]
        recent       = conn.execute('SELECT ts, what_i_see, task, difficulty FROM scans ORDER BY id DESC LIMIT 20').fetchall()
        popular      = conn.execute('SELECT task, COUNT(*) as c FROM scans GROUP BY task ORDER BY c DESC LIMIT 10').fetchall()
        top_stores   = conn.execute('SELECT store, COUNT(*) as c FROM store_clicks GROUP BY store ORDER BY c DESC').fetchall()
        emails       = conn.execute('SELECT ts, email FROM emails ORDER BY id DESC LIMIT 50').fetchall()
        top_tools    = conn.execute('SELECT tools, COUNT(*) as c FROM scans GROUP BY tools ORDER BY c DESC LIMIT 10').fetchall()
    return jsonify({
        'total_scans':  total,
        'total_emails': total_emails,
        'total_store_clicks': total_clicks,
        'emails': [{'ts': e[0], 'email': e[1]} for e in emails],
        'recent_scans': [{'ts': r[0], 'what_i_see': r[1], 'task': r[2], 'difficulty': r[3]} for r in recent],
        'popular_tasks': [{'task': p[0], 'count': p[1]} for p in popular],
        'top_stores': [{'store': s[0], 'clicks': s[1]} for s in top_stores],
        'top_tools': [{'tools': t[0], 'count': t[1]} for t in top_tools],
    })


if __name__ == '__main__':
    if not CLAUDE_API_KEY:
        print("⚠️  ERROR: CLAUDE_API_KEY is not set!")
        print("   Set it in your environment: export CLAUDE_API_KEY='sk-ant-...'")
        exit(1)
    else:
        print("✅ Claude API key configured")
        print("🥚 chickenegg is running at http://localhost:5000")
        print("📱 On your phone (same WiFi): find your PC IP with 'ipconfig'")
    app.run(debug=True, host='0.0.0.0', port=5000)
