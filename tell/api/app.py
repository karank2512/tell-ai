"""The FastAPI v1 service (spec section 10): ``uvicorn tell.api.app:app``.

Routes
======

======================================== ============================================
``GET /v1/companies/{domain}``           The static ``api/v0`` company object, computed
                                         now (``?profile=``, default ``default``); 404
                                         for a domain tell has no company for.
``GET /v1/in-market``                    ``?watchlist=`` (id or slug) a list the key's
                                         owner owns or that is public; ``since`` (date,
                                         default 7 days ago), ``min_score`` (default
                                         the profile's alert threshold), ``family``
                                         (repeatable). Highest score first.
``GET /v1/companies/{domain}/events``    Raw events with receipt URLs, newest first,
                                         at most 200; ``?since=`` date, ``?types=``
                                         (repeatable or comma-separated).
``POST /v1/watchlists``                  ``{name, icp_profile, domains[]}``: a new
                                         private list owned by the key's owner; known
                                         domains are added, unknown ones returned.
``POST /v1/feedback``                    ``{signal_id, verdict, note?, watchlist_id?}``
                                         on one of the owner's watchlists.
``GET /healthz``                         ``api/v0/health.json`` computed now; no auth.
======================================== ============================================

Every ``/v1`` route authenticates the bearer key and meters the call *before* the
handler runs (:mod:`tell.api.auth`); the metering row is committed on its own so a
handler error never un-counts a call. Errors are RFC 9457 problem bodies
(``application/problem+json``). CORS is off: no CORS middleware is installed, so
browsers on other origins cannot read responses (Clay and server-side callers are
unaffected). Scores are computed fresh at request time (``write=False``), so the
service never writes ``scores``.

:func:`create_app` takes an ``engine`` and a ``clock`` so tests bind the temporary
database and a fixed instant; the module-level :data:`app` uses ``DATABASE_URL``
lazily (nothing connects at import).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, date, datetime, time, timedelta
from http import HTTPStatus
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, Path, Query, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import HTTPException, RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from tell import __version__
from tell.api.auth import authenticate, meter
from tell.api.schemas import (
    CompanyOut,
    ContributionOut,
    EventOut,
    EventsOut,
    FeedbackCreated,
    FeedbackIn,
    HealthOut,
    InMarketCompany,
    InMarketOut,
    Problem,
    WatchlistCreated,
    WatchlistIn,
    WatchlistRef,
)
from tell.collectors.base import parse_iso, utcnow
from tell.db import SessionLocal, get_engine
from tell.discovery import registrable_domain
from tell.models import ApiKey, Company, EventRow, Feedback, Signal, Watchlist, WatchlistItem
from tell.publish.health import compute_health
from tell.publish.json_api import (
    apportion_tenths,
    build_company_json,
    build_health_json,
    round_half_up,
)
from tell.signals.profiles import DEFAULT_PROFILE_NAME, Profile, load_profiles
from tell.signals.scoring import heat, score_companies
from tell.signals.taxonomy import FAMILIES, OTHER

__all__ = [
    "DEFAULT_SINCE_DAYS",
    "MAX_EVENTS",
    "PROBLEM_MEDIA_TYPE",
    "app",
    "create_app",
    "current_key",
    "get_session",
    "normalize_domain",
]

Clock = Callable[[], datetime]

PROBLEM_MEDIA_TYPE = "application/problem+json"
MAX_EVENTS = 200
DEFAULT_SINCE_DAYS = 7
TOP_CONTRIBUTIONS = 3
KNOWN_FAMILIES: frozenset[str] = frozenset((*FAMILIES, OTHER))

_bearer = HTTPBearer(auto_error=False, description="Authorization: Bearer tell_...")

_PROBLEM_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": Problem, "content": {PROBLEM_MEDIA_TYPE: {}}} for code in (401, 404, 422, 429)
}


# --- errors ------------------------------------------------------------------------------------


def _problem(
    status_code: int,
    detail: str | None,
    *,
    headers: dict[str, str] | None = None,
    errors: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    try:
        title = HTTPStatus(status_code).phrase
    except ValueError:
        title = "Error"
    body = Problem(title=title, status=status_code, detail=detail, errors=errors)
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(exclude_none=True),
        headers=headers,
        media_type=PROBLEM_MEDIA_TYPE,
    )


async def _http_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, StarletteHTTPException):  # registered for this type only
        raise exc
    detail = exc.detail if isinstance(exc.detail, str) else None
    return _problem(exc.status_code, detail, headers=dict(exc.headers or {}))


async def _validation_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):  # registered for this type only
        raise exc
    errors = [
        {"loc": list(err.get("loc", ())), "msg": str(err.get("msg", "")), "type": err.get("type")}
        for err in jsonable_encoder(exc.errors())
    ]
    return _problem(422, "request failed validation", errors=errors)


async def _unhandled_exception_handler(_request: Request, _exc: Exception) -> JSONResponse:
    return _problem(500, "internal error")


def _not_found(what: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=what)


def _unprocessable(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)


# --- dependencies --------------------------------------------------------------------------------


def _engine(request: Request) -> Engine:
    engine: Engine | None = request.app.state.engine
    return engine if engine is not None else get_engine()


def _now(request: Request) -> datetime:
    clock: Clock = request.app.state.clock
    return clock()


def get_session(request: Request) -> Iterator[Session]:
    """One session per request; commits on success, rolls back on any error."""
    session = SessionLocal(bind=_engine(request))
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


SessionDep = Annotated[Session, Depends(get_session)]


def current_key(
    request: Request,
    session: SessionDep,
    _credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> ApiKey:
    """Authenticate the bearer key and meter the call, before the handler runs.

    The metering upsert (and ``last_used_at``) is committed here, so a handler that
    fails afterwards still counts as a call.
    """
    key = authenticate(session, request.headers.get("authorization"))
    meter(session, key, _now(request))
    session.commit()
    return key


KeyDep = Annotated[ApiKey, Depends(current_key)]


# --- helpers ---------------------------------------------------------------------------------------


def normalize_domain(value: str) -> str:
    """A lookup key for ``companies.domain``: lowercase host, no scheme, path or ``www.``."""
    text = value.strip().lower()
    if "://" in text:
        text = text.split("://", 1)[1]
    text = text.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    text = text.rsplit("@", 1)[-1].split(":", 1)[0].rstrip(".")
    return text.removeprefix("www.")


def _find_company(session: Session, domain: str) -> Company | None:
    wanted = normalize_domain(domain)
    if not wanted:
        return None
    candidates = [wanted]
    registrable = registrable_domain(wanted)
    if registrable and registrable != wanted:
        candidates.append(registrable)
    for candidate in candidates:
        company = session.scalars(select(Company).where(Company.domain == candidate)).one_or_none()
        if company is not None:
            return company
    return None


def _profile(name: str) -> Profile:
    profiles = load_profiles()
    if name not in profiles:
        msg = f"no ICP profile named {name!r} (known: {', '.join(sorted(profiles))})"
        raise _unprocessable(msg)
    return profiles[name]


def _visible_watchlist(session: Session, key: ApiKey, ref: str) -> Watchlist:
    """A watchlist by id or slug that the key's owner owns or that is public, else 404."""
    text = ref.strip()
    stmt = select(Watchlist)
    stmt = (
        stmt.where(Watchlist.id == int(text))
        if text.isdigit()
        else stmt.where(Watchlist.slug == text.lower())
    )
    watchlist = session.scalars(stmt).one_or_none()
    if watchlist is None or not (watchlist.is_public or watchlist.owner == key.owner):
        # Same answer for "missing" and "someone else's private list": no existence leak.
        raise _not_found(f"no watchlist {ref!r} visible to this key")
    return watchlist


