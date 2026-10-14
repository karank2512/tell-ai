"""``tell resolve``: the entity-resolution typer sub-app (mounted into ``tell/cli.py`` in Phase 9).

Commands:

* ``run [--limit N] [--retry]``: match unlinked events (R1-R6), write audit payloads,
  open review items.
* ``backfill-aliases``: seed ``company_aliases`` from company names, domain labels and
  linked ATS boards.
* ``review-export PATH``: write open ``entity_match`` items to a CSV for a human.
* ``review-import PATH``: apply the verdicts filled into that CSV.
* ``stats``: counts of linked / unlinked events by rule and open review items.
* ``prune-aliases [--dry-run | --apply]``: delete ``manual`` aliases failing the alias rule
  (ADR 0033); never ``edgar`` / ``news`` / ``ats_board`` rows or aliases a review wrote.
* ``close-junk-reviews [--dry-run | --apply]``: close open ``entity_match`` items the
  current rules no longer send to review (no candidate under the alias rule, only a
  shared descriptor word, or a pooled-fund issuer; ADR 0033 / 0034) as ``rejected``
  (``resolved_by = alias_hygiene``).
* ``relink-formd [--dry-run | --apply]``: re-evaluate every linked Form D event under the
  Form D guards (ADR 0034); unlink the links that fail, open review items, and remove the
  aliases, legal names and CIKs those links wrote. Prints the changes.

The three maintenance commands are dry runs unless ``--apply`` is given.

Each command commits on success and prints one JSON object on stdout.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer
from sqlalchemy import func, select

from tell.collectors.base import utcnow
from tell.db import session_scope
from tell.models import EventRow, ReviewQueueItem
from tell.resolve.hygiene import close_junk_reviews, prune_aliases
from tell.resolve.match import REVIEW_KIND, backfill_aliases, resolve_events
from tell.resolve.relink import relink_formd
from tell.resolve.review import export_review_csv, import_review_csv

app = typer.Typer(
    name="resolve",
    help="Link events to companies (spec section 6) and work the review queue.",
    no_args_is_help=True,
    add_completion=False,
)


def _echo(record: dict[str, Any]) -> None:
    typer.echo(json.dumps(record, sort_keys=True))


@app.command("run")
def run_command(
    limit: Annotated[
        int | None, typer.Option("--limit", min=1, help="Max unlinked events to process.")
    ] = None,
    retry: Annotated[
        bool, typer.Option("--retry", help="Re-evaluate events that already carry a decision.")
    ] = False,
) -> None:
    """Resolve unlinked events with rules R1-R6."""
    with session_scope() as session:
        stats = resolve_events(session, now=utcnow(), limit=limit, retry=retry)
        _echo(stats.to_dict())


@app.command("backfill-aliases")
def backfill_command() -> None:
    """Seed company_aliases from company names, domain labels and linked ATS boards."""
    with session_scope() as session:
        _echo({"aliases_added": backfill_aliases(session)})


@app.command("review-export")
def review_export_command(
    path: Annotated[Path, typer.Argument(dir_okay=False, help="CSV to write.")],
) -> None:
    """Export open entity_match review items to a CSV."""
    with session_scope() as session:
        rows = export_review_csv(session, path)
        _echo({"path": str(path), "rows": rows})


@app.command("review-import")
def review_import_command(
    path: Annotated[
        Path, typer.Argument(exists=True, dir_okay=False, help="CSV with verdicts filled in.")
    ],
) -> None:
    """Apply the verdicts in a review CSV (link | create | reject)."""
    with session_scope() as session:
        stats = import_review_csv(session, path, now=utcnow())
        _echo(stats.to_dict())


def gather_stats(session: Any) -> dict[str, Any]:
    """Linked / unlinked event counts, decisions by rule, open and resolved review items."""
    rule = EventRow.payload.op("->")("match").op("->>")("rule")
    by_rule = {
        str(key): int(count)
        for key, count in session.execute(
            select(rule, func.count()).where(rule.is_not(None)).group_by(rule)
        )
    }
    linked = session.scalar(
        select(func.count()).select_from(EventRow).where(EventRow.company_id.is_not(None))
    )
    unlinked = session.scalar(
        select(func.count()).select_from(EventRow).where(EventRow.company_id.is_(None))
    )
    review = {
        str(status): int(count)
        for status, count in session.execute(
            select(ReviewQueueItem.status, func.count())
            .where(ReviewQueueItem.kind == REVIEW_KIND)
            .group_by(ReviewQueueItem.status)
        )
    }
    return {
        "events_linked": int(linked or 0),
        "events_unlinked": int(unlinked or 0),
        "by_rule": dict(sorted(by_rule.items())),
        "review": dict(sorted(review.items())),
    }


@app.command("stats")
def stats_command() -> None:
    """Print resolution statistics."""
    with session_scope() as session:
        _echo(gather_stats(session))


_DRY_RUN = typer.Option("--dry-run/--apply", help="Report only (default), or write the changes.")


@app.command("prune-aliases")
def prune_aliases_command(dry_run: Annotated[bool, _DRY_RUN] = True) -> None:
    """Delete manual aliases that fail the alias rule (ADR 0033); dry run by default."""
    with session_scope() as session:
        _echo(prune_aliases(session, apply=not dry_run))


@app.command("close-junk-reviews")
def close_junk_reviews_command(dry_run: Annotated[bool, _DRY_RUN] = True) -> None:
    """Close open entity_match items with no candidate left under the alias rule; dry run by default."""
    with session_scope() as session:
        _echo(close_junk_reviews(session, now=utcnow(), apply=not dry_run))


@app.command("relink-formd")
def relink_formd_command(dry_run: Annotated[bool, _DRY_RUN] = True) -> None:
    """Re-earn every Form D link under the ADR 0034 guards and undo what failing links wrote; dry run by default."""
    with session_scope() as session:
        _echo(relink_formd(session, now=utcnow(), apply=not dry_run))
