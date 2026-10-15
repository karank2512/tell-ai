"""Spec 13.1 speed vs press: how long after a Form D does press appear, and when would tell alert?

The implementation is :data:`SQL`, the spec's query run as printed in the report. It
differs from the spec text only where the spec's version cannot run as a dated,
reproducible report; each change is listed in :data:`SQL_CHANGES` and printed beside it.

* **Cohort.** ``form_d_filed`` events with ``is_amendment = false`` filed in the 90 days
  before the evaluation instant whose link is trusted: rules R0-R4 whose
  ``payload.match.guard_failed`` is absent or empty (R0, the exact CIK rule of ADR 0035,
  never has one), or a reviewer's link (``payload.match.rule = 'manual'``, which wins over a
  guard failure left from the R5 candidate it resolved). A filing the EDGAR collector
  linked by CIK (no match record, the issuer CIK is the company's ``cik``) is in only when
  that CIK has a trusted ``companies.cik_source`` or was set by a trusted link: another
  ``edgar`` event of the company with the same CIK and a trusted match. Rules R5 and R6,
  guard failures and a CIK written by such a link are out (2026-10-14 production audit:
  20 of 41 automatic Form D links were the wrong company).
* **Cohort split (ADR 0035).** Every number is reported for two cohorts and pooled, each with
  its N: *monitored before the filing* (a YC directory company, ``source = 'yc'``, which the
  study treats as monitored all along, or any company created before the filing date) and
  *found from the filing* (created on or after the filing date and not from the YC
  directory: issuer-first discovery created it from this filing).
* **Press.** ``news_funding`` events of the same company dated
  ``filing_date - 14d`` to ``filing_date + 60d``; ``first_press_on`` is the earliest. The
  cohort's press is collected beforehand by ``tell ingest --source news --cohort formd
  --when-days 120`` (Google News ``when:120d``); this report only reads events. When the
  cohort is too small, the CLI prints :func:`small_cohort_hint` (backfill the quarterly
  Form D data sets with ``tell backfill formd``, link them, collect their press).
* **alert_at.** EDGAR publishes a day's form index at about 02:02 UTC the next day, so a
  scheduled run sees a filing no earlier than filing date + 26 h. ``alert_at`` is the
  event's ``observed_at`` only when a scheduled run observed it promptly: not
  ``payload.backfill`` and ``observed_at`` within :func:`observed_window_hours` of the
  filing date 00:00 UTC (36 h at the 6-hour cadence). Every other filing (quarterly data
  set, a catch-up run days later, ``observed_at`` before the filing date) gets the
  estimate filing date + 1 day 02:00 UTC + the ingest cadence (``cadence_hours``, default
  6; 24 when ``TELL_RUNS_PER_DAY`` is not 4, see :func:`ingest_cadence_hours`). The report
  counts observed and estimated values and labels the alert median "estimated" when most
  values are estimates. A *found* filing cannot alert before tell linked it: its
  ``alert_at`` is the later of that schedule and the link time (``payload.match.resolved_at``,
  else ``companies.created_at``), so the time discovery took counts.
* **Press-ambiguous brands.** A company whose brand is one word (after dropping a trailing
  legal form, ``PBC`` included) is flagged and counted. For a company not from the YC
  directory whose legal name is longer, a funding headline counts as press only when it
  contains that full name ("Mangrove Therapeutics", not a headline about any Mangrove).
* **Numbers.** Share with any press in the window; median and p75 of
  ``first_press_on - filing_date`` in days; filing-first share (press strictly after the
  filing date) among filings with press and over all filings (no press counts as filing
  first); median ``alert_at - filing_date`` in hours, over all filings and over the
  observed ones only.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from tell.backtest.common import Metric, Query, Report, Section, Table, share
from tell.models import CIK_SOURCES

__all__ = [
    "ALERT_CADENCE_HOURS",
    "COHORTS",
    "COHORT_LABELS",
    "INDEX_LAG_HOURS",
    "MIN_COHORT",
    "NAME",
    "OBSERVED_SLACK_HOURS",
    "SQL",
    "SQL_CHANGES",
    "TRUSTED_CIK_SOURCES",
    "TRUSTED_RULES",
    "SpeedVsPress",
    "build_report",
    "compute",
    "compute_cohorts",
    "ingest_cadence_hours",
    "observed_window_hours",
    "small_cohort_hint",
]

NAME = "speed-vs-press"
SCRIPT = "tell/backtest/speed_vs_press.py"
MIN_COHORT = 100
"""Spec 13.1 step 1: below this many filings the report says the sample is too small."""

ALERT_CADENCE_HOURS = 6
"""Default ingest cadence: ``ingest.yml`` with ``TELL_RUNS_PER_DAY = 4`` (ADR 0029)."""

DAILY_CADENCE_HOURS = 24
"""Ingest cadence while ``TELL_RUNS_PER_DAY`` is not 4 (one run a day, ADR 0029)."""

INDEX_LAG_HOURS = 26
"""EDGAR publishes the daily form index at about 02:02 UTC the day after the filing date."""

OBSERVED_SLACK_HOURS = 4
"""Slack after the first run that can see the index (one late or failed run)."""

TRUSTED_RULES: tuple[str, ...] = ("R0", "R1", "R2", "R3", "R4")
"""Automatic rules whose links enter the cohort when no guard failed; ``manual`` also does."""

TRUSTED_CIK_SOURCES: tuple[str, ...] = CIK_SOURCES
"""The ``companies.cik_source`` values whose CIK rule R0 trusts (``tell.models.CIK_SOURCES``,
the same set as ``tell.resolve.guards.TRUSTED_CIK_SOURCES``): a collector's CIK match to such
a company enters the cohort. :data:`SQL` spells them out from this tuple, so the report's
printed query stays runnable and never drifts from rule R0 and ingest."""

_CIK_SOURCE_LIST = ", ".join(
    "'" + source.replace("'", "''") + "'" for source in TRUSTED_CIK_SOURCES
)


def ingest_cadence_hours(runs_per_day: str | None) -> int:
    """The ingest cadence for a ``TELL_RUNS_PER_DAY`` value: 6 h when it is ``4``, else 24 h."""
    if runs_per_day is not None and runs_per_day.strip() == "4":
        return ALERT_CADENCE_HOURS
    return DAILY_CADENCE_HOURS


def observed_window_hours(cadence_hours: int) -> int:
    """How long after the filing date 00:00 UTC an ``observed_at`` still counts as observed.

    The index lag plus one cadence plus :data:`OBSERVED_SLACK_HOURS`: 36 h at the 6-hour
    cadence, 54 h at the daily one (whose 15:17 UTC run sees a filing about 39 h after the
    filing date). Anything later is a catch-up run and its ``alert_at`` is estimated.
    """
    return INDEX_LAG_HOURS + cadence_hours + OBSERVED_SLACK_HOURS


def small_cohort_hint(n: int) -> str:
    """The one-line pointer ``tell backtest speed-vs-press`` prints when N < :data:`MIN_COHORT`."""
    return (
        f"hint: cohort N={n} is under {MIN_COHORT}. Fill it: `tell backfill formd --quarter "
        "YYYYQn` (SEC quarterly Form D data sets; needs SEC_USER_AGENT, or --file ZIP), "
        "`tell resolve`, then `tell ingest --source news --cohort formd --when-days 120`, "
        "and rerun this report."
    )


SQL = """\
with links as (
  select e.event_id, e.company_id, e.payload->>'cik' as cik,
         e.payload->'match'->>'rule' as rule,
         case jsonb_typeof(e.payload->'match'->'guard_failed')
           when 'boolean' then (e.payload->'match'->>'guard_failed')::bool
           when 'array' then jsonb_array_length(e.payload->'match'->'guard_failed') > 0
           when 'object' then e.payload->'match'->'guard_failed' <> '{}'::jsonb
           when 'string' then e.payload->'match'->>'guard_failed' <> ''
           when 'number' then (e.payload->'match'->>'guard_failed')::numeric <> 0
           else false end as guard_failed
  from events e
  where e.source = 'edgar' and e.company_id is not null and e.payload->'match' is not null),
