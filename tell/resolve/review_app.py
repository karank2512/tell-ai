"""Streamlit page over the entity-match review queue (optional; in-repo, not deployed).

Run with ``streamlit run tell/resolve/review_app.py``. The page is gated by
``TELL_REVIEW_PASSWORD`` (refused when unset) and offers the same three verdicts the
CSV workflow does: Link, Create company, Reject. Every rule lives in
:mod:`tell.resolve.review`; this module only draws widgets, so it is excluded from
coverage (``pragma: no cover``) and imports Streamlit lazily inside :func:`main` so
importing the module never needs a display.
"""

from __future__ import annotations

from typing import Any

from tell.config import get_settings
from tell.db import session_scope
from tell.resolve.review import (
    ReviewError,
    apply_verdict,
    check_password,
    discovery_details,
    open_items,
)

__all__ = ["main"]

TITLE = "tell: entity-match review"


def _candidates_text(payload: dict[str, Any]) -> str:
    """The item's alias candidates, then (for an issuer-discovery item, ADR 0035) the
    proposed domains, tier, evidence and amount sold, which a ``create`` verdict needs."""
    lines = [
        f"{c.get('company_id')}: {c.get('alias')} ({c.get('ratio')})"
        for c in payload.get("candidates") or []
    ]
    found = discovery_details(payload)
    if found["discovery_reason"] or found["discovery_domains"]:
        lines.append(
            f"issuer discovery: {found['discovery_reason'] or '-'}"
            f", tier {found['discovery_tier'] or '-'}"
        )
        lines.append(f"  domains: {found['discovery_domains'] or '(none)'}")
        if found["discovery_evidence"]:
            lines.append(f"  evidence: {found['discovery_evidence']}")
        if found["discovery_amount_sold"]:
            lines.append(f"  amount sold: {found['discovery_amount_sold']}")
        return "\n".join(lines)
    return "\n".join(lines) or "(no candidates)"


def main() -> None:  # pragma: no cover
    """Draw the page; UI only, the logic is tested in ``tests/resolve/test_review.py``."""
    import streamlit as st  # noqa: PLC0415 - lazy so importing this module needs no display

    st.set_page_config(page_title=TITLE)
    st.title(TITLE)
    expected = get_settings().tell_review_password
    if expected is None:
        st.error("TELL_REVIEW_PASSWORD is not set; the review page refuses to open.")
        st.stop()
    candidate = st.text_input("Password", type="password")
    if not check_password(candidate, expected):
        st.info("Enter the review password to continue.")
        st.stop()

    with session_scope() as session:
        items = open_items(session)
        st.caption(f"{len(items)} open entity_match item(s)")
        for item in items:
            payload = item.payload
            with st.expander(f"#{item.id} {payload.get('name')} ({payload.get('state') or '-'})"):
                st.text(_candidates_text(payload))
                ref = st.text_input("Company id or domain", key=f"ref-{item.id}")
                new_name = st.text_input("New company name (create)", key=f"name-{item.id}")
                link, create, reject = st.columns(3)
                verdict: str | None = None
                if link.button("Link", key=f"link-{item.id}"):
                    verdict = "link"
                if create.button("Create company", key=f"create-{item.id}"):
                    verdict = "create"
                if reject.button("Reject", key=f"reject-{item.id}"):
                    verdict = "reject"
                if verdict is not None:
                    try:
                        apply_verdict(
                            session,
                            item.id,
                            verdict,
                            company_id_or_domain=ref,
                            new_company_name=new_name,
                        )
                    except ReviewError as exc:
                        st.error(str(exc))
                    else:
                        session.commit()
                        st.success(f"#{item.id}: {verdict}")
                        st.rerun()


if __name__ == "__main__":  # pragma: no cover
    main()
