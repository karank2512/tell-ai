"""Intake: the Google Form for design partners into watchlists (spec section 15.6).

* :mod:`tell.intake.reader` reads the form's published-as-CSV sheet, validates rows and
  creates or updates ``watchlists`` / ``watchlist_items``; contact details and Slack
  webhooks go only to ``tell-private/watchlists/<slug>.yaml`` (ADR 0027), which
  :func:`tell.intake.reader.load_private_watchlists` reads back.
* :mod:`tell.intake.cli` is the ``tell intake`` typer sub-app.
"""

from __future__ import annotations

from tell.intake.reader import PrivateWatchlist, load_private_watchlists

__all__ = ["PrivateWatchlist", "load_private_watchlists"]
