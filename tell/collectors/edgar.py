"""SEC EDGAR collector (SPEC section 3.6): daily form index, Form D, 8-K and S-1 events.

One run reads the daily ``form.{YYYYMMDD}.idx`` for each requested date, keeps form
types ``D``, ``D/A``, ``8-K`` and ``S-1``, and turns them into events keyed
``edgar:{accession_number}``:

* ``D``   -> ``form_d_filed`` from ``primary_doc.xml`` (see :mod:`tell.collectors.edgar_formd`);
* ``D/A`` -> merged into the original ``form_d_filed`` payload, never a second event
  (SPEC section 5 rule 4), or ``form_d_amendment`` when the original is unknown;
* ``8-K`` -> ``form_8k_filed`` for monitored companies only (``companies.cik``), with
  items from ``data.sec.gov/submissions`` and items 5.02 / 1.01 / 2.01 flagged;
* ``S-1`` -> ``form_s1_filed``.

An event is linked at ingest by its CIK only to a company whose ``cik_source`` is trusted
(ADR 0035, the rule R0 set: :func:`tell.collectors.edgar_formd.company_ids_by_cik`); any
other event, including an 8-K of a company whose CIK has an unknown origin, is written
unlinked and left for ``tell resolve run``.

Dates (New York calendar): by default every day after the latest index an earlier
successful run read (``runs.notes.last_index_date``) up to today, at most 14 days back
(today and the 3 days before it on a first run); ``tell ingest --source edgar --since``
reaches up to 120 days back. The quarter ``index.json`` listing picks the days the SEC
actually published. Filings whose event already exists cost no request (except ``D/A``),
and each day is its own short transaction, so no transaction is idle across HTTP.

Every SEC request carries ``SEC_USER_AGENT`` and ``Accept-Encoding: gzip`` and draws from
one :class:`~tell.http.RateBudget` at ``TELL_RATE_EDGAR_RPS`` (5, half the SEC's 10 rps
fair-access ceiling) shared by ``www.sec.gov`` and ``data.sec.gov``. Officer and
director names in Form D are never read or stored.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

import httpx
import pydantic
from pydantic import ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from tell.collectors.base import CollectorSpec, Event, RunResult, sha256_hex, write_events
from tell.collectors.edgar_formd import (
    SOURCE,
    FormDFiling,
    FormDFilings,
    cik10,
    cik_holders,
    filing_folder_url,
    ingest_formd_filings,
    load_formd_quarterly,
    parse_formd_xml,
    primary_doc_url,
)
from tell.config import get_settings
from tell.http import RateBudget, Sleeper, fetch, make_client
from tell.models import EventRow, Run

__all__ = [
    "COLLECTOR",
    "FIRST_RUN_DAYS_BACK",
    "FORM_8K_FILED",
    "FORM_S1_FILED",
    "KEPT_FORMS",
    "MAX_CATCHUP_DAYS_BACK",
    "MAX_SINCE_DAYS_BACK",
    "EdgarRunResult",
    "IndexEntry",
    "SecClient",
    "TickerInfo",
    "accession_from_file_name",
    "collect",
    "daily_index_url",
    "default_dates",
    "edgar_event_id",
    "known_accessions",
    "last_index_date",
    "load_formd_quarterly",
    "load_ticker_map",
    "parse_daily_index",
    "parse_ticker_map",
    "quarter_listing_url",
    "run_fixture",
    "since_dates",
    "submissions_url",
]

FORM_8K_FILED = "form_8k_filed"
FORM_S1_FILED = "form_s1_filed"
KEPT_FORMS: frozenset[str] = frozenset({"D", "D/A", "8-K", "S-1"})
FLAGGED_8K_ITEMS: tuple[str, ...] = ("5.02", "1.01", "2.01")

DAILY_INDEX = "https://www.sec.gov/Archives/edgar/daily-index"
SUBMISSIONS = "https://data.sec.gov/submissions"
TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SEC_TZ = ZoneInfo("America/New_York")
ACCEPT_ENCODING = "gzip, deflate"

FIRST_RUN_DAYS_BACK = 3
"""With no successful run on record, read today's index and the 3 days before it."""
MAX_CATCHUP_DAYS_BACK = 14
"""A scheduled run catches up at most 14 days back; older gaps need ``--since``."""
MAX_SINCE_DAYS_BACK = 120
"""``--since`` reaches at most 120 days back; older filings come from ``tell backfill formd``."""
_RECENT_RUNS = 50

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / SOURCE

