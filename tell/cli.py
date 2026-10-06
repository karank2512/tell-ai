"""``tell`` command-line interface (spec sections 11 and 12; PLAN Phase 9).

Every command is a thin wrapper over library functions: it opens a session with
:func:`tell.db.session_scope`, records a ``runs`` row where the underlying function does
not, prints one JSON object per line on stdout and exits non-zero on failure.

Top-level commands, matching the spec section 11 workflow YAML:

* ``ingest --source S [--board ID] [--domain D] [--limit N] [--force]``: one collector
  run. ATS sources poll their ``active`` and ``dead`` boards (ADR 0015) with
  ``TELL_POLL_CONCURRENCY`` threads under each vendor's per-host rate budget, skipping
  boards polled in the last :data:`MIN_POLL_GAP` unless ``--force``; ``edgar``,
  ``news`` and ``github`` run their collector once. Prints the run line.
* ``resolve`` (no sub-command): ``backfill-aliases`` then ``run``, as one ``resolve``
  run. ``resolve <cmd>`` reaches the sub-app (:mod:`tell.resolve.cli`).
* ``score --all-profiles [--as-of DATE]``: the daily job as one ``score`` run: hiring
  velocity events, then ``signals`` from events, then every profile scored.
  ``score <cmd>`` reaches the sub-app (:mod:`tell.signals.cli`).
* ``publish --out DIR``: JSONL events + snapshots, then the site (account pages, the
  current weekly edition, ``health.html``/``health.json``); ``publish <cmd>`` reaches
  the sub-app (:mod:`tell.publish.cli`).
* ``notify --slack``, ``weekly --week current --out DIR``: aliases of the publish
  sub-app's commands.
* ``runlog --source S`` / ``runlog --week``: the latest run line of a source, or the
  weekly operations line (:mod:`tell.ops.runlog`).
* ``deadman [--source S ...]``: exit 1 when a scheduled source has no successful run in
  24 hours (:mod:`tell.ops.deadman`).
* ``prune --out DIR``: the spec section 5 size budget (:mod:`tell.ops.prune`).
* ``api``: v1 key management (:mod:`tell.api.cli`).
* ``intake``: the intake form's sheet into watchlists (:mod:`tell.intake.cli`).
* ``discover``, ``seed-yc`` (alias of ``discover seed-yc``), ``backtest``, ``version``.

The global flag ``tell --manual <command>`` marks every ``runs`` row the command records
with ``notes.manual = true``, the manual-intervention anchor of "weeks unattended" (SPEC
13.4); ``TELL_RUN_MANUAL=true`` in the environment does the same (``ingest.yml`` exports
it on ``workflow_dispatch`` only).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal, Protocol

import httpx
import typer
from sqlalchemy import select
from sqlalchemy.orm import Session

from tell import __version__
from tell.api import cli as api_cli
from tell.backtest import cli as backtest_cli
from tell.collectors import ashby, edgar, github_org, greenhouse, lever, news, smartrecruiters
from tell.collectors.base import RunResult, finish_run, set_manual_run, start_run, utcnow
from tell.config import get_settings
from tell.db import session_scope
from tell.discovery import cli as discovery_cli
from tell.http import make_client
from tell.intake import cli as intake_cli
from tell.models import AtsBoard, Company, Run
from tell.ops import deadman as deadman_ops
from tell.ops import prune as prune_ops
from tell.ops import runlog as runlog_ops
from tell.publish import cli as publish_cli
from tell.resolve import cli as resolve_cli
from tell.resolve.match import backfill_aliases, resolve_events
from tell.signals import cli as signals_cli
from tell.signals.scoring import build_signals, score_all
from tell.signals.velocity import emit_velocity_events

__all__ = [
    "ATS_INGEST",
    "MIN_POLL_GAP",
    "POLLED_STATUSES",
    "POLL_INTERVAL",
    "RESOLVE_SOURCE",
    "SCORE_RUN_SOURCE",
    "Source",
    "app",
]

_log = logging.getLogger(__name__)

POLL_INTERVAL = timedelta(hours=6)
"""A board is polled at most once per ingest cron slot (``17 2,8,14,20 * * *``)."""

MIN_POLL_GAP = POLL_INTERVAL - timedelta(hours=1)
"""Boards polled more recently than this are skipped without ``--force``: six hours less an
hour of slack, because scheduled runs start late by varying amounts and a strict 6 h gap
would skip every other slot."""

_MIN_POLL_GAP_HOURS = int(MIN_POLL_GAP.total_seconds() // 3600)

POLLED_STATUSES: tuple[str, ...] = ("active", "dead")
"""Board statuses an ATS run polls; a 200 revives a ``dead`` board (ADR 0015)."""

RESOLVE_SOURCE = "resolve"
"""``runs.source`` of ``tell resolve`` (an internal job on the health page)."""

SCORE_RUN_SOURCE = "score"
"""``runs.source`` of ``tell score --all-profiles`` (an internal job on the health page)."""

PRUNE_SOURCE = "prune"
"""``runs.source`` of ``tell prune``."""

_ERROR_CHARS = 300


class Source(StrEnum):
    """The ``--source`` values of ``tell ingest``."""

    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    SMARTRECRUITERS = "smartrecruiters"
    EDGAR = "edgar"
    NEWS = "news"
    GITHUB = "github"


class BoardIngest(Protocol):
    """The shared signature of the four ATS collectors' ``ingest_board``."""

    def __call__(
        self,
        session: Session,
        board: AtsBoard,
        *,
        run_id: str,
        now: datetime,
        client: httpx.Client | None = ...,
    ) -> RunResult: ...