trusted as (
  select event_id, company_id, cik from links
  where rule = 'manual' or (rule in ('R0', 'R1', 'R2', 'R3', 'R4') and not guard_failed)),
d as (
  select e.event_id, e.company_id, e.observed_at,
         (e.occurred_at at time zone 'UTC')::date as filed_on,
         coalesce((e.payload->>'backfill')::bool, false) as backfill,
         case when coalesce(e.payload->'match'->>'resolved_at', '') ~ '^\\d{4}-'
              then (e.payload->'match'->>'resolved_at')::timestamptz end as linked_at,
         c.source, c.created_at
  from events e join companies c on c.id = e.company_id
  where e.type = 'form_d_filed' and (e.payload->>'is_amendment')::bool = false
    and e.occurred_at >= cast(:as_of as timestamptz) - interval '90 days'
    and e.occurred_at < cast(:as_of as timestamptz)
    and e.company_id is not null
    and (e.event_id in (select event_id from trusted)
         or (e.payload->'match' is null and c.cik = e.payload->>'cik'
             and (c.cik_source in (__TRUSTED_CIK_SOURCES__)
                  or exists (select 1 from trusted t
                             where t.company_id = c.id and t.cik = c.cik))))),
names as (
  select c.id as company_id, x.kind,
         trim(regexp_replace(lower(x.value), '\\W+', ' ', 'g')) as norm
  from companies c
  cross join lateral (values ('name', c.name), ('legal', c.legal_name)) as x(kind, value)
  where c.id in (select company_id from d) and x.value is not null),
