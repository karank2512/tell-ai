"""Publishing (spec section 9): everything a user sees, generated nightly into ``tell-data``.

* :mod:`tell.publish.data_repo` writes the system of record: ``events/YYYY/MM/DD.jsonl``
  (append-only, also the 180-day prune archive) and ``postings/snapshots/`` (90 days).
* :mod:`tell.publish.json_api` builds and validates the static JSON API (``api/v0``).
* :mod:`tell.publish.health` computes run-health per source (``health.json`` / ``.html``).
* :mod:`tell.publish.weekly` gathers the weekly page's top lists and coverage numbers.
* :mod:`tell.publish.site` renders the GitHub Pages tree (``site/``) with Jinja.
* :mod:`tell.publish.slack` builds and sends the Block Kit digest.
* :mod:`tell.publish.csv_export` writes the "send me 50 domains" CSV with receipts.
* :mod:`tell.publish.cli` is the ``tell publish`` typer sub-app.

``docs/methods/publishing.md`` is the prose version (layout, retention, size budget,
digest timing).
"""

from __future__ import annotations
