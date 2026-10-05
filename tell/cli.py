"""``tell`` command-line interface.

Phase 0 ships only ``version``; later phases add ``ingest``, ``discover``,
``resolve``, ``score``, ``publish``, ``notify``, ``deadman``, ``runlog``,
``weekly``, ``backtest`` and ``prune`` (see ``docs/PLAN.md`` Phase 9).
"""

from __future__ import annotations

import typer

from tell import __version__

app = typer.Typer(
    name="tell",
    help="Open-source buying-signals pipeline.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def main() -> None:
    """tell: public buying signals with receipts."""


@app.command()
def version() -> None:
    """Print the installed tell version."""
    typer.echo(f"tell {__version__}")


if __name__ == "__main__":
    app()
