"""Intake reader (spec section 15.6, PLAN Phase 12): the Google Form sheet into watchlists.

The intake Google Form (field spec in ``docs/intake/google-form.md``) collects name,
email, company, role, up to 50 domains (one per line), "who you sell to" (an ICP
profile name), an optional Slack webhook URL and an "I'm a job seeker" checkbox. Its
response sheet is published as CSV; the nightly job reads that CSV and turns every
new, valid submission into a watchlist.

Privacy rule (ADR 0027, spec section 1 repos table: customer input lives only in
``tell-private``):

* the database gets only company-level data: ``watchlists.owner`` is an
  :func:`owner_handle` (``intake-`` plus 16 random hex characters, minted once per
  watchlist and kept in the private record; never derived from the email, so no two
  submitters can share one and nothing about the address can be guessed from it),
  ``watchlists.name`` / ``slug`` the slug of company + profile, ``watchlist_items`` the
  submitted domains;
* the raw contact details (name, email, role) and the Slack webhook URL are written
  only to ``tell-private/watchlists/<slug>.yaml``, which :func:`load_private_watchlists`
  reads back (the Slack digest's per-watchlist webhook hook);
* nothing personal reaches stdout, the ``runs`` row or a log line: rejected rows are
  reported by row number and reason only, never by a field value.

Pipeline:

1. :func:`read_csv_source` fetches the published CSV through :mod:`tell.http` (retries,
   polite User-Agent) or reads a local file.
2. :func:`parse_submissions` maps the header row onto the form fields
   (:data:`COLUMN_ALIASES`; a missing required column is :class:`IntakeError`, the form
   drifted) and validates each row: an email with a local part, a company, 1 to
   :data:`MAX_DOMAINS` registrable domains (``tldextract`` via
   :func:`tell.discovery.registrable_domain`; lines that are not domains are counted
   and dropped), a profile that exists in :func:`tell.signals.profiles.load_profiles`
   (the checkbox selects ``job-seeker``), and a webhook starting with
   :data:`SLACK_WEBHOOK_PREFIX` when one is given.
3. :func:`sync_submissions` applies each new submission (identity: :func:`submission_id`,
   the SHA-256 of timestamp + email + company) in one transaction: create or update the
   watchlist ``<slug>``, create ``companies`` rows for unknown domains (``name`` = the
   domain label, so ``tell discover batch`` picks them up) and add the missing
   ``watchlist_items``. A submission already recorded in ``tell-private`` whose watchlist
   exists is skipped, so a re-sync changes nothing. Items are only ever added. An
   existing watchlist with that slug is reused only when intake made it: it is private,
   its owner is an intake handle and the private record (when there is one) carries the
   same handle. Anything else (a public list, a hand-made list, a stale private record)
   rejects the row as ``slug taken`` rather than adding domains to someone else's list.
4. :func:`write_private_watchlists` writes the merged private records after the
   database commit; if that write fails the next sync re-applies the submissions
   idempotently and writes them again.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import secrets
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from tell.collectors.base import finish_run, iso_utc, start_run
from tell.discovery import domain_label, registrable_domain
from tell.http import get as http_get
from tell.models import Company, Watchlist, WatchlistItem
from tell.signals.profiles import Profile, load_profiles

__all__ = [
    "COLUMN_ALIASES",
    "DEFAULT_PRIVATE_DIR",
    "INTAKE_OWNER_PREFIX",
    "INTAKE_SOURCE",
    "JOB_SEEKER_PROFILE",
    "MAX_DOMAINS",
    "PRIVATE_WATCHLISTS_SUBDIR",
    "REQUIRED_FIELDS",
    "SLACK_WEBHOOK_PREFIX",
    "Contact",
    "IntakeError",
    "PrivateRecordError",
    "PrivateWatchlist",
    "RowError",
    "Submission",
    "SyncResult",
    "check_private_dir",
    "is_intake_owner",
    "load_private_watchlists",
    "owner_handle",
    "parse_domains",
    "parse_submissions",
    "read_csv_source",
    "slugify",
    "submission_id",
    "sync_submissions",
    "write_private_watchlists",
]

INTAKE_SOURCE: Final = "intake"
"""``runs.source`` of an intake sync (an internal job, not on the health page)."""

MAX_DOMAINS: Final = 50
"""Spec section 15.6: up to 50 domains per submission; more rejects the row."""

SLACK_WEBHOOK_PREFIX: Final = "https://hooks.slack.com/"
"""A Slack incoming webhook; anything else in the webhook field rejects the row."""

JOB_SEEKER_PROFILE: Final = "job-seeker"
"""The profile a submission gets when the "I'm a job seeker" box is ticked."""

INTAKE_OWNER_PREFIX: Final = "intake-"
"""Marks a ``watchlists.owner`` minted by the intake reader (see :func:`owner_handle`)."""

OWNER_HANDLE_LENGTH: Final = 16
"""Hex characters after the prefix (64 random bits)."""

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
"""The public ``tell`` checkout; the private directory must never be inside it."""

DEFAULT_PRIVATE_DIR: Final = Path("..") / "tell-private"
"""``../tell-private`` relative to the working directory (PLAN "Local build conventions":
the private checkout sits next to the code checkout, and ``tell`` runs from the code
checkout). The workflows pass ``--private-dir`` explicitly."""

PRIVATE_WATCHLISTS_SUBDIR: Final = "watchlists"
PRIVATE_PROFILES_SUBDIR: Final = "profiles"

PRIVATE_FILE_HEADER: Final = (
    "# PRIVATE: customer input from the tell intake form (ADR 0027).\n"
    "# Lives only in tell-private; never copy any of it into tell or tell-data.\n"
)

# Header aliases after _normalise_header (lowercase, punctuation to single spaces).
COLUMN_ALIASES: Final[Mapping[str, frozenset[str]]] = {
    "timestamp": frozenset({"timestamp"}),
    "name": frozenset({"name", "your name"}),
    "email": frozenset({"email", "email address", "your email"}),
    "company": frozenset({"company", "your company"}),
    "role": frozenset({"role", "your role"}),
    "domains": frozenset(
        {"domains", "domains up to 50 one per line", "up to 50 domains one per line"}
    ),
    "profile": frozenset({"who you sell to", "who do you sell to", "icp profile", "profile"}),
    "webhook": frozenset({"slack webhook url", "slack webhook url optional", "slack webhook"}),
    "job_seeker": frozenset({"i m a job seeker", "im a job seeker", "job seeker"}),
}
"""Form field -> accepted (normalised) sheet column headers."""

REQUIRED_FIELDS: Final = ("timestamp", "email", "company", "domains", "profile")
"""Columns the sheet must have; the others may be absent (treated as empty)."""

_FALSE_CHECKBOX: Final = frozenset({"", "no", "false", "0", "n"})
_DOMAIN_SPLIT: Final = re.compile(r"[\s,;]+")
_PROFILE_IN_LABEL: Final = re.compile(r"\(([^()]+)\)\s*$")
_NON_SLUG: Final = re.compile(r"[^a-z0-9]+")
_HEADER_PUNCT: Final = re.compile(r"[^a-z0-9]+")


class PrivateRecordError(ValueError):
    """A ``tell-private/watchlists/<slug>.yaml`` record does not validate or is misnamed.

    The message names the file (a customer's slug) and the failing fields, never a field
    value; callers that log publicly report only the type name.
    """


class IntakeError(RuntimeError):
    """The intake source cannot be read as a whole (fetch failed, form columns drifted)."""


# --- identity and normalisation --------------------------------------------------------


def owner_handle() -> str:
    """A fresh ``watchlists.owner`` for an intake watchlist: ``intake-`` + 16 random hex.

    Random, never derived from the email (ADR 0027). A hash of the address would let two
    unrelated submitters share a handle (``sales@`` at two companies), and the v1 API
    grants private-watchlist access by owner equality; a common local part would also be
    guessable. The handle is minted once per watchlist, written to the private record
    and copied from there (or from the existing database row) on every later sync, so
    the database holds nothing that points at a person. The prefix lets
    :func:`sync_submissions` tell an intake list from a hand-made one with the same slug.
    """
    return INTAKE_OWNER_PREFIX + secrets.token_hex(OWNER_HANDLE_LENGTH // 2)


def is_intake_owner(owner: str) -> bool:
    """``True`` for a handle in the :func:`owner_handle` format."""
    body = owner.removeprefix(INTAKE_OWNER_PREFIX)
    return (
        owner.startswith(INTAKE_OWNER_PREFIX)
        and len(body) == OWNER_HANDLE_LENGTH
        and all(c in "0123456789abcdef" for c in body)
    )


def submission_id(timestamp: str, email: str, company: str) -> str:
    """Stable identity of one form response: SHA-256 of ``timestamp|email|company``.

    Whitespace is trimmed and the email lowercased, so re-publishing the sheet (or a
    re-export with different padding) yields the same id. Stored only in
    ``tell-private``.
    """
    key = "|".join((timestamp.strip(), email.strip().lower(), company.strip()))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def slugify(text: str) -> str:
    """ASCII-folded, lowercase, runs of anything else to ``-``: ``Acme, Inc.`` -> ``acme-inc``."""
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return _NON_SLUG.sub("-", folded.lower()).strip("-")


def watchlist_slug(company: str, profile: str) -> str:
    """``slug(company) + "-" + slug(profile)``; a company with no ASCII letters hashes."""
    company_part = slugify(company)
    if not company_part:
        company_part = "company-" + hashlib.sha256(company.strip().encode("utf-8")).hexdigest()[:8]
    return f"{company_part}-{slugify(profile)}"


def parse_domains(cell: str) -> tuple[list[str], int]:
    """``(domains, rejected)``: registrable domains in input order, deduplicated.

    Lines (or comma / whitespace separated tokens) that hold no registrable domain,
    and anything with an ``@``, are counted in ``rejected`` and dropped.
    """
    domains: list[str] = []
    seen: set[str] = set()
    rejected = 0
    for token in _DOMAIN_SPLIT.split(cell):
        if not token:
            continue
        domain = None if "@" in token else registrable_domain(token)
        if domain is None:
            rejected += 1
            continue
        if domain not in seen:
            seen.add(domain)
            domains.append(domain)
    return domains, rejected


def _valid_email(value: str) -> bool:
    local, sep, host = value.rpartition("@")
    return bool(sep and local and "." in host and not any(c.isspace() for c in value))


def _normalise_header(header: str) -> str:
    return _HEADER_PUNCT.sub(" ", header.strip().lower()).strip()


def _resolve_profile(value: str, job_seeker: bool, profiles: Mapping[str, Profile]) -> str | None:
    """The profile name for a row: the checkbox wins, else the dropdown value.

    A dropdown option is either a profile name verbatim (``default``) or a label that
    ends in the name in brackets (``Sales teams (default)``); case is ignored.
    """
    if job_seeker and JOB_SEEKER_PROFILE in profiles:
        return JOB_SEEKER_PROFILE
    text = value.strip()
    match = _PROFILE_IN_LABEL.search(text)
    if match:
        text = match.group(1).strip()
    by_lower = {name.lower(): name for name in profiles}
    return by_lower.get(text.lower())


# --- parsing -----------------------------------------------------------------------------


@dataclass(frozen=True)
class RowError:
    """A rejected row: its 1-based sheet row number (header = 1) and a reason, no values."""

    row: int
    reason: str

    def to_dict(self) -> dict[str, int | str]:
        return {"row": self.row, "reason": self.reason}


@dataclass(frozen=True, kw_only=True)
class Submission:
    """One valid form response. Personal fields stay in memory and in ``tell-private``."""

    row: int
    submission_id: str
    submitted_at: str
    name: str
    email: str
    company: str
    role: str
    domains: tuple[str, ...]
    rejected_domains: int
    icp_profile: str
    slack_webhook_url: str | None
    job_seeker: bool

    @property
    def slug(self) -> str:
        return watchlist_slug(self.company, self.icp_profile)


def _column_map(fieldnames: Sequence[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for index, header in enumerate(fieldnames):
        normalised = _normalise_header(header)
        for name, aliases in COLUMN_ALIASES.items():
            if normalised in aliases and name not in mapping:
                mapping[name] = index
    missing = [name for name in REQUIRED_FIELDS if name not in mapping]
    if missing:
        msg = (
            f"intake sheet is missing required columns: {missing} (see docs/intake/google-form.md)"
        )
        raise IntakeError(msg)
    return mapping


def _contact_error(email: str, company: str) -> str | None:
    if not email:
        return "missing email"
    if not _valid_email(email):
        return "invalid email"
    if not company:
        return "missing company"
    return None


def _content_error(domains: Sequence[str], profile: str | None, webhook: str | None) -> str | None:
    if not domains:
        return "no valid domains"
    if len(domains) > MAX_DOMAINS:
        return f"too many domains ({len(domains)} > {MAX_DOMAINS})"
    if profile is None:
        return "unknown profile"
    if webhook is not None and not webhook.startswith(SLACK_WEBHOOK_PREFIX):
        return "invalid slack webhook url"
    return None


def _validate_row(
    row_number: int, cells: Mapping[str, str], profiles: Mapping[str, Profile]
) -> Submission | RowError:
    email = cells["email"].strip()
    company = " ".join(cells["company"].split())
    domains, rejected = parse_domains(cells["domains"])
    job_seeker = cells["job_seeker"].strip().lower() not in _FALSE_CHECKBOX
    profile = _resolve_profile(cells["profile"], job_seeker, profiles)
    webhook = cells["webhook"].strip() or None
    reason = _contact_error(email, company) or _content_error(domains, profile, webhook)
    if reason is not None or profile is None:
        return RowError(row_number, reason or "unknown profile")
    timestamp = cells["timestamp"].strip()
    return Submission(
        row=row_number,
        submission_id=submission_id(timestamp, email, company),
        submitted_at=timestamp,
        name=" ".join(cells["name"].split()),
        email=email,
        company=company,
        role=" ".join(cells["role"].split()),
        domains=tuple(domains),
        rejected_domains=rejected,
        icp_profile=profile,
        slack_webhook_url=webhook,
        job_seeker=job_seeker,
    )


def parse_submissions(
    csv_text: str, profiles: Mapping[str, Profile]
) -> tuple[list[Submission], list[RowError]]:
    """Validate every response row of the published sheet; blank rows are ignored.

    Raises :class:`IntakeError` when the header row lacks a :data:`REQUIRED_FIELDS`
    column (the form changed and this reader must follow).
    """
    reader = csv.reader(io.StringIO(csv_text.lstrip("\ufeff")))
    header = next(reader, None)
    if header is None:
        msg = "intake sheet is empty (no header row)"
        raise IntakeError(msg)
    columns = _column_map(header)
    submissions: list[Submission] = []
    errors: list[RowError] = []
    for offset, raw in enumerate(reader):
        row_number = offset + 2  # the header is sheet row 1
        if not any(cell.strip() for cell in raw):
            continue
        cells = {
            name: (raw[columns[name]] if name in columns and columns[name] < len(raw) else "")
            for name in COLUMN_ALIASES
        }
        result = _validate_row(row_number, cells, profiles)
        if isinstance(result, RowError):
            errors.append(result)
        else:
            submissions.append(result)
    return submissions, errors


def read_csv_source(
    *,
    csv_url: str | None = None,
    csv_file: Path | None = None,
    client: httpx.Client | None = None,
) -> str:
    """The sheet as text: GET ``csv_url`` through :mod:`tell.http`, or read ``csv_file``.

    Exactly one of the two must be given. Fetch failures raise :class:`IntakeError`
    whose message never repeats the URL (a published-sheet link is a secret); so does a
    ``csv_file`` that is not UTF-8 (``UnicodeDecodeError`` is a ``ValueError``, which the
    CLI would otherwise report as a bad private watchlist file).
    """
    if (csv_url is None) == (csv_file is None):
        msg = "pass exactly one of csv_url or csv_file"
        raise ValueError(msg)
    if csv_file is not None:
        try:
            with csv_file.open(encoding="utf-8-sig", newline="") as fh:  # csv keeps newlines
                return fh.read()
        except UnicodeDecodeError:
            msg = "intake CSV is not UTF-8"
            raise IntakeError(msg) from None
    assert csv_url is not None  # narrowed by the exclusive check above
    if not csv_url.startswith("https://"):
        msg = "the intake CSV URL must be https"
        raise IntakeError(msg)
    try:
        response = http_get(csv_url, client=client)
    except httpx.HTTPStatusError as exc:
        msg = f"intake CSV fetch failed: HTTP {exc.response.status_code}"
        raise IntakeError(msg) from None
    except httpx.HTTPError as exc:
        msg = f"intake CSV fetch failed: {type(exc).__name__}"
        raise IntakeError(msg) from None
    return response.text.lstrip("\ufeff")


# --- private records (tell-private/watchlists/<slug>.yaml) -------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Contact(_Strict):
    """One person who submitted for this watchlist. Private: ``tell-private`` only."""

    name: str = ""
    email: str
    role: str = ""
    submitted_at: str = ""


class PrivateWatchlist(_Strict):
    """``tell-private/watchlists/<slug>.yaml``: the customer side of one watchlist.

    ``slack_webhook_url`` is the per-watchlist digest destination (the last non-empty
    one submitted); ``submissions`` lists the :func:`submission_id` of every response
    already applied, which is what makes a re-sync a no-op.
    """

    slug: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    company: str
    icp_profile: str
    owner: str
    job_seeker: bool = False
    slack_webhook_url: str | None = None
    contacts: list[Contact] = Field(default_factory=list)
    domains: list[str] = Field(default_factory=list)
    submissions: list[str] = Field(default_factory=list)
    updated_at: str = ""

    def merged(self, sub: Submission, now: datetime) -> PrivateWatchlist:
        """This record with ``sub`` folded in (contacts by email, domains unioned)."""
        contacts = [c for c in self.contacts if c.email.lower() != sub.email.lower()]
        contacts.append(_contact(sub))
        domains = list(self.domains) + [d for d in sub.domains if d not in self.domains]
        submissions = list(self.submissions)
        if sub.submission_id not in submissions:
            submissions.append(sub.submission_id)
        return self.model_copy(
            update={
                "company": sub.company,
                "job_seeker": sub.job_seeker,
                "slack_webhook_url": sub.slack_webhook_url or self.slack_webhook_url,
                "contacts": contacts,
                "domains": domains,
                "submissions": submissions,
                "updated_at": iso_utc(now),
            }
        )


def _contact(sub: Submission) -> Contact:
    return Contact(name=sub.name, email=sub.email, role=sub.role, submitted_at=sub.submitted_at)


def _new_private(sub: Submission, now: datetime, owner: str) -> PrivateWatchlist:
    return PrivateWatchlist(
        slug=sub.slug,
        company=sub.company,
        icp_profile=sub.icp_profile,
        owner=owner,
        job_seeker=sub.job_seeker,
        slack_webhook_url=sub.slack_webhook_url,
        contacts=[_contact(sub)],
        domains=list(sub.domains),
        submissions=[sub.submission_id],
        updated_at=iso_utc(now),
    )


def check_private_dir(private_dir: Path) -> Path:
    """``private_dir`` resolved; :class:`IntakeError` when it lies inside the public checkout.

    Contact details must never land in a directory that ``tell`` would commit.
    """
    resolved = private_dir.expanduser().resolve()
    if resolved == REPO_ROOT or REPO_ROOT in resolved.parents:
        msg = "the private directory must be outside the public tell checkout"
        raise IntakeError(msg)
    return resolved


def load_private_watchlists(private_dir: Path = DEFAULT_PRIVATE_DIR) -> dict[str, PrivateWatchlist]:
    """``{slug: PrivateWatchlist}`` from ``<private_dir>/watchlists/*.yaml``.

    The hook for the Slack digest (:mod:`tell.publish.slack`): look a watchlist up by
    ``Watchlist.slug`` and post to its ``slack_webhook_url`` when one is set. A missing
    directory yields ``{}`` (fork checkouts have no ``tell-private``). A file whose
    ``slug`` differs from its name, or that does not validate, raises
    :class:`PrivateRecordError` (a ``ValueError``) naming the file but not its contents; a
    file that is not YAML raises ``yaml.YAMLError`` (whose message may quote the file, so
    callers log only its type).
    """
    directory = private_dir / PRIVATE_WATCHLISTS_SUBDIR
    if not directory.is_dir():
        return {}
    records: dict[str, PrivateWatchlist] = {}
    for path in sorted(directory.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        try:
            record = PrivateWatchlist.model_validate(data)
        except ValidationError as exc:
            fields = sorted({".".join(str(p) for p in err["loc"]) for err in exc.errors()})
            msg = f"{path.name}: invalid private watchlist (fields: {fields})"
            raise PrivateRecordError(msg) from None
        if record.slug != path.stem:
            msg = f"{path.name}: slug does not match the file name"
            raise PrivateRecordError(msg)
        records[record.slug] = record
    return records


def write_private_watchlists(
    private_dir: Path, records: Mapping[str, PrivateWatchlist]
) -> list[Path]:
    """Write each record to ``<private_dir>/watchlists/<slug>.yaml``; returns the paths."""
    directory = check_private_dir(private_dir) / PRIVATE_WATCHLISTS_SUBDIR
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for slug, record in sorted(records.items()):
        path = directory / f"{slug}.yaml"
        body = yaml.safe_dump(record.model_dump(mode="json"), sort_keys=False, allow_unicode=True)
        tmp = path.with_suffix(".yaml.tmp")
        tmp.write_text(PRIVATE_FILE_HEADER + body, encoding="utf-8")
        tmp.replace(path)
        written.append(path)
    return written


# --- applying to the database ------------------------------------------------------------


@dataclass
class SyncResult:
    """What one sync did. ``private_updates`` must be written after the commit."""

    run_id: str
    rows: int = 0
    applied: int = 0
    skipped: int = 0
    watchlists_created: int = 0
    companies_created: int = 0
    items_added: int = 0
    rejected_domains: int = 0
    errors: list[RowError] = field(default_factory=list)
    private_updates: dict[str, PrivateWatchlist] = field(default_factory=dict)

    def log_record(self) -> dict[str, object]:
        """Counts only: safe for stdout, Actions logs and ``runs.notes``."""
        return {
            "run_id": self.run_id,
            "source": INTAKE_SOURCE,
            "rows": self.rows,
            "applied": self.applied,
            "skipped": self.skipped,
            "errors": len(self.errors),
            "watchlists_created": self.watchlists_created,
            "companies_created": self.companies_created,
            "items_added": self.items_added,
            "rejected_domains": self.rejected_domains,
            "rejected_rows": [e.to_dict() for e in self.errors],
        }


class _SlugTakenError(Exception):
    """The slug belongs to a watchlist intake did not make (or whose record disagrees)."""


def _watchlist(session: Session, slug: str) -> Watchlist | None:
    return session.scalar(select(Watchlist).where(Watchlist.slug == slug))


def _reusable(watchlist: Watchlist, record: PrivateWatchlist | None) -> bool:
    """Intake may add to ``watchlist`` only when intake made it (see the module docstring).

    No private record (the write after the commit was lost) is fine as long as the row
    carries an intake handle; a record with a different handle means the files and the
    database disagree, which a person must sort out.
    """
    if watchlist.is_public or not is_intake_owner(watchlist.owner):
        return False
    return record is None or record.owner == watchlist.owner


def _ensure_companies(session: Session, domains: Iterable[str]) -> tuple[dict[str, int], int]:
    wanted = list(domains)
    stmt = select(Company.domain, Company.id).where(Company.domain.in_(wanted))
    found: dict[str, int] = {row.domain: row.id for row in session.execute(stmt)}
    created = 0
    for domain in wanted:
        if domain in found:
            continue
        company = Company(domain=domain, name=domain_label(domain) or domain, source="manual")
        session.add(company)
        session.flush()
        found[domain] = company.id
        created += 1
    return found, created


def _apply(
    session: Session, sub: Submission, record: PrivateWatchlist | None, result: SyncResult
) -> str:
    """Create or extend the watchlist for ``sub``; returns its owner handle.

    Raises :class:`_SlugTakenError` before touching anything when the slug's watchlist is not
    one intake may extend. The owner comes from the private record when there is one
    (the database lost the list), else from the existing row, else it is minted.
    """
    watchlist = _watchlist(session, sub.slug)
    if watchlist is not None and not _reusable(watchlist, record):
        raise _SlugTakenError
    if watchlist is None:
        watchlist = Watchlist(
            owner=record.owner if record is not None else owner_handle(),
            name=sub.slug,
            icp_profile=sub.icp_profile,
            slug=sub.slug,
            is_public=False,
        )
        session.add(watchlist)
        session.flush()
        result.watchlists_created += 1
    companies, created = _ensure_companies(session, sub.domains)
    result.companies_created += created
    members = set(
        session.scalars(
            select(WatchlistItem.company_id).where(WatchlistItem.watchlist_id == watchlist.id)
        )
    )
    for domain in sub.domains:
        company_id = companies[domain]
        if company_id not in members:
            session.add(WatchlistItem(watchlist_id=watchlist.id, company_id=company_id))
            members.add(company_id)
            result.items_added += 1
    session.flush()
    return watchlist.owner


def sync_submissions(
    session: Session,
    csv_text: str,
    *,
    private_dir: Path,
    now: datetime,
    profiles: Mapping[str, Profile] | None = None,
) -> SyncResult:
    """Apply every new valid submission of ``csv_text`` as one ``intake`` run.

    Flushes, never commits. The caller commits and then passes
    ``result.private_updates`` to :func:`write_private_watchlists`. ``profiles``
    defaults to the shipped profiles plus ``<private_dir>/profiles``. A missing
    required column fails the run (``status='failed'``) and re-raises.
    """
    resolved = check_private_dir(private_dir)
    known = (
        profiles
        if profiles is not None
        else load_profiles(private_dir=resolved / PRIVATE_PROFILES_SUBDIR)
    )
    run_id = start_run(session, INTAKE_SOURCE, now=now)
    result = SyncResult(run_id=run_id)
    try:
        submissions, errors = parse_submissions(csv_text, known)
    except IntakeError:
        finish_run(
            session, run_id, status="failed", fetched=0, inserted=0, skipped=0, errors=1, now=now
        )
        raise
    result.errors = errors
    result.rows = len(submissions) + len(errors)
    records = load_private_watchlists(resolved)
    for sub in submissions:
        result.rejected_domains += sub.rejected_domains
        record = records.get(sub.slug)
        already = record is not None and sub.submission_id in record.submissions
        if already and _watchlist(session, sub.slug) is not None:
            result.skipped += 1
            continue
        try:
            owner = _apply(session, sub, record, result)
        except _SlugTakenError:
            result.errors.append(RowError(sub.row, "slug taken"))
            continue
        result.applied += 1
        records[sub.slug] = (
            record.merged(sub, now) if record is not None else _new_private(sub, now, owner)
        )
        result.private_updates[sub.slug] = records[sub.slug]
    result.errors.sort(key=lambda e: e.row)
    notes = {k: v for k, v in result.log_record().items() if k not in ("run_id", "source")}
    finish_run(
        session,
        run_id,
        status="ok",
        fetched=result.rows,
        inserted=result.applied,
        skipped=result.skipped,
        errors=len(result.errors),
        notes=notes,
        now=now,
    )
    return result
