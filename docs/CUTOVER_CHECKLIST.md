# Cutover checklist (Phase 13)

Derived from `docs/PLAN.md` Phase 13. Nothing here is needed before code-complete.

## Pre-flight decisions

- [ ] Set `SEC_USER_AGENT` with a real contact address, then re-record the hand-crafted EDGAR fixtures with `make refresh-fixtures` and diff.
- [ ] Confirm or reverse ADR 0016 (news headline redaction for leadership events).
- [ ] Confirm ADR 0015 (ATS 404 rule).

## Steps

- [ ] 1. Install `gh` CLI and authenticate; create `tell` (public), `tell-data` (public, Pages enabled on `site/`), `tell-private` (private); push.
- [ ] 2. Create a Neon free project and store `DATABASE_URL` as an Actions secret; run `alembic upgrade head`; seed the YC directory; run a supervised first discovery pass, spread over days per the 300/run budget.
- [ ] 3. Create a fine-grained PAT (contents:write, `tell-data` only) as `TELL_DATA_PUSH_TOKEN`; add a read-only deploy key for `tell-private`.
- [ ] 4. Create a Slack incoming webhook as `SLACK_WEBHOOK_URL`; pick the SEC contact email for `SEC_USER_AGENT`.
- [ ] 5. Create a PyPI account and trusted publishing for `tell-mcp`; add the tag-triggered publish workflow.
- [ ] 6. Run `ingest.yml` supervised; verify the health page, digest and account pages against live boards (SPEC section 16 M1/M2 hand-checks).
- [ ] 7. Begin the unattended soak: M2-M4 sign-offs (digest two mornings untouched, reports with real N, README quickstart on a clean machine) are calendar-bound.