def _members(session: Session, watchlist_id: int) -> list[Company]:
    return list(
        session.scalars(
            select(Company)
            .join(WatchlistItem, WatchlistItem.company_id == Company.id)
            .where(WatchlistItem.watchlist_id == watchlist_id)
            .order_by(Company.domain)
        )
    )


def _split(values: Sequence[str]) -> list[str]:
    out: list[str] = []
    for value in values:
        for part in value.split(","):
            item = part.strip()
            if item and item not in out:
                out.append(item)
    return out


def _contribution_entries(
    contributions: Sequence[dict[str, Any]], total: float
) -> list[ContributionOut]:
    """Score contributions in the static object's shape (rounded so they add up)."""
    shown = apportion_tenths([float(c["contribution"]) for c in contributions], total)
    entries: list[ContributionOut] = []
    for item, value in zip(contributions, shown, strict=True):
        fields: dict[str, Any] = {"type": item["signal_type"]}
        if item.get("role_family"):
            fields["role_family"] = item["role_family"]
        fields.update(
            contribution=value,
            occurred_at=parse_iso(str(item["occurred_at"])).date().isoformat(),
            summary=item["summary"],
            url=item["url"],
        )
        entries.append(ContributionOut(**fields))
    return entries


def _day_start(day: date) -> datetime:
    return datetime.combine(day, time(), UTC)


