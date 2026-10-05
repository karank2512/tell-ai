"""Contract tests, generic over every registered collector (spec section 12).

Each ``CollectorSpec.response_model`` must parse the ``normal*.json`` and
``empty*.json`` fixtures and must reject ``drift.json`` with a ``ValidationError``
that names ``drift_missing_field``. Adding a source never edits this file.
"""

from __future__ import annotations

from pathlib import Path

import pydantic
import pytest

from tell.collectors.base import CollectorSpec, discover_collectors

SPECS: list[CollectorSpec] = discover_collectors()
NO_COLLECTORS = (
    "no collectors registered yet (tell/collectors/*.py with COLLECTOR); Phase 2 adds them"
)


def _spec_id(spec: CollectorSpec | None) -> str:
    return spec.source if spec is not None else "none"


def _parsing_fixtures(spec: CollectorSpec) -> list[Path]:
    files = sorted(spec.fixture_dir.glob("normal*.json")) + sorted(
        spec.fixture_dir.glob("empty*.json")
    )
    assert files, f"{spec.source}: no normal*.json / empty*.json under {spec.fixture_dir}"
    return files


@pytest.mark.parametrize("spec", SPECS or [None], ids=_spec_id)
def test_fixture_dir_is_under_tests_fixtures(spec: CollectorSpec | None) -> None:
    if spec is None:
        pytest.skip(NO_COLLECTORS)
    assert spec.fixture_dir.is_dir(), spec.fixture_dir
    assert spec.fixture_dir.name == spec.source
    assert spec.fixture_dir.parent.name == "fixtures"
    assert (spec.fixture_dir / "drift.json").is_file()


@pytest.mark.parametrize("spec", SPECS or [None], ids=_spec_id)
def test_normal_and_empty_fixtures_parse(spec: CollectorSpec | None) -> None:
    if spec is None:
        pytest.skip(NO_COLLECTORS)
    for path in _parsing_fixtures(spec):
        parsed = spec.response_model.model_validate_json(path.read_text(encoding="utf-8"))
        assert isinstance(parsed, spec.response_model), path.name


@pytest.mark.parametrize("spec", SPECS or [None], ids=_spec_id)
def test_drift_fixture_fails_naming_the_missing_field(spec: CollectorSpec | None) -> None:
    if spec is None:
        pytest.skip(NO_COLLECTORS)
    drift = (spec.fixture_dir / "drift.json").read_text(encoding="utf-8")
    with pytest.raises(pydantic.ValidationError) as excinfo:
        spec.response_model.model_validate_json(drift)
    assert spec.drift_missing_field in str(excinfo.value)
