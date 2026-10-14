"""Resolution test family (spec section 12): precision and recall over the gold set.

The gold CSV (``domain, name, legal_name, cik, hq_city, hq_state, hq_country,
board_vendor, board_identifier, verification_source, notes``) seeds ``companies`` and, for
every row with a supported board, an ``ats_boards`` row linked to the company the way
discovery links it. Then:

* every legal name is fed through :func:`match_name` as a Form D issuer (with the row's
  state and city) and has to come back through the name rules R2-R4. This is the
  **headline metric** (spec: "runs the matcher over the gold set's names"), reported
  and gated on its own as ``names``;
* every board is fed the way a job event arrives: carrying its board's ``company_id``
  (collectors copy ``board.company_id`` onto the event), so it has to come back through R1.
  These cases are tautological (the expected id is the input) and are reported
  separately as ``boards``; they verify the production path, not the matcher. The board
  identifier is never matched as free text; in production it never is, and the gold set
  deliberately holds near-duplicate pairs (two companies sharing one word) where a
  free-text slug would link to the wrong one.

Precision is correct links over asserted auto-links, recall is links found over links that
exist. Negatives, for 30% of the rows: another gold company's legal name is fed with *this*
row's geography and must not be linked to this row (asserted); and two token *supersets*
of this row's name (``<name> Press LLC`` and ``Blue <name> Bakery``, with this row's
geography) are fed and must not be auto-linked to anything. ``token_set_ratio`` scores a
superset 100, so the superset negatives measure the risk the guard in ``match_name``
exists for; a superset that still auto-links counts as a wrong link in the precision.

Since ADR 0034 every name is fed with Form D issuer facts (:func:`_issuer`), so the Form D
rules apply: R3 is review-only and a fund-like name is R6. The gold CSV has no entity type
or jurisdiction column, so each issuer is a Delaware corporation of unknown age (guards
(b)-(d) pass; they are unit-tested on production's own links in
``tests/resolve/test_guards.py``), and the gold companies carry no YC status or batch.

Files: ``tests/fixtures/gold_sample.csv`` (public 50-row sample) always; the full private
set when ``TELL_GOLD_SET`` points at it. The variable is read at import time because the
hermetic settings fixture strips ``TELL_*`` from the environment inside each test.
Gate: ``names`` precision >= 0.97 and recall >= 0.85, and no wrong auto-link at all.
"""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tell.models import AtsBoard, Company
from tell.resolve.guards import IssuerFacts
from tell.resolve.match import (
    SUPPORTED_BOARD_VENDORS,
    AliasIndex,
    MatchDecision,
    backfill_aliases,
    match_name,
)
from tell.resolve.normalize import normalize_name, slug_words

REPO_ROOT = Path(__file__).resolve().parents[1]
GOLD_SAMPLE = REPO_ROOT / "tests" / "fixtures" / "gold_sample.csv"
GOLD_SAMPLE_ROWS = 50
GOLD_FULL = os.environ.get("TELL_GOLD_SET")

PRECISION_FLOOR = 0.97
RECALL_FLOOR = 0.85
NEGATIVE_SHARE = 0.30

GOLD_COLUMNS = {
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
}


def _gold_files() -> list[Path]:
    files = [GOLD_SAMPLE] if GOLD_SAMPLE.is_file() else []
    if GOLD_FULL and Path(GOLD_FULL).is_file() and Path(GOLD_FULL).resolve() != GOLD_SAMPLE:
        files.append(Path(GOLD_FULL))
    return files


GOLD_FILES = _gold_files()


@dataclass
class GoldRow:
    domain: str
    name: str
    legal_name: str
    hq_city: str | None
    hq_state: str | None
    board_vendor: str | None
    board_identifier: str | None
    group: str
    company_id: int = 0

    @property
    def has_board(self) -> bool:
        return self.board_vendor in SUPPORTED_BOARD_VENDORS and bool(self.board_identifier)


