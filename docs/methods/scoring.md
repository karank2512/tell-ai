# Method: signals, decay, scoring

Spec: `docs/SPEC.md` section 8 (signal table, ICP profiles, `contribution()` / `score()`,
heat, the daily job, the six explainability tests, feedback loop v1) and section 5
(`signals`, `scores`, `feedback`). Code: `tell/signals/profiles.py` (ICP profiles),
`tell/signals/scoring.py` (signals, contributions, scores, crossings, heat),
`tell/signals/feedback.py` (labels, precision report), `tell/signals/cli.py`
(`tell score`). Tests: `tests/test_scoring.py` (the six spec tests and the derivation
rules), `tests/signals/test_profiles.py`, `tests/signals/test_feedback.py`. Decisions
that go beyond the spec's wording: `docs/decisions/0021-scoring-interpretations.md`.

A score is a sum of decayed contributions, one per signal. Nothing is stored that cannot
be rebuilt from `events`, and every stored contribution carries the URL of the event it
came from. The definitions below describe what the code does.

## 1. From events to signals (`build_signals`)

`signals` rows are derived from `events` rows that are linked to a company
(`company_id` set) and occurred at or before the run's `now`.

| `signal_type` | from events of type | `magnitude` | `role_family` |
| --- | --- | --- | --- |
| `hiring_velocity` | `hiring_velocity` (Phase 6 episodes) | the event's magnitude, `min(new7, 10)` | `payload.role_family` |
| `funding` | `form_d_filed` with `payload.is_amendment` false | `log10(total_offering_amount)` capped at 8; undisclosed, zero or missing → 6 | NULL |
| `leadership` | `news_leadership`; `form_8k_filed` with `payload.item_5_02` true (or `5.02` in `payload.items`) | 1 | NULL |
| `launch` | `news_launch`; `github_release` with `payload.is_flagship` true | 1 | NULL |
| `expansion` | `news_expansion` | 1 | NULL |
| `tech_adoption` | `tech_mentioned_first_time`, unless flagged `first_poll` (a board's baseline, not news) | 1 | NULL |

Worked funding magnitudes: $18,000,000 → `log10(18e6) = 7.2553`; $1,000,000,000 → 9 capped to
8; undisclosed → 6; $1,000,000 → 6 (coincidentally the undisclosed value); $500,000 → 5.70.

Each row is stamped with `weight` = the `default` profile's `w_k` for that signal (family-aware
for `hiring_velocity`, 0 for `tech_adoption`) and `half_life_days` = the `default` profile's,
exactly as the spec's `signals` DDL says; `starts_at = event.occurred_at`. The stamped values
are informational: scoring always uses the profile being scored (section 3).

**Idempotency.** The natural key is `(event_id, signal_type, role_family)`. `role_family` is NULL
for every type but `hiring_velocity`, and PostgreSQL treats NULLs as distinct in a unique
constraint, so `ON CONFLICT DO NOTHING` alone would duplicate them; `build_signals` reads the
existing keys of the candidate events first and skips them. A rerun inserts zero rows
(`test_build_signals_is_idempotent_including_null_role_family`).

**Not signals.** `news_funding` headlines (the spec fires `funding` on Form D only, the filing is
the receipt); Form D amendments (`D/A` merges into the original event's payload upstream and
never creates a second event; an unlinked `D/A` is stored as `form_d_amendment`, which no
rule reads); `form_8k_filed` without Item 5.02; non-flagship GitHub releases; events whose
company is unknown.

**Amended amounts.** A `D/A` that changes the offering amount is merged into the original
`form_d_filed` payload by the collector. The next `build_signals` notices that the payload now
derives a different magnitude and rewrites the existing `funding` signal's `magnitude` in
place (nothing else on the row changes, and no second signal is ever created). So a Form D at
$18,000,000 (`7.2553`) amended to $400,000,000 becomes `8` (`log10(4e8) = 8.6`, capped), the
next day's contribution is computed from `8`, and the summary beside it reads
`"Form D: $400,000,000 offering"`: the number and its receipt always come from the same payload,
and `signals` stays rebuildable from `events`
(`test_amended_offering_amount_rewrites_the_funding_magnitude_in_place`, ADR 0021 item 8).

## 2. ICP profiles (`profiles.py`)

A profile is the spec's YAML, validated by a pydantic model with `extra="forbid"`, plus one
key the spec describes in prose, `alert_threshold` (default 6.0):