_log = logging.getLogger(__name__)

_ACCESSION_IN_PATH = re.compile(r"(\d{10}-\d{2}-\d{6})")
_INDEX_ROW = re.compile(
    r"^(?P<head>.*?)\s+(?P<cik>\d{1,10})\s+(?P<date>\d{8}|\d{4}-\d{2}-\d{2})\s+"
    r"(?P<file>edgar/\S+)\s*$"
)


# --- daily index ------------------------------------------------------------------------------


@dataclass(frozen=True)
class IndexEntry:
    """One row of ``form.{YYYYMMDD}.idx``."""

    form_type: str
    company_name: str
    cik: str
    date_filed: date
    file_name: str

    @property
    def accession_number(self) -> str:
        return accession_from_file_name(self.file_name)

    @property
    def folder_url(self) -> str:
        return filing_folder_url(self.cik, self.accession_number)

    @property
    def primary_doc_url(self) -> str:
        return primary_doc_url(self.cik, self.accession_number)


def daily_index_url(day: date) -> str:
    """``.../daily-index/{YYYY}/QTR{n}/form.{YYYYMMDD}.idx`` with the quarter from ``day``."""
    quarter = (day.month - 1) // 3 + 1
    return f"{DAILY_INDEX}/{day:%Y}/QTR{quarter}/form.{day:%Y%m%d}.idx"


def quarter_listing_url(day: date) -> str:
    """``.../daily-index/{YYYY}/QTR{n}/index.json``: the files the SEC has published."""
    quarter = (day.month - 1) // 3 + 1
    return f"{DAILY_INDEX}/{day:%Y}/QTR{quarter}/index.json"


def _listing_parent(url: str) -> tuple[str, str] | None:
    """``.../2026/QTR4/index.json`` -> (``.../2026/index.json``, ``QTR4``); ``None`` at the root."""
    folder = url.removesuffix("/index.json")
    if folder == DAILY_INDEX:
        return None
    parent, _, child = folder.rpartition("/")
    return f"{parent}/index.json", child


def _listing_names(sec: SecClient, url: str, cache: dict[str, set[str]]) -> set[str]:
    """The entry names of one ``index.json`` directory listing, fetched once per run.

    A 404 lists nothing. A 403 lists nothing only when the parent listing does not name
    this directory (a quarter or year whose first index is not out yet: sec.gov answers
    a missing path with 403); a 403 on a directory its parent names is a real block and
    propagates, as does a 403 on the ``daily-index`` root itself.
    """
    if url in cache:
        return cache[url]
    names: set[str] = set()
    try:
        response = sec.get(url)
    except httpx.HTTPStatusError as exc:
        parent = _listing_parent(url)
        if exc.response.status_code != httpx.codes.FORBIDDEN or parent is None:
            raise
        parent_url, child = parent
        if child in _listing_names(sec, parent_url, cache):
            raise
    else:
        if response is not None:
            items = response.json().get("directory", {}).get("item", [])
            names = {
                str(item.get("name", "")).rstrip("/") for item in items if isinstance(item, dict)
            }
    cache[url] = names
    return names


def _index_is_listed(sec: SecClient, day: date, cache: dict[str, set[str]]) -> bool:
    """Whether the quarter listing names ``form.{YYYYMMDD}.idx`` (one fetch per quarter).

    sec.gov answers a request for a file that does not exist (weekends, federal holidays
    such as Columbus Day, today's index before it is published) with **403**, not 404, so
    a 403 alone cannot tell "no filings that day" from "blocked". The listing can, so a
    run asks it first and requests only the indexes it names.
    """
    return f"form.{day:%Y%m%d}.idx" in _listing_names(sec, quarter_listing_url(day), cache)


