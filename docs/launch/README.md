# Launch asset drafts

Drafts of the launch copy from SPEC section 15.1-15.4, plus how to fill them. **Posting is Karan's. The agent never sends, posts or contacts anyone, and nothing ships while any `{{ placeholder }}` is still in the text.** A number that has no dated report behind it is deleted, not rounded up.

**Lead-time medians are quoted only from a dated report with N of at least 100 and full 60-day follow-up that marks them quotable** (that covers a median or p75 of the days between a Form D and its first press mention, any "days before press" figure and a "filing first" share). Until then the only speed claims are these two, each with its N: "of 331 Form D filings tracked, 44 (13.3%) had verified press within 60 days" and "29.1% of new Delaware-corporation issuers linked to a company at 59 of 59 precision" (2026-10-15 verified report; ADR 0041).

| Draft | SPEC | Purpose |
| --- | --- | --- |
| [50-domains-dm.md](50-domains-dm.md) | 15.1 | The cold and warm "send me 50 domains" message and the one-line follow-up |
| [delivery-message.md](delivery-message.md) | 15.2 | The message that goes with the scored CSV |
| [linkedin-post.md](linkedin-post.md) | 15.3 | The launch post (week 8) |
| [show-hn.md](show-hn.md) | 15.4 | Show HN title and text, with posting rules |
| [numbers.yaml](numbers.yaml) | 13 | Maps every placeholder to a report file and field |

## Placeholders

Every bracketed number in the SPEC text is a Jinja-style placeholder that names the report line it comes from, for example `{{ reports.speed_vs_press.n }}` (report `speed-vs-press`, the filings-with-press count). [numbers.yaml](numbers.yaml) maps each one to the dated report file (under `tell-data/site/methods/`, where `weekly.yml` publishes them, ADR 0025), the exact label of the field or table cell inside it, the drafts that use it and its status (`pending soak` until a dated report with a real N exists). `tests/backtest/test_launch_numbers.py` checks that every label is a line the report code writes. The `inputs` block lists the placeholders that are not report numbers (recipient first name, the one warm-variant sentence, the top company on a CSV, links); those are typed by hand at send time and are never stored here.

## How the reports are produced

`tell backtest refresh --out DIR` runs the section 13 studies (speed versus press, hiring velocity as a leading indicator, noise handling, operations) and writes a dated Markdown report `<name>-YYYY-MM-DD.md` for each, plus a `<name>-latest.md` convenience copy and an index in `DIR/README.md`. Each report has the question, the cohort, N, the query or script path, the numbers, the caveats and the date. `weekly.yml` runs it every Monday with `--out tell-data/site/methods` and commits the result to tell-data; the reports never land in this repo by a workflow (ADR 0025), and a maintainer who regenerates one by hand into `docs/methods/` commits it under their own name. Nothing is quoted before the soak after cutover has produced real data. `tell backtest list` prints the study names.

## Filling a draft by hand (until a renderer exists)

There is no renderer yet. A tiny one would do exactly this, and a person can do it in a few minutes:

1. Open the newest dated reports in `tell-data/site/methods/` (or run `tell backtest refresh --out <dir>` against the production database).
2. For each `{{ ... }}` in the draft, look it up in `numbers.yaml`, open the dated report its glob names (never the `-latest` copy) and copy the number from the line with exactly that label. Copy the report's date and N too.
3. If a report's N is under 30, quote the N beside the number, or drop that clause.
4. Replace the placeholder with the number. Fill `inputs` placeholders by hand.
5. Search the draft for `{{` and `[`: both must be gone. Open every link in the draft.
6. Read the result against the report once more: every number in the LinkedIn post and the Show HN text must trace to a report line (SPEC 16, M4).

A renderer, when someone writes one, should load `numbers.yaml`, read each report's labelled field, substitute it into the draft with Jinja in strict-undefined mode (a missing value fails loudly), and refuse to write output while any `status` is `pending soak`.

## Recording the README GIF

The README opens with a 30-second GIF of an account page with its receipts visible. There is no image yet and none is faked.

1. Publish a real account page (`tell publish --out DIR`, or open the deployed `site/companies/<domain>.html`) for a company whose events are all company-level (no person's name on screen).
2. Screen-record about 30 seconds: the score and heat, a contribution bar, a click through to the receipt (a posting or the sec.gov filing folder), the counted postings, the "why not higher" footer.
3. Convert to a GIF under about 5 MB (for example 800 px wide, 10 fps), save it as `docs/launch/account-page.gif`, and replace the placeholder note near the top of the README with an image link.
4. The same page, as a still, is the one image for the LinkedIn post.

## Warm variant and follow-up rules (SPEC 15.1)

- Warm variant: replace the first line with one sentence about the one thing that person told Karan in their own earlier conversation. Never use a detail from someone else's conversation.
- Follow-up after 5 days, one line: "Still happy to run this on 50 domains if you send them over." No third message.
- Who gets the first offers is Karan's list (SPEC 17); the agent never contacts anyone.

## Checks before anything is posted

- [ ] No `{{` and no bracketed `[N]`-style number remains.
- [ ] Every number traces to a dated report line in `numbers.yaml` (SPEC 16, M4).
- [ ] Every link opens.
- [ ] No Workday polling claim anywhere (ADR 0003).
- [ ] "$0 a month" still describes the deployed stack.
