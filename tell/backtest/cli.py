"""``tell backtest``: the spec section 13 reports (mounted into ``tell/cli.py``).

Commands (each prints one JSON line per report written and exits non-zero on failure):

* ``speed-vs-press``, ``velocity-lead``, ``noise``, ``ops`` ``[--as-of DATE] [--out DIR]
  [--refresh]``: write ``DIR/<name>-<as-of>.md`` and, unless a newer dated report is
  already there, ``DIR/<name>-latest.md`` (``DIR`` defaults to the checkout's
  ``docs/methods``; ``weekly.yml`` passes ``tell-data/site/methods``), and refresh the
  index in ``DIR/README.md`` (created when missing).
* ``dogfood [--as-of DATE] [--out DIR]``: the private 13.5 report, written only to
  ``tell-private/reports/dogfood-<as-of>.md``; a directory inside the public checkout is
  refused.
* ``refresh [--as-of DATE] [--out DIR] [--refresh]``: every public report (not
  ``dogfood``); ``tell backtest --refresh`` with no command does the same, the spelling
  PLAN Phase 10 uses for the weekly workflow.
* ``list``: the five report names.

``--refresh`` fetches external inputs before computing: for ``velocity-lead`` that is the
Wayback CDX listings and snapshots missing from ``tell-private/cache/wayback`` (1 request
per second, at most ``--wayback-budget`` requests). Without it only the database and the
cache are read; the other reports read only the database and accept the flag for
uniformity.

Reports only read the database: no ``runs`` row is written, so a report never shows up in
the success rate it reports.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from sqlalchemy.orm import Session

from tell.backtest import dogfood, noise, ops, speed_vs_press, velocity_lead
from tell.backtest.common import METHODS_DIR, Report, resolve_now, write_report
from tell.backtest.wayback import DEFAULT_CACHE_DIR, WaybackClient
from tell.db import session_scope

__all__ = ["PUBLIC_REPORTS", "REPORTS", "app", "run_public"]

REPORTS: tuple[str, ...] = ("speed-vs-press", "velocity-lead", "noise", "ops", "dogfood")
"""The section 13 studies, in spec order (13.1 to 13.5)."""

PUBLIC_REPORTS: tuple[str, ...] = REPORTS[:-1]
"""Everything ``refresh`` writes; ``dogfood`` is private."""

DEFAULT_WAYBACK_BUDGET = 300

app = typer.Typer(
    name="backtest",
    help="Backtest and methods reports (spec section 13).",
    no_args_is_help=True,
    add_completion=False,
)

AsOfOption = Annotated[
    str | None,
    typer.Option("--as-of", help="Report date (ISO date; default today, UTC)."),
]
OutOption = Annotated[
    Path,
    typer.Option("--out", help="Directory for the dated report and its -latest copy."),
]
RefreshOption = Annotated[
    bool,
    typer.Option("--refresh", help="Fetch external inputs (Wayback) missing from the cache."),
]
FormdOption = Annotated[
    list[Path] | None,
    typer.Option(
        "--formd",
        help="Quarterly Form D data set (SEC ZIP or unpacked directory); repeatable.",
    ),
]
CacheOption = Annotated[Path, typer.Option("--wayback-cache", help="Wayback JSON cache directory.")]
BudgetOption = Annotated[
    int,
    typer.Option("--wayback-budget", min=0, help="Max Wayback requests with --refresh."),
]


def _echo(record: dict[str, Any]) -> None:
    typer.echo(json.dumps(record, sort_keys=True, default=str))


def _now(as_of: str | None) -> datetime:
    if as_of is None:
        return resolve_now(None)
    try:
        day = date.fromisoformat(as_of)
    except ValueError as exc:
        msg = f"--as-of must be an ISO date (YYYY-MM-DD), got {as_of!r}"
        raise typer.BadParameter(msg) from exc
    return resolve_now(day)


def _write(builder: Callable[[Session, datetime], Report], now: datetime, out: Path) -> None:
    with session_scope() as session:
        report = builder(session, now)
    _echo(write_report(report, out).to_dict())


def _velocity_builder(
    formd: list[Path] | None, refresh: bool, cache: Path, budget: int
) -> Callable[[Session, datetime], Report]:
    def build(session: Session, now: datetime) -> Report:
        with WaybackClient(cache, network=refresh, max_requests=budget) as client:
            return velocity_lead.build_report(
                session, now, formd_paths=list(formd or []), wayback=client
            )

    return build


def run_public(
    now: datetime,
    out: Path,
    *,
    refresh: bool = False,
    formd: list[Path] | None = None,
    cache: Path = DEFAULT_CACHE_DIR,
    budget: int = DEFAULT_WAYBACK_BUDGET,
) -> None:
    """Write every public report at ``now`` into ``out``."""
    builders: dict[str, Callable[[Session, datetime], Report]] = {
        "speed-vs-press": speed_vs_press.build_report,
        "velocity-lead": _velocity_builder(formd, refresh, cache, budget),
        "noise": noise.build_report,
        "ops": ops.build_report,
    }
    for name in PUBLIC_REPORTS:
        _write(builders[name], now, out)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    refresh: Annotated[
        bool,
        typer.Option(
            "--refresh",
            help=(
                "With no command: write every public report, the spec's spelling of "
                "`tell backtest refresh`. Reads the Wayback cache only; `velocity-lead "
                "--refresh` is the command that fetches."
            ),
        ),
    ] = False,
) -> None:
    """Backtest and methods reports (spec section 13)."""
    if ctx.invoked_subcommand is not None:
        return
    if not refresh:
        typer.echo(ctx.get_help())
        raise typer.Exit(code=0)
    run_public(_now(None), METHODS_DIR)


@app.command("list")
def list_command() -> None:
    """Print the names of the backtest reports, one per line."""
    for name in REPORTS:
        typer.echo(name)


@app.command("speed-vs-press")
def speed_vs_press_command(
    as_of: AsOfOption = None, out: OutOption = METHODS_DIR, refresh: RefreshOption = False
) -> None:
    """13.1: Form D filings against the first funding headline."""
    del refresh  # reads only events; the news collector runs separately
    _write(speed_vs_press.build_report, _now(as_of), out)


@app.command("velocity-lead")
def velocity_lead_command(
    *,
    as_of: AsOfOption = None,
    out: OutOption = METHODS_DIR,
    refresh: RefreshOption = False,
    formd: FormdOption = None,
    wayback_cache: CacheOption = DEFAULT_CACHE_DIR,
    wayback_budget: BudgetOption = DEFAULT_WAYBACK_BUDGET,
) -> None:
    """13.2: hiring velocity as a leading indicator (prospective + retrospective)."""
    builder = _velocity_builder(formd, refresh, wayback_cache, wayback_budget)
    _write(builder, _now(as_of), out)


@app.command("noise")
def noise_command(
    as_of: AsOfOption = None, out: OutOption = METHODS_DIR, refresh: RefreshOption = False
) -> None:
    """13.3: reposts, posting lifetimes, long-open roles."""
    del refresh
    _write(noise.build_report, _now(as_of), out)


@app.command("ops")
def ops_command(
    as_of: AsOfOption = None, out: OutOption = METHODS_DIR, refresh: RefreshOption = False
) -> None:
    """13.4: weeks unattended, success rate, freshness, coverage, resolution."""
    del refresh
    _write(ops.build_report, _now(as_of), out)


@app.command("dogfood")
def dogfood_command(
    as_of: AsOfOption = None,
    out: Annotated[
        Path,
        typer.Option(
            "--out",
            help=(
                "Private directory, default tell-private/reports. Any path inside the public "
                "checkout is refused; any other path is accepted at the operator's risk (the "
                "report mixes personal outcomes with system output, SPEC 13.5)."
            ),
        ),
    ] = dogfood.DEFAULT_OUT,
    refresh: RefreshOption = False,
) -> None:
    """13.5 (private): hours to surface, applications, interviews; tell-private only."""
    del refresh
    try:
        target = dogfood.check_private_out(out)
    except dogfood.PrivateOutputError as exc:
        raise typer.BadParameter(str(exc), param_hint="--out") from exc
    now = _now(as_of)
    with session_scope() as session:
        report = dogfood.build_report(session, now)
    _echo(write_report(report, target, latest=False).to_dict())


@app.command("refresh")
def refresh_command(
    *,
    as_of: AsOfOption = None,
    out: OutOption = METHODS_DIR,
    refresh: RefreshOption = False,
    formd: FormdOption = None,
    wayback_cache: CacheOption = DEFAULT_CACHE_DIR,
    wayback_budget: BudgetOption = DEFAULT_WAYBACK_BUDGET,
) -> None:
    """Write every public report (speed-vs-press, velocity-lead, noise, ops)."""
    run_public(
        _now(as_of),
        out,
        refresh=refresh,
        formd=formd,
        cache=wayback_cache,
        budget=wayback_budget,
    )