def accession_from_file_name(file_name: str) -> str:
    """``edgar/data/1234567/0001234567-26-000123.txt`` -> ``0001234567-26-000123``."""
    match = _ACCESSION_IN_PATH.search(file_name)
    if match is None:
        msg = f"no accession number in {file_name!r}"
        raise ValueError(msg)
    return match.group(1)


def _parse_index_date(text: str) -> date:
    if "-" in text:
        return date.fromisoformat(text)
    return date(int(text[:4]), int(text[4:6]), int(text[6:8]))


def parse_daily_index(text: str, *, forms: Iterable[str] | None = KEPT_FORMS) -> list[IndexEntry]:
    """Parse the fixed-format form index; keep ``forms`` (all rows when ``None``).

    The header block runs to the dashed line. Each row is ``Form Type | Company Name |
    CIK | Date Filed | File Name``; the last three are read from the right, and the
    form type is cut at the ``Company Name`` column of the header (form types such as
    ``SC 13G`` contain spaces), falling back to the first run of two spaces.
    """
    wanted = None if forms is None else frozenset(forms)
    lines = text.splitlines()
    company_col: int | None = None
    start = None
    for number, line in enumerate(lines):
        if "Company Name" in line and line.lstrip().startswith("Form Type"):
            company_col = line.index("Company Name")
        if line.startswith("---"):
            start = number + 1
            break
    if start is None:
        msg = "daily index has no dashed header separator"
        raise ValueError(msg)
    entries: list[IndexEntry] = []
    for line in lines[start:]:
        match = _INDEX_ROW.match(line)
        if match is None:
            continue
        head = match.group("head")
        if company_col is not None and len(head) > company_col and head[company_col - 1] == " ":
            form_type, company = head[:company_col], head[company_col:]
        else:
            form_type, _, company = head.strip().partition("  ")
        form_type = form_type.strip()
        if wanted is not None and form_type not in wanted:
            continue
        entries.append(
            IndexEntry(
                form_type=form_type,
                company_name=" ".join(company.split()),
                cik=str(int(match.group("cik"))),
                date_filed=_parse_index_date(match.group("date")),
                file_name=match.group("file"),
            )
        )
    return entries


def sec_today(now: datetime) -> date:
    """Today on the SEC's (New York) calendar."""
    return now.astimezone(SEC_TZ).date()


def _day_range(first: date, last: date) -> list[date]:
    return [first + timedelta(days=n) for n in range((last - first).days + 1)]


def default_dates(now: datetime, last_index_date: date | None = None) -> list[date]:
    """The days a scheduled run reads: the catch-up window after ``last_index_date``.

    Every day from the one after ``last_index_date`` (the latest index a successful run
    already read) up to today on the New York calendar, starting no earlier than
    :data:`MAX_CATCHUP_DAYS_BACK` days ago; ``[]`` when today's index is already read.
    With no earlier run, today and the :data:`FIRST_RUN_DAYS_BACK` days before it.
    """
    today = sec_today(now)
    if last_index_date is None:
        first = today - timedelta(days=FIRST_RUN_DAYS_BACK)
    else:
        floor = today - timedelta(days=MAX_CATCHUP_DAYS_BACK)
        first = max(last_index_date + timedelta(days=1), floor)
    return _day_range(first, today)


def since_dates(now: datetime, since: date) -> list[date]:
    """``since`` through today (``tell ingest --source edgar --since``), at most 120 days back."""
    today = sec_today(now)
    if since > today:
        msg = f"--since {since.isoformat()} is after today ({today.isoformat()}, New York)"
        raise ValueError(msg)
    floor = today - timedelta(days=MAX_SINCE_DAYS_BACK)
    if since < floor:
        msg = (
            f"--since {since.isoformat()} is more than {MAX_SINCE_DAYS_BACK} days back "
            f"(earliest {floor.isoformat()}); use `tell backfill formd` for older filings"
        )
        raise ValueError(msg)
    return _day_range(since, today)


