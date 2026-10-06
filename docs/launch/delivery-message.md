# The delivery message, sent with the CSV (SPEC 15.2)

Status: draft. Sending is Karan's. Nothing ships with a `{{ placeholder }}` still in it (see [README.md](README.md)).

> Here's the CSV for your 50. Top of the list: {{ delivery.company }}, {{ delivery.why_now }}. Two things I'd ask: thumb each row (the last column is a 10-second form), and tell me which accounts you already knew about. Receipts are in the URL columns; the method is written up here: {{ delivery.methods_link }}.

## How to fill it

- `{{ delivery.company }}`: the highest-scored row of the recipient's CSV.
- `{{ delivery.why_now }}`: one sentence built from that row's top contribution, ending with its receipt URL (open the link first; it must resolve to the posting or the sec.gov filing folder). Use the account page's own wording ("opened 4 sales roles in 7 days against a baseline of 1.5") rather than a paraphrase that adds a claim.
- `{{ delivery.methods_link }}`: the dated report `tell-data/site/methods/<name>-YYYY-MM-DD.md` on the published tell-data repo (ADR 0025; the github.com file view renders it). Never link the `-latest` copy, which moves. The link must resolve before sending.
- The last CSV column is `rate_this_row`, the prefilled feedback form link (`TELL_FEEDBACK_FORM_URL`).
- Coverage: if some of the 50 domains have no board and no CIK, say so in one extra line (the CSV's `coverage_note` column already does); do not pad the list.