ATS_INGEST: dict[str, BoardIngest] = {
    "greenhouse": greenhouse.ingest_board,
    "lever": lever.ingest_board,
    "ashby": ashby.ingest_board,
    "smartrecruiters": smartrecruiters.ingest_board,
}

app = typer.Typer(
    name="tell",
    help="Open-source buying-signals pipeline.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main(
    manual: Annotated[
        bool,
        typer.Option(
            "--manual",
            help=(
                "Mark this command's runs as a manual intervention (runs.notes.manual = true, "
                "the anchor of 'weeks unattended'); same as TELL_RUN_MANUAL=true."
            ),
        ),
    ] = False,
) -> None:
    """tell: public buying signals with receipts."""
    get_settings()  # loaded (and validated) once; every command reads the cached instance
    # True forces the marker; None lets the TELL_RUN_MANUAL setting decide (and clears a
    # value left by an earlier in-process invocation, as in tests).
    set_manual_run(True if manual else None)


# --- shared helpers ---------------------------------------------------------------------------


def _echo(record: dict[str, Any]) -> None:
    typer.echo(json.dumps(record, sort_keys=True, default=str))


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:_ERROR_CHARS]


def _parse_day(value: str | None, option: str) -> date:
    if value is None:
        return utcnow().date()
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        msg = f"{option} must be an ISO date (YYYY-MM-DD), got {value!r}"
        raise typer.BadParameter(msg) from exc


def _normalize_domain(value: str) -> str:
    domain = value.strip().lower().rstrip(".")
    return domain.removeprefix("www.")


def _finish_line(run: Run, *, with_notes: bool) -> dict[str, Any]:
    line = runlog_ops.run_line(run)
    if with_notes:
        line["notes"] = dict(run.notes or {})
    return line


def _emit_run(line: dict[str, Any]) -> None:
    """Print a run line; exit 1 unless the run finished ``ok``."""
    _echo(line)
    if line.get("status") != "ok":
        raise typer.Exit(code=1)


def _start(source: str, now: datetime) -> str:
    """Insert and commit the ``runs`` row, so a failed run can still be closed."""
    with session_scope() as session:
        return start_run(session, source, now=now)


def _fail(run_id: str, exc: BaseException, *, with_notes: bool = False) -> dict[str, Any]:
    _log.error("run %s failed: %s", run_id, _error_text(exc))
    typer.echo(f"error: {_error_text(exc)}", err=True)
    with session_scope() as session:
        run = finish_run(
            session,
            run_id,
            status="failed",
            fetched=0,
            inserted=0,
            skipped=0,
            errors=1,
            notes={"error": _error_text(exc)},
        )
        return _finish_line(run, with_notes=with_notes)


def _record_failure(source: str, started: datetime, exc: BaseException) -> dict[str, Any]:
    """A run whose own transaction rolled back: record it as ``failed`` in a fresh one."""
    return _fail(_start(source, started), exc)


@dataclass
class _Outcome:
    """What a tracked job reports to ``finish_run``."""

    fetched: int
    inserted: int
    skipped: int
    errors: int = 0
    status: Literal["ok", "failed"] = "ok"
    notes: dict[str, Any] = field(default_factory=dict)


_Work = Callable[[Session, str, datetime], _Outcome]