def _notes_index_date(notes: Mapping[str, Any]) -> date | None:
    """The latest index a run's notes say was read.

    ``last_index_date`` when recorded. Runs from before it was recorded kept only
    ``dates`` (yesterday and today at run time); their earlier date is taken as read,
    since today's index may not have been out yet.
    """
    if "last_index_date" in notes:
        value = notes["last_index_date"]
        return date.fromisoformat(value) if isinstance(value, str) else None
    legacy = notes.get("dates")
    if isinstance(legacy, list) and legacy and all(isinstance(d, str) for d in legacy):
        return min(date.fromisoformat(d) for d in legacy)
    return None


def last_index_date(session: Session) -> date | None:
    """The latest daily index a successful ``edgar`` run read (``runs.notes``), or ``None``."""
    rows = session.scalars(
        select(Run.notes)
        .where(Run.source == SOURCE, Run.status == "ok", Run.notes.is_not(None))
        .order_by(Run.started_at.desc())
        .limit(_RECENT_RUNS)
    )
    found = [d for notes in rows if notes and (d := _notes_index_date(notes)) is not None]
    return max(found, default=None)


# --- HTTP ---------------------------------------------------------------------------------------


def submissions_url(cik: str | int) -> str:
    """``https://data.sec.gov/submissions/CIK{10 digits}.json``."""
    return f"{SUBMISSIONS}/CIK{cik10(cik)}.json"


def edgar_budget(*, sleep: Sleeper = time.sleep) -> RateBudget:
    """One bucket at ``TELL_RATE_EDGAR_RPS`` for every SEC host."""
    return RateBudget(get_settings().tell_rate_edgar_rps, sleep=sleep)


class SecClient:
    """SEC fetcher: declared User-Agent, gzip, shared rate budget, 404 -> ``None``."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        budget: RateBudget | None = None,
        sleep: Sleeper = time.sleep,
    ) -> None:
        settings = get_settings()
        self.user_agent = settings.sec_user_agent
        self._own = client is None
        self._client = client or make_client(
            user_agent=self.user_agent, headers={"Accept-Encoding": ACCEPT_ENCODING}
        )
        self.budget = budget or edgar_budget(sleep=sleep)
        self._sleep = sleep
        self._submissions: dict[str, Submissions | None] = {}

    def close(self) -> None:
        if self._own:
            self._client.close()

    def __enter__(self) -> SecClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get(self, url: str) -> httpx.Response | None:
        """GET ``url``; ``None`` on 404 (no index on weekends); raises on other errors."""
        response = fetch(
            url,
            client=self._client,
            user_agent=self.user_agent,
            headers={"Accept-Encoding": ACCEPT_ENCODING},
            budget=self.budget,
            sleep=self._sleep,
        )
        if response.status_code == httpx.codes.NOT_FOUND:
            return None
        response.raise_for_status()
        return response

    def submissions(self, cik: str) -> Submissions | None:
        """The issuer's submissions JSON, fetched at most once per run per CIK."""
        key = cik10(cik)
        if key not in self._submissions:
            response = self.get(submissions_url(key))
            self._submissions[key] = (
                None if response is None else Submissions.model_validate_json(response.content)
            )
        return self._submissions[key]


# --- submissions JSON (per-company history) --------------------------------------------------


