"""Operations helpers behind the ``tell`` CLI: dead-man switch, run log, size-budget prune.

* :mod:`tell.ops.deadman`: which scheduled sources have had no successful run in 24 hours
  (spec section 12, "Run-health in production").
* :mod:`tell.ops.runlog`: the latest run line per source and the weekly operations line
  (spec sections 12 and 13.4).
* :mod:`tell.ops.prune`: the Neon size budget (spec section 5): ``posting_observations``
  past 90 days deleted, events past 180 days archived to ``tell-data`` then deleted.

Each function reads or writes through a caller's session and never commits; the CLI
(``tell/cli.py``) owns the transaction and the ``runs`` row.
"""