def _tracked(source: str, work: _Work, *, with_notes: bool) -> dict[str, Any]:
    """Run ``work`` in one transaction between ``start_run`` and ``finish_run``.

    The ``runs`` row is committed first; if ``work`` raises, its transaction rolls back
    and the run is closed ``failed`` with the error in ``notes``.
    """
    now = utcnow()
    run_id = _start(source, now)
    try:
        with session_scope() as session:
            outcome = work(session, run_id, now)
            run = finish_run(
                session,
                run_id,
                status=outcome.status,
                fetched=outcome.fetched,
                inserted=outcome.inserted,
                skipped=outcome.skipped,
                errors=outcome.errors,
                notes=outcome.notes,
            )
            return _finish_line(run, with_notes=with_notes)
    except Exception as exc:
        return _fail(run_id, exc, with_notes=with_notes)


# --- ingest -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class _BoardOutcome:
    identifier: str
    fetched: int = 0
    inserted: int = 0
    skipped: int = 0
    error: str | None = None


@dataclass(frozen=True)
class _Due:
    boards: list[tuple[int, str]]
    not_due: int


def _select_boards(
    session: Session,
    vendor: str,
    *,
    board: str | None,
    domain: str | None,
    limit: int | None,
    force: bool,
    now: datetime,
) -> _Due:
    stmt = select(AtsBoard.id, AtsBoard.identifier, AtsBoard.last_polled_at).where(
        AtsBoard.vendor == vendor, AtsBoard.status.in_(POLLED_STATUSES)
    )
    if board is not None:
        stmt = stmt.where(AtsBoard.identifier == board)
    if domain is not None:
        stmt = stmt.join(Company, Company.id == AtsBoard.company_id).where(
            Company.domain == _normalize_domain(domain)
        )
    stmt = stmt.order_by(AtsBoard.last_polled_at.asc().nulls_first(), AtsBoard.id)
    rows = session.execute(stmt).all()
    if board is not None and not rows:
        msg = f"no pollable {vendor} board {board!r} (statuses {', '.join(POLLED_STATUSES)})"
        raise typer.BadParameter(msg, param_hint="--board")
    cutoff = now - MIN_POLL_GAP
    due = [
        (int(board_id), str(identifier))
        for board_id, identifier, polled in rows
        if force or polled is None or polled <= cutoff
    ]
    not_due = len(rows) - len(due)
    if limit is not None:
        due = due[:limit]
    return _Due(boards=due, not_due=not_due)


def _poll_board(
    vendor: str,
    board_id: int,
    identifier: str,
    *,
    run_id: str,
    now: datetime,
    client: httpx.Client,
) -> _BoardOutcome:
    """Poll one board in its own transaction; any failure becomes this board's error."""
    try:
        with session_scope() as session:
            row = session.get(AtsBoard, board_id)
            if row is None:
                return _BoardOutcome(identifier=identifier, error="board no longer exists")
            result = ATS_INGEST[vendor](session, row, run_id=run_id, now=now, client=client)
    except Exception as exc:
        _log.warning("%s board %s failed: %s", vendor, identifier, _error_text(exc))
        return _BoardOutcome(identifier=identifier, error=_error_text(exc))
    return _BoardOutcome(
        identifier=identifier,
        fetched=result.fetched,
        inserted=result.inserted,
        skipped=result.skipped,
    )


def _ingest_ats(
    vendor: str, *, board: str | None, domain: str | None, limit: int | None, force: bool
) -> dict[str, Any]:
    now = utcnow()
    with session_scope() as session:
        due = _select_boards(
            session, vendor, board=board, domain=domain, limit=limit, force=force, now=now
        )
    run_id = _start(vendor, now)
    workers = max(1, min(get_settings().tell_poll_concurrency, len(due.boards) or 1))
    try:
        with make_client() as client, ThreadPoolExecutor(max_workers=workers) as pool:
            outcomes = list(
                pool.map(
                    lambda item: _poll_board(
                        vendor, item[0], item[1], run_id=run_id, now=now, client=client
                    ),
                    due.boards,
                )
            )
    except Exception as exc:
        return _fail(run_id, exc)
    failed = [o for o in outcomes if o.error is not None]
    status: Literal["ok", "failed"] = (
        "failed" if outcomes and len(failed) == len(outcomes) else "ok"
    )
    notes: dict[str, Any] = {
        "boards_due": len(due.boards),
        "boards_polled": len(outcomes) - len(failed),
        "boards_not_due": due.not_due,
        "force": force,
    }
    if failed:
        notes["failed_boards"] = {o.identifier: o.error for o in failed[:50]}
    with session_scope() as session:
        run = finish_run(
            session,
            run_id,
            status=status,
            fetched=sum(o.fetched for o in outcomes),
            inserted=sum(o.inserted for o in outcomes),
            skipped=sum(o.skipped for o in outcomes),
            errors=len(failed),
            notes=notes,
        )
        return runlog_ops.run_line(run)


