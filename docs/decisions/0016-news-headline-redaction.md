# 0016. Leadership news headlines are stored with the person's name redacted

- Status: proposed (provisional orchestrator decision, pending Karan's confirmation)
- Date: 2026-10-05

## Context

SPEC section 3.7 says the news collector stores the headline ("Title, URL, `pubDate`,
source name, matched regex"). The project's PII rule (CLAUDE.md, SPEC section 12) says
no column, fixture, log line or payload holds a person's name. The two clash on one
category: leadership headlines ("Acme appoints {First Last} as CFO") almost always name a
person, while funding, launch and expansion headlines name companies and products.

Dropping leadership news would lose the section 8 `leadership` signal; storing the
headline as is would break the PII rule. The SPEC does not settle this, so it is a
business-rule decision (section 17 style). The orchestrator took the decision below
so the collector can ship, and it stays **provisional until Karan confirms or changes
it**.

## Decision

- `news_funding`, `news_launch`, `news_expansion`: `payload.title` is the headline,
  unchanged (as before), and `payload.title_redacted = false`.
- `news_leadership`: the raw headline is **never** stored. `payload.title` holds a
  redacted headline and `payload.title_redacted = true`. The article URL
  (`events.url`) stays the receipt for the full headline.
- Redaction is a deterministic heuristic on the whitespace-collapsed headline
  (`tell.collectors.news_classify.redact_leadership`):
  1. After a leadership verb (`appoints`, `names`, `hires`, `promotes`, `taps`,
     `welcomes`, and also `succeeds`, `succeeding`, `replaces`, `replacing` so an
     outgoing executive is caught too), the run of 1-4 capitalised tokens is the
     person. It counts only when it ends at a safe boundary: the end of the
     headline, ` as `, ` to `, ` its `, ` the ` (plus a few similar words such as
     `new`, `for`, `from`, `at`), a role word (`Chief`, `CEO`, `President`, ...), a
     separator (`-`, `|`) or a colon. Role and boundary words never join the run, so
     "Appoints {First Last} As Chief Revenue Officer" keeps its role. Initials and short
     honorifics (`Q.`, `Dr.`) stay inside the run.
  2. "X joins Acme as CFO": the run of 1-4 capitalised tokens right before ` joins`.
  3. A second person joined by `and` / `&`, or listed after a comma or semicolon
     once a leadership verb has been seen ("..., {Other Name} as COO"), is redacted the
     same way.
  4. Every span found is replaced with `[person]` and the rest of the headline is kept.
  5. When no span is found, or any run after a verb is not delimited (a comma
     apposition, `and` followed by more text, a fifth capitalised token), only the role
     phrase is kept, built from the regex groups: the verb, an `as`/`to` connector,
     qualifiers such as `new`, and the role ("appoints new chief revenue officer",
     "names as chief financial officer"). Without a recognisable role it is
     "<verb> [person]"; without a verb, "[person]".
- Tests use obvious placeholder names only inside test code strings, never
  in fixtures. `tests/test_news_classify.py` and `tests/collectors/test_news.py` prove
  the redaction and assert that no stored leadership title contains the capitalised run.

## Consequences

- The PII rule holds for news payloads; SPEC section 3.7's "store the title" holds
  for three categories out of four, and for leadership in redacted form. This record
  is the documentation of that difference; `docs/SPEC.md` is unchanged.
- The heuristic errs toward over-redaction: a capitalised company or product right
  after a verb can become `[person]`, and an undelimited headline keeps only its role
  phrase. Readers who need the full headline follow the URL.
- Known limits: in a leadership headline, a name written in lowercase or a name
  elsewhere in the headline that no rule above reaches (for example after "led by" or
  "under") can survive. A run longer than four tokens right after a verb is not
  delimited, so it falls back to the role phrase and is not leaked. Funding, launch and
  expansion headlines are stored unchanged by this decision, so a person named in one
  ("... round led by <investor name>") is stored too. If Karan wants a stricter rule,
  the simplest is to store only the role phrase for every leadership headline, and to
  run the same redaction over the other categories.
- If Karan rejects this decision, the alternatives are: store no title for leadership
  events (the URL and `matched_regex` only), or drop the PII rule for headlines. Either
  is a change to `derive_events` and this record.
