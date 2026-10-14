"""Re-check active slug-guessed boards under the ownership-evidence rule (ADR 0031).

``tell discover reverify-guesses [--limit N]`` runs :func:`reverify_guesses`: every
``active`` board with the guess confidence (0.60) on a supported vendor is checked with
:func:`tell.discovery.ats.ownership_evidence` (no slug probe; 1 to 3 requests per board,
see ``docs/methods/discovery.md``) and the name rule:

* **kept**: the board points at the company's registrable domain and its name still
  matches (or a person re-activated it after a ``guess_without_domain_evidence`` item);
  ``last_verified_at`` is stamped and open discovery review items about it are resolved.
* **paused**: name-only match (``guess_without_domain_evidence``) or a name mismatch:
  ``status='paused'`` (never polled), ``last_verified_at`` stamped, one open
  ``board_verify`` item (reused on reruns).
* **error**: a request failed and nothing pointed at the domain, so the check is
  inconclusive; the board is left untouched for the next run.

Idempotent: paused boards are no longer selected, kept boards only get a newer stamp.
Least recently verified boards go first. Every board's result is logged as one JSON line
on the ``tell.discovery`` logger.
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from tell.config import get_settings
from tell.discovery import domain_label
from tell.discovery.ats import (
    GUESS_CONFIDENCE,
    SUPPORTED_VENDORS,
    BoardProfile,
    _human_confirmed,
    _queue_review,
    _require_aware,
    _resolve_reviews,
    guess_rejection,
    name_ratio,
    ownership_evidence,
)
from tell.http import RateBudget, Sleeper
from tell.models import AtsBoard, Company

__all__ = [
    "GuessCheck",
    "ReverifyResult",
    "reverify_guesses",
    "select_guessed_boards",
]

CheckOutcome = Literal["kept", "paused", "error"]

_OUTCOMES: tuple[CheckOutcome, ...] = ("kept", "paused", "error")

_logger = logging.getLogger("tell.discovery")


@dataclass(frozen=True)
class GuessCheck:
    """What :func:`reverify_guesses` decided for one board."""

    board_id: int
    domain: str
    vendor: str
    identifier: str
    outcome: CheckOutcome
    reason: str | None = None
    evidence: str | None = None
    """The source that pointed at the domain (``ashby:hosted_page``, ...), if any."""
    board_name: str | None = None
    ratio: float | None = None
    review_id: int | None = None

    def log_record(self) -> dict[str, Any]:
        return {
            "check": "reverify_guess",
            "board_id": self.board_id,
            "domain": self.domain,
            "vendor": self.vendor,
            "identifier": self.identifier,
            "outcome": self.outcome,
            "reason": self.reason,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class ReverifyResult:
    """What one :func:`reverify_guesses` run did."""

    checks: list[GuessCheck] = field(default_factory=list)

    @property
    def outcomes(self) -> dict[str, int]:
        counts = Counter(check.outcome for check in self.checks)
        return {name: counts.get(name, 0) for name in _OUTCOMES}


def select_guessed_boards(session: Session, limit: int | None = None) -> list[AtsBoard]:
    """Active guessed boards (0.60, supported vendor, owned), least recently verified first."""
    stmt = (
        select(AtsBoard)
        .where(
            AtsBoard.status == "active",
            AtsBoard.confidence == GUESS_CONFIDENCE,
            AtsBoard.vendor.in_(SUPPORTED_VENDORS),
            AtsBoard.company_id.is_not(None),
        )
        .order_by(AtsBoard.last_verified_at.asc().nulls_first(), AtsBoard.id)
    )
    if limit is not None:
        stmt = stmt.limit(max(limit, 0))
    return list(session.scalars(stmt))


@dataclass(frozen=True)
class _Target:
    board_id: int
    company_id: int
    domain: str
    vendor: str
    identifier: str


def _targets(session: Session, limit: int | None) -> list[_Target]:
    targets: list[_Target] = []
    for board in select_guessed_boards(session, limit):
        company = session.get(Company, board.company_id)
        if company is not None:
            targets.append(
                _Target(board.id, company.id, company.domain, board.vendor, board.identifier)
            )
    return targets


def _check(
    session: Session,
    board: AtsBoard,
    company: Company,
    profile: BoardProfile,
    *,
    now: datetime,
) -> GuessCheck:
    """Record one board's check (database only; the requests were made before)."""
    evidence = profile.evidence
    ratio = name_ratio(profile.name, [domain_label(company.domain), company.name])
    reason = guess_rejection(ratio, evidence)

    def check(
        outcome: CheckOutcome, why: str | None = None, review_id: int | None = None
    ) -> GuessCheck:
        return GuessCheck(
            board_id=board.id,
            domain=company.domain,
            vendor=board.vendor,
            identifier=board.identifier,
            outcome=outcome,
            reason=why,
            evidence=evidence.matched,
            board_name=profile.name,
            ratio=ratio,
            review_id=review_id,
        )

    if reason is None or (reason != "name_mismatch" and _human_confirmed(session, board, company)):
        board.last_verified_at = now
        if reason is None:
            _resolve_reviews(session, company.domain, board.vendor, board.identifier, now)
        session.flush()
        return check("kept")
    if not evidence.conclusive or (profile.name is None and evidence.failed):
        # A failed request may have held the evidence or the name: leave the board as is.
        return check("error", "inconclusive")
    payload: dict[str, Any] = {
        "domain": company.domain,
        "vendor": board.vendor,
        "identifier": board.identifier,
        "board_name": profile.name,
        "ratio": ratio,
        "discovered_from": board.discovered_from,
        "reason": reason,
        **evidence.payload(),
    }
    item = _queue_review(session, payload, now)
    board.status = "paused"
    board.last_verified_at = now
    session.flush()
    return check("paused", reason, item.id)


def reverify_guesses(
    session: Session,
    client: httpx.Client,
    *,
    now: datetime,
    limit: int | None = None,
    rps: float | None = None,
    budget: RateBudget | None = None,
    sleep: Sleeper = time.sleep,
    commit: Callable[[Session], None] | None = None,
) -> ReverifyResult:
    """Re-check up to ``limit`` active guessed boards (all when ``None``); see the module doc.

    Requests draw from one :class:`RateBudget` at ``rps`` (default ``TELL_DISCOVERY_RPS``).
    Each board is recorded in its own savepoint and ``commit`` (the CLI passes
    ``Session.commit``) runs after each one; ``None`` only flushes.
    """
    _require_aware(now)
    rate = get_settings().tell_discovery_rps if rps is None else rps
    bucket = budget or RateBudget(rate, sleep=sleep)
    targets = _targets(session, limit)
    if commit is not None:
        commit(session)  # no transaction stays open while the vendor requests run
    checks: list[GuessCheck] = []
    for target in targets:
        profile = ownership_evidence(
            client, target.vendor, target.identifier, target.domain, budget=bucket, sleep=sleep
        )
        with session.begin_nested():
            board = session.get(AtsBoard, target.board_id)
            company = session.get(Company, target.company_id)
            if board is None or company is None or board.status != "active":
                continue  # changed by someone else meanwhile
            result = _check(session, board, company, profile, now=now)
        _logger.info(json.dumps(result.log_record(), sort_keys=True))
        checks.append(result)
        session.flush()
        if commit is not None:
            commit(session)
    return ReverifyResult(checks)