| key | `default` | `job-seeker` |
| --- | --- | --- |
| `target_families` (weight 1.0 on `hiring_velocity`) | `sales, gtm_ops, customer` | `engineering, ml_ai, data` |
| `other_families_weight` | 0.3 | 0.3 |
| `weights.funding` | 4.0 | 2.0 |
| `weights.leadership` | 2.0 | 0.5 |
| `weights.launch` | 1.5 | 1.0 |
| `weights.expansion` | 1.0 | 1.0 |
| `weights.tech_adoption` | 0 | 0.5 |
| `half_life_days` (`hiring_velocity, funding, leadership, launch, expansion, tech_adoption`) | 30, 90, 60, 45, 60, 90 | same |
| `tech_keywords` | `[]` | `[]` |
| `size_band` (open postings) | 3 to 400 | 3 to 2000 |
| `alert_threshold` | 6.0 | 6.0 |

`job-seeker` is the profile of a new CS graduate looking for an engineering job: spikes in the
families they apply to weigh 1.0, funding still matters (new money is new headcount) but less
than for a seller, a launch or new office is a modest growth sign, a leadership change says
little about entry-level hiring, and any new tool a company starts naming in postings is mildly
interesting (`tech_adoption` 0.5 with an empty keyword list, so every keyword counts). The band
is wide because a graduate applies to startups and large companies alike.

`weight_for(signal, keyword=None)`:

- `hiring_velocity`: 1.0 when the signal's `role_family` is a target family, otherwise
  `other_families_weight`.
- `tech_adoption`: the profile weight when `keyword` is in `tech_keywords` or that list is
  empty; 0 otherwise. `signals` rows have no payload, so the keyword is read from the
  originating event's payload at scoring time (`SignalView.keyword`).
- every other type: the profile weight. Unknown types weigh 0.

`profile.half_life_days[signal_type]` works as in the spec pseudo-code.

**Loading.** `load_profiles()` reads `tell/profiles/*.yaml` and then, when the sibling
`../tell-private/profiles/` exists, its `*.yaml` too; a private profile replaces a shipped one
of the same `name`. Profiles are keyed by their `name` field, not the file name (the shipped
files happen to agree). Two profiles with one name in one directory is an error.

## 3. Contributions and scores

```python
def contribution(sig, profile, t):
    w = profile.weight_for(sig)  # family-aware for hiring_velocity
    hl = profile.half_life_days[sig.signal_type]
    age_days = (t - sig.starts_at).total_seconds() / 86400
    if age_days < 0 or age_days > 4 * hl:  # before the signal, or past four half-lives
        return 0.0
    return w * float(sig.magnitude) * 0.5 ** (age_days / hl)


def score(company_id, profile, t):
    parts = [(s, contribution(s, profile, t)) for s in active_signals(company_id, t)]
    parts = [(s, c) for s, c in parts if c > 0]
    return sum(c for _, c in parts), parts
```

`active_signals(company_id, t)` are the company's signals with `starts_at <= t` and within
`4 * longest half-life` days of `t` (360 days for both shipped profiles); a signal of a
shorter-lived type inside that window but past its own four half-lives contributes 0 and is
dropped from the list. Zero-weight contributions (`tech_adoption` under `default`) are dropped
the same way: a stored contribution is always positive.

**The scoring instant** for a calendar date `as_of` is the end of that UTC day
(`23:59:59.999999`), so every event of the day counts. Decay numbers are therefore functions
of `(as_of, starts_at)` only and a rerun reproduces them.

**What is stored.** `scores(company_id, icp_profile, as_of)` holds `score` (the exact float
sum, as `numeric`) and `contributions`, a JSON list sorted largest first:

```json
{"signal_id": 17, "signal_type": "funding", "role_family": null, "contribution": 26.91,
 "url": "https://www.sec.gov/Archives/edgar/data/.../", "occurred_at": "2026-09-25T00:00:00Z",
 "summary": "Form D: $18,000,000 offering, $5,000,000 sold"}
```

Summaries: `"4 new sales roles in 7 days vs 0.5 baseline"` (velocity), `"Form D: $… offering[, $…
sold]"` / `"Form D: undisclosed offering"` (funding), the headline (news; leadership headlines
arrive already redacted, ADR 0016), `"Release v2.0.0 of acme"` (GitHub), `"8-K Item 5.02 …"`,
`"First posting mentioning kubernetes"` (tech). A company scored with no active signal gets a
row with score 0 and an empty list, so the day is accounted for (the crossing logic reads it).

