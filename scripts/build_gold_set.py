"""Build the entity-resolution gold set (SPEC section 6, PLAN Phase 5, ADR 0006).

Reproduces the 300-company gold set in ``tell-private/gold/companies.csv`` and the
50-row public sample in ``tests/fixtures/gold_sample.csv``:

1. **Candidate universe.** The public YC directory mirror (``tell.discovery.yc``
   ``YC_DIRECTORY_URL``), or a saved copy via ``--seed-file``. Only company facts are
   read (name, website, batch, location, status, hiring flag, former names); nothing
   about people is read or written. Domains listed by more than one YC company are
   dropped, as are names in :data:`EXCLUDED_NAMES` (names that carry a person's name).
2. **Selection** (deterministic for a given ``--seed`` and directory snapshot):
   ``board`` rows are active, hiring companies whose domain resolves to a supported
   board (``tell.discovery.ats.discover_domain``, outcome ``board``); ``no_board`` rows
   are active, non-hiring companies where discovery found nothing; ``hard`` rows are
   renamed companies, near-duplicate names (pairs sharing a first word), names with
   legal suffixes / punctuation / digits, and non-US companies, kept whatever discovery
   said. Discovery runs against a throwaway database (``tell_gold_<hex>``, created and
   migrated like ``tests/conftest.py`` does, dropped at exit); ``tell_dev`` is never
   touched.
3. **Legal name and CIK.** EDGAR full-text search for Form D filings
   (``efts.sec.gov/LATEST/search-index?q="name"&forms=D``): an issuer counts only when
   its normalised name equals the YC name (or a former name) and, when YC gives a US
   state, the filing's business state (or city) agrees; ambiguity leaves the row
   unverified. Public companies are also matched against
   ``company_tickers_exchange.json``. On HTTP 403 from the SEC every further SEC call is
   skipped and CIKs stay blank.

``verification_source`` says where ``legal_name`` came from: ``edgar_fts`` (Form D issuer
or SEC ticker file, CIK filled), ``discovery`` (the job board's own company name carries a
legal suffix), ``yc`` (the YC name already carries one), ``unverified`` (best-effort legal
form built from the name and country).

Politeness: every request passes a per-host token bucket (``--rps``, default 2 per host)
installed as an httpx request hook, the project User-Agent is sent to every non-SEC host
and ``SEC_USER_AGENT`` to sec.gov. Discovery and EDGAR answers are cached as JSON lines in
``--cache-dir`` so a rerun (or a resumed run) repeats no request.

Usage::

    uv run python scripts/build_gold_set.py \
        --out ../tell-private/gold/companies.csv \
        --sample-out tests/fixtures/gold_sample.csv --limit 300
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import re
import secrets
import sys
import tempfile
import time
import unicodedata
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from alembic import command
from alembic.config import Config
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter
from rapidfuzz import fuzz
from sqlalchemy import text
from sqlalchemy.engine import Engine, make_url

from tell.config import get_settings
from tell.db import make_engine, session_scope
from tell.discovery import registrable_domain
from tell.discovery.ats import SUPPORTED_VENDORS, discover_domain
from tell.discovery.yc import YC_DIRECTORY_URL, parse_location
from tell.http import HostRateBudgets, fetch, make_client

REPO_ROOT = Path(__file__).resolve().parents[1]
ALEMBIC_INI = REPO_ROOT / "alembic.ini"
DEFAULT_OUT = REPO_ROOT.parent / "tell-private" / "gold" / "companies.csv"
DEFAULT_SAMPLE_OUT = REPO_ROOT / "tests" / "fixtures" / "gold_sample.csv"
DEFAULT_CACHE = Path(tempfile.gettempdir()) / "tell-gold-cache"

USER_AGENT = "tell/0.1 (+https://github.com/karankapur/tell)"
EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
PROTECTED_DATABASES = frozenset({"tell_dev", "tell", "postgres"})

COLUMNS: tuple[str, ...] = (
    "domain",
    "name",
    "legal_name",
    "cik",
    "hq_city",
    "hq_state",
    "hq_country",
    "board_vendor",
    "board_identifier",
    "verification_source",
    "notes",
)

SHARE_BOARD, SHARE_NONE = 0.6, 0.2
"""Share of ``--limit`` for ``board`` and ``no_board`` rows; the rest are hard cases."""

HARD_KINDS: tuple[str, ...] = ("renamed", "near_duplicate", "suffix_punct_digit", "international")
SAMPLE_SHARE = {"board": 30, "no_board": 10, "hard": 10}

EXCLUDED_NAMES = frozenset(
    {
        # Company names that embed a person's name; kept out so no file carries one.
        "Mark Cuban Cost Plus Drug Company, PBC",
        "Leon & George",
        "Lucy Goods, Inc",
        "Gerald Technologies, Inc",
        "80,000 Hours",
    }
)

LEGAL_SUFFIXES = frozenset(
    {
        "inc",
        "incorporated",
        "llc",
        "ltd",
        "limited",
        "corp",
        "corporation",
        "co",
        "company",
        "plc",
        "gmbh",
        "sa",
        "sas",
        "pbc",
        "holdings",
        "lp",
        "llp",
        "ag",
        "bv",
        "pte",
        "pvt",
        "private",
        "pty",
        "ltda",
    }
)
_LEGAL_SUFFIX_RE = re.compile(
    r"(,?\s+(inc\.?|incorporated|llc|l\.l\.c\.|ltd\.?|limited|corp\.?|corporation|co\.|"
    r"company|plc|gmbh|s\.a\.|pbc|pte\.? ltd\.?|private limited|pty ltd))\s*$",
    re.IGNORECASE,
)
_HARD_NAME_RE = re.compile(
    r"\d|[.&,!'+|]|\b(inc|labs|llc|ltd|gmbh|co|corp|technologies|pbc)\b", re.IGNORECASE
)
_PLACEHOLDER_NAME = re.compile(r"incorporated|stealth|tbd|n/a", re.IGNORECASE)
_NOT_A_NAME = LEGAL_SUFFIXES | {""}
"""Former-name fragments that are not names (``"Inc."`` split off ``"Petricorp, Inc."``)."""
_PAREN = re.compile(r"\([^)]*\)")
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_DISPLAY_NAME = re.compile(r"^(?P<name>.+?)\s+(?:\([^()]*\)\s+)*\(CIK (?P<cik>\d{10})\)\s*$")

COUNTRIES: dict[str, str] = {
    "USA": "US",
    "United States": "US",
    "United Kingdom": "GB",
    "India": "IN",
    "Canada": "CA",
    "France": "FR",
    "Mexico": "MX",
    "Germany": "DE",
    "Singapore": "SG",
    "Brazil": "BR",
    "Nigeria": "NG",
    "Israel": "IL",
    "Indonesia": "ID",
    "Colombia": "CO",
    "Spain": "ES",
    "Sweden": "SE",
    "Australia": "AU",
    "Argentina": "AR",
    "Chile": "CL",
    "United Arab Emirates": "AE",
    "Netherlands": "NL",
    "Denmark": "DK",
    "Egypt": "EG",
    "Switzerland": "CH",
    "South Korea": "KR",
    "Pakistan": "PK",
    "Kenya": "KE",
    "Norway": "NO",
    "Ireland": "IE",
    "Philippines": "PH",
    "Malaysia": "MY",
    "Peru": "PE",
    "Hong Kong": "HK",
    "Poland": "PL",
    "Austria": "AT",
}
LEGAL_FORMS: dict[str, str] = {
    "GB": "Ltd",
    "IE": "Ltd",
    "DE": "GmbH",
    "AT": "GmbH",
    "CH": "AG",
    "IN": "Private Limited",
    "FR": "SAS",
    "NL": "B.V.",
    "SG": "Pte. Ltd.",
    "AU": "Pty Ltd",
    "BR": "Ltda.",
    "MX": "S.A.P.I. de C.V.",
    "ES": "S.L.",
    "SE": "AB",
    "NO": "AS",
    "DK": "ApS",
    "PL": "sp. z o.o.",
    "HK": "Limited",
    "NG": "Limited",
    "KE": "Limited",
    "IL": "Ltd",
}

log = logging.getLogger("build_gold_set")


# --- candidate universe ------------------------------------------------------------------------


class YcRaw(BaseModel):
    """The company facts read from the directory; every other field is dropped on parse."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    name: str
    website: str | None = None
    batch: str | None = None
    all_locations: str | None = None
    status: str | None = None
    is_hiring: bool = Field(default=False, alias="isHiring")
    former_names: list[str] = Field(default_factory=list)


