"""External-effect gate: a fail-closed kill switch in front of every outbound effect.

An *external effect* is anything that leaves this process's local, reviewable state: a
scored write of the scheduled pipeline (``tell score --all-profiles``), a file written
into the public ``tell-data`` checkout (``tell publish``, ``tell weekly``,
``tell prune``), a Slack POST (``tell notify``) or any HubSpot HTTP call
(``tell crm hubspot check|setup|push`` and the nudge's HubSpot rule 3 and push).

:data:`EXTERNAL_EFFECTS_ENABLED` is a code constant, never a setting: no environment
variable, ``.env`` file or Actions variable can flip it, and it is read at call time.
While it is not exactly ``True`` every gated path refuses *first*, before any database
session, token use, file write, runs row or HTTP client, so a refused command has no side
effect and does not rely on a later step failing. Enabling it is the owner's
activation decision, made as a reviewed code change; it is separate from
:data:`tell.ops.source_eligibility.WIRED` (whether outputs read only eligible rows), and
both must hold for the publish and notify paths.

The command-line refusal exits :data:`EFFECTS_BLOCKED_EXIT_CODE` (3), distinct from the
``WIRED`` refusal (2) and from an ordinary failure (1). It is non-zero on purpose: in a
workflow a refused step must not let the steps after it (the shell ``git push`` of
``tell-data``) run as if it had succeeded.
"""

from __future__ import annotations

from typing import Final

import typer

__all__ = [
    "EFFECTS_BLOCKED_EXIT_CODE",
    "EXTERNAL_EFFECTS_ENABLED",
    "ExternalEffectsBlockedError",
    "effects_blocked",
    "ensure_effects_enabled",
    "require_effects",
]

EXTERNAL_EFFECTS_ENABLED: Final[bool] = False
"""Whether any gated path may produce an external effect. ``False``: everything refuses."""

EFFECTS_BLOCKED_EXIT_CODE: Final = 3
"""Exit code of a command refused by this gate (before any side effect)."""


class ExternalEffectsBlockedError(RuntimeError):
    """Raised by a library entry point (e.g. the HubSpot client) while the gate is closed."""


def effects_blocked() -> bool:
    """True unless :data:`EXTERNAL_EFFECTS_ENABLED` is exactly ``True`` (read at call time)."""
    return EXTERNAL_EFFECTS_ENABLED is not True


def _reason(what: str) -> str:
    return (
        f"{what} is disabled: external effects are not enabled "
        "(tell/ops/effects.py EXTERNAL_EFFECTS_ENABLED is False); refused before any "
        "database, file or network access"
    )


def ensure_effects_enabled(what: str) -> None:
    """Raise :class:`ExternalEffectsBlockedError` while the gate is closed."""
    if effects_blocked():
        raise ExternalEffectsBlockedError(_reason(what))


def require_effects(command: str) -> None:
    """CLI form: print the refusal on stderr and exit 3 while the gate is closed.

    Call it first in a command, before any session, token use, file or HTTP client.
    """
    if effects_blocked():
        typer.echo(f"error: {_reason(f'`tell {command}`')}", err=True)
        raise typer.Exit(code=EFFECTS_BLOCKED_EXIT_CODE)