# --- routes --------------------------------------------------------------------------------------

router = APIRouter()


@router.get("/healthz", response_model=HealthOut, tags=["meta"])
def healthz(request: Request, session: SessionDep) -> dict[str, Any]:
    """Run-health, the same object as ``api/v0/health.json`` (no auth)."""
    return build_health_json(compute_health(session, _now(request)))


@router.get(
    "/v1/companies/{domain}",
    response_model=CompanyOut,
    response_model_exclude_unset=True,
    tags=["companies"],
)
def get_company(
    *,
    request: Request,
    session: SessionDep,
    _key: KeyDep,
    domain: Annotated[str, Path(min_length=1, max_length=253)],
    profile: Annotated[str, Query(min_length=1, max_length=100)] = DEFAULT_PROFILE_NAME,
) -> CompanyOut:
    """The per-domain object of ``api/v0/companies/{domain}.json``, computed now."""
    chosen = _profile(profile)
    company = _find_company(session, domain)
    if company is None:
        raise _not_found(f"no company for domain {normalize_domain(domain)!r}")
    obj = build_company_json(session, company, chosen, _now(request).date())
    return CompanyOut.model_validate(obj)


@router.get(
    "/v1/in-market",
    response_model=InMarketOut,
    response_model_exclude_unset=True,
    tags=["companies"],
)
def in_market(
    *,
    request: Request,
    session: SessionDep,
    key: KeyDep,
    watchlist: Annotated[str, Query(min_length=1, max_length=200)],
    since: date | None = None,
    min_score: Annotated[float | None, Query(ge=0)] = None,
    family: Annotated[list[str] | None, Query()] = None,
) -> InMarketOut:
    """Companies on a watchlist scoring at least ``min_score`` with a fresh signal."""
    chosen = _visible_watchlist(session, key, watchlist)
    prof = _profile(chosen.icp_profile)
    as_of = _now(request).date()
    start = since or as_of - timedelta(days=DEFAULT_SINCE_DAYS)
    if start > as_of:
        raise _unprocessable(f"since {start.isoformat()} is after today")
    families = _split(family or [])
    unknown = [f for f in families if f not in KNOWN_FAMILIES]
    if unknown:
        known = ", ".join(sorted(KNOWN_FAMILIES))
        raise _unprocessable(f"unknown role family {unknown[0]!r} (known: {known})")
    floor = prof.alert_threshold if min_score is None else min_score
    members = {c.id: c for c in _members(session, chosen.id)}
    results = score_companies(session, prof, as_of, list(members), write=False)
    heats = heat({r.company_id: r.score for r in results})
    rows: list[InMarketCompany] = []
    for result in results:
        if not result.in_band or result.score < floor:
            continue
        fresh = [
            c
            for c in result.contributions
            if parse_iso(str(c["occurred_at"])).date() >= start
            and (not families or c.get("role_family") in families)
        ]
        if not fresh:
            continue
        company = members[result.company_id]
        entries = _contribution_entries(result.contributions, result.score)
        rows.append(
            InMarketCompany(
                domain=company.domain,
                name=company.name,
                score=round_half_up(result.score),
                heat=heats.get(result.company_id, 0),
                top_contributions=entries[:TOP_CONTRIBUTIONS],
            )
        )
    rows.sort(key=lambda r: (-r.score, r.domain))
    return InMarketOut(
        watchlist=WatchlistRef(
            id=chosen.id,
            name=chosen.name,
            slug=chosen.slug,
            icp_profile=chosen.icp_profile,
            is_public=chosen.is_public,
        ),
        profile=prof.name,
        as_of=as_of,
        since=start,
        min_score=floor,
        families=families,
        companies=rows,
    )


@router.get("/v1/companies/{domain}/events", response_model=EventsOut, tags=["companies"])
def company_events(
    *,
    session: SessionDep,
    _key: KeyDep,
    domain: Annotated[str, Path(min_length=1, max_length=253)],
    since: date | None = None,
    types: Annotated[list[str] | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=MAX_EVENTS)] = MAX_EVENTS,
) -> EventsOut:
    """Raw events with their receipt URLs, newest first (at most 200)."""
    company = _find_company(session, domain)
    if company is None:
        raise _not_found(f"no company for domain {normalize_domain(domain)!r}")
    wanted = _split(types or [])
    stmt = select(EventRow).where(EventRow.company_id == company.id)
    if since is not None:
        stmt = stmt.where(EventRow.occurred_at >= _day_start(since))
    if wanted:
        stmt = stmt.where(EventRow.type.in_(wanted))
    stmt = stmt.order_by(EventRow.occurred_at.desc(), EventRow.event_id).limit(limit)
    events = [
        EventOut(
            event_id=row.event_id,
            source=row.source,
            type=row.type,
            occurred_at=row.occurred_at,
            observed_at=row.observed_at,
            magnitude=float(row.magnitude),
            url=row.url,
            payload=dict(row.payload or {}),
        )
        for row in session.scalars(stmt)
    ]
    return EventsOut(domain=company.domain, since=since, types=wanted, events=events)