keys as (
  select company_id, kind,
         regexp_replace(
           regexp_replace(norm,
             '^(.*\\S) (inc|incorporated|llc|ltd|limited|corp|corporation|co|plc|gmbh|pbc)$',
             '\\1'),
           '^(.*\\S) (inc|incorporated|llc|ltd|limited|corp|corporation|co|plc|gmbh|pbc)$',
           '\\1') as brand
  from names),
amb as (
  select b.company_id, l.brand as full_brand
  from keys b left join keys l on l.company_id = b.company_id and l.kind = 'legal'
  where b.kind = 'name' and b.brand <> '' and position(' ' in b.brand) = 0),
f as (
  select d.*, d.filed_on::timestamp at time zone 'UTC' as filed_at,
         case when d.source <> 'yc' and d.created_at >= d.filed_on::timestamp at time zone 'UTC'
              then 'found' else 'monitored' end as cohort,
         amb.company_id is not null as press_ambiguous,
         case when amb.company_id is not null and d.source <> 'yc'
                   and position(' ' in coalesce(amb.full_brand, '')) > 0
              then amb.full_brand end as full_brand
  from d left join amb on amb.company_id = d.company_id),
o as (
  select f.*,
         (not f.backfill and f.observed_at >= f.filed_at
          and f.observed_at <= f.filed_at
                               + cast(:observed_window_hours as integer) * interval '1 hour')
           as prompt
  from f),
a as (
  select o.*,
         o.cohort = 'monitored' and o.prompt as alert_observed,
         case when o.cohort = 'found'
              then greatest(case when o.prompt then o.observed_at
                                 else o.filed_at + interval '1 day 2 hours'
                                      + cast(:cadence_hours as integer) * interval '1 hour' end,
                            coalesce(o.linked_at, o.created_at))
              when o.prompt then o.observed_at
              else o.filed_at + interval '1 day 2 hours'
                   + cast(:cadence_hours as integer) * interval '1 hour' end as alert_at
  from o),
p as (
  select a.event_id, min((n.occurred_at at time zone 'UTC')::date) as first_press_on
  from a join events n on n.company_id = a.company_id
  where n.type = 'news_funding'
    and (n.occurred_at at time zone 'UTC')::date between a.filed_on - 14 and a.filed_on + 60
    and (a.full_brand is null
         or position(' ' || a.full_brand || ' ' in
                     ' ' || trim(regexp_replace(lower(coalesce(n.payload->>'title', '')),
                                                '\\W+', ' ', 'g')) || ' ') > 0)
  group by a.event_id)
