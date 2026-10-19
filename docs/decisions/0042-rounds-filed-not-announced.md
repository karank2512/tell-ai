# 0042. Rounds filed, not announced: a weekly list of original Form D filings with no funding headline yet

- Status: proposed
- Date: 2026-10-19

## Context

SPEC section 9's weekly page carries the top 25 by sales hiring, the top 25 new Form D
filings by amount, the method line, coverage numbers and the repo link. The founder wants
one more piece of weekly, public, data-as-content built from public filings only, every
row with its EDGAR receipt, to post on LinkedIn: the rounds that were filed but that no
funding headline has followed yet. The speed-vs-press report (SPEC 13.1,
`tell/backtest/speed_vs_press.py`) already defines the trusted Form D cohort and the press
join; this list names the filings behind that join instead of counting them. SPEC section 9
does not describe this output and `docs/SPEC.md` is read-only, so the addition is recorded
here.

## Decision

1. **Rows.** Every `form_d_filed` event from EDGAR that is an original filing
   (`is_amendment` false: an amendment never appears) of an operating company (not a pooled
   fund by `tell.resolve.guards.fund_signals`, guard (e) of ADR 0034), filed in the 14 days
   that end with the edition's ISO week (the week and the week before), and linked by a
   link the 13.1 cohort trusts: rules R0-R4 with no `payload.match.guard_failed`, a
   reviewer's link (`rule = 'manual'`), or a CIK match whose CIK has a trusted
   `cik_source` or was carried by such a link. `tests/publish/test_rounds.py` checks every
   link case against `speed_vs_press.compute`, so the two rules cannot drift apart
   silently.
2. **Not announced.** The filing's company has no `news_funding` event dated from the
   filing date minus 14 days through the publish instant. Any funding headline counts,
   including the ones 13.1 discounts for one-word brands (there a headline must carry the
   full legal name): on a public page a false "not announced" is the worse error. The 13.1
   window's far end (filing + 60 days) does not apply; the list is about now.
3. **Privacy.** Only companies that already have a public account page (a signal starting
   in the last 180 days, the size budget of ADR 0023) are listed, and never one whose
   `companies.source` is `manual`. The publish code applies no public/private rule to
   companies beyond the size budget, so this one is added here: `manual` rows are intake
   lists, review verdicts and domains added by hand with `tell discover`, and listing them
   would let anyone read a customer's private list off the page. Watchlists are never read.
   The name shown is the company's (`companies.name`, as on its account page); no person's
   name is read or shown (Form D related persons are never stored).
4. **Columns.** Company (linked to its account page), domain, filing date, offering amount
   and amount sold exactly as the payload stores them, and the receipt: the filing's EDGAR
   folder on `www.sec.gov` (the JSON schema pins that prefix). An amount column is shown
   only when at least one row states that amount; an indefinite offering reads
   "indefinite" and a row without the amount "undisclosed". Newest filing first, then
   domain. Under the table, one sentence of method: the filing window, the cohort, the press
   window and that the list changes as news arrives. No percentage or other statistic is
   printed.
5. **Where.** A section of `weekly/{ISO-week}.html`, its own page
   `rounds-not-announced.html` and `api/v0/rounds-not-announced.json` (`ROUNDS_SCHEMA` in
   `tell/publish/json_api.py`, validated before writing and again by `validate_site_tree`).
   `tell weekly` (Monday) and every nightly `tell publish` write all three for the current
   edition. `api/v0/weekly/latest.json` is unchanged: it stays fixed for the week (ADR 0023)
   and its readers (`tell-mcp`) keep their contract.

## Consequences

- A filing stays on the list for two consecutive editions while no headline arrives.
- ADR 0023's "the weekly page is stable for a week" no longer holds for this one section:
  the renders after each ingest during the following week drop rows as headlines arrive.
  The archived page keeps the list as it last stood.
- "Not announced" means no funding headline in tell's news data. Google News coverage of
  small companies is incomplete, scheduled news polls ask for the last 7 days only, and a
  company the news collector has not polled since the filing is listed like one it has; the
  method sentence says "in tell's news data" for that reason.
- Account pages themselves have no source rule: a `manual` company with a signal in the last
  180 days still gets an account page. That is outside this decision and left for a
  separate change.