class _Loose(pydantic.BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class RecentFilings(_Loose):
    """Parallel arrays under ``filings.recent``; only the columns tell reads."""

    accession_number: list[str] = Field(alias="accessionNumber")
    form: list[str]
    filing_date: list[str] = Field(alias="filingDate")
    file_number: list[str] = Field(default_factory=list, alias="fileNumber")
    items: list[str] = Field(default_factory=list)


class SubmissionsFilings(_Loose):
    recent: RecentFilings


class Submissions(_Loose):
    cik: str
    filings: SubmissionsFilings

    def lookup(self, accession: str) -> tuple[str | None, list[str]]:
        """``(file number, 8-K items)`` for ``accession``; ``(None, [])`` when absent."""
        recent = self.filings.recent
        try:
            index = recent.accession_number.index(accession)
        except ValueError:
            return None, []
        file_number = recent.file_number[index] if index < len(recent.file_number) else ""
        raw_items = recent.items[index] if index < len(recent.items) else ""
        items = [part.strip() for part in raw_items.split(",") if part.strip()]
        return file_number or None, items


# --- CIK -> ticker ---------------------------------------------------------------------------------


class TickerInfo(NamedTuple):
    ticker: str
    exchange: str | None
    name: str


class TickerExchangeFile(_Loose):
    fields: list[str]
    data: list[list[str | int | None]]


def parse_ticker_map(raw: bytes | str) -> dict[str, TickerInfo]:
    """``{10-digit cik: (ticker, exchange, name)}``; the first listed ticker wins per CIK."""
    parsed = TickerExchangeFile.model_validate_json(raw)
    columns = {name: index for index, name in enumerate(parsed.fields)}
    for needed in ("cik", "name", "ticker", "exchange"):
        if needed not in columns:
            msg = f"company_tickers_exchange.json lacks the {needed!r} column"
            raise ValueError(msg)
    out: dict[str, TickerInfo] = {}
    for row in parsed.data:
        cik = row[columns["cik"]]
        ticker = row[columns["ticker"]]
        if cik is None or ticker is None:
            continue
        key = cik10(cik)
        if key in out:
            continue
        exchange = row[columns["exchange"]]
        out[key] = TickerInfo(
            ticker=str(ticker),
            exchange=None if exchange is None else str(exchange),
            name=str(row[columns["name"]] or ""),
        )
    return out


def load_ticker_map(
    client: httpx.Client | None = None,
    *,
    budget: RateBudget | None = None,
    sleep: Sleeper = time.sleep,
) -> dict[str, TickerInfo]:
    """Fetch ``company_tickers_exchange.json`` and return the CIK -> ticker map."""
    with SecClient(client, budget=budget, sleep=sleep) as sec:
        response = sec.get(TICKERS_URL)
    if response is None:
        msg = f"{TICKERS_URL} returned 404"
        raise LookupError(msg)
    return parse_ticker_map(response.content)


# --- events for 8-K and S-1 -------------------------------------------------------------------------


def _occurred_at(day: date) -> datetime:
    return datetime.combine(day, dtime(0, 0), tzinfo=UTC)


def _base_payload(entry: IndexEntry) -> dict[str, Any]:
    return {
        "accession_number": entry.accession_number,
        "cik": cik10(entry.cik),
        "form_type": entry.form_type,
        "company_name": entry.company_name,
        "date_filed": entry.date_filed.isoformat(),
    }


def _filing_event(
    entry: IndexEntry,
    *,
    type_: str,
    payload: dict[str, Any],
    run_id: str,
    now: datetime,
    company_id: int | None,
) -> Event:
    return Event(
        source=SOURCE,
        type=type_,
        external_id=entry.accession_number,
        version=entry.accession_number,
        occurred_at=_occurred_at(entry.date_filed),
        observed_at=now,
        url=entry.folder_url,
        payload=payload,
        run_id=run_id,
        company_id=company_id,
    )


def form_8k_payload(
    entry: IndexEntry, items: Sequence[str], file_number: str | None
) -> dict[str, Any]:
    """8-K payload: the items list plus ``item_5_02`` / ``item_1_01`` / ``item_2_01`` flags."""
    payload = _base_payload(entry)
    payload["file_number"] = file_number
    payload["items"] = list(items)
    for item in FLAGGED_8K_ITEMS:
        payload[f"item_{item.replace('.', '_')}"] = item in items
    return payload


# --- run -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class EdgarRunResult(RunResult):
    """:class:`RunResult` plus what ``finish_run`` and the run notes want."""

    errors: int = 0
    amended: int = 0
    notes: dict[str, Any] = field(default_factory=dict)


def _fetch_formd(sec: SecClient, entry: IndexEntry) -> FormDFiling | None:
    response = sec.get(entry.primary_doc_url)
    if response is None:
        return None
    submissions = sec.submissions(entry.cik)
    file_number = submissions.lookup(entry.accession_number)[0] if submissions else None
    return parse_formd_xml(
        response.content,
        accession_number=entry.accession_number,
        cik=entry.cik,
        form_type=entry.form_type,
        date_filed=entry.date_filed,
        file_number=file_number,
    )


def edgar_event_id(accession: str) -> str:
    """``events.event_id`` of the EDGAR event keyed by ``accession`` (``edgar:{accession}``)."""
    return sha256_hex(f"{SOURCE}|{accession}|{accession}")


def known_accessions(session: Session, accessions: Iterable[str]) -> set[str]:
    """The accession numbers among ``accessions`` that already have an ``edgar`` event."""
    by_id = {edgar_event_id(a): a for a in accessions}
    if not by_id:
        return set()
    rows = session.scalars(select(EventRow.event_id).where(EventRow.event_id.in_(list(by_id))))
    return {by_id[event_id] for event_id in rows}


def _fetch_day(sec: SecClient, day: date) -> list[IndexEntry] | None:
    """Kept rows of one published daily index; ``None`` when it answers 404.

    Only days the quarter listing names get here, so a 403 is a real block and
    propagates, failing the run.
    """
    response = sec.get(daily_index_url(day))
    if response is None:
        _log.info("edgar: listed daily index for %s answered 404", day.isoformat())
        return None
    return parse_daily_index(response.text)


def _release(session: Session, own: bool) -> None:
    """End the transaction before network I/O when ``collect`` opened it (``own``).

    Nothing a day writes is left pending: its events are flushed, so committing here is
    the per-day commit. A caller that handed in a session with its own open transaction
    keeps it; everything then lands in that transaction.
    """
    if own:
        session.commit()


@dataclass
class _Tally:
    fetched: int = 0
    inserted: int = 0
    skipped: int = 0
    errors: int = 0
    amended: int = 0
    known: int = 0
    events: list[Event] = field(default_factory=list)


def _collect_day(
    session: Session,
    sec: SecClient,
    entries: list[IndexEntry],
    tally: _Tally,
    *,
    run_id: str,
    now: datetime,
    own: bool,
) -> None:
    """One daily index: two short reads, the HTTP, then one write (per-day transaction).

    An entry whose event already exists is skipped without a request (Form D's
    ``primary_doc.xml``, a monitored 8-K's submissions JSON), so a rerun costs one
    index fetch per day. A ``D/A`` is always fetched: it amends an existing event.
    """
    known = known_accessions(session, [e.accession_number for e in entries if e.form_type != "D/A"])
    holders = cik_holders(session, [e.cik for e in entries])
    # Linked at ingest only by a trusted CIK (ADR 0035, the rule R0 set); a company whose
    # CIK has an unknown origin still marks its 8-Ks as monitored, but those events (and
    # its S-1s) are written unlinked and left for ``tell resolve run``.
    linked = {cik: h.company_id for cik, h in holders.items() if h.trusted}
    _release(session, own)

    filings: list[FormDFiling] = []
    other_events: list[Event] = []
    for entry in entries:
        if entry.accession_number in known:
            tally.known += 1
            continue
        try:
            if entry.form_type in {"D", "D/A"}:
                filing = _fetch_formd(sec, entry)
                if filing is None:
                    tally.errors += 1
                    _log.warning("edgar: no primary_doc.xml for %s", entry.accession_number)
                else:
                    filings.append(filing)
            elif entry.form_type == "8-K":
                if cik10(entry.cik) not in holders:
                    continue
                submissions = sec.submissions(entry.cik)
                file_number, items = (
                    submissions.lookup(entry.accession_number) if submissions else (None, [])
                )
                other_events.append(
                    _filing_event(
                        entry,
                        type_=FORM_8K_FILED,
                        payload=form_8k_payload(entry, items, file_number),
                        run_id=run_id,
                        now=now,
                        company_id=linked.get(cik10(entry.cik)),
                    )
                )
            elif entry.form_type == "S-1":
                other_events.append(
                    _filing_event(
                        entry,
                        type_=FORM_S1_FILED,
                        payload=_base_payload(entry),
                        run_id=run_id,
                        now=now,
                        company_id=linked.get(cik10(entry.cik)),
                    )
                )
        except (httpx.HTTPError, pydantic.ValidationError, ValueError) as exc:
            tally.errors += 1
            _log.warning("edgar: %s %s failed: %s", entry.form_type, entry.accession_number, exc)

    formd = ingest_formd_filings(session, filings, run_id=run_id, now=now)
    others = write_events(session, other_events)
    session.flush()
    _release(session, own)
    tally.inserted += formd.stats.inserted + others.inserted
    tally.skipped += formd.stats.skipped + others.skipped
    tally.amended += formd.amended
    tally.events += formd.events + other_events


def collect(
    session: Session,
    *,
    run_id: str,
    now: datetime,
    client: httpx.Client | None = None,
    dates: list[date] | None = None,
    since: date | None = None,
    budget: RateBudget | None = None,
    sleep: Sleeper = time.sleep,
) -> EdgarRunResult:
    """Read the published daily indexes for ``dates`` and write this run's EDGAR events.

    ``dates`` defaults to :func:`since_dates` when ``since`` is given, else to the
    catch-up window of :func:`default_dates` after :func:`last_index_date`. The quarter
    ``index.json`` listings (one request per quarter) pick the days actually published;
    only those indexes are requested. Days run oldest first, each as its own
    transaction, and no transaction stays open across HTTP when the session arrives
    without one (the CLI's case); otherwise the caller's transaction holds everything.

    ``fetched`` counts index rows of the kept form types (deduplicated by accession);
    ``skipped`` includes rows whose event already exists (``notes.known``). A filing
    that fails (404 primary document, validation error, HTTP error) is logged, counted
    in ``errors`` and skipped; the rest of the run continues. The caller owns the
    ``runs`` row (``start_run`` / ``finish_run``). ``notes.last_index_date`` is the
    latest index read so far by this or an earlier run: the next run starts after it.
    """
    own = not session.in_transaction()
    previous = last_index_date(session)
    if dates is not None:
        days = sorted(set(dates))
    elif since is not None:
        days = since_dates(now, since)
    else:
        days = default_dates(now, previous)
    _release(session, own)

    tally = _Tally()
    read: list[date] = []
    seen: set[str] = set()
    with SecClient(client, budget=budget, sleep=sleep) as sec:
        listings: dict[str, set[str]] = {}
        published: list[date] = []
        for day in days:
            if _index_is_listed(sec, day, listings):
                published.append(day)
            else:
                _log.info("edgar: no daily index for %s (not in the quarter listing)", day)
        for day in published:
            rows = _fetch_day(sec, day)
            if rows is None:
                continue
            read.append(day)
            entries: list[IndexEntry] = []
            for entry in rows:  # first row wins: co-issuers repeat one accession
                if entry.accession_number not in seen:
                    seen.add(entry.accession_number)
                    entries.append(entry)
            tally.fetched += len(entries)
            _collect_day(session, sec, entries, tally, run_id=run_id, now=now, own=own)

    latest = max([*read, *([previous] if previous else [])], default=None)
    notes: dict[str, Any] = {
        "dates": [d.isoformat() for d in days],
        "indexes": [d.isoformat() for d in read],
        "last_index_date": latest.isoformat() if latest else None,
        "previous_index_date": previous.isoformat() if previous else None,
        "known": tally.known,
        "amended": tally.amended,
    }
    if since is not None and dates is None:
        notes["since"] = since.isoformat()
    return EdgarRunResult(
        fetched=tally.fetched,
        inserted=tally.inserted,
        skipped=tally.skipped + tally.known,
        events=tally.events,
        errors=tally.errors,
        amended=tally.amended,
        notes=notes,
    )


def run_fixture(session: Session, case_path: Path, /, *, run_id: str, now: datetime) -> RunResult:
    """Replay a JSON rendering of Form D filings through the real write path, no network."""
    filings = FormDFilings.model_validate_json(case_path.read_text(encoding="utf-8")).root
    result = ingest_formd_filings(session, filings, run_id=run_id, now=now)
    return EdgarRunResult(
        fetched=len(filings),
        inserted=result.stats.inserted,
        skipped=result.stats.skipped,
        events=result.events,
        amended=result.amended,
    )


COLLECTOR = CollectorSpec(
    source=SOURCE,
    kind="filings",
    response_model=FormDFilings,
    fixture_dir=FIXTURE_DIR,
    drift_missing_field="totalOfferingAmount",
    run_fixture=run_fixture,
)
