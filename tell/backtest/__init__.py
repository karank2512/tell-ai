"""Backtest and methods reports (spec section 13; PLAN Phase 11).

``tell backtest <name>`` (:mod:`tell.backtest.cli`) writes one dated Markdown report per
study, rendered by :mod:`tell.backtest.common` from ``docs/methods/templates/``:

* :mod:`tell.backtest.speed_vs_press` (13.1), the spec's SQL as the implementation;
* :mod:`tell.backtest.velocity_lead` (13.2), prospective harness plus the two
  retrospective reconstructions (current ``posted_at`` and :mod:`tell.backtest.wayback`);
* :mod:`tell.backtest.noise` (13.3);
* :mod:`tell.backtest.ops` (13.4);
* :mod:`tell.backtest.dogfood` (13.5), private, written to ``tell-private/reports`` only.
"""