_RAW = TypeAdapter(list[YcRaw])


@dataclass(frozen=True)
class Candidate:
    name: str
    domain: str
    status: str
    hiring: bool
    locations: str | None
    former_names: tuple[str, ...]


def load_directory(seed_file: Path | None, cache_dir: Path, client: httpx.Client) -> list[YcRaw]:
    """Read the directory from ``seed_file``, the cache, or the mirror (one GET, then cached)."""
    cached = cache_dir / "yc_directory.json"
    path = seed_file or (cached if cached.is_file() else None)
    if path is not None:
        return _RAW.validate_python(json.loads(path.read_text(encoding="utf-8")))
    response = fetch(YC_DIRECTORY_URL, client=client)
    response.raise_for_status()
    records = _RAW.validate_python(response.json())
    trimmed = [record.model_dump(by_alias=True) for record in records]
    cached.write_text(json.dumps(trimmed, ensure_ascii=False), encoding="utf-8")
    return records


def candidates_from(records: Iterable[YcRaw]) -> list[Candidate]:
    """Active (or public) companies with a usable, unshared registrable domain."""
    by_domain: dict[str, list[YcRaw]] = defaultdict(list)
    for record in records:
        domain = registrable_domain(record.website) if record.website else None
        if domain:
            by_domain[domain].append(record)
    out: list[Candidate] = []
    for domain, group in sorted(by_domain.items()):
        if len(group) != 1:
            continue
        record = group[0]
        name = " ".join(record.name.split())
        if record.status not in {"Active", "Public"} or name in EXCLUDED_NAMES:
            continue
        out.append(
            Candidate(
                name=name,
                domain=domain,
                status=record.status,
                hiring=record.is_hiring,
                locations=record.all_locations,
                former_names=tuple(
                    n.strip() for n in record.former_names if name_key(n) not in _NOT_A_NAME
                ),
            )
        )
    return out


