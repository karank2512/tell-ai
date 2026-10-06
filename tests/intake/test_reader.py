"""Intake reader (spec section 15.6): parsing, validation, the privacy rule, idempotent sync.

The response sheet is built inline: placeholder contacts use ``example.invalid``
addresses here only, never in a file under ``tests/fixtures`` (PII guard).
"""

from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
import respx
import yaml
from freezegun import freeze_time
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

import tell.intake.reader as reader
from tell.intake import load_private_watchlists
from tell.intake.reader import (
    INTAKE_SOURCE,
    MAX_DOMAINS,
    REPO_ROOT,
    IntakeError,
    PrivateWatchlist,
    RowError,
    check_private_dir,
    is_intake_owner,
    owner_handle,
    parse_domains,
    parse_submissions,
    read_csv_source,
    slugify,
    submission_id,
    sync_submissions,
    watchlist_slug,
    write_private_watchlists,
)
from tell.models import Company, Run, Watchlist, WatchlistItem
from tell.signals.profiles import SHIPPED_PROFILES_DIR, Profile, load_profiles

NOW = datetime(2026, 10, 6, 7, 0, tzinfo=UTC)
LATER = datetime(2026, 10, 7, 7, 0, tzinfo=UTC)
WEBHOOK_1 = "https://hooks.slack.com/services/TPLACE/BPLACE/placeholder1"
WEBHOOK_2 = "https://hooks.slack.com/services/TPLACE/BPLACE/placeholder2"
CSV_URL = "https://docs.google.invalid/spreadsheets/d/e/placeholder/pub?output=csv"

HEADER = [
    "Timestamp",
    "Name",
    "Email",
    "Company",
    "Role",
    "Domains (up to 50, one per line)",
    "Who do you sell to?",
    "Slack webhook URL (optional)",
    "I'm a job seeker",
]
CONTACT_1 = ("Placeholder One", "user1@example.invalid")
CONTACT_2 = ("Placeholder Two", "user2@example.invalid")
CONTACT_3 = ("Placeholder Three", "user3@example.invalid")


def _row(
    timestamp: str,
    contact: tuple[str, str],
    company: str,
    domains: str,
    profile: str = "default",
    webhook: str = "",
    job_seeker: str = "",
    role: str = "Head of Sales",
) -> list[str]:
    name, email = contact
    return [timestamp, name, email, company, role, domains, profile, webhook, job_seeker]


def _csv(rows: list[list[str]], header: list[str] = HEADER) -> str:
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(header)
    writer.writerows(rows)
    return out.getvalue()


SHEET_ROWS = [
    _row(
        "10/5/2026 9:15:03",
        CONTACT_1,
        "Acme Rockets",
        "acme.example.com\nhttps://www.Widgets.co.uk/careers\nnot a domain\nacme.example.com",
        "Sales teams (default)",
        WEBHOOK_1,
    ),
    _row("10/5/2026 10:01:44", CONTACT_2, "Grad Search", "beta.io", "", "", "Yes", "Student"),
    _row("10/5/2026 11:30:00", CONTACT_3, "Nope Corp", "gamma.io", "unknown-profile"),
    ["", "", "", "", "", "", "", "", ""],
]
SHEET = _csv(SHEET_ROWS)


@pytest.fixture
def profiles() -> dict[str, Profile]:
    return load_profiles(private_dir=None)


@pytest.fixture
def private_dir(tmp_path: Path) -> Path:
    return tmp_path / "tell-private"


def _count(session: Session, model: type[object]) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


# --- identity helpers --------------------------------------------------------------------


def test_owner_handle_is_random_and_recognisable() -> None:
    handle = owner_handle()
    assert handle.startswith("intake-")
    body = handle.removeprefix("intake-")
    assert len(body) == 16
    assert all(c in "0123456789abcdef" for c in body)
    assert is_intake_owner(handle)
    assert len({owner_handle() for _ in range(50)}) == 50  # never derived from an address
    for other in ("", "intake-", "intake-0123456789abcdeg", "0123456789abcdef", "karan"):
        assert not is_intake_owner(other)