**What is rebuilt.** Everything: `signals` from `events`, `scores` from `signals`. The six
explainability tests prove the arithmetic: sum to 1e-9 over 200 companies × 5 dates, monotone
decay, exact halving after one half-life, no orphan contribution (every one traces to a
`signals` row, an `events` row and a non-empty URL), profile isolation, and threshold hysteresis.

## 4. Size band

The raw score is computed and stored for every scored company, inside the band or not, so the
sum test holds everywhere and a page can always explain a number. The band gates what is acted
on: a company whose stale-weighted open-posting count (`velocity.open_postings`) at the scoring
instant is outside `[min_open_postings, max_open_postings]` never emits
`score_crossed_threshold` and is left off in-market lists (`ScoreResult.in_band`,
`in_size_band()`). A company with no active ATS board has no measurable size and counts as in
band: the filter is about company size, not about what tell can see.

## 5. Daily job, episodes and crossings

`tell score all --as-of D` (or `score_all`) runs every shipped and private profile:

- the `default` profile scores every company with at least one active signal (the public site
  and the weekly page read these scores);
- every other profile scores the companies on watchlists whose `icp_profile` is that profile.

`tell score run --profile X --as-of D` does one profile. Both then run crossing detection.

A `score_crossed_threshold` event (`source="tell"`, `type="score_crossed_threshold"`,
`external_id=f"{company_id}:{profile}"`, `version` = the `as_of` ISO date, `occurred_at` =
`observed_at` = the run instant, `url` = the top contribution's URL, `magnitude` = the score,
payload `{profile, as_of, score, previous_score, previous_as_of, threshold,
top_contributions[:2]}`) is written when all of the following hold:

1. the company is in the profile's size band and has at least one contribution;
2. today's score is at or above `alert_threshold`;
3. the most recent stored score before `as_of` for this company and profile was below the
   threshold (no earlier score counts as 0, so a company's first scored day can fire);
4. **cooldown**: no crossing event for this company and profile whose scored day
   (`payload.as_of`) is within the last 7 days of `as_of`;
5. **re-arm**: if a crossing ever fired, some stored score since that crossing's `as_of` was
   below `threshold - 0.5`.

The crossing is something tell observed, so the event is stamped with the instant the run
wrote it, never with the end-of-day scoring instant (which would place it in the future). The
scored day travels in `version` and `payload.as_of`; the cooldown and the re-arm compare those
dates and ignore `occurred_at`, so a late backfill of an old day neither suppresses nor
fabricates a crossing on a later day
(`test_crossing_defaults_to_the_wall_clock_and_cooldown_compares_scored_days`).

The deterministic `event_id` makes a same-day rerun a no-op. A score oscillating around 6.0 by
less than 0.5 therefore fires once, on the first upward crossing, and never again until it has
genuinely cooled off (`test_6_oscillation_under_half_a_point_alerts_at_most_once_per_week`,
`test_crossing_rearms_after_dropping_half_a_point_and_seven_days`).

An *episode* is the run of days a company sits above the threshold; the crossing event marks
its first day and the Slack digest keys off that event. Nothing is emitted when a score falls.

## 6. Heat

Heat is a 0 to 100 percentile rank of raw scores within one watchlist, shown beside the raw
sum: for each company, the share of the other companies on the list scoring strictly lower,
ties counting half, rounded. The unique top is 100, the unique bottom 0, two equal scores on a
two-company list are both 50, and a one-company list is 100 when its score is positive and 0
otherwise. Companies on the list without a stored score for the day count as 0.
`heat_for_watchlist(watchlist_id, as_of)` reads the stored scores of the watchlist's profile.
Heat is display-only and never stored.

## 7. "Why not higher"

`why_not_higher(company_id, profile, as_of)` lists the company's signals whose contribution
decayed to zero in the last 30 days, that is whose `starts_at + 4 * half_life` (the profile's
half-life for the type) falls in `(t - 30d, t]`, newest expiry first, each with its URL and
summary. The account page footer shows them so a reader can see what the score used to carry.

## 8. Feedback loop (v1)

`feedback.verdict` labels: `useful`, `worked`, `booked`, `won` are positives; `not_useful`,
`already_knew` are negatives. `tell score feedback-report --profile X` prints precision by
`signal_type` and `role_family` for the feedback of watchlists bound to that profile as a
Markdown table with a totals line. While a profile has fewer than 300 labels the report says
"weights stay hand-tuned"; nothing here changes a weight. Past 300 labels the spec allows a
logistic re-weighting to be *proposed* as a PR with the before/after precision in its
description; that is a later phase.
