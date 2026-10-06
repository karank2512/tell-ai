# Method: hiring velocity

Spec: `docs/SPEC.md` section 7 (taxonomy, fingerprints, reposts, stale requisitions,
velocity) and section 8 (`hiring_velocity`: magnitude `min(new7, 10)`, half-life 30 days,
weight 1.0 for the profile's target families, 0.3 for the others). Code:
`tell/signals/taxonomy.py`, `tell/collectors/base.py` (posting state machine),
`tell/signals/velocity.py`. Tests: `tests/test_velocity.py` (acceptance),
`tests/signals/test_taxonomy.py`, `tests/collectors/test_base.py`. Decisions that go
beyond the spec's wording: `docs/decisions/0019-velocity-cold-start-and-reposts.md`
(cold start, vanished-listing reposts, long episodes) and
`docs/decisions/0020-taxonomy-interpretations.md` (keyword matching details, department
hints, non-English titles, location codes).

Hiring velocity counts net-new requisitions per role family over a 7-day window and
compares the count with the company's own 28-day baseline. Reposts, interns and stale
requisitions are removed first. The definitions below describe what the code does.

## 1. Role families

The title is lower-cased and every run of characters other than `a-z`, `0-9` and `&`
becomes a single space. The result is then matched against the ordered keyword lists of
the spec table (`FAMILY_KEYWORDS`): `sales`, `gtm_ops`, `marketing`, `customer`,
`engineering`, `ml_ai`, `data`, `product_design`, `finance_ops`, `exec`.

- **Word boundaries.** A keyword matches as whole words, with an optional suffix `s`,
  `es`, `ing`, `er` or `ers`. So `ae` matches "AE" and "AEs" but not "aerospace",
  `design` matches "Designer", and `software engineer` matches "Software Engineering
  Manager". Keywords of three letters or fewer (`ae`, `sdr`, `bdr`, `csm`, `sre`, `bi`,
  `ux`, `vp`, `seo`, `hr`, ...) take only the plural `s`, and `hr` takes no suffix at all:
  "Barista (20 hrs)" is about hours, not HR, and is `other`.
- **Spelling variants** (`KEYWORD_VARIANTS`) count as the spec keyword they spell.
  "business development representative" counts as `business development rep`, "front
  end" as `frontend`, "demand generation" as `demand gen`, and "svp"/"evp"/"avp" as
  `vp`.
- **First family wins**, in table order. One refinement: a match that lies strictly
  inside a longer match from another family is ignored. Without this, the `gtm_ops`
  keyword `revenue operations` could never win, because the `sales` keyword `revenue`
  always matches first. With it, "Revenue Operations Manager" is `gtm_ops`, while "Chief
  Revenue Officer" and "Revenue Accountant" stay `sales`, as the table says.
- **`sales engineer` and `solutions engineer` are `sales`.** The `sales` list comes
  before `engineering`.
- **`exec` only when nothing else matched.** It is last in the table, so "Head of
  Talent" is `finance_ops`, "Chief of Staff" is `finance_ops` and "VP Engineering" is
  `exec`.
- **The department hint is a tie-breaker.** It is used only when no title keyword of any
  family matched, including the `exec` keywords. Two hints are shared by two families in
  the spec table: `marketing` (gtm_ops, marketing) and `data` (ml_ai, data). A department
  alone cannot tell the ops or ML variant from the core function, so these hints go to
  the broader family. Priority: sales, marketing, customer/support, engineering, data,
  research → ml_ai, product, operations → gtm_ops, g&a → finance_ops.
- **Non-English titles are `other`.** A title is non-English when it has no ASCII letters,
  when fewer than half of its letters are ASCII (Cyrillic, CJK, Arabic script), or when
  it contains a whole word from a short list of unmistakably non-English role words
  (`NON_ENGLISH_WORDS`: "développeur", "ingénieur", "responsable", "ingeniero",
  "desarrollador", "gerente", "entwickler", "praktikant", "werkstudent", "sviluppatore",
  ...; French, Spanish, Portuguese, German, Italian). So "Développeur Backend",
  "Responsable Marketing" and "Ingeniero de Software" are `other`, as the spec asks,
  while an accent alone does not decide ("Résumé Screener") and an English title with a
  German gender marker ("Software Engineer (m/w/d)") is matched normally. Words that are
  also English words ("commercial", "director") are not listed. This is a stop-word list,
  not language detection; it stays until a translation table exists (ADR 0020). Anything
  that matches nothing is also `other`.
- **Interns and contractors** (`is_intern`): the tokens `intern`, `interns`,
  `internship(s)`, `contract`, `contractor(s)` in the title, or in the provider's
  employment type (Lever `commitment`, Ashby `employmentType`, SmartRecruiters
  `typeOfEmployment`). In a title, `contract` directly followed by a role noun is not
  tagged, because "Contract Manager" names a legal role. The posting keeps its family;
  the job event payload carries `is_intern` and velocity skips it. `job_closed` has no
  observation to read the employment type from, so it copies `is_intern` from the
  posting's own `job_opened` / `job_reposted` event (found by its deterministic
  `event_id`) and falls back to the title only when that event is gone.