def test_submission_id_is_stable_under_padding_and_case() -> None:
    a = submission_id("10/5/2026 9:15:03", "user1@example.invalid", "Acme")
    assert a == submission_id(" 10/5/2026 9:15:03 ", "USER1@example.invalid ", "Acme ")
    assert a != submission_id("10/5/2026 9:15:04", "user1@example.invalid", "Acme")
    assert len(a) == 64


def test_slugs() -> None:
    assert slugify("Acme, Inc.") == "acme-inc"
    assert slugify("Café Société") == "cafe-societe"
    assert watchlist_slug("Acme Rockets", "default") == "acme-rockets-default"
    assert watchlist_slug("Acme", "job-seeker") == "acme-job-seeker"
    fallback = watchlist_slug("株式会社", "default")
    assert fallback.startswith("company-")
    assert fallback.endswith("-default")
    assert fallback == watchlist_slug(" 株式会社 ", "default")


def test_parse_domains_normalises_and_counts_junk() -> None:
    domains, rejected = parse_domains(
        "Acme.com\nhttps://www.acme.com/jobs\n  sub.widgets.co.uk ,beta.io;\n"
        "localhost\nperson@example.invalid\n\n10.0.0.1"
    )
    assert domains == ["acme.com", "widgets.co.uk", "beta.io"]
    assert rejected == 3


# --- parsing -----------------------------------------------------------------------------


def test_parse_submissions_validates_rows(profiles: dict[str, Profile]) -> None:
    subs, errors = parse_submissions(SHEET, profiles)
    assert errors == [RowError(row=4, reason="unknown profile")]
    assert [s.row for s in subs] == [2, 3]
    acme, grad = subs
    assert acme.domains == ("example.com", "widgets.co.uk")  # registrable domains
    assert acme.rejected_domains == 3  # "not a domain" is three rejected tokens
    assert acme.icp_profile == "default"
    assert acme.slack_webhook_url == WEBHOOK_1
    assert acme.slug == "acme-rockets-default"
    assert not acme.job_seeker
    assert grad.job_seeker
    assert grad.icp_profile == "job-seeker"  # the checkbox wins over an empty dropdown
    assert grad.slack_webhook_url is None
    assert grad.slug == "grad-search-job-seeker"


@pytest.mark.parametrize(
    ("row", "reason"),
    [
        (_row("t", ("Placeholder", ""), "Acme", "acme.com"), "missing email"),
        (_row("t", ("Placeholder", "no-at-sign"), "Acme", "acme.com"), "invalid email"),
        (_row("t", ("Placeholder", "user1@localhost"), "Acme", "acme.com"), "invalid email"),
        (_row("t", CONTACT_1, "   ", "acme.com"), "missing company"),
        (_row("t", CONTACT_1, "Acme", "nothing here"), "no valid domains"),
        (
            _row("t", CONTACT_1, "Acme", "\n".join(f"d{i}.com" for i in range(MAX_DOMAINS + 1))),
            f"too many domains ({MAX_DOMAINS + 1} > {MAX_DOMAINS})",
        ),
        (_row("t", CONTACT_1, "Acme", "acme.com", ""), "unknown profile"),
        (
            _row("t", CONTACT_1, "Acme", "acme.com", "default", "https://example.invalid/hook"),
            "invalid slack webhook url",
        ),
    ],
)
def test_invalid_rows_are_reported_without_values(
    profiles: dict[str, Profile], row: list[str], reason: str
) -> None:
    subs, errors = parse_submissions(_csv([row]), profiles)
    assert subs == []
    assert errors == [RowError(row=2, reason=reason)]
    assert errors[0].to_dict() == {"row": 2, "reason": reason}


def test_fifty_domains_is_the_limit(profiles: dict[str, Profile]) -> None:
    cell = "\n".join(f"d{i}.com" for i in range(MAX_DOMAINS))
    subs, errors = parse_submissions(_csv([_row("t", CONTACT_1, "Acme", cell)]), profiles)
    assert errors == []
    assert len(subs[0].domains) == MAX_DOMAINS


def test_profile_matching_is_case_insensitive_and_checkbox_falsey(
    profiles: dict[str, Profile],
) -> None:
    rows = [
        _row("t1", CONTACT_1, "Acme", "acme.com", "DEFAULT", job_seeker="no"),
        _row("t2", CONTACT_1, "Acme", "acme.com", "Engineers (job-seeker)"),
    ]
    subs, errors = parse_submissions(_csv(rows), profiles)
    assert errors == []
    assert [(s.icp_profile, s.job_seeker) for s in subs] == [
        ("default", False),
        ("job-seeker", False),
    ]


