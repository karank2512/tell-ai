"""``tell publish``: the publishing typer sub-app (mounted into ``tell/cli.py`` in Phase 9).

Commands (``DIR`` is the ``tell-data`` checkout):

* ``site --out DIR [--as-of DATE] [--profile NAME]``: render ``DIR/site`` (pages + api/v0).
* ``data --out DIR [--day DATE]``: append the day's (and the previous day's) events to
  ``events/YYYY/MM/DD.jsonl``, write today's board snapshots, prune snapshots past 90 days.
* ``csv --watchlist NAME --out FILE [--as-of DATE]``: the CSV with receipts for one watchlist.
* ``weekly [--week current|YYYY-Www] --out DIR``: the weekly page and ``latest.json`` only.
* ``health --out DIR``: ``health.html`` and ``api/v0/health.json`` only.
* ``notify --slack [--force]``: the Slack digest (sends only on the digest run unless forced).

``tell publish --out DIR`` with no command runs ``data`` then ``site`` (the nightly step
in ``ingest.yml``). ``data`` and ``site`` are recorded as ``runs`` rows with source
``publish`` (the health page's ``publish`` job); every command commits on success and
prints one JSON object on stdout.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Any

import typer
from sqlalchemy import select
from sqlalchemy.orm import Session

from tell.collectors.base import finish_run, start_run, utcnow
from tell.config import get_settings
from tell.db import session_scope
from tell.models import Watchlist, WatchlistItem
from tell.publish.csv_export import export_csv
from tell.publish.data_repo import prune_snapshots, write_events_jsonl, write_snapshots
from tell.publish.health import compute_health
from tell.publish.json_api import (
    build_health_json,
    build_weekly_json,
    code_commit_sha,
    validate_health_json,
    validate_weekly_json,
    watchlist_profile,
)
from tell.publish.site import environment, page_context, render_site, weekly_view, write_json
from tell.publish.slack import send_digests
from tell.publish.weekly import build_weekly, resolve_week
from tell.signals.profiles import DEFAULT_PROFILE_NAME, ProfileNotFoundError

PUBLISH_SOURCE = "publish"

app = typer.Typer(
    name="publish",
    help="Publish tell-data: JSONL events, snapshots, the static site, CSV, Slack (spec section 9).",
    add_completion=False,
)

OutDir = Annotated[Path, typer.Option("--out", file_okay=False, help="The tell-data checkout.")]
AsOfOption = Annotated[
    str | None, typer.Option("--as-of", help="Calendar day to publish (ISO date; default today).")
]


def _echo(record: dict[str, Any]) -> None:
    typer.echo(json.dumps(record, sort_keys=True, default=str))


def _date(value: str | None, option: str) -> date:
    if value is None:
        return utcnow().date()
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        msg = f"{option} must be an ISO date (YYYY-MM-DD), got {value!r}"
        raise typer.BadParameter(msg) from exc


def _run_data(session: Session, out: Path, day: date) -> dict[str, Any]:
    now = utcnow()
    run_id = start_run(session, PUBLISH_SOURCE, now=now)
    written = 0
    for d in (day - timedelta(days=1), day):
        written += len(write_events_jsonl(session, out, d).written)
    snapshots = write_snapshots(session, out, day)
    pruned = prune_snapshots(out, today=day)
    finish_run(
        session,
        run_id,
        status="ok",
        fetched=written,
        inserted=written,
        skipped=0,
        notes={"step": "data", "snapshots": len(snapshots), "pruned": len(pruned)},
    )
    return {"events_written": written, "snapshots": len(snapshots), "snapshots_pruned": len(pruned)}


def _run_site(session: Session, out: Path, as_of: date, profile: str) -> dict[str, Any]:
    now = utcnow()
    run_id = start_run(session, PUBLISH_SOURCE, now=now)
    result = render_site(session, out, as_of=as_of, profile=profile, now=now)
    finish_run(
        session,
        run_id,
        status="ok",
        fetched=len(result.companies),
        inserted=len(result.companies),
        skipped=len(result.removed),
        notes={"step": "site", "weekly": result.weekly, "validated": result.validated},
    )
    return {
        "companies": len(result.companies),
        "removed": len(result.removed),
        "watchlists": result.watchlists,
        "weekly": result.weekly,
        "validated": result.validated,
        "stale_sources": result.stale_sources,
    }


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out", file_okay=False, help="Run data + site into this tell-data checkout."
        ),
    ] = None,
) -> None:
    """Publish tell-data. With --out and no command: data, then site."""
    if ctx.invoked_subcommand is not None:
        return
    if out is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(code=0)
    day = utcnow().date()
    with session_scope() as session:
        record = _run_data(session, out, day)
        record.update(_run_site(session, out, day, DEFAULT_PROFILE_NAME))
        _echo(record)


@app.command("site")
def site_command(
    out: OutDir,
    as_of: AsOfOption = None,
    profile: Annotated[str, typer.Option("--profile", help="ICP profile.")] = DEFAULT_PROFILE_NAME,
) -> None:
    """Render the static site and the api/v0 JSON under OUT/site."""
    day = _date(as_of, "--as-of")
    with session_scope() as session:
        try:
            record = _run_site(session, out, day, profile)
        except ProfileNotFoundError as exc:
            raise typer.BadParameter(str(exc)) from exc
        _echo(record)


@app.command("data")
def data_command(
    out: OutDir,
    day: Annotated[
        str | None, typer.Option("--day", help="UTC day to write (ISO date; default today).")
    ] = None,
) -> None:
    """Append JSONL events, write board snapshots, prune old snapshots."""
    chosen = _date(day, "--day")
    with session_scope() as session:
        _echo(_run_data(session, out, chosen))


@app.command("csv")
def csv_command(
    watchlist: Annotated[str, typer.Option("--watchlist", help="Watchlist name.")],
    out: Annotated[Path, typer.Option("--out", dir_okay=False, help="CSV file to write.")],
    as_of: AsOfOption = None,
) -> None:
    """Write the CSV with receipts for one watchlist."""
    day = _date(as_of, "--as-of")
    with session_scope() as session:
        found = session.scalars(
            select(Watchlist).where(Watchlist.name == watchlist).order_by(Watchlist.id)
        ).first()
        if found is None:
            msg = f"no watchlist named {watchlist!r}"
            raise typer.BadParameter(msg)
        try:
            profile = watchlist_profile(found, None)
        except ProfileNotFoundError as exc:
            raise typer.BadParameter(str(exc)) from exc
        ids = [
            int(r[0])
            for r in session.execute(
                select(WatchlistItem.company_id).where(WatchlistItem.watchlist_id == found.id)
            )
        ]
        rows = export_csv(session, out, ids, profile, day)
        _echo({"path": str(out), "rows": rows, "profile": profile.name, "as_of": day.isoformat()})


@app.command("weekly")
def weekly_command(
    out: OutDir,
    week: Annotated[
        str, typer.Option("--week", help="'current' (last complete ISO week) or YYYY-Www.")
    ] = "current",
) -> None:
    """Render one weekly page and api/v0/weekly/latest.json."""
    now = utcnow()
    try:
        chosen = resolve_week(week, now)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    site = out / "site"
    sha = code_commit_sha()
    with session_scope() as session:
        data = build_weekly(session, chosen)
        obj = build_weekly_json(data, sha=sha)
        validate_weekly_json(obj)
        write_json(site / "api" / "v0" / "weekly" / "latest.json", obj)
        html = (
            environment()
            .get_template("weekly.html")
            .render(
                **page_context(now.date(), sha, chosen.label, "../"), w=weekly_view(data, set())
            )
        )
        path = site / "weekly" / f"{chosen.label}.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html, encoding="utf-8")
        _echo({"week": chosen.label, "path": str(path), "top_hiring": len(data.top_hiring)})


@app.command("health")
def health_command(out: OutDir) -> None:
    """Render health.html and api/v0/health.json."""
    now = utcnow()
    site = out / "site"
    sha = code_commit_sha()
    with session_scope() as session:
        report = compute_health(session, now)
        obj = build_health_json(report, sha=sha)
        validate_health_json(obj)
        write_json(site / "api" / "v0" / "health.json", obj)
        week = resolve_week("current", now).label
        html = (
            environment()
            .get_template("health.html")
            .render(**page_context(now.date(), sha, week, ""), h=obj)
        )
        (site / "health.html").write_text(html, encoding="utf-8")
        _echo({"stale_sources": report.stale_sources, "sources": len(report.sources)})


@app.command("notify")
def notify_command(
    slack: Annotated[bool, typer.Option("--slack", help="Send the Slack digest.")] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Send even when this is not the digest run.")
    ] = False,
) -> None:
    """Send notifications (the Slack digest, on the first run at or after the digest hour)."""
    if not slack:
        msg = "nothing to notify: pass --slack"
        raise typer.BadParameter(msg)
    secret = get_settings().slack_webhook_url
    webhook = secret.get_secret_value() if secret is not None else None
    with session_scope() as session:
        outcome = send_digests(session, now=utcnow(), webhook_url=webhook, force=force)
        _echo(
            {
                "sent": outcome.sent,
                "errors": outcome.errors,
                "skipped": outcome.skipped,
                "run_id": outcome.run_id,
            }
        )
        if outcome.errors and not outcome.sent:
            raise typer.Exit(code=1)


if __name__ == "__main__":  # pragma: no cover
    app()