# --- name helpers ------------------------------------------------------------------------------


def name_key(name: str) -> str:
    """Comparison key: NFKC, no accents, no parentheticals, legal suffixes and ``the`` dropped."""
    text_ = unicodedata.normalize("NFKC", name)
    text_ = "".join(c for c in unicodedata.normalize("NFKD", text_) if not unicodedata.combining(c))
    text_ = _PAREN.sub(" ", text_.lower()).replace("&", " and ")
    text_ = text_.replace("l.l.c.", "llc").replace("s.a.", "sa")
    tokens = [t for t in _NON_ALNUM.split(text_) if t]
    while len(tokens) > 1 and tokens[-1] in LEGAL_SUFFIXES:
        tokens.pop()
    if len(tokens) > 1 and tokens[0] == "the":
        tokens = tokens[1:]
    return "".join(tokens)


def first_token(name: str) -> str:
    tokens = [t for t in _NON_ALNUM.split(_PAREN.sub(" ", name.lower())) if t]
    return tokens[0] if tokens else ""


def has_legal_suffix(name: str) -> bool:
    return _LEGAL_SUFFIX_RE.search(name) is not None


def location_of(candidate: Candidate) -> tuple[str, str, str]:
    """``(city, state, country)``; US via :func:`parse_location`, else ``City, ..., Country``."""
    city, state, country = parse_location(candidate.locations)
    if country:
        return city or "", state or "", country
    us_state = re.fullmatch(
        r"\s*([A-Z]{2}), (USA|United States)\s*", (candidate.locations or "").split(";", 1)[0]
    )
    if us_state:
        return "", us_state.group(1), "US"
    first = (candidate.locations or "").split(";", 1)[0]
    parts = [p.strip() for p in first.split(",") if p.strip()]
    if len(parts) >= 2 and parts[-1] in COUNTRIES:
        iso = COUNTRIES[parts[-1]]
        return parts[0], "", iso
    return "", "", ""


def best_effort_legal_name(name: str, country: str) -> str:
    clean = " ".join(_PAREN.sub(" ", name).split()) or name
    if has_legal_suffix(clean):
        return clean
    form = LEGAL_FORMS.get(country)
    return f"{clean} {form}" if form else f"{clean}, Inc."


# --- hard cases --------------------------------------------------------------------------------


@dataclass
class Pick:
    candidate: Candidate
    group: str
    """``board``, ``no_board`` or ``hard:<kind>``."""
    detail: str = ""


def hard_quotas(total: int) -> dict[str, int]:
    base, extra = divmod(total, len(HARD_KINDS))
    quotas = {kind: base + (1 if i < extra else 0) for i, kind in enumerate(HARD_KINDS)}
    quotas["near_duplicate"] -= quotas["near_duplicate"] % 2  # whole pairs only
    quotas["international"] += total - sum(quotas.values())
    return quotas