def test_optional_columns_may_be_absent_and_short_rows_pad(profiles: dict[str, Profile]) -> None:
    header = ["Timestamp", "Email Address", "Company", "Domains", "ICP profile"]
    text = "\ufeff" + _csv([["t", CONTACT_1[1], "Acme", "acme.com"]], header)
    subs, errors = parse_submissions(text, profiles)
    assert errors == [RowError(row=2, reason="unknown profile")]  # the short row has none
    subs, errors = parse_submissions(
        _csv([["t", CONTACT_1[1], "Acme", "acme.com", "default"]], header), profiles
    )
    assert errors == []
    assert (subs[0].name, subs[0].role, subs[0].slack_webhook_url) == ("", "", None)


def test_missing_required_column_or_empty_sheet_raises(profiles: dict[str, Profile]) -> None:
    with pytest.raises(IntakeError, match="missing required columns"):
        parse_submissions(_csv([], [h for h in HEADER if not h.startswith("Domains")]), profiles)
    with pytest.raises(IntakeError, match="empty"):
        parse_submissions("", profiles)


# --- reading the source ------------------------------------------------------------------


@respx.mock
def test_read_csv_source_fetches_through_tell_http() -> None:
    respx.get(CSV_URL).mock(return_value=httpx.Response(200, text="\ufeff" + SHEET))
    assert read_csv_source(csv_url=CSV_URL) == SHEET


@respx.mock
def test_read_csv_source_errors_never_repeat_the_url() -> None:
    respx.get(CSV_URL).mock(return_value=httpx.Response(404))
    with pytest.raises(IntakeError) as info:
        read_csv_source(csv_url=CSV_URL)
    assert str(info.value) == "intake CSV fetch failed: HTTP 404"
    assert "placeholder" not in str(info.value)


def test_read_csv_source_transport_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(url: str, **_: object) -> httpx.Response:
        raise httpx.ConnectError("refused", request=httpx.Request("GET", url))

    monkeypatch.setattr(reader, "http_get", boom)
    with pytest.raises(IntakeError, match="ConnectError"):
        read_csv_source(csv_url=CSV_URL)


def test_read_csv_source_arguments(tmp_path: Path) -> None:
    path = tmp_path / "sheet.csv"
    path.write_text("\ufeff" + SHEET, encoding="utf-8")
    assert read_csv_source(csv_file=path) == SHEET
    with pytest.raises(ValueError, match="exactly one"):
        read_csv_source()
    with pytest.raises(ValueError, match="exactly one"):
        read_csv_source(csv_url=CSV_URL, csv_file=path)
    with pytest.raises(IntakeError, match="https"):
        read_csv_source(csv_url="http://docs.google.invalid/x.csv")


def test_read_csv_source_rejects_a_non_utf8_file(tmp_path: Path) -> None:
    path = tmp_path / "sheet.csv"
    path.write_bytes(SHEET.encode("utf-16"))  # a UnicodeDecodeError is a ValueError
    with pytest.raises(IntakeError, match="not UTF-8"):
        read_csv_source(csv_file=path)


# --- private records -----------------------------------------------------------------------


def test_private_dir_must_be_outside_the_public_checkout(tmp_path: Path) -> None:
    assert check_private_dir(tmp_path) == tmp_path.resolve()
    for inside in (REPO_ROOT, REPO_ROOT / "tell-private"):
        with pytest.raises(IntakeError, match="outside"):
            check_private_dir(inside)


def test_load_private_watchlists_edge_cases(private_dir: Path) -> None:
    assert load_private_watchlists(private_dir) == {}
    folder = private_dir / "watchlists"
    folder.mkdir(parents=True)
    record = PrivateWatchlist(slug="acme-default", company="Acme", icp_profile="default", owner="0")
    (folder / "other-name.yaml").write_text(yaml.safe_dump(record.model_dump()))
    with pytest.raises(ValueError, match="does not match"):
        load_private_watchlists(private_dir)
    (folder / "other-name.yaml").unlink()
    secret = "user9@example.invalid"
    (folder / "bad.yaml").write_text(yaml.safe_dump({"slug": "bad", "contacts": [{"x": secret}]}))
    with pytest.raises(ValueError, match="invalid private watchlist") as info:
        load_private_watchlists(private_dir)
    assert secret not in str(info.value)


