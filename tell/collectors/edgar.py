"""SEC EDGAR collector (SPEC section 3.6): daily form index, Form D, 8-K and S-1 events.

One run reads the daily ``form.{YYYYMMDD}.idx`` for each requested date (yesterday and
today in New York by default), keeps form types ``D``, ``D/A``, ``8-K`` and ``S-1``,
and turns them into events keyed ``edgar:{accession_number}``:

* ``D``   -> ``form_d_filed`` from ``primary_doc.xml`` (see :mod:`tell.collectors.edgar_formd`);
* ``D/A`` -> merged into the original ``form_d_filed`` payload, never a second event
  (SPEC section 5 rule 4), or ``form_d_amendment`` when the original is unknown;
* ``8-K`` -> ``form_8k_filed`` for monitored companies only (``companies.cik``), with
  items from ``data.sec.gov/submissions`` and items 5.02 / 1.01 / 2.01 flagged;
* ``S-1`` -> ``form_s1_filed``.

Every SEC request carries ``SEC_USER_AGENT`` and ``Accept-Encoding: gzip`` and draws from
one :class:`~tell.http.RateBudget` at ``TELL_RATE_EDGAR_RPS`` (5, half the SEC's 10 rps
fair-access ceiling) shared by ``www.sec.gov`` and ``data.sec.gov``. Officer and
director names in Form D are never read or stored.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

import httpx
import pydantic
from pydantic import ConfigDict, Field
from sqlalchemy.orm import Session

from tell.collectors.base import CollectorSpec, Event, RunResult, write_events
from tell.collectors.edgar_formd import (
    SOURCE,
    FormDFiling,
    FormDFilings,
    cik10,
    company_ids_by_cik,
    filing_folder_url,
    ingest_formd_filings,
    load_formd_quarterly,
    parse_formd_xml,
    primary_doc_url,
)
from tell.config import get_settings
from tell.http import RateBudget, Sleeper, fetch, make_client

__all__ = [
    "COLLECTOR",
    "FORM_8K_FILED",
    "FORM_S1_FILED",
    "KEPT_FORMS",
    "EdgarRunResult",
    "IndexEntry",
    "SecClient",
    "TickerInfo",
    "accession_from_file_name",
    "collect",
    "daily_index_url",
    "default_dates",
    "load_formd_quarterly",
    "load_ticker_map",
    "parse_daily_index",
    "parse_ticker_map",
    "run_fixture",
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


def default_dates(now: datetime) -> list[date]:
    """Yesterday and today on the SEC's (New York) calendar."""
    today = now.astimezone(SEC_TZ).date()
    return [today - timedelta(days=1), today]


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


def collect(
    session: Session,
    *,
    run_id: str,
    now: datetime,
    client: httpx.Client | None = None,
    dates: list[date] | None = None,
    budget: RateBudget | None = None,
    sleep: Sleeper = time.sleep,
) -> EdgarRunResult:
    """Fetch the daily indexes for ``dates`` and write this run's EDGAR events.

    ``fetched`` counts index rows of the kept form types (deduplicated by accession).
    A filing that fails (404 primary document, validation error, HTTP error) is
    logged, counted in ``errors`` and skipped; the rest of the run continues. The
    caller owns the transaction and the ``runs`` row (``start_run`` / ``finish_run``).
    """
    entries: dict[str, IndexEntry] = {}
    errors = 0
    filings: list[FormDFiling] = []
    other_events: list[Event] = []
    with SecClient(client, budget=budget, sleep=sleep) as sec:
        days = dates if dates is not None else default_dates(now)
        for day in days:
            response = sec.get(daily_index_url(day))
            if response is None:
                _log.info("edgar: no daily index for %s", day.isoformat())
                continue
            for entry in parse_daily_index(response.text):
                entries.setdefault(entry.accession_number, entry)

        monitored = company_ids_by_cik(session, [e.cik for e in entries.values()])
        for entry in entries.values():
            try:
                if entry.form_type in {"D", "D/A"}:
                    filing = _fetch_formd(sec, entry)
                    if filing is None:
                        errors += 1
                        _log.warning("edgar: no primary_doc.xml for %s", entry.accession_number)
                    else:
                        filings.append(filing)
                elif entry.form_type == "8-K":
                    company_id = monitored.get(cik10(entry.cik))
                    if company_id is None:
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
                            company_id=company_id,
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
                            company_id=monitored.get(cik10(entry.cik)),
                        )
                    )
            except (httpx.HTTPError, pydantic.ValidationError, ValueError) as exc:
                errors += 1
                _log.warning(
                    "edgar: %s %s failed: %s", entry.form_type, entry.accession_number, exc
                )

    formd = ingest_formd_filings(session, filings, run_id=run_id, now=now)
    others = write_events(session, other_events)
    session.flush()
    return EdgarRunResult(
        fetched=len(entries),
        inserted=formd.stats.inserted + others.inserted,
        skipped=formd.stats.skipped + others.skipped,
        events=formd.events + other_events,
        errors=errors,
        amended=formd.amended,
        notes={"dates": [d.isoformat() for d in days], "amended": formd.amended},
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
