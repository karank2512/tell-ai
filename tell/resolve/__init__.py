"""Entity resolution (spec section 6): earn every source's link to a domain.

* :mod:`tell.resolve.normalize` is the five-step name normalization.
* :mod:`tell.resolve.match` applies rules R1-R6 and resolves unlinked events.
* :mod:`tell.resolve.guards` holds the Form D linking guards (ADR 0034).
* :mod:`tell.resolve.relink` repairs Form D links made before them (``relink-formd``).
* :mod:`tell.resolve.review` is the review-queue workflow (CSV round trip, verdicts).
* :mod:`tell.resolve.review_app` is the optional Streamlit page over ``review``.
* :mod:`tell.resolve.cli` is the ``tell resolve`` typer sub-app.
"""

from __future__ import annotations

from tell.resolve.normalize import domain_label, normalize_name, normalize_name_alt

__all__ = ["domain_label", "normalize_name", "normalize_name_alt"]
