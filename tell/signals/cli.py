"""``tell score``: the scoring typer sub-app (mounted into ``tell/cli.py`` in Phase 9).

Commands:

* ``run --profile NAME [--as-of DATE]``: score one profile for a day (the ``default``
  profile scores every company with an active signal, any other profile the
  companies on its watchlists), write ``scores`` and emit ``score_crossed_threshold``.
* ``all [--as-of DATE]``: the daily job, every shipped and private profile.
* ``build-signals``: derive ``signals`` rows from events (idempotent).
* ``feedback-report --profile NAME``: the weekly precision-by-signal-type report (Markdown).

Each command commits on success and prints one JSON object on stdout, except
``feedback-report`` which prints Markdown.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Annotated, Any

import typer

from tell.collectors.base import utcnow
from tell.db import session_scope
from tell.signals.feedback import format_markdown, precision_report
from tell.signals.profiles import DEFAULT_PROFILE_NAME, ProfileNotFoundError, get_profile
from tell.signals.scoring import build_signals, score_all, score_profile

app = typer.Typer(
    name="score",
    help="Score companies per ICP profile (spec section 8) and emit threshold crossings.",
    no_args_is_help=True,
    add_completion=False,
)

ProfileOption = Annotated[
    str, typer.Option("--profile", help="ICP profile name (shipped or tell-private).")
]
AsOfOption = Annotated[
    str | None, typer.Option("--as-of", help="Calendar day to score (ISO date; default today).")
]


def _echo(record: dict[str, Any]) -> None:
    typer.echo(json.dumps(record, sort_keys=True, default=str))


def _as_of(value: str | None) -> date:
    if value is None:
        return utcnow().date()
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        msg = f"--as-of must be an ISO date (YYYY-MM-DD), got {value!r}"
        raise typer.BadParameter(msg) from exc


def _profile(name: str) -> Any:
    try:
        return get_profile(name)
    except ProfileNotFoundError as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command("run")
def run_command(
    profile: ProfileOption = DEFAULT_PROFILE_NAME,
    as_of: AsOfOption = None,
) -> None:
    """Score one profile for a day and emit score_crossed_threshold events."""
    chosen = _profile(profile)
    day = _as_of(as_of)
    with session_scope() as session:
        results, events = score_profile(session, chosen, day, now=utcnow())
        _echo(
            {
                "profile": chosen.name,
                "as_of": day.isoformat(),
                "companies": len(results),
                "crossings": len(events),
                "above_threshold": sum(1 for r in results if r.score >= chosen.alert_threshold),
            }
        )


@app.command("all")
def all_command(as_of: AsOfOption = None) -> None:
    """The daily job: score every profile for a day."""
    day = _as_of(as_of)
    with session_scope() as session:
        stats = score_all(session, as_of=day, now=utcnow())
        _echo({"as_of": day.isoformat(), "profiles": stats})


@app.command("build-signals")
def build_signals_command() -> None:
    """Derive signals rows from events (idempotent)."""
    now = utcnow()
    with session_scope() as session:
        inserted = build_signals(session, now=now)
        _echo({"inserted": inserted, "now": now.isoformat()})


@app.command("feedback-report")
def feedback_report_command(profile: ProfileOption = DEFAULT_PROFILE_NAME) -> None:
    """Print the weekly precision-by-signal-type report for a profile as Markdown."""
    with session_scope() as session:
        typer.echo(format_markdown(precision_report(session, profile)), nl=False)


if __name__ == "__main__":
    app()
