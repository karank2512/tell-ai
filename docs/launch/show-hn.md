# Show HN (SPEC 15.4)

Status: draft. Posting is Karan's, and nothing ships with a `{{ placeholder }}` still in it (see [README.md](README.md) and [numbers.yaml](numbers.yaml)).

Title: `Show HN: Tell – explainable buying signals from job boards and SEC filings`

> I built a scanner to watch ATS boards (Greenhouse, Lever, Ashby, SmartRecruiters) during my job search, then noticed hiring velocity is a buying signal: a company opening six SDR roles is about to buy sales tools. Tell is that scanner pointed at sales teams.
>
> It polls public job-board JSON, SEC EDGAR Form D filings and news feeds, resolves everything to a company domain, and computes an additive score with per-signal exponential decay, so every score decomposes into named, dated, source-linked events. The parts that took real work: entity resolution across sources (careers-page name vs EDGAR filer name), telling reposts and ghost jobs from real hiring, and keeping ingestion idempotent so reruns add zero rows.
>
> It runs on GitHub Actions cron, Neon's free Postgres and GitHub Pages: $0 a month. Numbers so far: {{ reports.ops.companies_monitored }} companies and {{ reports.ops.postings_tracked }} postings tracked, p95 freshness {{ reports.ops.freshness_p95_h }} hours, {{ reports.ops.success_rate_pct }}% of runs succeeded over {{ reports.ops.weeks_unattended }} weeks unattended, {{ reports.noise.repost_share_pct }}% of postings are reposts, and {{ reports.noise.stale_board_share_pct }}% of boards carry a role open for more than 180 days. What it deliberately does not do: scrape LinkedIn or store anything about people.
>
> Repo: {{ links.repo }}. Methods write-up with the measured repost rates: {{ links.methods }}. I'd like feedback on the scoring model and on public sources I'm missing.

## Deviation from the SPEC text

The SPEC draft lists Workday among the polled vendors. Workday and Oracle collectors were dropped (ADR 0003): discovery detects those boards and records them as `unsupported`, nothing polls them. The vendor list above names the four that are actually polled. Do not put Workday back.

## Posting rules (SPEC 15.4)

- Post between 8 and 10 am Eastern on a Tuesday or Wednesday.
- Answer every comment for the first six hours.
- Never argue about the ATS vendors' terms; link the per-source legal notes instead ([docs/sources/README.md](../sources/README.md)).
- The "numbers so far" sentence is replaced by only those bullets whose report exists and is dated; if the soak has not produced a number, delete that clause rather than keeping it as a placeholder.
- Before posting, confirm "$0 a month" and "Neon's free Postgres" describe the deployed stack at that moment.
