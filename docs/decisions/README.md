# Decision records

Template: [0000-template.md](0000-template.md). Each record covers one locked decision from `docs/PLAN.md`.

| ADR | Decision |
| --- | --- |
| [0001](0001-license-apache-2.0.md) | License is Apache-2.0 |
| [0002](0002-repos-under-personal-account.md) | Repos live under the personal GitHub account |
| [0003](0003-drop-workday-oracle-collectors.md) | Drop the Workday and Oracle collectors |
| [0004](0004-ci-postgres-service-container.md) | CI uses a Postgres 16 service container |
| [0005](0005-local-first-dev-database.md) | Local-first dev database |
| [0006](0006-gold-set-location-and-public-sample.md) | Gold set location and public sample |
| [0007](0007-collectors-fresh-from-spec.md) | Collectors are built fresh from the spec |
| [0008](0008-optional-code-included-not-deployed.md) | Optional code is included but not deployed |
| [0009](0009-yc-directory-only-seed.md) | Seed domains come from the YC directory only |
| [0010](0010-pypi-tell-mcp-only.md) | Publish only tell-mcp to PyPI |
| [0011](0011-venv-outside-icloud.md) | Keep the uv environment outside iCloud-synced folders |
| [0012](0012-tell-mcp-as-workspace-package.md) | tell-mcp is a uv workspace package, not `tell/mcp/server.py` |
| [0013](0013-postings-version-marker.md) | `postings.version_marker` holds the provider change marker |
| [0014](0014-company-feeds-table.md) | `company_feeds` keeps discovered company RSS/Atom feeds |
| [0015](0015-ats-404-and-reverification.md) | One 404 / dead-board rule for every ATS collector |
| [0016](0016-news-headline-redaction.md) | Leadership news headlines are stored with the person's name redacted (provisional, pending confirmation) |
| [0017](0017-posting-ghost-votes.md) | `posting_ghost_votes` stores anonymous job-seeker ghost-job votes (inert until 500) |
| [0018](0018-entity-resolution-rule-refinements.md) | Entity-resolution refinements beyond the SPEC wording: R3 tie to review, R4 over the whole table, token-superset guard, international suffixes, R1 audit record, one link function, name-only headline metric |
| [0019](0019-velocity-cold-start-and-reposts.md) | Hiring velocity: first-poll events flagged and never counted, `vanished_within_30d` repost rule, capped walk-back marks the whole episode |
| [0020](0020-taxonomy-interpretations.md) | Role-family taxonomy interpretations: keyword masking, shared department hints, stale-weighted small-company count, non-English stop words, short-keyword suffixes, location code collision kept |
| [0021](0021-scoring-interpretations.md) | Scoring interpretations: size band gates alerts not storage, 7-day cooldown + 0.5 re-arm hysteresis, end-of-day scoring instant, `default` scores every active company, `news_funding` and first-poll tech events carry no weight, heat ties count half |