@dataclass
class Tally:
    exists: int = 0
    found: int = 0
    asserted: int = 0
    correct: int = 0
    negatives: int = 0
    supersets: int = 0
    by_rule: dict[str, int] = field(default_factory=dict)
    by_group: dict[str, list[int]] = field(default_factory=dict)
    """``group -> [found, exists]`` for the positive cases."""
    wrong: list[str] = field(default_factory=list)
    missed: list[str] = field(default_factory=list)

    def record(self, decision: MatchDecision, expected: int | None, *, label: str) -> None:
        self.by_rule[decision.rule] = self.by_rule.get(decision.rule, 0) + 1
        if decision.action != "auto_link":
            return
        self.asserted += 1
        if expected is not None and decision.company_id == expected:
            self.correct += 1
        else:
            self.wrong.append(f"{label} -> company {decision.company_id} via {decision.rule}")

    def positive(self, decision: MatchDecision, expected: int, *, label: str, group: str) -> None:
        self.exists += 1
        counts = self.by_group.setdefault(group, [0, 0])
        counts[1] += 1
        before = self.correct
        self.record(decision, expected, label=label)
        if self.correct > before:
            self.found += 1
            counts[0] += 1
        elif decision.action != "auto_link":
            self.missed.append(f"{label} -> {decision.rule} ratio={decision.ratio}")

    @property
    def precision(self) -> float:
        return self.correct / self.asserted if self.asserted else 0.0

    @property
    def recall(self) -> float:
        return self.found / self.exists if self.exists else 0.0

    def group_recall(self) -> dict[str, str]:
        return {
            group: f"{found}/{exists}" for group, (found, exists) in sorted(self.by_group.items())
        }


def load_gold(path: Path) -> list[GoldRow]:
    rows: list[GoldRow] = []
    seen: set[str] = set()
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        assert reader.fieldnames is not None
        missing = GOLD_COLUMNS - set(reader.fieldnames)
        assert not missing, f"gold set {path} lacks columns {sorted(missing)}"
        for raw in reader:
            domain = (raw.get("domain") or "").strip().lower()
            if not domain or domain in seen:
                continue
            seen.add(domain)
            name = (raw.get("name") or "").strip()
            legal = (raw.get("legal_name") or "").strip() or name
            notes = (raw.get("notes") or "").strip()
            group = notes.split(";", 1)[0].removeprefix("set=").strip() or "unlabelled"
            rows.append(
                GoldRow(
                    domain=domain,
                    name=name or legal or domain,
                    legal_name=legal,
                    hq_city=(raw.get("hq_city") or "").strip() or None,
                    hq_state=(raw.get("hq_state") or "").strip() or None,
                    board_vendor=(raw.get("board_vendor") or "").strip().lower() or None,
                    board_identifier=(raw.get("board_identifier") or "").strip() or None,
                    group=group,
                )
            )
    return rows


def seed(session: Session, rows: list[GoldRow]) -> None:
    """Companies from the gold rows, their boards linked as discovery links them, aliases."""
    for row in rows:
        company = Company(
            domain=row.domain, name=row.name, hq_city=row.hq_city, hq_state=row.hq_state
        )
        session.add(company)
        session.flush()
        row.company_id = company.id
        if row.has_board:
            assert row.board_vendor is not None
            assert row.board_identifier is not None
            session.add(
                AtsBoard(
                    company_id=company.id,
                    vendor=row.board_vendor,
                    identifier=row.board_identifier,
                    discovered_from=row.domain,
                    confidence=Decimal("1.00"),
                    status="active",
                )
            )
    session.flush()
    backfill_aliases(session)


def _board_company_id(session: Session, row: GoldRow) -> int | None:
    """What a job event carries: the ``company_id`` of the board it was polled from."""
    board = session.scalar(
        select(AtsBoard).where(
            AtsBoard.vendor == row.board_vendor, AtsBoard.identifier == row.board_identifier
        )
    )
    return board.company_id if board is not None else None


@dataclass
class GoldResult:
    """``names`` is the headline metric (R2-R6 over legal names); ``boards`` the R1 check."""

    names: Tally
    boards: Tally


def _issuer(name: str) -> IssuerFacts:
    """A Form D issuer named ``name``: a Delaware corporation, year of incorporation unknown."""
    return IssuerFacts(name=name, entity_type="Corporation", jurisdiction="DELAWARE")