def choose_hard(pool: Sequence[Candidate], total: int, rng: random.Random) -> list[Pick]:
    quotas = hard_quotas(total)
    used: set[str] = set()
    picks: list[Pick] = []

    def take(kind: str, items: Iterable[tuple[Candidate, str]]) -> None:
        count = 0
        for candidate, detail in items:
            if count >= quotas[kind]:
                return
            if candidate.domain in used:
                continue
            used.add(candidate.domain)
            picks.append(Pick(candidate, f"hard:{kind}", detail))
            count += 1

    shuffled = list(pool)
    rng.shuffle(shuffled)
    take("renamed", _renamed(shuffled))
    take("near_duplicate", _near_duplicate_pairs(pool, quotas["near_duplicate"] // 2, used, rng))
    take(
        "suffix_punct_digit",
        (
            (c, "name has legal suffix/punctuation/digits")
            for c in shuffled
            if _HARD_NAME_RE.search(c.name)
        ),
    )
    take("international", _international(shuffled, used))
    return picks


def _renamed(shuffled: Iterable[Candidate]) -> Iterator[tuple[Candidate, str]]:
    """Companies with a former name that looks nothing like the current one."""
    for c in shuffled:
        current = name_key(c.name)
        for former in c.former_names:
            old = name_key(former)
            if (
                old
                and old not in current
                and current not in old
                and not _PLACEHOLDER_NAME.search(former)
                and fuzz.ratio(old, current) < 60
            ):
                yield c, f"renamed from {former}"
                break


def _near_duplicate_pairs(
    pool: Iterable[Candidate], pairs_wanted: int, used: set[str], rng: random.Random
) -> list[tuple[Candidate, str]]:
    """Pairs of different companies whose names start with the same word."""
    by_token: dict[str, list[Candidate]] = defaultdict(list)
    for c in pool:
        token = first_token(c.name)
        if len(token) >= 4 and token not in LEGAL_SUFFIXES:
            by_token[token].append(c)
    tokens = sorted(t for t, cs in by_token.items() if len(cs) >= 2)
    rng.shuffle(tokens)
    items: list[tuple[Candidate, str]] = []
    for token in tokens:
        if len(items) // 2 >= pairs_wanted:
            break
        a, b = sorted(by_token[token], key=lambda c: c.domain)[:2]
        if a.domain in used or b.domain in used:
            continue
        items.append((a, f"shares '{token}' with {b.domain}"))
        items.append((b, f"shares '{token}' with {a.domain}"))
    return items


def _international(shuffled: Iterable[Candidate], used: set[str]) -> list[tuple[Candidate, str]]:
    """Non-US companies, round-robin over countries so one country does not fill the quota."""
    by_country: dict[str, list[Candidate]] = defaultdict(list)
    for c in shuffled:
        country = location_of(c)[2]
        if country and country != "US" and c.domain not in used:
            by_country[country].append(c)
    countries = sorted(by_country, key=lambda k: (-len(by_country[k]), k))
    order: list[tuple[Candidate, str]] = []
    while any(by_country.values()):
        order.extend((by_country[k].pop(0), f"non-US ({k})") for k in countries if by_country[k])
    return order


# --- politeness --------------------------------------------------------------------------------


def throttled_client(user_agent: str, budgets: HostRateBudgets, timeout_s: float) -> httpx.Client:
    """The shared client with a per-host token bucket acquired before every request."""

    def throttle(request: httpx.Request) -> None:
        budgets.for_host(request.url.host).acquire()

    return make_client(
        user_agent=user_agent, timeout_s=timeout_s, event_hooks={"request": [throttle]}
    )


class JsonlCache:
    """Append-only JSON-lines cache keyed by one field."""

    def __init__(self, path: Path, key: str) -> None:
        self.path = path
        self.key = key
        self.data: dict[str, dict[str, Any]] = {}
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    row = json.loads(line)
                    self.data[str(row[key])] = row

    def get(self, key: str) -> dict[str, Any] | None:
        return self.data.get(key)

    def put(self, row: dict[str, Any]) -> None:
        self.data[str(row[self.key])] = row
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")


# --- scratch database --------------------------------------------------------------------------


@contextmanager
def scratch_database(admin_url: str, *, keep: bool = False) -> Iterator[Engine]:
    """Create ``tell_gold_<hex>``, migrate it to head, yield an engine, drop it afterwards."""
    db_name = f"tell_gold_{secrets.token_hex(4)}"
    admin = make_url(admin_url)
    admin_engine = make_engine(
        admin.render_as_string(hide_password=False), isolation_level="AUTOCOMMIT"
    )
    with admin_engine.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{db_name}"'))
    url = admin.set(database=db_name).render_as_string(hide_password=False)
    engine = make_engine(url)
    try:
        cfg = Config(str(ALEMBIC_INI))
        cfg.attributes["url"] = url
        cfg.attributes["skip_logging_config"] = True
        command.upgrade(cfg, "head")
        yield engine
    finally:
        engine.dispose()
        if not keep:
            with admin_engine.connect() as conn:
                conn.execute(
                    text(
                        "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                        "WHERE datname = :name AND pid <> pg_backend_pid()"
                    ),
                    {"name": db_name},
                )
                conn.execute(text(f'DROP DATABASE IF EXISTS "{db_name}"'))
        admin_engine.dispose()


# --- discovery ---------------------------------------------------------------------------------


def discover_one(engine: Engine, client: httpx.Client, candidate: Candidate) -> dict[str, Any]:
    started = time.monotonic()
    try:
        with session_scope(engine) as session:
            result = discover_domain(
                session, client, candidate.domain, candidate.name, now=datetime.now(UTC)
            )
            row: dict[str, Any] = {
                "domain": candidate.domain,
                "outcome": result.outcome,
                "vendor": result.vendor,
                "identifier": result.identifier,
                "board_name": result.board_name,
                "ratio": result.ratio,
                "discovered_from": result.discovered_from,
                "pages_checked": list(result.pages_checked),
            }
    except Exception as exc:  # one bad domain must not stop the batch
        row = {"domain": candidate.domain, "outcome": "error", "error": type(exc).__name__}
    row["seconds"] = round(time.monotonic() - started, 2)
    row["checked_at"] = datetime.now(UTC).date().isoformat()
    return row


@dataclass
class Discoverer:
    engine: Engine
    client: httpx.Client
    cache: JsonlCache
    workers: int
    live_calls: int = 0

    def run(self, candidates: Sequence[Candidate]) -> dict[str, dict[str, Any]]:
        todo = [c for c in candidates if self.cache.get(c.domain) is None]
        if todo:
            engine = self.engine
            with ThreadPoolExecutor(max_workers=self.workers) as pool:
                for row in pool.map(lambda c: discover_one(engine, self.client, c), todo):
                    self.cache.put(row)
                    self.live_calls += 1
                    log.info("discovery %s -> %s", row["domain"], row["outcome"])
        return {c.domain: self.cache.data[c.domain] for c in candidates}


def fill_group(
    discoverer: Discoverer,
    pool: Sequence[Candidate],
    *,
    want: int,
    accept: Callable[[dict[str, Any]], bool],
    group: str,
    chunk: int,
    max_tries: int,
) -> tuple[list[Pick], int]:
    """Discover ``pool`` in chunks until ``want`` rows pass ``accept``; return picks and tries."""
    picks: list[Pick] = []
    tried = 0
    limit = min(len(pool), max_tries)
    while len(picks) < want and tried < limit:
        batch = list(pool[tried : min(limit, tried + max(chunk, want - len(picks)))])
        results = discoverer.run(batch)
        tried += len(batch)
        for candidate in batch:
            if len(picks) < want and accept(results[candidate.domain]):
                picks.append(Pick(candidate, group))
    return picks, tried


# --- EDGAR -------------------------------------------------------------------------------------


@dataclass
class Edgar:
    client: httpx.Client
    cache: JsonlCache
    enabled: bool = True
    blocked: bool = False
    reachable: bool | None = None
    live_calls: int = 0
    tickers: dict[str, list[tuple[str, str]]] = field(default_factory=dict)

    def _get(self, url: str, params: dict[str, str] | None = None) -> Any | None:
        if not self.enabled or self.blocked:
            return None
        try:
            response = fetch(url, client=self.client, params=params)
        except httpx.HTTPError:
            log.warning("sec request failed: %s", url)
            return None
        self.live_calls += 1
        if response.status_code == 403:
            log.warning("sec.gov answered 403; no further SEC requests")
            self.blocked = True
            self.reachable = False
            return None
        self.reachable = True
        if response.status_code != 200:
            return None
        return response.json()

    def search(self, name: str) -> list[dict[str, Any]]:
        """Form D issuers whose filings mention ``"name"`` (cached by query)."""
        query = name.replace('"', "").strip()
        cached = self.cache.get(query)
        if cached is not None:
            hits: list[dict[str, Any]] = cached["issuers"]
            return hits
        data = self._get(EFTS_URL, {"q": f'"{query}"', "forms": "D"})
        if data is None:
            return []
        issuers: list[dict[str, Any]] = []
        for hit in data.get("hits", {}).get("hits", []):
            source = hit.get("_source", {})
            states = source.get("biz_states") or []
            locations = source.get("biz_locations") or []
            for i, display in enumerate(source.get("display_names") or []):
                match = _DISPLAY_NAME.match(display)
                if match is None:
                    continue
                issuers.append(
                    {
                        "entity": match["name"].strip(),
                        "cik": match["cik"],
                        "state": states[i] if i < len(states) else None,
                        "location": locations[i] if i < len(locations) else None,
                        "file_date": source.get("file_date"),
                    }
                )
        self.cache.put({"query": query, "issuers": issuers})
        return issuers

    def load_tickers(self) -> None:
        cached = self.cache.get("__tickers__")
        rows: list[list[Any]]
        if cached is not None:
            rows = cached["rows"]
        else:
            data = self._get(TICKERS_URL)
            if data is None:
                return
            fields = data["fields"]
            cik_i, name_i = fields.index("cik"), fields.index("name")
            rows = [[row[cik_i], row[name_i]] for row in data["data"]]
            self.cache.put({"query": "__tickers__", "rows": rows})
        for cik, title in rows:
            self.tickers.setdefault(name_key(str(title)), []).append(
                (f"{int(cik):010d}", str(title))
            )


@dataclass(frozen=True)
class LegalMatch:
    legal_name: str
    cik: str
    note: str


def _agrees(rows: Iterable[dict[str, Any]], city: str, state: str) -> bool:
    """A filing's business address agrees with YC: same US state, else same city."""
    for row in rows:
        if state and row["state"] == state:
            return True
        if city and (row["location"] or "").lower().startswith(city.lower()):
            return True
    return False


def match_form_d(
    edgar: Edgar, candidate: Candidate, city: str, state: str
) -> tuple[LegalMatch | None, str]:
    """The unique same-name Form D issuer whose address agrees with YC, plus a review note.

    No YC geography means no link: a same-name issuer elsewhere is too often another company.
    """
    names = [candidate.name, *candidate.former_names[:2]]
    keys = {name_key(n) for n in names if name_key(n)}
    unlinked = 0
    for query in names:
        by_cik: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for issuer in edgar.search(query):
            if name_key(issuer["entity"]) in keys:
                by_cik[issuer["cik"]].append(issuer)
        agreeing = {cik: rows for cik, rows in by_cik.items() if _agrees(rows, city, state)}
        unlinked = max(unlinked, len(by_cik) - len(agreeing))
        if len(agreeing) > 1:
            return None, f"{len(agreeing)} same-name form d issuers, not linked"
        if len(agreeing) == 1:
            cik, rows = next(iter(agreeing.items()))
            latest = max(rows, key=lambda r: r["file_date"] or "")
            note = ""
            if name_key(latest["entity"]) != name_key(candidate.name):
                note = "form d issuer under former name"
            return LegalMatch(latest["entity"], cik, note), ""
    if candidate.status == "Public":
        listed = [m for k in keys for m in edgar.tickers.get(k, [])]
        if len({cik for cik, _ in listed}) == 1:
            cik, title = listed[0]
            return LegalMatch(title, cik, "legal name from SEC ticker file"), ""
    note = f"{unlinked} same-name form d issuer(s) elsewhere, not linked" if unlinked else ""
    return None, note


# --- rows --------------------------------------------------------------------------------------


_OUTCOME_NOTES = {
    "review": "board found but ownership unverified",
    "no_board": "discovery found no board",
    "error": "discovery errored",
}


def board_columns(discovery: dict[str, Any]) -> tuple[str, str, str]:
    """``(board_vendor, board_identifier, note)``; vendor columns only for a verified board."""
    outcome = str(discovery.get("outcome") or "")
    if outcome == "board" and discovery.get("vendor") in SUPPORTED_VENDORS:
        return str(discovery["vendor"]), str(discovery["identifier"]), ""
    if outcome == "unsupported":
        return "", "", f"unsupported ats: {discovery.get('vendor')}"
    return "", "", _OUTCOME_NOTES.get(outcome, "")


def build_row(pick: Pick, discovery: dict[str, Any], edgar: Edgar) -> dict[str, str]:
    c = pick.candidate
    city, state, country = location_of(c)
    notes = [f"set={pick.group}"]
    if pick.detail:
        notes.append(pick.detail)
    vendor, identifier, board_note = board_columns(discovery)
    if board_note:
        notes.append(board_note)

    legal, edgar_note = match_form_d(edgar, c, city, state)
    if edgar_note:
        notes.append(edgar_note)
    cik = ""
    if legal is not None:
        legal_name, cik, source = legal.legal_name, legal.cik, "edgar_fts"
        if legal.note:
            notes.append(legal.note)
    else:
        board_name = str(discovery.get("board_name") or "")
        if board_name and has_legal_suffix(board_name) and name_key(board_name) == name_key(c.name):
            legal_name, source = board_name, "discovery"
        elif has_legal_suffix(c.name):
            legal_name, source = best_effort_legal_name(c.name, country), "yc"
        else:
            legal_name, source = best_effort_legal_name(c.name, country), "unverified"
    return {
        "domain": c.domain,
        "name": c.name,
        "legal_name": legal_name,
        "cik": cik,
        "hq_city": city,
        "hq_state": state,
        "hq_country": country,
        "board_vendor": vendor,
        "board_identifier": identifier,
        "verification_source": source,
        "notes": "; ".join(notes),
    }


def write_csv(path: Path, rows: Iterable[dict[str, str]]) -> int:
    ordered = sorted(rows, key=lambda r: r["domain"])
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(COLUMNS), lineterminator="\n")
        writer.writeheader()
        writer.writerows(ordered)
    return len(ordered)


def sample_rows(
    rows: Sequence[dict[str, str]], size: int, rng: random.Random
) -> list[dict[str, str]]:
    """Stratified sample: boards, no-boards, and hard cases (near-duplicates in whole pairs)."""
    total = sum(SAMPLE_SHARE.values())
    want = {k: round(v * size / total) for k, v in SAMPLE_SHARE.items()}
    want["board"] += size - sum(want.values())

    def group(row: dict[str, str]) -> str:
        return row["notes"].split(";", 1)[0].removeprefix("set=")

    out: list[dict[str, str]] = []
    for key in ("board", "no_board"):
        pool = sorted((r for r in rows if group(r) == key), key=lambda r: r["domain"])
        out.extend(rng.sample(pool, min(want[key], len(pool))))
    hard = [r for r in rows if group(r).startswith("hard:")]
    by_kind: dict[str, list[dict[str, str]]] = defaultdict(list)
    for r in sorted(hard, key=lambda r: r["domain"]):
        by_kind[group(r)].append(r)
    domains = {r["domain"]: r for r in hard}
    chosen: list[dict[str, str]] = []
    pairs = by_kind.get("hard:near_duplicate", [])
    pair_keys = sorted({tuple(sorted((r["domain"], _partner(r)))) for r in pairs})
    for a, b in rng.sample(pair_keys, min(2, len(pair_keys))):
        chosen.extend(domains[d] for d in (a, b) if d in domains)
    rest = want["hard"] - len(chosen)
    kinds = [k for k in sorted(by_kind) if k != "hard:near_duplicate"]
    i = 0
    pools = {k: rng.sample(by_kind[k], len(by_kind[k])) for k in kinds}
    while rest > 0 and any(pools.values()):
        kind = kinds[i % len(kinds)]
        if pools[kind]:
            chosen.append(pools[kind].pop())
            rest -= 1
        i += 1
    return out + chosen


def _partner(row: dict[str, str]) -> str:
    match = re.search(r"with (\S+)", row["notes"])
    return match.group(1).rstrip(";") if match else row["domain"]


# --- main --------------------------------------------------------------------------------------


@dataclass
class Summary:
    rows: int = 0
    sample_rows: int = 0
    by_group: dict[str, int] = field(default_factory=dict)
    by_source: dict[str, int] = field(default_factory=dict)
    boards_found: int = 0
    boards_by_vendor: dict[str, int] = field(default_factory=dict)
    discovery_tried: int = 0
    discovery_live: int = 0
    sec_reachable: bool | None = None
    sec_blocked: bool = False
    sec_live_calls: int = 0
    with_cik: int = 0
    seconds: float = 0.0
    shortfalls: list[str] = field(default_factory=list)


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0] if __doc__ else "")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="full gold set CSV")
    parser.add_argument(
        "--sample-out", type=Path, default=DEFAULT_SAMPLE_OUT, help="public sample CSV"
    )
    parser.add_argument("--limit", type=int, default=300, help="rows in the full set")
    parser.add_argument("--sample-size", type=int, default=50, help="rows in the public sample")
    parser.add_argument("--seed", type=int, default=20261005, help="random seed for selection")
    parser.add_argument("--seed-file", type=Path, help="saved YC directory JSON (all.json shape)")
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE, help="request cache")
    parser.add_argument(
        "--admin-url", help="Postgres URL of a maintenance DB (default: DATABASE_URL server)"
    )
    parser.add_argument("--keep-db", action="store_true", help="do not drop the scratch database")
    parser.add_argument("--workers", type=int, default=8, help="parallel discovery workers")
    parser.add_argument("--rps", type=float, default=2.0, help="max requests per second per host")
    parser.add_argument("--timeout", type=float, default=10.0, help="per-request timeout (s)")
    parser.add_argument("--no-edgar", action="store_true", help="skip SEC lookups")
    parser.add_argument("-v", "--verbose", action="store_true")
    return parser.parse_args(argv)