## 2. Requisition fingerprint

`sha1(title_norm|department_norm|location_norm|remote_flag)`:

- `title_norm`: lower-cased, punctuation removed, seniority tokens dropped (`senior`,
  `sr`/`sr.`, `staff`, `principal`, `lead`, roman numerals `i`–`x`, level codes `l5`,
  `e4`, `ic3`, bare one- or two-digit numbers), whitespace collapsed.
- `department_norm`: lower-cased, punctuation removed (`&` kept).
- `location_norm`: `"city, country"`.
  - Only the first location counts: the string is split on `;`, `|` and a
    space-delimited " or " that does not follow a comma, so "New York or Remote" is New
    York while "Portland, OR 97201" and "Eugene, OR - Hybrid" keep their state.
  - Parenthesised notes, work-mode words ("Hybrid", "On-site") and postal codes are
    dropped.
  - Any mention of remote/anywhere/distributed gives `remote, <country>`: "Remote - US",
    "US (Remote)" and "Remote, U.S." all become `remote, us`.
  - `City, Region, …, Country` keeps the first and last parts.
  - `City, XX` means the US when `XX` is a US state code, Canada when it is a province
    code, and otherwise reads `XX` as a country. Known collision (ADR 0020): a two-letter
    country code that is also a state or province code is read as the state, so
    "Toronto, CA" is `toronto, us` and "Berlin, DE" is `berlin, us`. ATS boards write
    "City, ST" far more often than "City, CC", each board formats its locations
    consistently, and the fingerprint only has to be stable per board, so the wrong
    country does not split or merge requisitions; it would matter only if location_norm
    were ever used for geography, which it is not.
  - Country spellings fold to one code ("United States", "USA" → `us`; "UK", "England"
    → `gb`).
- `remote_flag`: `"1"`, `"0"` or `""` (unknown).

Postings with the same fingerprint on one board within 30 days are one requisition.
Velocity applies this when counting (section 5).

## 3. Reposts

When a new `external_id` appears on a board, it is compared with the board's postings
that **are not listed in the same poll**. A repost replaces an earlier listing; it does
not sit beside it. It is a repost when:

1. a posting with the same fingerprint has `closed_at` within the last 30 days
   (`closed_within_30d`; the most recently closed one is the original), or
2. a posting with the same fingerprint and the same non-null `content_hash` was last
   seen, open or closed, within the last 7 days (`same_content_within_7d`; the most
   recently seen one is the original). This covers a listing whose `external_id`
   changed before the old one had time to close, or
3. a posting with the same fingerprint is still open but vanished from the board within
   the last 30 days (`vanished_within_30d`; the most recently seen one is the original).
   Closing a posting needs two absences at least 12 hours apart, so an old listing that
   disappears in the very poll its reworded twin appears is not closed yet and rule 1
   cannot see it; rule 3 is rule 1 caught before the close is written (ADR 0019).
   Without it a reworded twin replacing a requisition older than 30 days in one poll
   counted as `job_opened`.

A repost gets `postings.repost_of` set and emits `job_reposted` instead of `job_opened`.
The event has the same key a `job_opened` would have had, and its payload carries
`repost_of_external_id` and `repost_reason`. Later `job_updated` and `job_closed` events
of the repost name the original too. When the same `external_id` comes back after a
close, it is the same posting reopened: `closed_at` is cleared and nothing is emitted.

## 4. Stale requisitions

`stale_weight(posting, now)` measures age from `posted_at` (from `first_seen_at` when the
provider gives no date). An open posting weighs 1.0 at 90 days or less, 0.25 beyond 90
days, and 0 (excluded) beyond 180 days.

`job_opened` events inside a 7-day window are dated by `posted_at`, so they are never 90
days old, and stale weighting cannot change `new7`. It applies to:

- `open_postings(company, t)`, the stale-weighted count of the company's open postings,
  which feeds the small-company rule below. A board full of 200-day-old postings does
  not make a company "large".
- `long_open_roles(company, now)`, the account page's "long-open roles" list. It holds
  every open posting older than 90 days, oldest first, with its age and weight (0.25,
  or 0.0 when excluded).

**Ghost votes** (ADR 0017) are anonymous "this looks like a ghost job" votes, stored one
row per vote in `posting_ghost_votes`. They are inert: no weight reads them until there
are at least 500 votes in total, and even then the rule that uses them is a later
decision.

## 5. Velocity

For company `c`, family `f`, instant `t`:

- **Counted posting**: a `job_opened` event of `c` whose payload `role_family` is `f`.
  It must not be tagged `is_intern` and must carry no `repost_of_external_id`. It must
  also be **net-new**: no other counted posting with the same (vendor, board,
  fingerprint) opened within the 30 days before it. The chain is followed, so a
  fingerprint re-listed every 25 days stays one requisition.
