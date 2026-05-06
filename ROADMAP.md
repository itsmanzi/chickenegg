# chickenegg — Roadmap to €1M

Built from competitor research + 2026 design research, mapped to what's already shipped.

## The thesis (one line)

> **chickenegg is FixBot for everything else in your house — IKEA, plumbing, walls, appliances. In het Nederlands.**

iFixit FixBot owns electronics. No competitor owns the rest, and none are localized for NL retailers. That's the lane.

## The math

€1M ARR ≈ one of:

- 25,000 users × €40/yr (annual sub at €39.99)
- 4,000 users × €4.99/wk × ~52% retention = ~€1M
- Or hybrid: 15k subs + Intergamma affiliate rev share + B2B insurance/landlord deal

Today: ~16 scans, 0 paying. The gap is **distribution + paywall**, not product.

## What's just shipped this sprint

- ✅ Dashboard token auth, rate limiting, image-size cap (production hardening)
- ✅ Free scan limit dropped 5 → 3 (matches PictureThis / Cal AI 2025 median)
- ✅ Deprecated Haiku 3 fallbacks removed
- ✅ Favicon + robots.txt
- ✅ React MVP signup hardened (proper email regex, submitting state)
- ✅ Live deploy: https://chickeneggbackup.vercel.app

## Highest-leverage next moves (ranked)

### 1. Position the landing page (this week — code change)

Current home page leads with the egg + "Point. Snap. Fixed." Change the hero to:

> **"Klus elke kapotte ding in je huis. Wij herkennen het, jij repareert het.** AI voor alles behalve je telefoon — IKEA, leidingen, muren, apparaten."

Subhero: "3 gratis scans · Geen account nodig · 🔒 AVG-veilig · Gemaakt in Nederland"

### 2. Paywall after first wow (this week — design + code)

Current state: no paywall, only €29 lifetime popup. Switch to **PictureThis pattern**:

- Show identification + first 2 steps **clear**
- Blur steps 3..N with a single CTA: **"Ontgrendel volledige reparatie — €4.99/week of €39.99/jaar (bespaar 84%)"**
- Hard wall after scan #3
- Keep €29 lifetime as a *founder-only* offer with a visible "X/100 plekken over" counter (scarcity)

This single change typically 2-4× conversion in the utility AI category.

### 3. Intergamma affiliate (this month — only Sammy can do)

Gamma + Karwei = Intergamma. One contract gets you both. Action:

- Email: `partnerships@intergamma.nl` and `affiliates@intergamma.nl`
- Subject: "We sturen Nederlandse koopintentie naar Gamma — gratis"
- Pitch: "Ik bouw chickenegg, een AI-app die thuisreparaties herkent. Elke scan eindigt met een lijst gereedschap. We willen die direct linken naar Gamma/Karwei. Commission share, wij genereren traffic."

This is the line in your pitch deck. No competitor has it.

### 4. KVK + Benelux trademark (this week — only Sammy)

- KVK Eenmanszaak: kvk.nl, ~€80, 1 hour. Required for affiliate deals + acquirer due diligence.
- Benelux trademark "chickenegg": boip.int, €249, 30 minutes. Files the IP that a buyer pays for.

Without these two, no serious buyer will touch you.

### 5. Distribution: TikTok + a single viral moment

Cash-mode-style "I pointed my phone at this broken X and AI fixed it" videos. The research says one viral video = 50k–200k downloads. Top hooks (in Dutch):

- "AI fixt mijn IKEA-nachtmerrie"
- "Geen idee wat dit kapotte ding was — chickenegg wel"
- "Ik wist niets van loodgieten. Nu wel."

Post 1/day for 30 days. The dataset says 1 in 30 hits.

### 6. The acquisition narrative (build the deck now, even at zero users)

Buyers (Amazon, IKEA, Intergamma, Google) buy three things: users, data, IP. Build the narrative early:

- **One-pager**: "We're building Dutch home-repair vision search. iFixit owns electronics. Google Lens identifies but doesn't help. We do everything else — and every scan is purchase intent."
- **Data asset**: every scan is logged in `scans.db` (postgres in prod). At 100k scans, the dataset itself is worth millions to a retailer.
- **Defensibility**: NL knowledge base in the system prompt + Intergamma exclusivity (when signed) + community of users = the moat Google can't copy without a partnership team.

## What's broken right now (visible to users)

- Time-to-camera too slow vs. 2026 best-in-class (target <800ms; currently has PIN gate)
- No traffic-light hazard chip (Yuka-style legibility)
- Email collected before paywall (research says: defer email to *after* first wow)
- Prices not shown including BTW (NL conversion killer)
- No KVK badge in footer (NL trust signal)

## What's NOT on the critical path (don't get distracted)

- Native iOS/Android apps (Capacitor wrap is fine until €5k MRR)
- Patents (€25k+, useless at this stage)
- Apple Vision Pro / Meta glasses (post-€100k MRR conversation)
- More categories (cooking, art) — focus IKEA/plumbing/walls first

## Rough 90-day milestones

| By | Target |
|---|---|
| Week 1 | Hard paywall live, €29 lifetime capped at 100, KVK filed |
| Week 2 | Trademark filed, Intergamma email sent, first paywall conversion |
| Week 4 | 1k scans, 50 paying users, first viral TikTok |
| Week 8 | 10k scans, 500 paying users (€2.5k MRR), Intergamma affiliate signed |
| Week 12 | 100k scans, 2k paying users (€10k MRR), first press feature, acquisition deck ready |

At €10k MRR you're worth roughly €600k–€1M to an acquirer at 5-8× ARR. At €30k MRR you're at €1.8M–€3M. €1M ARR (€83k MRR) → €5M–€8M acquisition target. That's the trajectory.

## Three things only Sammy can do (this week)

1. **KVK Eenmanszaak** — kvk.nl
2. **Benelux trademark** — boip.int
3. **Intergamma email** — partnerships@intergamma.nl

Everything else I can build.
