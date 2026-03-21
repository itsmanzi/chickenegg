# chickenegg — Deploy to Render.com

## Quick Start (5 minutes)

### Step 1: Push to GitHub
```bash
git init
git add .
git commit -m "Initial commit: chickenegg MVP"
git remote add origin https://github.com/YOUR_USERNAME/chickenegg.git
git push -u origin main
```

### Step 2: Create Render Account
- Go to https://render.com
- Sign up with GitHub
- Authorize Render to access your repos

### Step 3: Deploy
1. Click "New +" → "Web Service"
2. Select your `chickenegg` repository
3. Configure:
   - **Name**: `chickenegg`
   - **Environment**: `Python 3`
   - **Build Command**: (auto-detect from Procfile)
   - **Start Command**: (auto-detect from Procfile)
   - **Plan**: Free (or Starter for 24/7)

4. Add Environment Variables:
   - `CLAUDE_API_KEY` = your Claude API key
   - `APP_PIN` = 2606 (or change to your PIN)
   - `SECRET_KEY` = (auto-generated)

5. Click "Create Web Service"

**Wait 2-3 minutes for deployment to complete.**

### Step 4: Test
- Your app will be live at: `https://chickenegg-XXXXX.onrender.com`
- Open `/pin` and enter PIN `2606`

## Database Notes
- SQLite database (`scans.db`) will be created automatically
- On free tier, database resets when app restarts (use PostgreSQL for production)
- Free tier sleeps after 15 min of inactivity

## For Production
- Upgrade to Starter or higher plan ($7/month)
- Add PostgreSQL database (free tier available)
- Update `app.py` to use PostgreSQL instead of SQLite

## Monitoring
- View logs: Dashboard → Select app → "Logs"
- Check stats: `/stats` endpoint (PIN protected)

## Next Steps
1. Update Gumroad links to point to your Render URL
2. Film TikTok videos linking to the app
3. Start collecting emails and scan data
4. Monitor analytics at `/stats`

---
**Deploy on every code change:**
```bash
git add .
git commit -m "Fix: feature X"
git push
# Render auto-deploys!
```
