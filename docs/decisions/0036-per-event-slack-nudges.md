# 0036. Per-event Slack nudges to a watchlist's own webhook, and digest routing

- Status: proposed
- Date: 2026-10-15

## Context

- **"Nudge, not feed."** The 2026-10-15 next-steps research (`tell — Next steps research
  2026-10-15.md`, folder root) concluded that tell's wedge for founder-led sales at seed
  stage is finishing the loop that already exists: the moment a watched account crosses the
  alert threshold, the person handling the sale is told where they already work, with the
  receipt and a first read on whether the lead is worth an enrichment credit. SPEC section
  9 defines only a once-a-day digest at 08:00 local time, one message per watchlist, on one
  global `SLACK_WEBHOOK_URL`; with the ingest running up to four times a day (ADR 0029) a
  crossing scored at 20:17 UTC waited until the next morning's digest.
- **Per-watchlist webhooks exist but were never wired.** ADR 0027 put each design
  partner's Slack webhook in `tell-private/watchlists/<slug>.yaml` and said the digest
  "is expected to use it"; `tell/publish/slack.py` still posted every watchlist's digest to
  the global webhook, so a design partner could not receive their own list.
- **The job had no way to read `tell-private`.** SPEC section 2's secrets policy already
  foresaw "design-partner watchlists live in `tell-private`, read at run time by a job with
  a read-only deploy key"; `ingest.yml` checked out only the code and `tell-data`.

## Decision

1. **A nudge per crossing, on the run that scored it.** `tell notify --slack` (every
   ingest run, no longer gated on the tell-data configuration) first sends the nudges, then
   the digest under its existing timing. A nudge is one Block Kit message for one
   `score_crossed_threshold` event of one watchlist: `*{name}* ({domain}) · score {score} ·
   crossed {threshold}`, one receipt bullet per contribution (same escaping and 2900-character
   budget as the digest), the `Why now:` sentence, a `Worth enriching:` line, and a context
   line with the account page plus, when `TELL_FEEDBACK_FORM_URL` is set, prefilled
   `useful` / `not useful` links for the top contribution's signal (the CSV's `rate_this_row`
   helper). Sends are paced 1.1 s apart.
2. **Only to a watchlist's own webhook.** Nudges go to the `slack_webhook_url` of the
   watchlist's `tell-private` record (looked up by `watchlists.slug`), never to the global
   `SLACK_WEBHOOK_URL`, which keeps receiving the digest only. A watchlist without a private
   webhook is never nudged.
3. **Digest routing.** The digest of a watchlist with a private webhook goes there; every
   other watchlist's digest goes to the global webhook; a watchlist with neither is skipped
   and counted (`runs.skipped`, `without_webhook` in the JSON line). The old `no webhook`
   early return now applies only when there is no global and no private webhook.
4. **Idempotency: the `nudges` table (migration 0010).** One row per (event, watchlist):
   `status` `ok` or `failed`, `http_status` (`NULL` on a transport error), `sent_at`. A
   crossing is a candidate while it is inside the digest's 24-hour window and has no `ok`
   row for that watchlist; a `failed` row is updated in place and retried on the next run;
   after 24 hours nothing is ever nudged, so a backfill or a long outage cannot flood a
   channel. A run with at least one candidate is a `runs` row with source `slack_nudge`
   (`fetched` = candidates, `inserted` = sent, `errors`; `failed` when nothing was sent).
   `slack_nudge` is not a dead-man source.
5. **`Worth enriching` is v0 of the enrichment gate.** `yes · {family a} + {family b}` when
   at least two distinct `signal_type` values contributed with a receipt, else `not yet · one
   signal family`. A crossing already implies the profile threshold and the size band
   (`emit_crossings`). The research's full gate (already in the CRM, enriched in the last 90
   days, credit budget) lands with the CRM write path; this line is the contract the nudge
   exposes so the wording does not change later.
6. **`ingest.yml` reads `tell-private` read-only.** A new secret `TELL_PRIVATE_READ_TOKEN`
   (fine-grained PAT, repository access only `tell-private`, permission Contents: Read)
   gates a third checkout at `tell-private`; the Slack step runs
   `tell notify --slack --private-dir tell-private` on every run. Without the token the
   directory is absent, no nudge is sent, and the digest keeps going to the global webhook,
   so the change is inert until the secret exists. Nothing is written back to `tell-private`
   from the code repo's workflows. A PAT instead of the deploy key SPEC section 2 mentions:
   `actions/checkout` takes a token directly and the other private checkout (tell-data)
   already uses one, so the operator has one procedure, not two.

Interpretations made while implementing (flag if wrong):

- One `tell-private` record that does not parse or validate disables every private webhook
  for that run (logged by error type only, ADR 0027) rather than dropping one list's nudges
  silently while sending another's; the digest still goes global.
- The nudge's `crossed {threshold}` reads the event payload; an event without one (none are
  written today) shows no threshold rather than guessing the current profile value.
- "Top tier" is the existing profile `alert_threshold` (6.0, 7-day cooldown, re-arm below
  5.5): every crossing is nudged, no second threshold was added. The mid tier stays the
  daily digest and the low tier the account page and CSV.
- The private webhook is read at run time and never stored in the database, which keeps
  ADR 0027's split (the database holds company-level data and a random owner handle only).

## Consequences

- A design partner who fills in the intake form's webhook field gets their own channel:
  nudges within hours of a crossing (once a day until `TELL_RUNS_PER_DAY` is `4`) and their
  own morning digest. Karan's channel keeps the global digest and sees no nudges.
- Two new operator steps, both documented in `docs/deploy/github-actions.md`: create the
  `TELL_PRIVATE_READ_TOKEN` secret on `tell-ai`, and run migration 0010 on production before
  the first run with the token (the `nudges` table is read on every notify call once a
  private webhook exists).
- `tests/test_workflows.py` now asserts the third checkout, the ungated Slack step and the
  `--private-dir` argument; `tests/publish/test_nudge.py` covers the blocks, the gate, the
  loader, idempotency, retries, the window, routing and the CLI line.
- Volume stays inside Slack's free webhook rate: a nudge per crossing per watchlist, paced
  1.1 s apart, bounded by the 7-day cooldown per company and profile.
- Not in this ADR: interactive buttons (claim, snooze, push to CRM), which need a Slack app
  rather than an incoming webhook, and the CRM write itself.