# --- sync ----------------------------------------------------------------------------------


def _all_db_text(session: Session) -> str:
    """Every text value of the tables an intake sync writes, joined."""
    chunks: list[str] = []
    for table in ("watchlists", "watchlist_items", "companies", "runs"):
        for row in session.execute(text(f"select * from {table}")):
            chunks.extend(str(value) for value in row)
    return "\n".join(chunks)


def test_sync_creates_watchlists_and_keeps_contacts_private(
    db_session: Session, private_dir: Path
) -> None:
    db_session.add(Company(domain="widgets.co.uk", name="Widgets Ltd"))
    db_session.flush()

    result = sync_submissions(db_session, SHEET, private_dir=private_dir, now=NOW)
    assert (result.rows, result.applied, result.skipped) == (3, 2, 0)
    assert result.errors == [RowError(row=4, reason="unknown profile")]
    assert (result.watchlists_created, result.companies_created, result.items_added) == (2, 2, 3)
    assert result.rejected_domains == 3  # "not", "a", "domain"

    watchlists = {w.slug: w for w in db_session.scalars(select(Watchlist))}
    assert set(watchlists) == {"acme-rockets-default", "grad-search-job-seeker"}
    acme = watchlists["acme-rockets-default"]
    assert acme.name == "acme-rockets-default"
    assert acme.icp_profile == "default"
    assert is_intake_owner(acme.owner)
    assert acme.owner != watchlists["grad-search-job-seeker"].owner
    assert acme.is_public is False
    assert watchlists["grad-search-job-seeker"].icp_profile == "job-seeker"

    companies = {c.domain: c.name for c in db_session.scalars(select(Company))}
    assert companies == {
        "widgets.co.uk": "Widgets Ltd",  # known company reused, name untouched
        "example.com": "example",  # acme.example.com -> registrable example.com
        "beta.io": "beta",
    }
    items = db_session.execute(
        select(Watchlist.slug, Company.domain)
        .join(WatchlistItem, WatchlistItem.watchlist_id == Watchlist.id)
        .join(Company, Company.id == WatchlistItem.company_id)
    ).all()
    assert sorted(tuple(row) for row in items) == [
        ("acme-rockets-default", "example.com"),
        ("acme-rockets-default", "widgets.co.uk"),
        ("grad-search-job-seeker", "beta.io"),
    ]

    run = db_session.get(Run, result.run_id)
    assert run is not None
    assert (run.source, run.status) == (INTAKE_SOURCE, "ok")
    assert (run.fetched, run.inserted, run.skipped, run.errors) == (3, 2, 0, 1)
    assert run.notes is not None
    assert run.notes["rejected_rows"] == [{"row": 4, "reason": "unknown profile"}]

    stored = _all_db_text(db_session)
    for secret in (*CONTACT_1, *CONTACT_2, *CONTACT_3, WEBHOOK_1, "Head of Sales", "Student"):
        assert secret not in stored
    assert "@" not in stored

    # Nothing is on disk until the caller writes the private updates after its commit.
    assert load_private_watchlists(private_dir) == {}
    paths = write_private_watchlists(private_dir, result.private_updates)
    assert sorted(p.name for p in paths) == [
        "acme-rockets-default.yaml",
        "grad-search-job-seeker.yaml",
    ]
    assert paths[0].read_text(encoding="utf-8").startswith("# PRIVATE")
    records = load_private_watchlists(private_dir)
    acme_private = records["acme-rockets-default"]
    assert acme_private.slack_webhook_url == WEBHOOK_1
    assert acme_private.owner == acme.owner
    assert acme_private.company == "Acme Rockets"
    assert [(c.name, c.email, c.role) for c in acme_private.contacts] == [
        (CONTACT_1[0], CONTACT_1[1], "Head of Sales")
    ]
    assert acme_private.domains == ["example.com", "widgets.co.uk"]
    assert acme_private.updated_at == "2026-10-06T07:00:00+00:00"
    assert records["grad-search-job-seeker"].job_seeker is True
    assert records["grad-search-job-seeker"].slack_webhook_url is None