select coalesce(cohort, 'pooled') as cohort,
       count(*) as n, count(p.event_id) as with_press,
       percentile_cont(0.5) within group (order by first_press_on - filed_on) as median_lag_days,
       percentile_cont(0.75) within group (order by first_press_on - filed_on) as p75_lag_days,
       sum(case when first_press_on > filed_on then 1 else 0 end) as filing_first,
       percentile_cont(0.5) within group (
         order by extract(epoch from alert_at - filed_at) / 3600.0
       ) as median_alert_hours,
       percentile_cont(0.5) within group (
         order by extract(epoch from alert_at - filed_at) / 3600.0
       ) filter (where alert_observed) as median_observed_alert_hours,
       sum(case when alert_observed then 1 else 0 end) as alert_observed,
       sum(case when cohort = 'monitored' and not alert_observed then 1 else 0 end)
         as alert_estimated,
       sum(case when cohort = 'found' then 1 else 0 end) as alert_linked,
       sum(case when press_ambiguous then 1 else 0 end) as press_ambiguous,
       sum(case when full_brand is not null then 1 else 0 end) as full_brand_required
from a left join p using (event_id)
group by grouping sets ((cohort), ());""".replace("__TRUSTED_CIK_SOURCES__", _CIK_SOURCE_LIST)

SQL_CHANGES: tuple[str, ...] = (
    "`now()` is the bound `:as_of` instant, so a dated report can be regenerated.",
    (
        "Filings are keyed by `event_id`, not `company_id`: the spec's `group by n.company_id` "
        "merges two filings of one company into one press date."
    ),
    "Dates are taken in UTC (`at time zone 'UTC'`), independent of the session time zone.",
    (
        "The cohort keeps only trusted links, as step 1 asks for linked issuers: rules R1-R4 "
        "with no `payload.match.guard_failed`, a reviewer's link (`rule = 'manual'`), or a "
        "CIK match whose CIK was set by such a link; R5, R6 and guard failures are out."
    ),
    (
        "`alert_at` is `observed_at` only for a scheduled observation within "
        "`:observed_window_hours` of the filing date; otherwise it is the filing date + 1 day "
        "02:00 UTC (the daily index) + `:cadence_hours`."
    ),
    (
        "Added columns: p75 lag, the median alert lag (`alert_at` per step 3) over all filings "
        "and over observed ones, and the observed and estimated counts. The news type name "
        "`news_funding` is unchanged."
    ),
    (
        "Rule R0 (an EDGAR event linked by a CIK with a trusted `companies.cik_source`, ADR "
        "0035) is a trusted rule, and a CIK match with no match record is in when the "
        "company's `cik_source` is set."
    ),
    (
        "The cohort is split, every row grouped by `grouping sets ((cohort), ())`: "
        "'monitored' (the company is from the YC directory, `source = 'yc'`, or existed before "
        "the filing date) and 'found' (created on or after the filing date and not from the YC "
        "directory: issuer-first discovery found it from this filing). The pooled row is both."
    ),
    (
        "For a 'found' filing `alert_at` is the link time (`payload.match.resolved_at`, else "
        "`companies.created_at`) when that is later than the scheduled alert, so the time "
        "discovery took counts."
    ),
    (
        "Press-ambiguous companies: a brand of one word (after dropping a trailing legal form, "
        "PBC included) is flagged. For a company not from the YC directory whose legal name is "
        "longer, a funding headline counts only when it contains that full name."
    ),
)

COHORTS: tuple[str, ...] = ("monitored", "found", "pooled")
"""The query's rows: monitored before the filing, found from the filing, and both."""

COHORT_LABELS: dict[str, str] = {
    "monitored": "Monitored before the filing",
    "found": "Found from the filing",
    "pooled": "Pooled",
}


@dataclass(frozen=True)
class SpeedVsPress:
    """One cohort row of the query (``pooled`` unless noted), plus the cadence it used."""

    n: int
    with_press: int
    median_lag_days: float | None
    p75_lag_days: float | None
    filing_first: int
    median_alert_hours: float | None
    median_observed_alert_hours: float | None
    alert_observed: int
    alert_estimated: int
    cadence_hours: int = ALERT_CADENCE_HOURS
    alert_linked: int = 0
    """'found' filings, whose ``alert_at`` is the link time (neither observed nor estimated)."""
    press_ambiguous: int = 0
    """Filings of a company whose brand is one word."""
    full_brand_required: int = 0
    """Filings whose funding headline had to contain the company's full legal name."""
    cohort: str = "pooled"

    @property
    def press_share(self) -> float | None:
        return share(self.with_press, self.n)

    @property
    def no_press(self) -> int:
        return self.n - self.with_press

    @property
    def filing_first_share_with_press(self) -> float | None:
        return share(self.filing_first, self.with_press)

    @property
    def filing_first_share_all(self) -> float | None:
        return share(self.filing_first + self.no_press, self.n)

    @property
    def mostly_estimated(self) -> bool:
        """More than half of the ``alert_at`` values are estimates, not observations."""
        return 2 * self.alert_estimated > self.n

    @property
    def observed_window_hours(self) -> int:
        return observed_window_hours(self.cadence_hours)

    @property
    def estimate_hours(self) -> int:
        """``alert_at - filing_date`` of every estimated filing."""
        return INDEX_LAG_HOURS + self.cadence_hours


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


