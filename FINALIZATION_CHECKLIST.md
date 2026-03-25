# Chickenegg Finalization Checklist

Use this as the operating checklist while keeping the current product design unchanged.

## Phase A: Foundation (Week 1-2)

- [ ] Wedge scope locked to home installation + simple plumbing/furniture.
- [ ] Safety exclusions explicit in UI and terms (gas, mains electrical, structural).
- [ ] Event schema live and validated:
  - `scan_started`
  - `scan_completed`
  - `hazard_flagged`
  - `step_completed`
  - `job_completed`
  - `email_collected`
  - `cta_clicked`
  - `founding_offer_clicked`
  - `tool_link_clicked`
- [ ] `/metrics` endpoint checked in production.

## Phase B: Data + Email Engine (Week 2-4)

- [ ] Email capture shown after successful scan.
- [ ] Email capture shown after job completion.
- [ ] Offer split test running:
  - [ ] Variant A: Free lifetime early access
  - [ ] Variant B: Founding member EUR29
- [ ] 5-email lifecycle set live:
  - [ ] Welcome + promise
  - [ ] Before/after use case
  - [ ] Safety credibility
  - [ ] Social proof
  - [ ] Founding offer deadline

## Phase C: Go-to-Market Loop (Week 3-8)

- [ ] Daily short-form content cadence.
- [ ] Weekly problem themes mapped (sink leak, IKEA, hinge, radiator, etc.).
- [ ] Before/after user clips collected.
- [ ] "Fix of the Week" format published.

## Phase D: Monetization Proof (Week 6-10)

- [ ] Free scans limit in place before paywall.
- [ ] Founding pass live.
- [ ] Subscription offer live.
- [ ] Tool affiliate links active and tracked.
- [ ] Unit economics tracked:
  - [ ] Conversion to paid
  - [ ] ARPU
  - [ ] API cost per successful job

## Phase E: Funding Readiness (Week 8-12)

- [ ] 60-second product demo video.
- [ ] Weekly traction graph.
- [ ] D7 / D30 cohort retention.
- [ ] Monetization proof slide.
- [ ] Data moat slide (scan taxonomy + outcomes).
- [ ] Compliance/safety roadmap.
- [ ] 12-month use-of-funds.

## KPI Targets

- [ ] 300+ emails collected
- [ ] 100+ unique scan users
- [ ] 25%+ scan-to-email conversion
- [ ] 15%+ week-1 repeat users
- [ ] Consistent week-over-week growth trend