def test_resync_is_idempotent(db_session: Session, private_dir: Path) -> None:
    first = sync_submissions(db_session, SHEET, private_dir=private_dir, now=NOW)
    write_private_watchlists(private_dir, first.private_updates)
    before = (
        _count(db_session, Watchlist),
        _count(db_session, WatchlistItem),
        _count(db_session, Company),
    )
    files_before = {p.name: p.read_text() for p in (private_dir / "watchlists").glob("*.yaml")}

    again = sync_submissions(db_session, SHEET, private_dir=private_dir, now=LATER)
    assert (again.applied, again.skipped, again.items_added) == (0, 2, 0)
    assert again.private_updates == {}
    assert write_private_watchlists(private_dir, again.private_updates) == []
    after = (
        _count(db_session, Watchlist),
        _count(db_session, WatchlistItem),
        _count(db_session, Company),
    )
    assert after == before
    assert {
        p.name: p.read_text() for p in (private_dir / "watchlists").glob("*.yaml")
    } == files_before


def test_later_submission_updates_the_watchlist(db_session: Session, private_dir: Path) -> None:
    first = sync_submissions(db_session, SHEET, private_dir=private_dir, now=NOW)
    write_private_watchlists(private_dir, first.private_updates)
    first_owner = first.private_updates["acme-rockets-default"].owner
    rows = [
        *SHEET_ROWS,
        # A teammate adds two domains (one already listed) and a new webhook.
        _row(
            "10/6/2026 8:00:00",
            CONTACT_3,
            "Acme Rockets",
            "widgets.co.uk\ndelta.dev",
            "default",
            WEBHOOK_2,
            role="RevOps",
        ),
        # The first submitter resubmits with no webhook: the stored one is kept.
        _row("10/6/2026 9:00:00", CONTACT_1, "Acme Rockets", "epsilon.dev", "default"),
        # An exact duplicate row of the one above within the same sheet.
        _row("10/6/2026 9:00:00", CONTACT_1, "Acme Rockets", "epsilon.dev", "default"),
    ]
    result = sync_submissions(db_session, _csv(rows), private_dir=private_dir, now=LATER)
    assert (result.applied, result.skipped, result.watchlists_created) == (2, 3, 0)
    assert result.items_added == 2
    write_private_watchlists(private_dir, result.private_updates)

    acme = db_session.scalar(select(Watchlist).where(Watchlist.slug == "acme-rockets-default"))
    assert acme is not None
    assert acme.owner == first_owner  # the handle minted for the list stays
    domains = set(
        db_session.scalars(
            select(Company.domain)
            .join(WatchlistItem, WatchlistItem.company_id == Company.id)
            .where(WatchlistItem.watchlist_id == acme.id)
        )
    )
    assert domains == {"example.com", "widgets.co.uk", "delta.dev", "epsilon.dev"}

    record = load_private_watchlists(private_dir)["acme-rockets-default"]
    assert record.slack_webhook_url == WEBHOOK_2
    assert [c.email for c in record.contacts] == [CONTACT_3[1], CONTACT_1[1]]
    assert record.domains == ["example.com", "widgets.co.uk", "delta.dev", "epsilon.dev"]
    assert len(record.submissions) == 3
    assert record.updated_at == "2026-10-07T07:00:00+00:00"


def test_lost_private_write_or_fresh_database_reapplies_without_duplicates(
    db_session: Session, private_dir: Path
) -> None:
    # The private files were never written (e.g. the write failed after the commit).
    sync_submissions(db_session, SHEET, private_dir=private_dir, now=NOW)
    owners = {w.slug: w.owner for w in db_session.scalars(select(Watchlist))}
    second = sync_submissions(db_session, SHEET, private_dir=private_dir, now=LATER)
    assert (second.applied, second.watchlists_created, second.items_added) == (2, 0, 0)
    write_private_watchlists(private_dir, second.private_updates)
    assert _count(db_session, Watchlist) == 2
    assert _count(db_session, WatchlistItem) == 3
    # The records take the handle the database row already had.
    assert {slug: r.owner for slug, r in second.private_updates.items()} == owners

    # The database lost its watchlists but tell-private still has the records.
    db_session.execute(WatchlistItem.__table__.delete())
    db_session.execute(Watchlist.__table__.delete())
    third = sync_submissions(db_session, SHEET, private_dir=private_dir, now=LATER)
    assert (third.applied, third.skipped, third.watchlists_created) == (2, 0, 2)
    assert _count(db_session, WatchlistItem) == 3
    record = third.private_updates["acme-rockets-default"]
    assert len(record.submissions) == 1  # the same submission is not recorded twice
    # ... and the new rows take the handle from the records, so API keys keep working.
    assert {w.slug: w.owner for w in db_session.scalars(select(Watchlist))} == owners


