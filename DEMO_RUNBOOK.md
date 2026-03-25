# Chickenegg Demo Runbook

Use this flow for a clean, repeatable live demo.

## 1) Pre-Demo Setup (2-3 minutes)

- Open the app URL: `https://chickeneggbackup.vercel.app/?demo=1`
- Allow camera access.
- Confirm HUD text appears and updates.
- Hold an object in center frame for 2-3 seconds.
- Confirm live label stabilizes (no rapid flicker).

## 2) Demo Script (60-90 seconds)

1. **Problem framing (5s)**
   - "You point your phone at something unknown at home."

2. **Live brain moment (10-15s)**
   - Show object in camera center.
   - Let live label appear (short object name).
   - "It sees what you see in real time."

3. **One-tap scan (5s)**
   - Tap egg once.
   - "One tap to scan."

4. **Guided capture + result (20-30s)**
   - Let scan prompts run (hold steady / go closer).
   - Show result cards:
     - what it is
     - how to fix/clean
     - tools/materials
     - safety warning if needed

5. **Actionability close (10-15s)**
   - "This gives you exactly what to do next and what to buy."

## 3) Fallback Lines (if network is slow)

- "Live label is confidence-gated, so it waits for a stable read."
- "Even if live HUD is delayed, one tap still gives full fix plan and safety."

## 4) Safety/Trust Line

- "We prioritize safety; risky cases are flagged with warnings and stop guidance."

## 5) Demo-Mode Notes

- `?demo=1` enables a stability profile:
  - slower label churn
  - stricter confidence gating
  - slightly wider tap guard

