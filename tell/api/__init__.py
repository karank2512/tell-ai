"""The FastAPI v1 service (spec section 10): coded and tested, not deployed.

* :mod:`tell.api.app` is the ASGI app (``uvicorn tell.api.app:app``).
* :mod:`tell.api.auth` generates, hashes and checks bearer keys and meters calls.
* :mod:`tell.api.schemas` holds the Pydantic request and response models.
* :mod:`tell.api.cli` is the ``tell api`` typer sub-app (``create-key``, ``revoke-key``,
  ``usage``).

Everything the service answers is also in the static JSON (``api/v0``), so an outage
degrades to stale, not to down. ``docs/deploy/render.md`` has the deployment notes.
"""
