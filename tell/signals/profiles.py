"""ICP profiles (spec section 8): the YAML schema, family-aware weights, loading.

A profile is the YAML document the spec shows under "ICP profiles"::

    name: sells - to - sales - teams
    target_families: [sales, gtm_ops, customer]  # weight 1.0 on hiring_velocity
    other_families_weight: 0.3
    weights: {funding: 4.0, leadership: 2.0, launch: 1.5, expansion: 1.0, tech_adoption: 0}
    half_life_days: {
        hiring_velocity: 30,
        funding: 90,
        leadership: 60,
        launch: 45,
        expansion: 60,
        tech_adoption: 90,
    }
    tech_keywords: []  # devtools profiles list the keywords that count
    size_band: {min_open_postings: 3, max_open_postings: 400}

plus one optional key the spec describes in prose, ``alert_threshold`` (default 6.0,
the score at which ``score_crossed_threshold`` fires).

Two profiles ship in-repo under ``tell/profiles/``: ``default`` (the spec's defaults)
and ``job-seeker``. Partner profiles are checked into ``tell-private/profiles/`` and
:func:`load_profiles` reads them too; a private profile with the same ``name`` as a
shipped one replaces it.

Weights are family-aware for ``hiring_velocity`` (1.0 for a target family,
``other_families_weight`` otherwise) and keyword-aware for ``tech_adoption``: the
profile's ``tech_adoption`` weight applies when the signal's keyword is listed in
``tech_keywords`` or the list is empty (every keyword counts), and is 0 otherwise.
``signals`` rows carry no payload, so the keyword comes from the originating event
(``tell.signals.scoring`` passes it along); with no keyword in hand only an empty
``tech_keywords`` list lets the weight apply.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from tell.models import SIGNAL_TYPES
from tell.signals.taxonomy import FAMILIES

__all__ = [
    "DEFAULT_ALERT_THRESHOLD",
    "DEFAULT_PROFILE_NAME",
    "PRIVATE_PROFILES_DIR",
    "SHIPPED_PROFILES_DIR",
    "HalfLives",
    "Profile",
    "ProfileNotFoundError",
    "SignalLike",
    "SizeBand",
    "Weights",
    "default_profile",
    "get_profile",
    "load_profile_file",
    "load_profiles",
]

DEFAULT_PROFILE_NAME = "default"
DEFAULT_ALERT_THRESHOLD = 6.0
"""Spec section 8: roughly one funding event or two fresh velocity signals."""

SHIPPED_PROFILES_DIR = Path(__file__).resolve().parents[1] / "profiles"
"""``tell/profiles/``: the profiles checked into the public repo."""

PRIVATE_PROFILES_DIR = Path(__file__).resolve().parents[2].parent / "tell-private" / "profiles"
"""``../tell-private/profiles/`` next to the checkout (PLAN "Local build conventions")."""

HIRING_VELOCITY = "hiring_velocity"
TECH_ADOPTION = "tech_adoption"
_WEIGHTED_TYPES: tuple[str, ...] = tuple(t for t in SIGNAL_TYPES if t != HIRING_VELOCITY)


class ProfileNotFoundError(KeyError):
    """No profile of that name is shipped or present in ``tell-private/profiles/``."""


class SignalLike(Protocol):
    """What :meth:`Profile.weight_for` needs from a signal (``Signal`` rows qualify)."""

    @property
    def signal_type(self) -> str: ...

    @property
    def role_family(self) -> str | None: ...


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Weights(_Strict):
    """``w_k`` per signal type other than ``hiring_velocity`` (which is family-aware)."""

    funding: float = Field(ge=0)
    leadership: float = Field(ge=0)
    launch: float = Field(ge=0)
    expansion: float = Field(ge=0)
    tech_adoption: float = Field(ge=0)

    def __getitem__(self, signal_type: str) -> float:
        if signal_type not in _WEIGHTED_TYPES:
            raise KeyError(signal_type)
        return float(getattr(self, signal_type))


class HalfLives(_Strict):
    """Half-life in days per signal type; ``profile.half_life_days[sig.signal_type]`` works."""

    hiring_velocity: int = Field(gt=0)
    funding: int = Field(gt=0)
    leadership: int = Field(gt=0)
    launch: int = Field(gt=0)
    expansion: int = Field(gt=0)
    tech_adoption: int = Field(gt=0)

    def __getitem__(self, signal_type: str) -> int:
        if signal_type not in SIGNAL_TYPES:
            raise KeyError(signal_type)
        return int(getattr(self, signal_type))

    @property
    def longest(self) -> int:
        """The longest half-life; ``4 * longest`` days bounds every active signal."""
        return max(self[t] for t in SIGNAL_TYPES)


class SizeBand(_Strict):
    """Open-posting range a company must fall in to be on this profile's in-market list."""

    min_open_postings: int = Field(ge=0)
    max_open_postings: int = Field(ge=0)

    def contains(self, open_postings: float) -> bool:
        return self.min_open_postings <= open_postings <= self.max_open_postings