@pytest.mark.parametrize(
    ("owner", "is_public", "stale_record"),
    [
        ("karan", False, False),  # a hand-made private list that happens to share the slug
        (owner_handle(), True, False),  # a published list (ADR 0022)
        (owner_handle(), False, True),  # an intake list whose private record disagrees
    ],
)
def test_a_watchlist_intake_did_not_make_is_never_extended(
    db_session: Session, private_dir: Path, owner: str, is_public: bool, stale_record: bool
) -> None:
    taken = Watchlist(
        owner=owner,
        name="Acme Rockets",
        icp_profile="default",
        slug="acme-rockets-default",
        is_public=is_public,
    )
    db_session.add(taken)
    db_session.flush()
    if stale_record:
        record = PrivateWatchlist(
            slug="acme-rockets-default", company="Acme", icp_profile="default", owner=owner_handle()
        )
        write_private_watchlists(private_dir, {record.slug: record})
    before = (_count(db_session, WatchlistItem), _count(db_session, Company))

    result = sync_submissions(db_session, SHEET, private_dir=private_dir, now=NOW)
    assert result.errors == [
        RowError(row=2, reason="slug taken"),
        RowError(row=4, reason="unknown profile"),
    ]
    assert (result.rows, result.applied, result.skipped) == (3, 1, 0)
    assert list(result.private_updates) == ["grad-search-job-seeker"]
    assert (_count(db_session, WatchlistItem), _count(db_session, Company)) == (
        before[0] + 1,
        before[1] + 1,
    )  # only the job-seeker row landed
    assert taken.owner == owner
    assert taken.is_public is is_public
    run = db_session.get(Run, result.run_id)
    assert run is not None
    assert run.errors == 2
    assert run.notes is not None
    assert run.notes["rejected_rows"][0] == {"row": 2, "reason": "slug taken"}


def test_private_profiles_are_valid_choices(db_session: Session, private_dir: Path) -> None:
    profile_dir = private_dir / "profiles"
    profile_dir.mkdir(parents=True)
    shipped = (SHIPPED_PROFILES_DIR / "default.yaml").read_text(encoding="utf-8")
    (profile_dir / "partner.yaml").write_text(
        shipped.replace("name: default", "name: devtools-buyers"), encoding="utf-8"
    )
    sheet = _csv([_row("t", CONTACT_1, "Acme", "acme.com", "devtools-buyers")])
    result = sync_submissions(db_session, sheet, private_dir=private_dir, now=NOW)
    assert result.errors == []
    assert list(result.private_updates) == ["acme-devtools-buyers"]


def test_drifted_sheet_fails_the_run(db_session: Session, private_dir: Path) -> None:
    sheet = _csv([], [h for h in HEADER if h != "Company"])
    with pytest.raises(IntakeError):
        sync_submissions(db_session, sheet, private_dir=private_dir, now=NOW)
    run = db_session.scalar(select(Run).where(Run.source == INTAKE_SOURCE))
    assert run is not None
    assert (run.status, run.errors) == ("failed", 1)


@freeze_time("2026-10-06 07:00:00")
def test_sync_rejects_a_private_dir_inside_the_repo(db_session: Session) -> None:
    with pytest.raises(IntakeError, match="outside"):
        sync_submissions(db_session, SHEET, private_dir=REPO_ROOT / "private", now=NOW)
    with pytest.raises(IntakeError, match="outside"):
        write_private_watchlists(REPO_ROOT / "private", {})
    assert _count(db_session, Run) == 0
