"""Spec 13.1 speed vs press: how long after a Form D does press appear, and when would tell alert?

The implementation is :data:`SQL`, the spec's query run as printed in the report. It
differs from the spec text only where the spec's version cannot run as a dated,
reproducible report; each change is listed in :data:`SQL_CHANGES` and printed beside it.

* **Cohort.** ``form_d_filed`` events with ``is_amendment = false`` filed in the 90 days
  before the evaluation instant, linked to a monitored company by rules R1-R4
  (``payload.match.rule``) or by CIK (the EDGAR collector links a filing whose issuer CIK
  is already a company's ``cik`` at collection time and writes no match record).
* **Press.** ``news_funding`` events of the same company dated
  ``[filing_date - 14d, filing_date + 60d]``; ``first_press_on`` is the earliest. The
  cohort's press is collected beforehand by ``tell ingest --source news --cohort formd
  --when-days 120`` (Google News ``when:120d``); this report only reads events. When the
  cohort is too small, the CLI prints :func:`small_cohort_hint` (backfill the quarterly
  Form D data sets with ``tell backfill formd``, link them, collect their press).
* **alert_at.** The instant tell first saw the filing: the event's ``observed_at``, which
  already includes the time to the next 6-hourly ingest run after the filing reached the
  daily index. For a filing whose index time is unknown (``payload.backfill``, loaded from
  the quarterly data sets, or ``observed_at`` before the filing date) it is computed as the
  filing date 00:00 UTC plus the 6-hour cadence. The number of such estimates is reported.
* **Numbers.** Share with any press in the window; median and p75 of
  ``first_press_on - filing_date`` in days; filing-first share (press strictly after the
  filing date) among filings with press and over all filings (no press counts as filing
  first); median ``alert_at - filing_date`` in hours.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from tell.backtest.common import Metric, Query, Report, Section, share

__all__ = [
    "ALERT_CADENCE_HOURS",
    "MIN_COHORT",
    "NAME",
    "SQL",
    "SQL_CHANGES",
    "SpeedVsPress",
    "build_report",
    "compute",
    "small_cohort_hint",
]

NAME = "speed-vs-press"
SCRIPT = "tell/backtest/speed_vs_press.py"
MIN_COHORT = 100
"""Spec 13.1 step 1: below this many filings the report says the sample is too small."""

ALERT_CADENCE_HOURS = 6
"""The ingest cadence added to an unknown index-appearance time."""


def small_cohort_hint(n: int) -> str:
    """The one-line pointer ``tell backtest speed-vs-press`` prints when N < :data:`MIN_COHORT`."""
    return (
        f"hint: cohort N={n} is under {MIN_COHORT}. Fill it: `tell backfill formd --quarter "
        "YYYYQn` (SEC quarterly Form D data sets; needs SEC_USER_AGENT, or --file ZIP), "
        "`tell resolve`, then `tell ingest --source news --cohort formd --when-days 120`, "
        "and rerun this report."
    )


SQL = """\
with d as (
  select e.event_id, e.company_id, e.observed_at,
         (e.occurred_at at time zone 'UTC')::date as filed_on,
         (coalesce((e.payload->>'backfill')::bool, false)
          or e.observed_at < e.occurred_at) as alert_estimated
  from events e join companies c on c.id = e.company_id
  where e.type = 'form_d_filed' and (e.payload->>'is_amendment')::bool = false
    and e.occurred_at >= cast(:as_of as timestamptz) - interval '90 days'
    and e.occurred_at < cast(:as_of as timestamptz)
    and e.company_id is not null
    and (e.payload->'match'->>'rule' in ('R1', 'R2', 'R3', 'R4')
         or c.cik = e.payload->>'cik')),
a as (
  select d.*,
         case when d.alert_estimated
              then (d.filed_on + time '06:00') at time zone 'UTC'
              else d.observed_at end as alert_at
  from d),
p as (
  select d.event_id, min((n.occurred_at at time zone 'UTC')::date) as first_press_on
  from d join events n on n.company_id = d.company_id
  where n.type = 'news_funding'
    and (n.occurred_at at time zone 'UTC')::date between d.filed_on - 14 and d.filed_on + 60
  group by d.event_id)
