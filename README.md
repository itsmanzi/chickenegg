# chickenegg (MVP)

Point your camera at something at home → get safe, step-by-step fix instructions.

## Run locally (Windows PowerShell)

```powershell
cd C:\Users\smanz\chickenegg_backup
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

$env:ANTHROPIC_API_KEY = "YOUR_KEY"
$env:APP_PIN = "2606"
$env:SECRET_KEY = "dev-secret"

python app.py
```

Open `http://localhost:5000/pin` and enter your PIN.

## Deploy to Render

This repo includes:
- `Procfile` (runs `gunicorn app:app`)
- `.render.yaml` (Render blueprint)

Set these env vars in Render:
- `ANTHROPIC_API_KEY`
- `APP_PIN`
- `SECRET_KEY` (Render can auto-generate)

See `RENDER_DEPLOY.md` for the full checklist.

---

## Investor MVP — React + FastAPI (demo tomorrow)

Premium mobile-first UI with strict JSON contract. **Offline demo never breaks** (mock if the API key is missing or the model errors).

### Structure

| Path | Role |
|------|------|
| `frontend/` | React (Vite, TypeScript) — camera, alien-metal scan orb, step-by-step flow |
| `backend/` | FastAPI — `POST /analyze` (multipart `file`), Anthropic vision or mock |

### 1) Backend

```powershell
cd C:\Users\smanz\chickenegg_backup\backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Optional: real vision (otherwise mock / airfryer demo)
$env:ANTHROPIC_API_KEY = "YOUR_KEY"

# Optional: always use mock (safe for live investor rooms)
# $env:FORCE_MOCK = "1"

python -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Health check: `http://127.0.0.1:8000/health`

Email signups from the React **“You did it”** flow post to `POST /collect-email` (JSON: `email`, `source`, `language`) and append to `backend/mvp_emails.log`.

### 2) Frontend

```powershell
cd C:\Users\smanz\chickenegg_backup\frontend
npm install
npm run dev
```

Open `http://localhost:5173`. Vite proxies `/analyze` to `http://127.0.0.1:8000`.

**Production build:** `npm run build` → static files in `frontend/dist/`.

**Remote API:** set `VITE_API_URL=https://your-api.example.com` before `npm run build`.

### API response shape

`POST /analyze` returns JSON:

`object`, `problem`, `danger_level` (`low` \| `medium` \| `high`), `warnings`, `tools_needed`, `steps`, `extra_tips`, plus `success` and `demo_mode`.

The root **Flask** app (`app.py`, `templates/`) is unchanged for Vercel/Render deployments; this MVP is an additional, clean stack for pitches and filming.