def _binds(now: datetime, cadence_hours: int) -> dict[str, Any]:
    return {
        "as_of": now,
        "cadence_hours": cadence_hours,
        "observed_window_hours": observed_window_hours(cadence_hours),
    }


def _empty(cohort: str, cadence_hours: int) -> SpeedVsPress:
    return SpeedVsPress(
        n=0,
        with_press=0,
        median_lag_days=None,
        p75_lag_days=None,
        filing_first=0,
        median_alert_hours=None,
        median_observed_alert_hours=None,
        alert_observed=0,
        alert_estimated=0,
        cadence_hours=cadence_hours,
        cohort=cohort,
    )


def compute_cohorts(
    session: Session, now: datetime, *, cadence_hours: int = ALERT_CADENCE_HOURS
) -> dict[str, SpeedVsPress]:
    """Run :data:`SQL` at ``now``; one :class:`SpeedVsPress` per name in :data:`COHORTS`."""
    if cadence_hours <= 0:
        msg = f"cadence_hours must be positive, got {cadence_hours}"
        raise ValueError(msg)
    results = {name: _empty(name, cadence_hours) for name in COHORTS}
    for row in session.execute(text(SQL), _binds(now, cadence_hours)):
        results[str(row.cohort)] = SpeedVsPress(
            n=int(row.n),
            with_press=int(row.with_press),
            median_lag_days=_float(row.median_lag_days),
            p75_lag_days=_float(row.p75_lag_days),
            filing_first=int(row.filing_first or 0),
            median_alert_hours=_float(row.median_alert_hours),
            median_observed_alert_hours=_float(row.median_observed_alert_hours),
            alert_observed=int(row.alert_observed or 0),
            alert_estimated=int(row.alert_estimated or 0),
            cadence_hours=cadence_hours,
            alert_linked=int(row.alert_linked or 0),
            press_ambiguous=int(row.press_ambiguous or 0),
            full_brand_required=int(row.full_brand_required or 0),
            cohort=str(row.cohort),
        )
    return results


def compute(
    session: Session, now: datetime, *, cadence_hours: int = ALERT_CADENCE_HOURS
) -> SpeedVsPress:
    """The pooled row of :data:`SQL` at ``now`` for an ingest cadence of ``cadence_hours``."""
    return compute_cohorts(session, now, cadence_hours=cadence_hours)["pooled"]


def _alert_label(result: SpeedVsPress) -> str:
    label = "Median alert_at - filing_date, hours"
    if result.mostly_estimated:
        label += (
            f", estimated ({result.alert_estimated:,} of {result.n:,} alert times are "
            "estimates, not observations)"
        )
    return label