def _edgar_work(session: Session, run_id: str, now: datetime) -> _Outcome:
    result = edgar.collect(session, run_id=run_id, now=now)
    return _Outcome(
        fetched=result.fetched,
        inserted=result.inserted,
        skipped=result.skipped,
        errors=result.errors,
        notes=result.notes,
    )


def _company_ids(session: Session, domain: str) -> list[int]:
    ids = list(
        session.scalars(select(Company.id).where(Company.domain == _normalize_domain(domain)))
    )
    if not ids:
        msg = f"no company with domain {domain!r}"
        raise typer.BadParameter(msg, param_hint="--domain")
    return [int(i) for i in ids]


def _ingest_news(domain: str | None, limit: int | None) -> dict[str, Any]:
    now = utcnow()
    try:
        with session_scope() as session:
            ids = _company_ids(session, domain) if domain is not None else None
            run = news.ingest(
                session,
                now=now,
                company_ids=ids,
                max_companies=limit if limit is not None else news.MAX_COMPANIES,
            )
            return runlog_ops.run_line(run)
    except typer.BadParameter:
        raise
    except Exception as exc:
        return _record_failure(news.SOURCE, now, exc)


def _ingest_github() -> dict[str, Any]:
    now = utcnow()
    secret = get_settings().github_token
    token = secret.get_secret_value() if secret is not None else None
    try:
        with session_scope() as session:
            return runlog_ops.run_line(github_org.ingest(session, now=now, token=token))
    except Exception as exc:
        return _record_failure(github_org.SOURCE, now, exc)


@app.command()
def ingest(
    source: Annotated[Source, typer.Option("--source", help="The collector to run.")],
    board: Annotated[
        str | None,
        typer.Option("--board", help="Poll only this board identifier (ATS sources)."),
    ] = None,
    domain: Annotated[
        str | None,
        typer.Option("--domain", help="Only this company's boards (ATS) or news (news)."),
    ] = None,
    limit: Annotated[
        int | None,
        typer.Option("--limit", min=1, help="Max boards (ATS) or companies (news) this run."),
    ] = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help=(
                f"Also poll boards polled within the last {_MIN_POLL_GAP_HOURS} hours "
                "(one cron slot less an hour of slack; see MIN_POLL_GAP)."
            ),
        ),
    ] = False,
) -> None:
    """Run one collector and print its JSON run line (exit 1 when the run failed)."""
    name = source.value
    if name in ATS_INGEST:
        line = _ingest_ats(name, board=board, domain=domain, limit=limit, force=force)
    else:
        if board is not None:
            raise typer.BadParameter("--board applies to ATS sources only", param_hint="--board")
        if source is not Source.NEWS and (domain is not None or limit is not None):
            msg = "--domain and --limit apply to ATS sources and news only"
            raise typer.BadParameter(msg)
        if source is Source.EDGAR:
            line = _tracked(Source.EDGAR.value, _edgar_work, with_notes=False)
        elif source is Source.NEWS:
            line = _ingest_news(domain, limit)
        else:
            line = _ingest_github()
    _emit_run(line)


# --- resolve / score / publish / discover / backtest sub-apps --------------------------------


def _resolve_work(session: Session, _run_id: str, now: datetime) -> _Outcome:
    aliases = backfill_aliases(session)
    stats = resolve_events(session, now=now)
    return _Outcome(
        fetched=stats.scanned,
        inserted=stats.auto_linked,
        skipped=stats.scanned - stats.auto_linked,
        notes={"aliases_backfilled": aliases, **stats.to_dict()},
    )


def resolve_main(ctx: typer.Context) -> None:
    """Link events to companies. With no command: backfill-aliases, then run (one run row)."""
    if ctx.invoked_subcommand is not None:
        return
    _emit_run(_tracked(RESOLVE_SOURCE, _resolve_work, with_notes=True))


