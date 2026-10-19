# LinkedIn launch post (SPEC 15.3)

Status: draft, about 180 words once filled. Posting is Karan's, in week 8 of the plan, and only after every placeholder below is filled from a dated report (see [README.md](README.md) and [numbers.yaml](numbers.yaml)). Nothing ships with a `{{ placeholder }}` in it.

> I built a tool to watch companies' job boards for my job search. Around week two I realized I had accidentally built sales software.
>
> A company that opens four sales-engineer roles in a week is about to buy sales-engineering tools. The posting is public, timestamped and free. So is the Form D they filed last Tuesday.
>
> tell reads those public sources and turns them into buying signals with receipts. Every score is a list of dated events, each linked to the filing or posting behind it. No LinkedIn scraping, no personal data.
>
> What it caught in its first {{ reports.ops.weeks_live }} weeks:
>
> - Of {{ reports.speed_vs_press.cohort_n }} Form D filings tracked, {{ reports.speed_vs_press.n }} ({{ reports.speed_vs_press.press_share_pct }}%) had verified press within 60 days
> - {{ reports.velocity_lead.spike_share_pct }}% of {{ reports.velocity_lead.n }} funding events had a hiring spike in the 8 weeks before
> - {{ reports.ops.companies_monitored }} companies watched, {{ reports.ops.postings_tracked }} postings tracked, p95 freshness {{ reports.ops.freshness_p95_h }} hours, $0 a month to run
>
> It's open source: {{ links.repo }}. If you're in RevOps or sales and want to try it on your accounts, send me 50 domains and I'll send back a scored CSV.

## Image

One image: the account page for a real company with the receipts visible. No carousel. (The same capture feeds the README GIF; see [README.md](README.md).)

## Rules before posting

- Each number comes from the report line named in `numbers.yaml`; the report must be dated and show its N. If a study has N under 30, quote it with the N or drop the bullet.
- The second bullet is the prospective study (SPEC 13.2). The retrospective version is rougher; if only that exists, say "retrospectively" in the bullet. The SPEC text says "60 days"; the report measures the 8 weeks (56 days) before the filing, so the bullet says "8 weeks" to match its source line.
- The first bullet is the press-share claim, worded as in the rule in [README.md](README.md), with both its Ns: `cohort_n` (the Form D filings tracked) and `n` (the filings with press). Fill it from the report that rule cites, or a later dated report that supports the same sentence; a newer report with a smaller N does not replace it. No lead-time figure goes in this post (no median of the days between a filing and its press, no "days before the press", no "filing first" share) until that rule is met.
- "$0 a month" is true only when the deployed stack is still on free tiers (Actions, Neon free, Pages). Check at posting time.
- No person's name in the image: use a company whose account page shows only company-level events.