def admin_url_from(explicit: str | None) -> str:
    url = make_url(explicit or get_settings().database_url)
    return url.set(database="postgres").render_as_string(hide_password=False)


def select_picks(
    args: argparse.Namespace,
    pool: Sequence[Candidate],
    rng: random.Random,
    web: httpx.Client,
    summary: Summary,
) -> tuple[list[Pick], JsonlCache]:
    """Pick hard cases, then discover boards / no-boards until each quota is met."""
    n_board = round(args.limit * SHARE_BOARD)
    n_none = round(args.limit * SHARE_NONE)
    hard = choose_hard(pool, args.limit - n_board - n_none, rng)
    taken = {p.candidate.domain for p in hard}
    hiring = [c for c in pool if c.hiring and c.domain not in taken]
    quiet = [c for c in pool if not c.hiring and c.domain not in taken]
    rng.shuffle(hiring)
    rng.shuffle(quiet)
    cache = JsonlCache(args.cache_dir / "discovery.jsonl", "domain")
    with scratch_database(admin_url_from(args.admin_url), keep=args.keep_db) as engine:
        discoverer = Discoverer(engine=engine, client=web, cache=cache, workers=args.workers)
        boards, tried_b = fill_group(
            discoverer,
            hiring,
            want=n_board,
            accept=lambda r: r.get("outcome") == "board",
            group="board",
            chunk=48,
            max_tries=n_board * 5,
        )
        nones, tried_n = fill_group(
            discoverer,
            quiet,
            want=n_none,
            accept=lambda r: r.get("outcome") == "no_board",
            group="no_board",
            chunk=24,
            max_tries=n_none * 4,
        )
        discoverer.run([p.candidate for p in hard])
    summary.discovery_tried = tried_b + tried_n + len(hard)
    summary.discovery_live = discoverer.live_calls
    if len(boards) < n_board:
        summary.shortfalls.append(f"board: {len(boards)}/{n_board}")
    if len(nones) < n_none:
        summary.shortfalls.append(f"no_board: {len(nones)}/{n_none}")
    return boards + nones + hard, cache