def _score_work(day: date) -> _Work:
    def work(session: Session, run_id: str, now: datetime) -> _Outcome:
        velocity = emit_velocity_events(session, now=now, run_id=run_id)
        signals = build_signals(session, now=now)
        stats = score_all(session, as_of=day, now=now, run_id=run_id)
        companies = sum(s["companies"] for s in stats.values())
        crossings = sum(s["crossings"] for s in stats.values())
        return _Outcome(
            fetched=companies,
            inserted=len(velocity) + signals + crossings,
            skipped=0,
            notes={
                "as_of": day.isoformat(),
                "velocity_events": len(velocity),
                "signals_inserted": signals,
                "crossings": crossings,
                "profiles": stats,
            },
        )

    return work


def score_main(
    ctx: typer.Context,
    all_profiles: Annotated[
        bool,
        typer.Option(
            "--all-profiles",
            help="The daily job: velocity events, signals, then every profile scored.",
        ),
    ] = False,
    as_of: Annotated[
        str | None, typer.Option("--as-of", help="Calendar day to score (ISO date; default today).")
    ] = None,
) -> None:
    """Score companies. --all-profiles runs the daily job as one 'score' run."""
    if ctx.invoked_subcommand is not None:
        return
    if not all_profiles:
        typer.echo(ctx.get_help())
        raise typer.Exit(code=0)
    day = _parse_day(as_of, "--as-of")
    _emit_run(_tracked(SCORE_RUN_SOURCE, _score_work(day), with_notes=True))


app.add_typer(
    resolve_cli.app,
    name="resolve",
    callback=resolve_main,
    invoke_without_command=True,
    no_args_is_help=False,
)
app.add_typer(
    signals_cli.app,
    name="score",
    callback=score_main,
    invoke_without_command=True,
    no_args_is_help=False,
)
app.add_typer(publish_cli.app, name="publish")
app.add_typer(discovery_cli.app, name="discover")
app.add_typer(backtest_cli.app, name="backtest")
app.add_typer(api_cli.app, name="api")
app.add_typer(intake_cli.app, name="intake")

# Aliases matching the spec section 11 workflow lines verbatim.
app.command("notify")(publish_cli.notify_command)
app.command("weekly")(publish_cli.weekly_command)
app.command("seed-yc")(discovery_cli.seed_yc_command)


# --- run health ---------------------------------------------------------------------------------


@app.command()
def runlog(
    source: Annotated[
        str | None, typer.Option("--source", help="Print this source's latest run line.")
    ] = None,
    week: Annotated[bool, typer.Option("--week", help="Print the weekly operations line.")] = False,
) -> None:
    """Print a source's latest run line, or the weekly operations line."""
    if (source is None) == (not week):
        raise typer.BadParameter("pass exactly one of --source SOURCE or --week")
    now = utcnow()
    with session_scope() as session:
        if source is None:
            _echo(runlog_ops.ops_line(session, now).to_dict())
            return
        run = runlog_ops.latest_run(session, source)
        if run is None:
            typer.echo(f"error: no runs recorded for source {source!r}", err=True)
            raise typer.Exit(code=1)
        _echo(runlog_ops.run_line(run))


@app.command()
def deadman(
    source: Annotated[
        list[str] | None,
        typer.Option(
            "--source", help="Source to check (repeatable; default: the scheduled collectors)."
        ),
    ] = None,
) -> None:
    """Exit 1 when a source has had no successful run in 24 hours (or never ran)."""
    sources = tuple(source) if source else deadman_ops.SCHEDULED_SOURCES
    with session_scope() as session:
        report = deadman_ops.check(session, utcnow(), sources)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        deadman_ops.write_step_summary(report, Path(summary))
    _echo(report.to_dict())
    if not report.ok:
        typer.echo(f"dead-man: silent sources: {', '.join(report.silent)}", err=True)
        raise typer.Exit(code=1)


@app.command()
def prune(
    out: Annotated[
        Path,
        typer.Option("--out", file_okay=False, help="The tell-data checkout (events archive)."),
    ],
) -> None:
    """Archive events older than 180 days to OUT/events, then delete them; drop old observations."""

    def work(session: Session, _run_id: str, now: datetime) -> _Outcome:
        result = prune_ops.prune(session, out, now=now)
        return _Outcome(
            fetched=result.events_archived,
            inserted=0,
            skipped=result.events_kept,
            notes=result.to_dict(),
        )

    _emit_run(_tracked(PRUNE_SOURCE, work, with_notes=True))


@app.command()
def version() -> None:
    """Print the installed tell version."""
    typer.echo(f"tell {__version__}")


if __name__ == "__main__":
    app()
