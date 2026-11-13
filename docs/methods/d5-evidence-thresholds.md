# Tell D5: evidence thresholds

Owner decision: 13 November 2026. These are minimum evidence bars for future claim/review policy, not evidence that a claim is valid or permission to collect real data.

## Current build stays synthetic-only

The accepted Stage 1 prototype refuses real-label events on append and refuses reports containing them. No threshold, config file, flag, user count or policy document enables real collection or reporting. Enabling real data requires a separate deliberate owner decision and a separately reviewed implementation gate. This policy does not change scoring, weights, ingestion, automatic actions or production Tell.

## Tier 1: internal counts

Internal counts can begin with the first logged attempt. Keep unknowns and silence separate. An unobserved outcome is not a negative response. A closed observation window can describe no response through its cutoff, not prove that no later response will arrive. Current synthetic-only restrictions still apply.

## Tier 2: two separate bars

- **Signal precision:** at least 100 distinct rated signals from 5+ raters before claiming signal precision.
- **Offline baseline comparison:** separately, at least 100 tracked attempts across 5 users before any offline baseline comparison.

These are different denominators and different evidence tasks. Meeting one bar does not satisfy the other. Repeated ratings or corrections of one signal do not create new distinct signals; duplicate/replayed log events do not create new attempts. Reaching either bar is a prerequisite for review, not an automatic claim or reporting-mode change.

## Tier 3: weight-change proposals

At least 300 distinct labelled examples per profile, plus a held-out set meeting Tier 2, before any weight-change proposal. The held-out set must meet the applicable Tier 2 requirements independently of the development examples. Weight-change proposals always go through a reviewed PR; this policy does not authorize automatic reweighting or merging.

## Standing qualifier

Below the bars, report what was logged, never "the signal works". Above them, say "among contacted accounts", not causation. Meeting a sample-size bar does not establish that a signal caused an outcome or that a selected contacted cohort represents every account. Preserve unknowns, silence, cohort definitions, dates and selection limits alongside any later eligible analysis.

These bars do not waive source permissions, privacy requirements, schema validation, review, or the separate real-data enablement decision. They are not claims that the required examples, raters, attempts or users have already been observed.
