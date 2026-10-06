"""ICP profiles: the spec YAML shape, shipped defaults, private overrides, family weights."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from tell.models import SIGNAL_TYPES
from tell.signals.profiles import (
    DEFAULT_ALERT_THRESHOLD,
    PRIVATE_PROFILES_DIR,
    SHIPPED_PROFILES_DIR,
    Profile,
    ProfileNotFoundError,
    default_profile,
    get_profile,
    load_profile_file,
    load_profiles,
)

SPEC_YAML = """
name: sells-to-sales-teams
target_families: [sales, gtm_ops, customer]   # weight 1.0 on hiring_velocity
other_families_weight: 0.3
weights: { funding: 4.0, leadership: 2.0, launch: 1.5, expansion: 1.0, tech_adoption: 0 }
half_life_days: { hiring_velocity: 30, funding: 90, leadership: 60, launch: 45, expansion: 60, tech_adoption: 90 }
tech_keywords: []                               # devtools profiles list the keywords that count
size_band: { min_open_postings: 3, max_open_postings: 400 }
"""


@dataclass(frozen=True)
class Sig:
    signal_type: str
    role_family: str | None = None


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# --- shipped profiles ------------------------------------------------------------------------


def test_shipped_default_matches_the_spec_table() -> None:
    profiles = load_profiles(private_dir=None)
    assert set(profiles) == {"default", "job-seeker"}
    default = profiles["default"]
    assert default.target_families == ["sales", "gtm_ops", "customer"]
    assert default.other_families_weight == 0.3
    assert default.weights.model_dump() == {
        "funding": 4.0,
        "leadership": 2.0,
        "launch": 1.5,
        "expansion": 1.0,
        "tech_adoption": 0.0,
    }
    assert default.half_life_days.model_dump() == {
        "hiring_velocity": 30,
        "funding": 90,
        "leadership": 60,
        "launch": 45,
        "expansion": 60,
        "tech_adoption": 90,
    }
    assert default.tech_keywords == []
    assert default.size_band.model_dump() == {"min_open_postings": 3, "max_open_postings": 400}
    assert default.alert_threshold == DEFAULT_ALERT_THRESHOLD == 6.0
    assert default_profile() == default or PRIVATE_PROFILES_DIR.is_dir()


def test_shipped_job_seeker_targets_a_graduates_families() -> None:
    profile = load_profiles(private_dir=None)["job-seeker"]
    assert profile.target_families == ["engineering", "ml_ai", "data"]
    assert profile.weights.funding == 2.0
    assert profile.weights.launch == 1.0
    assert profile.size_band.min_open_postings == 3
    assert profile.size_band.max_open_postings == 2000
    assert profile.half_life_days.longest == 90
    assert all(profile.weight_for(Sig(t), keyword="x") > 0 for t in SIGNAL_TYPES)


def test_shipped_files_are_named_after_their_profile() -> None:
    for path in SHIPPED_PROFILES_DIR.glob("*.yaml"):
        assert load_profile_file(path).name == path.stem


# --- the spec YAML and round trips ---------------------------------------------------------


def test_spec_yaml_parses_verbatim(tmp_path: Path) -> None:
    profile = load_profile_file(_write(tmp_path / "p.yaml", SPEC_YAML))
    assert profile.name == "sells-to-sales-teams"
    assert profile.alert_threshold == 6.0  # the one key the spec leaves to prose
    assert profile.half_life_days["funding"] == 90
    assert profile.weights["funding"] == 4.0


def test_yaml_round_trip(tmp_path: Path) -> None:
    original = load_profile_file(_write(tmp_path / "p.yaml", SPEC_YAML))
    text = original.to_yaml()
    assert list(yaml.safe_load(text)) == [
        "name",
        "target_families",
        "other_families_weight",
        "weights",
        "half_life_days",
        "tech_keywords",
        "size_band",
        "alert_threshold",
    ]
    again = load_profile_file(_write(tmp_path / "again.yaml", text))
    assert again == original
    assert again.model_dump() == original.model_dump()


# --- private overrides -------------------------------------------------------------------------


def test_private_directory_overrides_by_name_and_adds_profiles(tmp_path: Path) -> None:
    shipped = load_profiles(private_dir=None)
    private = tmp_path / "profiles"
    private.mkdir()
    override = shipped["default"].model_copy(
        update={"weights": shipped["default"].weights.model_copy(update={"funding": 5.5})}
    )
    _write(private / "anything.yaml", override.to_yaml())
    partner = shipped["default"].model_copy(update={"name": "partner-x", "tech_keywords": ["Rust"]})
    _write(private / "partner-x.yaml", partner.to_yaml())
    _write(private / "notes.txt", "ignored: not yaml")

    merged = load_profiles(private_dir=private)
    assert set(merged) == {"default", "job-seeker", "partner-x"}
    assert merged["default"].weights.funding == 5.5
    assert merged["job-seeker"] == shipped["job-seeker"]
    assert merged["partner-x"].tech_keywords == ["rust"]
    assert get_profile("partner-x", private_dir=private) == merged["partner-x"]
    assert load_profiles(private_dir=tmp_path / "missing")["default"].weights.funding == 4.0


def test_duplicate_names_in_one_directory_fail(tmp_path: Path) -> None:
    _write(tmp_path / "a.yaml", SPEC_YAML)
    _write(tmp_path / "b.yaml", SPEC_YAML)
    with pytest.raises(ValueError, match="duplicate profile name"):
        load_profiles(shipped_dir=tmp_path, private_dir=None)


def test_unknown_profile_raises(tmp_path: Path) -> None:
    with pytest.raises(ProfileNotFoundError, match="no ICP profile named 'nope'"):
        get_profile("nope", private_dir=tmp_path)


# --- validation -------------------------------------------------------------------------------


def test_validation_errors_name_the_file(tmp_path: Path) -> None:
    bad = SPEC_YAML.replace("[sales, gtm_ops, customer]", "[sales, plumbing]")
    with pytest.raises(ValueError, match=r"(?s)bad\.yaml.*plumbing"):
        load_profile_file(_write(tmp_path / "bad.yaml", bad))
    with pytest.raises(ValueError, match="must be a YAML mapping"):
        load_profile_file(_write(tmp_path / "list.yaml", "- a\n- b\n"))
    with pytest.raises(ValueError, match=r"extra_forbidden|Extra inputs"):
        load_profile_file(_write(tmp_path / "extra.yaml", SPEC_YAML + "colour: blue\n"))
    inverted = SPEC_YAML.replace("max_open_postings: 400", "max_open_postings: 2")
    with pytest.raises(ValueError, match=r"size_band\.max_open_postings"):
        load_profile_file(_write(tmp_path / "band.yaml", inverted))
    negative = SPEC_YAML.replace("funding: 4.0", "funding: -1")
    with pytest.raises(ValueError, match="greater than or equal to 0"):
        load_profile_file(_write(tmp_path / "neg.yaml", negative))
    missing = SPEC_YAML.replace("funding: 90, ", "")
    with pytest.raises(ValueError, match="funding"):
        load_profile_file(_write(tmp_path / "missing.yaml", missing))


def test_indexers_reject_unknown_types() -> None:
    profile = load_profiles(private_dir=None)["default"]
    with pytest.raises(KeyError):
        profile.half_life_days["hiring"]
    with pytest.raises(KeyError):
        profile.weights["hiring_velocity"]  # family-aware, not a flat weight
    with pytest.raises(KeyError):
        profile.half_life_for("nope")
    assert profile.half_life_for("launch") == 45
    assert [profile.half_life_days[t] for t in SIGNAL_TYPES] == [30, 90, 60, 45, 60, 90]


# --- weight_for -----------------------------------------------------------------------------


def test_weight_for_hiring_velocity_is_family_aware() -> None:
    profile = load_profiles(private_dir=None)["default"]
    assert profile.weight_for(Sig("hiring_velocity", "sales")) == 1.0
    assert profile.weight_for(Sig("hiring_velocity", "gtm_ops")) == 1.0
    assert profile.weight_for(Sig("hiring_velocity", "customer")) == 1.0
    assert profile.weight_for(Sig("hiring_velocity", "engineering")) == 0.3
    assert profile.weight_for(Sig("hiring_velocity", None)) == 0.3
    assert profile.weight_for(Sig("funding")) == 4.0
    assert profile.weight_for(Sig("leadership")) == 2.0
    assert profile.weight_for(Sig("launch")) == 1.5
    assert profile.weight_for(Sig("expansion")) == 1.0
    assert profile.weight_for(Sig("not_a_signal")) == 0.0


def test_weight_for_tech_adoption_depends_on_the_keyword_list() -> None:
    default = load_profiles(private_dir=None)["default"]
    assert default.weight_for(Sig("tech_adoption"), keyword="kubernetes") == 0.0  # weight 0
    devtools = default.model_copy(
        update={
            "name": "devtools",
            "tech_keywords": ["Kubernetes", " terraform "],
            "weights": default.weights.model_copy(update={"tech_adoption": 2.0}),
        }
    )
    devtools = Profile.model_validate(devtools.model_dump())  # re-run the validators
    assert devtools.tech_keywords == ["kubernetes", "terraform"]
    assert devtools.weight_for(Sig("tech_adoption"), keyword="Kubernetes") == 2.0
    assert devtools.weight_for(Sig("tech_adoption"), keyword="terraform") == 2.0
    assert devtools.weight_for(Sig("tech_adoption"), keyword="rust") == 0.0
    assert devtools.weight_for(Sig("tech_adoption")) == 0.0  # keyword unknown, list non-empty
    any_tool = devtools.model_copy(update={"tech_keywords": []})
    assert any_tool.weight_for(Sig("tech_adoption"), keyword="rust") == 2.0
    assert any_tool.weight_for(Sig("tech_adoption")) == 2.0
