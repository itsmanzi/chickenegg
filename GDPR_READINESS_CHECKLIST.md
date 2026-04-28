# Chickenegg GDPR Readiness Checklist

Use this as an execution checklist before scaling in NL/EU.

## 1) Legal Basis and Transparency
- [ ] Define legal basis for each data flow:
  - app usage analytics (legitimate interest or consent)
  - account/email capture (consent or contract)
  - safety logs/outcomes (legitimate interest)
- [ ] Publish clear Privacy Policy with:
  - what data is collected (photos, events, email, device/network metadata)
  - why it is collected
  - retention periods
  - third parties/processors
  - user rights and contact method
- [ ] Add in-app "Privacy" link in onboarding and settings.

## 2) Processor Compliance
- [ ] Sign Data Processing Agreement (DPA) with AI provider.
- [ ] Sign DPA with hosting/database providers.
- [ ] Maintain a processor list with purpose + region.
- [ ] Document cross-border transfer mechanism (SCCs where needed).

## 3) Data Minimization
- [ ] Store only required fields for safety, product quality, and support.
- [ ] Avoid storing raw images by default unless required.
- [ ] If image retention is needed, define strict TTL (for example 7-30 days).
- [ ] Pseudonymize user identifiers where possible.

## 4) Security Controls
- [ ] Keep API keys server-side only.
- [ ] Enforce HTTPS everywhere.
- [ ] Add rate limits and abuse controls on API routes.
- [ ] Encrypt data in transit and at rest.
- [ ] Restrict production DB access by role and IP where possible.
- [ ] Remove secrets from logs/errors.

## 5) User Rights Workflow
- [ ] Implement export endpoint/process (access request).
- [ ] Implement delete endpoint/process (right to erasure).
- [ ] Implement correction request process.
- [ ] Add identity verification step before fulfilling requests.
- [ ] Define SLA for rights requests (within 30 days).

## 6) Retention and Deletion
- [ ] Define retention schedule per table:
  - events
  - emails
  - outcomes
  - support tickets
- [ ] Implement scheduled cleanup jobs.
- [ ] Log deletion operations for audit.

## 7) Consent and Sensitive Flows
- [ ] Capture consent for marketing emails separately.
- [ ] If cookies/tracking beyond strict necessity are used, add consent banner.
- [ ] Add explicit warnings for high-risk repair categories.
- [ ] Keep server-side hard-stop safety policy active.

## 8) Incident Response
- [ ] Maintain incident runbook (detect, contain, assess, notify).
- [ ] Define breach notification path (authority + users when required).
- [ ] Keep contact details and escalation owners documented.

## 9) Documentation (Accountability)
- [ ] Maintain Record of Processing Activities (ROPA).
- [ ] Maintain risk register (safety + privacy + abuse).
- [ ] Run and document DPIA if high-risk processing expands.
- [ ] Version-control policy and checklist updates.

## Quick "Done This Week" Targets
- [ ] DPA signed with AI provider
- [ ] Privacy Policy published
- [ ] Data retention policy defined
- [ ] User delete/export workflow documented
- [ ] Security baseline (server-side keys, rate limits, DB access control) verified