def run_gold(session: Session, rows: list[GoldRow]) -> GoldResult:
    index = AliasIndex.from_session(session)
    names = Tally()
    boards = Tally()
    for row in rows:
        decision = match_name(
            session,
            row.legal_name,
            state=row.hq_state,
            city=row.hq_city,
            index=index,
            issuer=_issuer(row.legal_name),
        )
        names.positive(
            decision,
            row.company_id,
            label=f"formd:{row.domain}:{row.legal_name!r}",
            group=row.group,
        )
        if row.has_board:
            assert row.board_identifier is not None
            words = slug_words(row.board_identifier)
            decision = match_name(
                session, words, on_domain_company_id=_board_company_id(session, row), index=index
            )
            boards.positive(
                decision, row.company_id, label=f"ats:{row.domain}:{words!r}", group=row.group
            )

    n = len(rows)
    for i, row in enumerate(rows):
        if (i % 10) / 10 >= NEGATIVE_SHARE:
            continue
        other = rows[(i + 7) % n]
        if other is not row and normalize_name(other.legal_name) != normalize_name(row.legal_name):
            names.negatives += 1
            decision = match_name(
                session,
                other.legal_name,
                state=row.hq_state,
                city=row.hq_city,
                index=index,
                issuer=_issuer(other.legal_name),
            )
            names.record(
                decision, other.company_id, label=f"negative:{row.domain}:{other.legal_name!r}"
            )
            assert decision.company_id != row.company_id, (
                f"{other.legal_name!r} with {row.domain}'s geography linked to {row.domain}"
            )
        base = normalize_name(row.name) or normalize_name(row.legal_name)
        for superset in (f"{base.title()} Press LLC", f"Blue {base.title()} Bakery"):
            if normalize_name(superset) == normalize_name(row.legal_name):
                continue
            names.negatives += 1
            names.supersets += 1
            decision = match_name(
                session,
                superset,
                state=row.hq_state,
                city=row.hq_city,
                index=index,
                issuer=_issuer(superset),
            )
            names.record(decision, None, label=f"superset:{row.domain}:{superset!r}")
    return GoldResult(names=names, boards=boards)


def _report(label: str, tally: Tally, *, n: int, file: str) -> None:
    print(
        f"{label}: precision={tally.precision:.4f} recall={tally.recall:.4f} n={n} "
        f"cases={tally.exists} negatives={tally.negatives} supersets={tally.supersets} "
        f"by_rule={dict(sorted(tally.by_rule.items()))} by_group={tally.group_recall()} "
        f"file={file}"
    )


@pytest.mark.skipif(
    not GOLD_FILES,
    reason="no gold set: tests/fixtures/gold_sample.csv missing and TELL_GOLD_SET unset",
)
@pytest.mark.parametrize(
    "gold_path", GOLD_FILES, ids=[p.name if p == GOLD_SAMPLE else "full" for p in GOLD_FILES]
)
def test_gold_set_precision_and_recall(db_session: Session, gold_path: Path) -> None:
    rows = load_gold(gold_path)
    assert rows, f"{gold_path} has no rows"
    seed(db_session, rows)
    result = run_gold(db_session, rows)
    names, boards = result.names, result.boards
    _report("names", names, n=len(rows), file=gold_path.name)
    _report("boards", boards, n=len(rows), file=gold_path.name)
    for miss in names.missed:
        print(f"missed: {miss}")

    # The headline metric: legal names through the name rules R2-R6 only.
    assert names.wrong == [], names.wrong
    assert names.precision >= PRECISION_FLOOR, (
        f"names precision {names.precision:.4f} < {PRECISION_FLOOR}"
    )
    assert names.recall >= RECALL_FLOOR, f"names recall {names.recall:.4f} < {RECALL_FLOOR}"
    assert names.by_rule.get("R2", 0) > 0
    assert "R1" not in names.by_rule

    # The production path for boards: every one links by domain (R1) to its own company.
    expected_boards = sum(1 for row in rows if row.has_board)
    assert boards.exists == expected_boards
    assert boards.by_rule.get("R1", 0) == expected_boards
    assert boards.wrong == []
    if expected_boards:
        assert (boards.precision, boards.recall) == (1.0, 1.0)


def test_gold_sample_is_companies_only() -> None:
    """The public sample holds 50 company rows and nothing that looks personal."""
    if not GOLD_SAMPLE.is_file():
        pytest.skip("tests/fixtures/gold_sample.csv not present yet")
    text = GOLD_SAMPLE.read_text(encoding="utf-8")
    assert "@" not in text
    header = text.splitlines()[0]
    assert set(header.split(",")) >= GOLD_COLUMNS
    rows = load_gold(GOLD_SAMPLE)
    assert len(rows) == GOLD_SAMPLE_ROWS
    assert all(row.board_vendor in SUPPORTED_BOARD_VENDORS for row in rows if row.board_vendor)