def build_report(
    session: Session, now: datetime, *, cadence_hours: int = ALERT_CADENCE_HOURS
) -> Report:
    """The 13.1 report at ``now`` (see the module docstring)."""
    cohorts = compute_cohorts(session, now, cadence_hours=cadence_hours)
    result = cohorts["pooled"]
    window = result.observed_window_hours
    estimate = (
        f"filing date + 1 day 02:00 UTC + the {cadence_hours}-hour cadence, "
        f"{result.estimate_hours} h after the filing date"
    )
    report = Report(
        name=NAME,
        title="Speed vs press: Form D filings against the first funding headline",
        question=(
            "How long after a Form D filing does the first news mention appear, and how fast "
            "would tell have alerted?"
        ),
        cohort=(
            "Every Form D (isAmendment = false) filed in the 90 days before the evaluation "
            "instant by an issuer linked to a monitored company by a trusted link: rules R0-R4 "
            "with no guard failure recorded, a reviewer's link, or the issuer's CIK when that "
            "CIK was set by such a link or has a trusted source. Review (R5), unlinked (R6) and "
            "guard-failed matches are excluded. Split in two and reported pooled: monitored "
            "before the filing (a YC directory company, or one that existed before the filing "
            "date) and found from the filing (created on or after the filing date by "
            "issuer-first discovery, whose alert time is the link time). Press: news_funding "
            "events of that company dated from 14 days before to 60 days after the filing date "
            "(collected first with Google News when:120d by `tell ingest --source news "
            "--cohort formd --when-days 120`); for a one-word brand not from the YC directory "
            "the headline must carry the full legal name."
        ),
        n=result.n,
        as_of=now.date(),
        generated_at=now,
        script=SCRIPT,
    )
    if result.n < MIN_COHORT:
        line = (
            f"Sample too small: N={result.n} filings, fewer than the {MIN_COHORT} the method "
            "requires; the numbers are shown for tracking only."
        )
        report.notices.append(line)
        report.blockers.append(f"sample too small, N={result.n} under {MIN_COHORT}")
    if result.n and result.mostly_estimated:
        report.notices.append(
            f"alert_at is estimated for {result.alert_estimated} of {result.n} filings "
            f"({estimate}); the alert median is an estimate, not a measurement."
        )
    report.sections = [
        Section(
            title="Press after filing",
            metrics=[
                Metric(
                    "Cohort N: Form D filings (isAmendment = false) linked to a monitored company",
                    result.n,
                    kind="count",
                ),
                Metric(
                    "Share with any press within the window (filing - 14d to filing + 60d)",
                    result.press_share,
                    result.n,
                    "share",
                ),
                Metric(
                    "Filings with any press within the window (the N behind the lag numbers)",
                    result.with_press,
                    kind="count",
                    headline=False,
                ),
                Metric(
                    "Share with no press within 60 days",
                    share(result.no_press, result.n),
                    result.n,
                    "share",
                ),
                Metric(
                    "Median of first_press_at - filing_date, days",
                    result.median_lag_days,
                    result.with_press,
                    "days",
                ),
                Metric(
                    "p75 of first_press_at - filing_date, days",
                    result.p75_lag_days,
                    result.with_press,
                    "days",
                ),
            ],
        ),
        Section(
            title="Who came first",
            metrics=[
                Metric(
                    "Filing first, among filings with press (press strictly after the filing date)",
                    result.filing_first_share_with_press,
                    result.with_press,
                    "share",
                ),
                Metric(
                    "Filing first, over all filings (no press counts as filing first)",
                    result.filing_first_share_all,
                    result.n,
                    "share",
                ),
            ],
        ),
        Section(
            title="How fast tell alerts",
            metrics=[
                Metric(_alert_label(result), result.median_alert_hours, result.n, "hours"),
                Metric(
                    "Median alert_at - filing_date, hours, observed filings only",
                    result.median_observed_alert_hours,
                    result.alert_observed,
                    "hours",
                    headline=False,
                ),
                Metric(
                    f"Filings whose alert_at is observed (a scheduled run within {window} h of "
                    "the filing date)",
                    result.alert_observed,
                    kind="count",
                    headline=False,
                ),
                Metric(
                    f"Filings whose alert_at is estimated ({estimate})",
                    result.alert_estimated,
                    kind="count",
                    headline=False,
                ),
            ],
        ),
    ]
    report.sections.append(_cohort_section(cohorts))
    binds = _binds(now, cadence_hours)
    report.queries = [
        Query(
            "Implementation (bind :as_of = "
            + now.isoformat()
            + f", :cadence_hours = {binds['cadence_hours']}"
            + f", :observed_window_hours = {binds['observed_window_hours']})",
            SQL,
        )
    ]
    report.caveats = [
        (
            "Google News coverage is incomplete for small companies; the no-press share is "
            "reported, not hidden."
        ),
        (
            "first_press_at is the earliest funding headline the news collector kept (name in the "
            "headline, funding regex match); a story that never names the company in its headline "
            "is invisible."
        ),
        (
            "The press window (from 14 days before to 60 days after the filing date, as SPEC "
            "13.1 sets it) misses same-round press published more than 14 days before the "
            "filing: such a round counts as no press, or as filing first when later press "
            "exists, so the filing-first shares lean high."
        ),
        (
            "EDGAR publishes a day's form index at about 02:02 UTC the next day, so no "
            "scheduled run can see a filing before filing date + 26 h. alert_at is the event's "
            "observed_at only when a scheduled run (not the quarterly backfill) saw the filing "
            f"within {window} h of the filing date; filings from the quarterly data sets, "
            "from a catch-up run days later, or with observed_at before the filing date get "
            f"the estimate {estimate}. Observed: {result.alert_observed}; estimated: "
            f"{result.alert_estimated}. The estimate adds one full {cadence_hours}-hour "
            "cadence after the index appears, the longest wait for the next run."
            + (
                " With one run a day (TELL_RUNS_PER_DAY not 4) that wait is up to 24 h; pass "
                "the 24-hour cadence for such a period."
                if cadence_hours < DAILY_CADENCE_HOURS
                else ""
            )
        ),
        (
            "Links: the 2026-10-14 production audit found 20 of 41 automatic Form D links were "
            "the wrong company, so only links that are trusted after the guards are counted; "
            "a wrong link that passed every guard is still in the cohort."
        ),
        (
            "Found-from-the-filing companies (issuer-first discovery, ADR 0035) are monitored "
            "because they filed: their alert time includes how long discovery took to link the "
            "filing, and they have no hiring history before it. Report them apart; the pooled "
            "row mixes two different questions."
        ),
        (
            "Press-ambiguous brands: tell has no dictionary to tell a common word from a coined "
            "one, so every one-word brand is flagged ("
            f"{result.press_ambiguous} of {result.n} filings). Only for companies not from the "
            "YC directory with a longer legal name does the headline have to carry that name ("
            f"{result.full_brand_required} filings); a YC one-word brand still counts any "
            "headline naming it."
        ),
        "Changes from the spec's SQL, all needed to run it as a dated report: "
        + " ".join(SQL_CHANGES),
    ]
    return report