def summarize(summary: Summary, rows: Sequence[dict[str, str]], edgar: Edgar) -> None:
    summary.by_group = dict(Counter(r["notes"].split(";", 1)[0] for r in rows))
    summary.by_source = dict(Counter(r["verification_source"] for r in rows))
    summary.boards_found = sum(1 for r in rows if r["board_vendor"])
    summary.boards_by_vendor = dict(Counter(r["board_vendor"] for r in rows if r["board_vendor"]))
    summary.with_cik = sum(1 for r in rows if r["cik"])
    summary.sec_reachable = edgar.reachable
    summary.sec_blocked = edgar.blocked
    summary.sec_live_calls = edgar.live_calls


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, stream=sys.stderr)
    logging.getLogger("tell").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    started = time.monotonic()
    rng = random.Random(args.seed)  # noqa: S311 - reproducible sampling, not cryptography
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    summary = Summary()

    budgets = HostRateBudgets(args.rps, 1)
    with (
        throttled_client(USER_AGENT, budgets, args.timeout) as web,
        throttled_client(get_settings().sec_user_agent, budgets, args.timeout) as sec,
    ):
        sec.headers["Accept-Encoding"] = "gzip, deflate"
        pool = candidates_from(load_directory(args.seed_file, args.cache_dir, web))
        picks, discovered = select_picks(args, pool, rng, web, summary)
        edgar = Edgar(
            client=sec,
            cache=JsonlCache(args.cache_dir / "edgar.jsonl", "query"),
            enabled=not args.no_edgar,
        )
        edgar.load_tickers()
        rows = [
            build_row(pick, discovered.get(pick.candidate.domain) or {}, edgar) for pick in picks
        ]

    summary.rows = write_csv(args.out, rows)
    summary.sample_rows = write_csv(args.sample_out, sample_rows(rows, args.sample_size, rng))
    summarize(summary, rows, edgar)
    summary.seconds = round(time.monotonic() - started, 1)
    print(json.dumps(asdict(summary), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