select count(*) as n, count(p.event_id) as with_press,
       percentile_cont(0.5) within group (order by first_press_on - filed_on) as median_lag_days,
       percentile_cont(0.75) within group (order by first_press_on - filed_on) as p75_lag_days,
       sum(case when first_press_on > filed_on then 1 else 0 end) as filing_first,
       percentile_cont(0.5) within group (
         order by extract(epoch from alert_at - (filed_on::timestamp at time zone 'UTC')) / 3600.0
       ) as median_alert_hours,
       sum(case when alert_estimated then 1 else 0 end) as alert_estimated
from a left join p using (event_id);"""

SQL_CHANGES: tuple[str, ...] = (
    "`now()` is the bound `:as_of` instant, so a dated report can be regenerated.",
    (
        "Filings are keyed by `event_id`, not `company_id`: the spec's `group by n.company_id` "
        "merges two filings of one company into one press date."
    ),
    "Dates are taken in UTC (`at time zone 'UTC'`), independent of the session time zone.",
    (
        "The cohort keeps only links made by rules R1-R4 or by CIK, as step 1 asks; a link made "
        "only by a reviewer is in the cohort when it also set the company's CIK."
    ),
    (
        "Added columns: p75 lag, the median alert lag (`alert_at` per step 3) and the number of "
        "estimated `alert_at` values. The news type name `news_funding` is unchanged."
    ),
)


@dataclass(frozen=True)
class SpeedVsPress:
    """The query's one result row."""

    n: int
    with_press: int
    median_lag_days: float | None
    p75_lag_days: float | None
    filing_first: int
    median_alert_hours: float | None
    alert_estimated: int

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


def _float(value: Any) -> float | None:
    return None if value is None else float(value)


def compute(session: Session, now: datetime) -> SpeedVsPress:
    """Run :data:`SQL` at ``now``."""
    row = session.execute(text(SQL), {"as_of": now}).one()
    return SpeedVsPress(
        n=int(row.n),
        with_press=int(row.with_press),
        median_lag_days=_float(row.median_lag_days),
        p75_lag_days=_float(row.p75_lag_days),
        filing_first=int(row.filing_first or 0),
        median_alert_hours=_float(row.median_alert_hours),
        alert_estimated=int(row.alert_estimated or 0),
    )


def build_report(session: Session, now: datetime) -> Report:
    """The 13.1 report at ``now`` (see the module docstring)."""
    result = compute(session, now)
    report = Report(
        name=NAME,
        title="Speed vs press: Form D filings against the first funding headline",
        question=(
            "How long after a Form D filing does the first news mention appear, and how fast "
            "would tell have alerted?"
        ),
        cohort=(
            "Every Form D (isAmendment = false) filed in the 90 days before the evaluation "
            "instant by an issuer linked to a monitored company by rules R1-R4 or by CIK. "
            "Press: news_funding events of that company dated from 14 days before to 60 days "
            "after the filing date (collected first with Google News when:120d by "
            "`tell ingest --source news --cohort formd --when-days 120`)."
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
                Metric(
                    "Median alert_at - filing_date, hours",
                    result.median_alert_hours,
                    result.n,
                    "hours",
                ),
                Metric(
                    "Filings whose alert_at is estimated (filing date 00:00 UTC + 6 h)",
                    result.alert_estimated,
                    kind="count",
                    headline=False,
                ),
            ],
        ),
    ]
    report.queries = [Query("Implementation (bind :as_of = " + now.isoformat() + ")", SQL)]
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
            "alert_at is the event's observed_at: when the scheduled run that read the daily index "
            "saw the filing. For filings loaded from the quarterly data sets the index time is "
            "unknown and alert_at is computed as the filing date 00:00 UTC plus the 6-hour ingest "
            "cadence, which is optimistic: EDGAR publishes the daily index in the evening."
        ),
        "Changes from the spec's SQL, all needed to run it as a dated report: "
        + " ".join(SQL_CHANGES),
    ]
    return report