def _cohort_rows(r: SpeedVsPress) -> list[Metric]:
    return [
        Metric("Filings", r.n, kind="count", headline=False),
        Metric("Share with any press", r.press_share, r.n, "share", headline=False),
        Metric("Share with no press within 60 days", share(r.no_press, r.n), r.n, "share", False),
        Metric("Median lag, days", r.median_lag_days, r.with_press, "days", headline=False),
        Metric("p75 lag, days", r.p75_lag_days, r.with_press, "days", headline=False),
        Metric(
            "Filing first, among filings with press",
            r.filing_first_share_with_press,
            r.with_press,
            "share",
            headline=False,
        ),
        Metric(
            "Filing first, over all filings",
            r.filing_first_share_all,
            r.n,
            "share",
            headline=False,
        ),
        Metric("Median alert_at - filing_date, hours", r.median_alert_hours, r.n, "hours", False),
        Metric("Alert times observed", r.alert_observed, kind="count", headline=False),
        Metric("Alert times estimated", r.alert_estimated, kind="count", headline=False),
        Metric("Alert times at the link", r.alert_linked, kind="count", headline=False),
        Metric("Press-ambiguous filings (one-word brand)", r.press_ambiguous, r.n, "count", False),
    ]


def _cohort_section(cohorts: dict[str, SpeedVsPress]) -> Section:
    """Every number per cohort, with its N (the pooled column repeats the sections above)."""
    columns = [_cohort_rows(cohorts[name]) for name in COHORTS]
    rows: list[list[Metric | str]] = []
    for i, first in enumerate(columns[0]):
        rows.append([first.label, *(column[i] for column in columns)])
    return Section(
        title="By cohort",
        text=(
            "Monitored before the filing: a YC directory company, or one tell had before the "
            "filing date. Found from the filing: created on or after the filing date by "
            "issuer-first discovery; its alert time is the link time. Pooled: both."
        ),
        table=Table(
            headers=["Number", *(COHORT_LABELS[name] for name in COHORTS)],
            rows=rows,
        ),
    )