- **Cold start** (ADR 0019): a `job_opened` event flagged `first_poll` was emitted by a
  board's first poll. Those postings were already open when tell started watching, the
  requisitions that had already closed are invisible, and postings without `posted_at`
  are all dated to that poll, so counting them would read as a spike against an empty
  baseline (at launch every seed board is polled for the first time at once). They are
  never counted, in `new7` or in any of the `base` windows, but they stay in the
  same-requisition chain: a same-fingerprint posting that follows one of them within 30
  days is still the same requisition. The events exist as receipts and carry
  `first_poll: true`. The first real signal a new board can produce is therefore the
  second poll's postings against the 0.5 floor.
- `new7(c, f, t)`: counted postings with `occurred_at` in `(t - 7d, t]`.
- `base(c, f, t)`: the mean of `new7` at `t-7d`, `t-14d`, `t-21d` and `t-28d`, which are
  the four non-overlapping windows covering `(t - 35d, t - 7d]`. It is floored at 0.5.
- `ratio = new7 / base`.
- The signal **fires** when `new7 >= 3` and `ratio >= 1.5`. For `sales`, `gtm_ops` and
  `customer` it also fires when `new7 >= 2` at a company with a stale-weighted open
  posting count below 50.
- `magnitude = min(new7, 10)`.

Only companies get velocity. Events from a board not yet resolved to a company have no
`company_id`.

## 6. Episodes and the `hiring_velocity` event

`emit_velocity_events(session, now=...)` looks at every (company, family) pair with a
`job_opened` in the last 7 days, restricted to the ten families (not `other`). For each
pair that fires at `now`, it finds the **episode** the pair is in. An episode is a run of
firing days that ends once the condition has been false for 7 consecutive days. The
start is found by evaluating the condition day by day backwards from `now`, for at most
90 days.

It writes one event per episode:

| Field | Value |
| --- | --- |
| `source` / `type` | `tell` / `hiring_velocity` |
| `external_id` | `f"{company_id}:{family}"` |
| `version` | ISO date of the episode's first firing day |
| `occurred_at`, `observed_at` | `now` |
| `url` | the board URL: the public board page with the most counted postings (`boards.greenhouse.io/<token>`, `jobs.lever.co/<site>`, `jobs.ashbyhq.com/<name>`, `careers.smartrecruiters.com/<id>`), else the first counted posting |
| `magnitude` | `min(new7, 10)` |
| `payload` | `{role_family, new7, base, ratio, open_postings, episode_start, counted_postings}` |

**The receipt** is `counted_postings`, a list of `{title, posted_at, url, external_id}`
for each posting counted in `new7`, in `occurred_at` order. Each posting links to its own
page. The board URL is the signal's link.

Every run on a later day of the same episode derives the same `event_id`, because the
version is the episode's start date. As a second guard, no event is written when a
`hiring_velocity` event for the same company and family already exists at or after
`episode start - 7 days`. An earlier episode's last firing day is more than 7 days
before this one's first, so the guard cannot confuse episodes. When the walk-back reaches
its 90-day cap without finding a 7-day gap, the episode is older than the walk-back can
see and *any* earlier event for the pair marks it, so a small company that opens two
sales roles every week (the small-company rule has no ratio condition) gets exactly one
event however long it keeps firing (ADR 0019).

This differs from the simplest reading of "one event per episode", which is "skip if an
event was written in the last 7 days". That reading re-emits on day 8 of a long episode.
The walk-back finds the actual start of the episode.

## 6b. Tech keywords

`tech_mentioned_first_time` fires for a keyword of the table in
`docs/methods/tech-keywords.md` that the company has never had an event for. A board's
first poll can emit one event per keyword found anywhere on the board, up to the whole
table; those events are a baseline, not news, and carry `first_poll: true` so consumers can
tell them apart (weight 0 in the default profile either way). Events a board emitted before
it was resolved to a company (scope `vendor:identifier`, `company_id NULL`) still count as
"seen" once the board is linked, so resolving a board never makes its keywords "first" a
second time. Every event of a linked board carries the R1 audit record
(`payload["match"]`, see `docs/methods/entity-resolution.md`).

## 7. Measured numbers (filled after the soak)

These are the launch numbers spec section 7 asks for. They are measured on the
production database after at least four weeks of polling, and filled in here with the
date and the board count.

| Measure | Definition | Value |
| --- | --- | --- |
| Repost share | `job_reposted / (job_opened + job_reposted)` over the soak, first-poll events excluded | _TBD after soak_ |
| …of which `closed_within_30d` | share of reposts by reason | _TBD after soak_ |
| …of which `same_content_within_7d` | share of reposts by reason | _TBD after soak_ |
| …of which `vanished_within_30d` | share of reposts by reason | _TBD after soak_ |
| Median posting lifetime, per family | median of `closed_at - coalesce(posted_at, first_seen_at)` over closed postings, by `role_family` | _TBD after soak_ |
| Boards with any posting over 180 days | share of active boards with at least one open posting older than 180 days | _TBD after soak_ |
| Interns/contract share | share of `job_opened` with `is_intern` | _TBD after soak_ |
| Firing rate | `hiring_velocity` events per 1,000 companies per week, by family | _TBD after soak_ |

Measured on: _date_, boards: _n_, companies: _n_.