class Profile(_Strict):
    """One ICP profile, exactly the spec's YAML plus ``alert_threshold``."""

    name: str = Field(min_length=1)
    target_families: list[str]
    other_families_weight: float = Field(default=0.3, ge=0)
    weights: Weights
    half_life_days: HalfLives
    tech_keywords: list[str] = Field(default_factory=list)
    size_band: SizeBand
    alert_threshold: float = DEFAULT_ALERT_THRESHOLD

    @field_validator("target_families")
    @classmethod
    def _known_families(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - set(FAMILIES))
        if unknown:
            msg = f"unknown role families in target_families: {unknown}"
            raise ValueError(msg)
        return value

    @field_validator("tech_keywords")
    @classmethod
    def _lowercase_keywords(cls, value: list[str]) -> list[str]:
        return [k.strip().lower() for k in value if k.strip()]

    @field_validator("size_band")
    @classmethod
    def _ordered_band(cls, value: SizeBand) -> SizeBand:
        if value.max_open_postings < value.min_open_postings:
            msg = "size_band.max_open_postings must be >= min_open_postings"
            raise ValueError(msg)
        return value

    # --- weights -------------------------------------------------------------------

    def weight_for(self, sig: SignalLike, *, keyword: str | None = None) -> float:
        """``w_k`` for one signal under this profile.

        ``hiring_velocity``: 1.0 when ``sig.role_family`` is a target family, else
        ``other_families_weight``. ``tech_adoption``: the profile weight when
        ``keyword`` is in ``tech_keywords`` or that list is empty, else 0. Every other
        type: the profile weight. Unknown types weigh 0.
        """
        signal_type = sig.signal_type
        if signal_type == HIRING_VELOCITY:
            return 1.0 if sig.role_family in self.target_families else self.other_families_weight
        if signal_type == TECH_ADOPTION:
            if self.tech_keywords and (keyword or "").strip().lower() not in self.tech_keywords:
                return 0.0
            return self.weights.tech_adoption
        if signal_type in _WEIGHTED_TYPES:
            return self.weights[signal_type]
        return 0.0

    def half_life_for(self, signal_type: str) -> int:
        """``half_life_days[signal_type]``; raises ``KeyError`` for an unknown type."""
        return self.half_life_days[signal_type]

    def to_yaml(self) -> str:
        """The profile as YAML, in spec key order (round-trips through :func:`load_profile_file`)."""
        return yaml.safe_dump(self.model_dump(mode="json"), sort_keys=False)


# --- loading -------------------------------------------------------------------------


def load_profile_file(path: Path) -> Profile:
    """Parse one profile YAML file; validation errors name the file."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, Mapping):
        msg = f"{path}: a profile must be a YAML mapping"
        raise ValueError(msg)
    try:
        return Profile.model_validate(dict(data))
    except ValueError as exc:
        msg = f"{path}: {exc}"
        raise ValueError(msg) from exc


def _load_dir(directory: Path) -> dict[str, Profile]:
    profiles: dict[str, Profile] = {}
    for path in sorted(directory.glob("*.yaml")):
        profile = load_profile_file(path)
        if profile.name in profiles:
            msg = f"{path}: duplicate profile name {profile.name!r} in {directory}"
            raise ValueError(msg)
        profiles[profile.name] = profile
    return profiles


def load_profiles(
    shipped_dir: Path = SHIPPED_PROFILES_DIR, private_dir: Path | None = PRIVATE_PROFILES_DIR
) -> dict[str, Profile]:
    """``{name: Profile}``: shipped profiles, then ``private_dir`` overriding by name.

    ``private_dir`` is skipped when it does not exist (fork checkouts have no
    ``tell-private``); pass ``None`` to ignore it entirely.
    """
    profiles = _load_dir(shipped_dir)
    if private_dir is not None and private_dir.is_dir():
        profiles.update(_load_dir(private_dir))
    return profiles


def get_profile(
    name: str,
    *,
    shipped_dir: Path = SHIPPED_PROFILES_DIR,
    private_dir: Path | None = PRIVATE_PROFILES_DIR,
) -> Profile:
    """One profile by name, or :class:`ProfileNotFoundError`."""
    profiles = load_profiles(shipped_dir, private_dir)
    try:
        return profiles[name]
    except KeyError:
        msg = f"no ICP profile named {name!r} (known: {sorted(profiles)})"
        raise ProfileNotFoundError(msg) from None


def default_profile() -> Profile:
    """The ``default`` profile: the weights and half-lives ``signals`` rows are stamped with."""
    return get_profile(DEFAULT_PROFILE_NAME)
