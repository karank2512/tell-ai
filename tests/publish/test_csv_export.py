"""CSV with receipts: the exact spec column list, one row per domain."""

from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path

import pytest
from freezegun import freeze_time
from sqlalchemy import select
from sqlalchemy.orm import Session

from tell.config import get_settings
from tell.models import Signal
from tell.publish.csv_export import CSV_COLUMNS, export_csv
from tests.publish import scenario as sc

pytestmark = pytest.mark.db

SPEC_COLUMNS = [
    "domain",
    "name",
    "score",
    "heat",
    "top_signal_1",
    "top_signal_1_date",
    "top_signal_1_url",
    "top_signal_2",
    "top_signal_2_date",
    "top_signal_2_url",
    "new_roles_7d_sales",
    "new_roles_7d_eng",
    "new_roles_7d_other",
    "funding_last_180d_usd",
    "funding_url",
    "open_postings",
    "long_open_roles",
    "ats",
    "coverage_note",
    "account_page",
    "rate_this_row",
]


@pytest.fixture(autouse=True)
def _frozen() -> Iterator[None]:
    with freeze_time(sc.NOW):
        yield


def _read(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return list(reader.fieldnames or []), list(reader)


def test_header_is_exactly_the_spec_list(db_session: Session, tmp_path: Path) -> None:
    assert list(CSV_COLUMNS) == SPEC_COLUMNS
    scenario = sc.build(db_session)
    path = tmp_path / "out" / "pilot.csv"
    rows = export_csv(db_session, path, [], scenario.default, sc.AS_OF)
    assert rows == 0
    assert path.read_text() == ",".join(SPEC_COLUMNS) + "\n"


def test_rows_carry_receipts(db_session: Session, tmp_path: Path) -> None:
    scenario = sc.build(db_session)
    ids = [scenario.companies[d] for d in (sc.ROBOTICS, sc.GREENHOUSE, sc.QUILLSTONE, sc.BLUEFIN)]
    path = tmp_path / "list.csv"
    assert export_csv(db_session, path, [*ids, 999_999], scenario.default, sc.AS_OF) == 4
    header, rows = _read(path)
    assert header == SPEC_COLUMNS
    assert [r["domain"] for r in rows] == [sc.ROBOTICS, sc.BLUEFIN, sc.QUILLSTONE, sc.GREENHOUSE]
    robotics = rows[0]
    assert robotics["score"] == "27.7"
    assert robotics["heat"] == "100"
    assert robotics["top_signal_1"] == "funding: Form D: $18,000,000 offering, $12,500,000 sold"
    assert robotics["top_signal_1_date"] == "2026-10-01"
    assert robotics["top_signal_1_url"].startswith("https://www.sec.gov/")
    assert robotics["top_signal_2"] == robotics["top_signal_2_url"] == ""
    assert robotics["funding_last_180d_usd"] == "18000000"
    assert robotics["funding_url"] == robotics["top_signal_1_url"]
    assert (robotics["ats"], robotics["coverage_note"]) == ("no_board", "no_board")
    assert robotics["account_page"] == (
        f"https://karank2512.github.io/tell-data/companies/{sc.ROBOTICS}.html"
    )
    assert robotics["rate_this_row"] == ""
    bluefin = rows[1]
    assert bluefin["funding_last_180d_usd"] == ""  # amount undisclosed
    assert bluefin["funding_url"].startswith("https://www.sec.gov/")
    board = rows[3]
    assert board["top_signal_1"].startswith("hiring_velocity (sales): 3 new sales roles")
    # The spike (2026-09-30 to 10-02) is inside the 7 days to the end of 10-06; the board's
    # first-poll postings are baselines and never count.
    assert (board["new_roles_7d_sales"], board["new_roles_7d_eng"]) == ("3", "0")
    assert board["new_roles_7d_other"] == "0"
    assert (board["open_postings"], board["long_open_roles"]) == ("5", "2")
    assert board["ats"] == board["coverage_note"] == "greenhouse:tell-fixture"
    news = rows[2]
    assert news["top_signal_2_url"].startswith("https://news.google.com/")


def test_rate_this_row_uses_the_feedback_form(
    db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TELL_FEEDBACK_FORM_URL", "https://forms.example/f?entry.1={signal_id}")
    get_settings.cache_clear()
    scenario = sc.build(db_session)
    path = tmp_path / "list.csv"
    export_csv(db_session, path, [scenario.companies[sc.ROBOTICS]], scenario.default, sc.AS_OF)
    _, rows = _read(path)
    assert rows[0]["rate_this_row"].startswith("https://forms.example/f?entry.1=")
    assert rows[0]["rate_this_row"] != "https://forms.example/f?entry.1="


def test_rate_this_row_is_empty_for_a_row_without_a_top_signal(
    db_session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The feedback form requires a Signal id and `tell intake feedback-sync` rejects an empty
    # one, so a company with nothing to rate (OLDCO's only signal is 200 days old: score 0.0,
    # no contribution) gets an empty cell, not a form link with an empty Signal id. A row
    # with a top signal keeps its link, filled in with that signal's id.
    monkeypatch.setenv(
        "TELL_FEEDBACK_FORM_URL", "https://forms.example/f?s={signal_id}&d={domain}&v={verdict}"
    )
    get_settings.cache_clear()
    scenario = sc.build(db_session)
    path = tmp_path / "list.csv"
    ids = [scenario.companies[sc.ROBOTICS], scenario.companies[sc.OLDCO]]
    assert export_csv(db_session, path, ids, scenario.default, sc.AS_OF) == 2
    _, rows = _read(path)
    by_domain = {r["domain"]: r for r in rows}
    (signal_id,) = db_session.scalars(
        select(Signal.id).where(Signal.company_id == scenario.companies[sc.ROBOTICS])
    )

    robotics = by_domain[sc.ROBOTICS]
    assert robotics["top_signal_1"] != ""
    assert robotics["rate_this_row"] == f"https://forms.example/f?s={signal_id}&d={sc.ROBOTICS}&v="

    oldco = by_domain[sc.OLDCO]
    assert (oldco["score"], oldco["top_signal_1"], oldco["top_signal_1_url"]) == ("0.0", "", "")
    assert oldco["rate_this_row"] == ""
    # The rest of the row is still there.
    assert oldco["account_page"] == (
        f"https://karank2512.github.io/tell-data/companies/{sc.OLDCO}.html"
    )