@router.post(
    "/v1/watchlists",
    response_model=WatchlistCreated,
    status_code=status.HTTP_201_CREATED,
    tags=["watchlists"],
)
def create_watchlist(session: SessionDep, key: KeyDep, body: WatchlistIn) -> WatchlistCreated:
    """Create a private watchlist owned by the key's owner from a list of domains."""
    _profile(body.icp_profile)
    watchlist = Watchlist(owner=key.owner, name=body.name, icp_profile=body.icp_profile)
    session.add(watchlist)
    session.flush()
    added: list[str] = []
    unknown: list[str] = []
    seen: set[int] = set()
    for raw in body.domains:
        company = _find_company(session, raw)
        if company is None:
            label = normalize_domain(raw) or raw
            if label not in unknown:
                unknown.append(label)
            continue
        if company.id in seen:
            continue
        seen.add(company.id)
        session.add(WatchlistItem(watchlist_id=watchlist.id, company_id=company.id))
        added.append(company.domain)
    session.commit()
    return WatchlistCreated(
        id=watchlist.id,
        name=watchlist.name,
        icp_profile=watchlist.icp_profile,
        added=added,
        unknown=unknown,
    )


@router.post(
    "/v1/feedback",
    response_model=FeedbackCreated,
    status_code=status.HTTP_201_CREATED,
    tags=["feedback"],
)
def create_feedback(session: SessionDep, key: KeyDep, body: FeedbackIn) -> FeedbackCreated:
    """Record a verdict on one signal, on a watchlist the key's owner owns."""
    signal = session.get(Signal, body.signal_id)
    if signal is None:
        raise _not_found(f"no signal {body.signal_id}")
    holds = (
        select(WatchlistItem.watchlist_id)
        .join(Watchlist, Watchlist.id == WatchlistItem.watchlist_id)
        .where(WatchlistItem.company_id == signal.company_id, Watchlist.owner == key.owner)
    )
    if body.watchlist_id is not None:
        owned = session.get(Watchlist, body.watchlist_id)
        if owned is None or owned.owner != key.owner:
            raise _not_found(f"no watchlist {body.watchlist_id} owned by this key")
        on_list = session.execute(holds.where(WatchlistItem.watchlist_id == owned.id)).first()
        if on_list is None:
            raise _unprocessable(
                f"signal {body.signal_id}'s company is not on watchlist {owned.id}"
            )
        watchlist_id = owned.id
    else:
        first = session.execute(holds.order_by(WatchlistItem.watchlist_id).limit(1)).first()
        if first is None:
            raise _unprocessable(f"none of your watchlists holds signal {body.signal_id}'s company")
        watchlist_id = int(first[0])
    row = Feedback(
        watchlist_id=watchlist_id,
        signal_id=signal.id,
        verdict=body.verdict,
        note=body.note or None,
    )
    session.add(row)
    session.flush()
    session.refresh(row)
    session.commit()
    return FeedbackCreated(
        id=row.id,
        watchlist_id=row.watchlist_id,
        signal_id=row.signal_id,
        verdict=body.verdict,
        note=row.note,
        created_at=row.created_at,
    )


# --- app -----------------------------------------------------------------------------------------


def create_app(engine: Engine | None = None, clock: Clock | None = None) -> FastAPI:
    """Build the v1 app; ``engine`` defaults to ``DATABASE_URL``, ``clock`` to UTC now."""
    api = FastAPI(
        title="tell API",
        version=__version__,
        summary="Buying signals with receipts (v1). Everything here is also in api/v0 JSON.",
        responses=_PROBLEM_RESPONSES,
    )
    api.state.engine = engine
    api.state.clock = clock or utcnow
    api.add_exception_handler(StarletteHTTPException, _http_exception_handler)
    api.add_exception_handler(RequestValidationError, _validation_exception_handler)
    api.add_exception_handler(Exception, _unhandled_exception_handler)

    api.include_router(router)
    return api


app = create_app()
"""The process-wide app (``uvicorn tell.api.app:app``)."""
