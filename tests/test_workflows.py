"""The GitHub Actions workflows (SPEC sections 11 and 12; PLAN Phase 10).

Every workflow under ``.github/workflows`` is parsed with PyYAML and checked for the
shape the spec fixes (schedules, matrix, concurrency, failure policy, secrets wiring),
and every ``uv run tell ...`` line is resolved against the real typer app, sub-apps and
options included, so a renamed command or flag fails here rather than in a 02:17 cron.
No network, no database.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
from functools import cache
from pathlib import Path
from typing import Any

import pytest
import typer.main
import yaml

from tell.cli import Source, app
from tell.ops.deadman import SCHEDULED_SOURCES

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
EXPECTED_WORKFLOWS = {"ci.yml", "ingest.yml", "weekly.yml", "publish-mcp.yml"}
TEMPLATE_WORKFLOWS = {
    "tell-data-pages.yml": REPO_ROOT / "docs" / "deploy" / "tell-data-pages.yml",
    "tell-private-intake.yml": REPO_ROOT / "docs" / "deploy" / "tell-private-intake.yml",
}
"""Workflows copied into the other repos at cutover; parsed and checked like the local ones."""
ATS_VENDORS = ["greenhouse", "lever", "ashby", "smartrecruiters"]
GUARDED_WORKFLOWS = ("ingest.yml", "weekly.yml")
"""Scheduled workflows that skip cleanly (green) until the cutover secrets exist."""
ENABLED = "needs.preflight.outputs.enabled == 'true'"
PUBLISH_ENABLED = "needs.preflight.outputs.publish_enabled == 'true'"
UNGATED_JOBS = {("weekly.yml", "keepalive")}
"""Jobs that need no secret and must run before cutover (keepalive keeps the cron alive)."""
ACTION_PINS = {
    "actions/checkout": "v7",
    "astral-sh/setup-uv": "v10.2.0",  # setup-uv publishes no major tags since v8
    "actions/configure-pages": "v6",
    "actions/upload-pages-artifact": "v5",
    "actions/deploy-pages": "v5",
    "actions/upload-artifact": "v7",
    "actions/download-artifact": "v8",
    "pypa/gh-action-pypi-publish": "release/v1",
    "gitleaks/gitleaks-action": "v3",
}
"""The Node 24 majors every workflow pins (Node 20 actions stop running on hosted runners)."""

_EXPR = re.compile(r"\$\{\{.*?\}\}")
_SHELL_OPERATORS = {"||", "&&", "|", ";", ">", ">>", "2>&1", "&"}
_TELL_CALL = re.compile(r"\buv run tell\b(?P<rest>.*)")


# --- helpers ----------------------------------------------------------------------------


def _load(name: str) -> dict[Any, Any]:
    path = TEMPLATE_WORKFLOWS.get(name, WORKFLOWS_DIR / name)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"{name} is not a mapping"
    return data


def _workflow_paths() -> list[Path]:
    return [*sorted(WORKFLOWS_DIR.glob("*.yml")), *TEMPLATE_WORKFLOWS.values()]


def _on(workflow: dict[Any, Any]) -> dict[str, Any]:
    """The ``on:`` block (PyYAML reads the bare key ``on`` as boolean True)."""
    raw: dict[Any, Any] = workflow
    block = raw["on"] if "on" in raw else raw[True]
    assert isinstance(block, dict)
    return block


def _steps(workflow: dict[str, Any], job: str) -> list[dict[str, Any]]:
    steps = workflow["jobs"][job]["steps"]
    assert isinstance(steps, list)
    return steps


def _run_scripts(workflow: dict[str, Any]) -> list[tuple[str, str]]:
    """``(job, run script)`` for every ``run:`` step in the workflow."""
    return [
        (job_name, str(step["run"]))
        for job_name, job in workflow["jobs"].items()
        for step in job.get("steps", [])
        if "run" in step
    ]


def _index(steps: list[dict[str, Any]], needle: str) -> int:
    """Index of the first step whose ``run`` contains ``needle``."""
    for i, step in enumerate(steps):
        if needle in str(step.get("run", "")):
            return i
    pytest.fail(f"no step runs {needle!r}")


def _tell_invocations() -> list[tuple[str, list[str]]]:
    """Every ``uv run tell ...`` call in every workflow: (label, argv after ``tell``)."""
    calls: list[tuple[str, list[str]]] = []
    for path in _workflow_paths():
        workflow = _load(path.name)
        for job, script in _run_scripts(workflow):
            for line in script.splitlines():
                for match in _TELL_CALL.finditer(line):
                    rest = _EXPR.sub("EXPR", match.group("rest"))
                    argv: list[str] = []
                    for token in shlex.split(rest, comments=True):
                        if token in _SHELL_OPERATORS:
                            break
                        argv.append(token)
                    calls.append((f"{path.name}:{job}: tell {' '.join(argv)}", argv))
    return calls


@cache
def _root() -> Any:
    """The click command tree of ``tell`` (typer vendors click, so this is duck-typed)."""
    return typer.main.get_command(app)


def _is_group(command: Any) -> bool:
    return hasattr(command, "commands")


def _resolve(argv: list[str]) -> tuple[Any, list[str]]:
    """Walk ``argv`` through the command tree; return the command reached and the rest."""
    command = _root()
    rest = list(argv)
    while _is_group(command) and rest and not rest[0].startswith("-"):
        name = rest.pop(0)
        sub = command.commands.get(name)
        if sub is None:
            pytest.fail(
                f"`{command.name} {name}` is not a command; known: {sorted(command.commands)}"
            )
        command = sub
    if command is _root():
        pytest.fail("`tell` called without a command")
    if _is_group(command) and not command.invoke_without_command:
        pytest.fail(f"`{command.name}` needs a sub-command; known: {sorted(command.commands)}")
    return command, rest


def _option_names(command: Any) -> set[str]:
    names: set[str] = set()
    for param in command.params:
        if param.param_type_name == "option":
            names.update(param.opts)
            names.update(param.secondary_opts)
    return names


# --- every workflow ---------------------------------------------------------------------


def test_expected_workflows_exist_and_parse() -> None:
    present = {p.name for p in WORKFLOWS_DIR.glob("*.yml")}
    assert present >= EXPECTED_WORKFLOWS
    for name in present:
        workflow = _load(name)
        assert workflow["name"]
        assert workflow["jobs"]
        _on(workflow)


@pytest.mark.parametrize("name", sorted(EXPECTED_WORKFLOWS))
def test_every_job_has_a_timeout(name: str) -> None:
    for job_name, job in _load(name)["jobs"].items():
        assert isinstance(job.get("timeout-minutes"), int), f"{name}:{job_name} has no timeout"


@pytest.mark.parametrize("name", sorted(EXPECTED_WORKFLOWS))
def test_default_token_is_read_only(name: str) -> None:
    assert _load(name)["permissions"] == {"contents": "read"}


def test_no_dropped_vendor_anywhere() -> None:
    """ADR 0003: the dropped collectors are never scheduled or mentioned."""
    for path in _workflow_paths():
        text = path.read_text(encoding="utf-8").lower()
        assert "workday" not in text, path.name
        assert "oracle" not in text, path.name


def test_every_schedule_avoids_the_top_of_the_hour() -> None:
    """SPEC section 11: GitHub delays cron runs at :00, so no schedule here uses it."""
    schedules = 0
    for path in _workflow_paths():
        for entry in _on(_load(path.name)).get("schedule", []):
            minute = str(entry["cron"]).split()[0]
            assert minute.isdigit(), f"{path.name}: cron {entry['cron']!r}"
            assert int(minute) != 0, f"{path.name}: cron {entry['cron']!r} runs at :00"
            schedules += 1
    assert schedules >= 4  # ingest, weekly, ci audit, intake


def test_workflows_find_tell_invocations() -> None:
    labels = [label for label, _ in _tell_invocations()]
    assert len(labels) >= 12
    joined = "\n".join(labels)
    for needle in ("tell ingest", "tell resolve", "tell deadman", "tell weekly", "tell backtest"):
        assert needle in joined


@pytest.mark.parametrize(
    ("label", "argv"), _tell_invocations(), ids=[label for label, _ in _tell_invocations()]
)
def test_tell_invocation_exists_in_cli(label: str, argv: list[str]) -> None:
    command, rest = _resolve(argv)
    known = _option_names(command)
    for token in rest:
        if token.startswith("--"):
            flag = token.split("=", 1)[0]
            assert flag in known, f"{label}: {flag} is not an option of `{command.name}`"
    if command.name == "ingest" and "--source" in rest:
        value = rest[rest.index("--source") + 1]
        if value != "EXPR":
            choices = next(p.type.choices for p in command.params if p.name == "source")
            assert value in choices, label


# --- ingest.yml -------------------------------------------------------------------------


def test_ingest_triggers_and_concurrency() -> None:
    wf = _load("ingest.yml")
    on = _on(wf)
    # ADR 0029: a daily slot that always runs, plus extra slots gated on TELL_RUNS_PER_DAY.
    assert on["schedule"] == [{"cron": DAILY_SLOT_CRON}, {"cron": EXTRA_SLOTS_CRON}]
    assert "source" in on["workflow_dispatch"]["inputs"]
    assert on["workflow_dispatch"]["inputs"]["source"]["required"] is False
    assert wf["concurrency"] == {"group": "ingest", "cancel-in-progress": False}


def test_ingest_env_wiring() -> None:
    env = _load("ingest.yml")["env"]
    assert env["DATABASE_URL"] == "${{ secrets.DATABASE_URL }}"
    assert env["SEC_USER_AGENT"] == "${{ secrets.SEC_USER_AGENT }}"
    assert env["TELL_HTTP_USER_AGENT"] == "tell/0.1 (+https://github.com/${{ github.repository }})"
    assert env["TELL_DATA_REPO"] == "${{ vars.TELL_DATA_REPO }}"


def test_ingest_marks_manual_runs() -> None:
    """SPEC 13.4: a workflow_dispatch run is the intervention 'weeks unattended' counts from."""
    env = _load("ingest.yml")["env"]
    assert env["TELL_RUN_MANUAL"] == "${{ github.event_name == 'workflow_dispatch' }}"
    for name in ("weekly.yml", "ci.yml", "publish-mcp.yml"):
        assert "TELL_RUN_MANUAL" not in _load(name).get("env", {}), name


def test_ingest_ats_matrix() -> None:
    ats = _load("ingest.yml")["jobs"]["ats"]
    assert ats["strategy"]["matrix"]["vendor"] == ATS_VENDORS
    assert ats["strategy"]["fail-fast"] is False
    assert ats["timeout-minutes"] == 90
    assert set(ATS_VENDORS) <= {s.value for s in Source}
    runs = [str(s.get("run", "")) for s in ats["steps"]]
    assert "uv run tell ingest --source ${{ matrix.vendor }}" in runs
    summary = next(r for r in runs if "tell runlog --source ${{ matrix.vendor }}" in r)
    assert "$GITHUB_STEP_SUMMARY" in summary


def test_ingest_discovery_job_feeds_ats() -> None:
    jobs = _load("ingest.yml")["jobs"]
    discover = jobs["discover"]
    assert discover["timeout-minutes"] == 30
    step = next(s for s in discover["steps"] if "tell discover batch" in str(s.get("run")))
    batch = str(step["run"])
    # 250 domains at the measured 4.74 s a domain (6 workers, one 4 rps budget, deeper scan
    # included; 4.47 s before it) is about 20 minutes: inside the 22-minute wall-clock
    # budget, which is inside the timeout.
    assert "--limit 250" in batch
    assert "--rps 4" in batch
    assert step["env"]["TELL_DISCOVERY_MAX_MINUTES"] == "22"
    assert float(step["env"]["TELL_DISCOVERY_MAX_MINUTES"]) < discover["timeout-minutes"]
    assert float(step["env"]["TELL_DISCOVERY_MAX_MINUTES"]) > 250 * 4.74 / 60
    assert jobs["ats"]["needs"] == ["preflight", "discover"]
    # A failed discovery pass never blocks polling.
    assert "cancelled()" in jobs["ats"]["if"]


def test_ingest_filings_edgar_then_news() -> None:
    filings = _load("ingest.yml")["jobs"]["filings"]
    # EDGAR + news took 29.9 of 30 minutes on the second production run; news keeps its
    # own 25-minute run budget inside the 45, and the optional issuer-first step (ADR 0035)
    # has 15 more.
    assert filings["timeout-minutes"] == 60
    steps = filings["steps"]
    assert _index(steps, "tell ingest --source edgar") < _index(steps, "tell ingest --source news")


def test_ingest_issuer_discovery_step_is_off_by_default() -> None:
    """ADR 0035: `tell discover issuers --apply` runs right after a successful EDGAR ingest,
    only when the Actions variable TELL_ISSUER_DISCOVERY is "on" (unset means off)."""
    steps = _load("ingest.yml")["jobs"]["filings"]["steps"]
    edgar = _index(steps, "tell ingest --source edgar")
    step_index = _index(steps, "tell discover issuers")
    assert step_index == edgar + 1
    assert step_index < _index(steps, "tell ingest --source news")
    step = steps[step_index]
    assert str(step["run"]).strip() == "uv run tell discover issuers --apply"
    condition = " ".join(str(step["if"]).split())
    assert "success()" in condition
    assert "vars.TELL_ISSUER_DISCOVERY == 'on'" in condition
    assert "inputs.source == 'edgar'" in condition
    assert step["timeout-minutes"] == 15
    # News never depends on the discovery step.
    news = steps[_index(steps, "tell ingest --source news")]
    assert "!cancelled()" in news["if"]
    summary = steps[_index(steps, "tell runlog --source issuer_discovery")]
    assert summary["env"]["ISSUER_DISCOVERY"] == "${{ vars.TELL_ISSUER_DISCOVERY }}"
    # The deploy guide documents the switch.
    guide = (REPO_ROOT / "docs" / "deploy" / "github-actions.md").read_text(encoding="utf-8")
    assert "TELL_ISSUER_DISCOVERY" in guide


def test_ingest_scheduled_sources_match_deadman() -> None:
    """The dead-man checks exactly the sources ingest.yml schedules."""
    assert set(SCHEDULED_SOURCES) == {*ATS_VENDORS, "edgar", "news"}


def test_resolve_score_publish_order_and_policy() -> None:
    wf = _load("ingest.yml")
    job = wf["jobs"]["resolve_score_publish"]
    assert job["needs"] == ["preflight", "ats", "filings"]
    assert job["if"] == f"${{{{ always() && {ENABLED} }}}}"
    assert job["timeout-minutes"] == 45
    steps = job["steps"]
    order = [
        _index(steps, "uv run tell resolve"),
        _index(steps, "uv run tell score --all-profiles"),
        _index(steps, "uv run tell publish --out tell-data"),
        _index(steps, "uv run tell prune --out tell-data"),
        _index(steps, "git push"),
        _index(steps, "uv run tell notify --slack"),
        _index(steps, "uv run tell deadman"),
    ]
    assert order == sorted(order)
    assert len(set(order)) == len(order)
    notify = steps[_index(steps, "uv run tell notify --slack")]
    assert notify["env"]["SLACK_WEBHOOK_URL"] == "${{ secrets.SLACK_WEBHOOK_URL }}"
    # ADR 0036: nudges need only the database, so the step is not gated on tell-data, and
    # it reads the per-watchlist webhooks from the read-only tell-private checkout.
    assert "if" not in notify
    assert str(notify["run"]).strip() == "uv run tell notify --slack --private-dir tell-private"
    # The dead-man must be the last step so it judges the whole run.
    assert _index(steps, "uv run tell deadman") == len(steps) - 1


def test_resolve_score_publish_tell_data_checkout_and_commit() -> None:
    steps = _load("ingest.yml")["jobs"]["resolve_score_publish"]["steps"]
    checkouts = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert len(checkouts) == 3
    data = checkouts[1]["with"]
    assert data == {
        "repository": "${{ vars.TELL_DATA_REPO }}",
        "path": "tell-data",
        "token": "${{ secrets.TELL_DATA_PUSH_TOKEN }}",
    }
    # ADR 0036: tell-private is read with its own token, only when that secret exists.
    private = checkouts[2]
    assert private["if"] == "needs.preflight.outputs.private_enabled == 'true'"
    assert private["with"] == {
        "repository": "karank2512/tell-private",
        "path": "tell-private",
        "token": "${{ secrets.TELL_PRIVATE_READ_TOKEN }}",
    }
    commit = steps[_index(steps, "git push")]
    assert commit["working-directory"] == "tell-data"
    script = str(commit["run"])
    assert "git config user.name tell-bot" in script
    assert "git config user.email tell-bot@users.noreply.github.com" in script
    assert 'git commit -m "ingest $(date -u +%FT%TZ)" || echo "nothing to commit"' in script


def test_ingest_dispatch_source_skips_other_legs() -> None:
    jobs = _load("ingest.yml")["jobs"]
    assert "inputs.source" in jobs["discover"]["if"]
    assert "inputs.source" in jobs["ats"]["if"]
    assert "inputs.source == matrix.vendor" in jobs["ats"]["env"]["RUN_LEG"]
    assert "inputs.source" in jobs["filings"]["if"]
    filings = jobs["filings"]["steps"]
    assert "'edgar'" in filings[_index(filings, "--source edgar")]["if"]
    assert "'news'" in filings[_index(filings, "--source news")]["if"]


# --- preflight guard (ingest.yml, weekly.yml) -------------------------------------------

DAILY_SLOT_CRON = "17 15 * * *"
EXTRA_SLOTS_CRON = "17 2,8,20 * * *"


def _run_ingest_preflight(tmp_path: Path, **env: str) -> tuple[dict[str, str], str]:
    """Execute ingest.yml's preflight script with bash, the way the runner does."""
    [step] = _load("ingest.yml")["jobs"]["preflight"]["steps"]
    output = tmp_path / "output"
    summary = tmp_path / "summary"
    output.write_text("")
    summary.write_text("")
    base = {
        "HAS_DATABASE_URL": "true",
        "HAS_TELL_DATA_PUSH_TOKEN": "true",
        "HAS_TELL_DATA_REPO": "true",
        "HAS_TELL_PRIVATE_READ_TOKEN": "",
        "EVENT_NAME": "schedule",
        "FIRED_CRON": DAILY_SLOT_CRON,
        "RUNS_PER_DAY": "",
        "EXTRA_SLOTS_CRON": str(step["env"]["EXTRA_SLOTS_CRON"]),
    }
    run_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GITHUB_OUTPUT": str(output),
        "GITHUB_STEP_SUMMARY": str(summary),
        **base,
        **env,
    }
    subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", str(step["run"])], env=run_env, check=True
    )
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines() if line)
    return outputs, summary.read_text()


@pytest.mark.parametrize(
    ("env", "enabled", "publish", "private", "summary_part"),
    [
        ({}, "true", "true", "false", ""),  # daily slot always runs
        ({"FIRED_CRON": EXTRA_SLOTS_CRON}, "false", "false", "false", "extra slot"),
        (
            {"FIRED_CRON": EXTRA_SLOTS_CRON, "RUNS_PER_DAY": "1"},
            "false",
            "false",
            "false",
            "extra slot",
        ),
        ({"FIRED_CRON": EXTRA_SLOTS_CRON, "RUNS_PER_DAY": "4"}, "true", "true", "false", ""),
        ({"EVENT_NAME": "workflow_dispatch", "FIRED_CRON": ""}, "true", "true", "false", ""),
        ({"HAS_DATABASE_URL": "false"}, "false", "false", "false", "DATABASE_URL secret not set"),
        (
            {"HAS_TELL_DATA_PUSH_TOKEN": "false"},
            "true",
            "false",
            "false",
            "tell-data publish skipped",
        ),
        # ADR 0036: the tell-private read token turns on nudges and digest routing.
        ({"HAS_TELL_PRIVATE_READ_TOKEN": "true"}, "true", "true", "true", ""),
        (
            {"HAS_TELL_PRIVATE_READ_TOKEN": "true", "HAS_DATABASE_URL": "false"},
            "false",
            "false",
            "false",
            "DATABASE_URL secret not set",
        ),
    ],
)
def test_ingest_preflight_slot_gate(
    tmp_path: Path,
    env: dict[str, str],
    enabled: str,
    publish: str,
    private: str,
    summary_part: str,
) -> None:
    """ADR 0029: extra slots run only with TELL_RUNS_PER_DAY=4; manual runs always run."""
    outputs, summary = _run_ingest_preflight(tmp_path, **env)
    assert outputs == {"enabled": enabled, "publish_enabled": publish, "private_enabled": private}
    if summary_part:
        assert summary_part in summary
    else:
        assert summary == ""


def test_extra_slots_cron_matches_the_schedule_line() -> None:
    """github.event.schedule is compared verbatim, so the two strings must stay identical."""
    wf = _load("ingest.yml")
    [step] = wf["jobs"]["preflight"]["steps"]
    crons = [entry["cron"] for entry in _on(wf)["schedule"]]
    assert step["env"]["EXTRA_SLOTS_CRON"] in crons
    assert DAILY_SLOT_CRON in crons


@pytest.mark.parametrize("name", GUARDED_WORKFLOWS)
def test_preflight_job_turns_secrets_into_outputs(name: str) -> None:
    jobs = _load(name)["jobs"]
    assert next(iter(jobs)) == "preflight"
    preflight = jobs["preflight"]
    assert preflight["runs-on"] == "ubuntu-latest"
    assert preflight["timeout-minutes"] == 5
    assert "needs" not in preflight
    assert "if" not in preflight
    outputs = {
        "enabled": "${{ steps.check.outputs.enabled }}",
        "publish_enabled": "${{ steps.check.outputs.publish_enabled }}",
    }
    if name == "ingest.yml":  # ADR 0036: the tell-private read token gates nudges
        outputs["private_enabled"] = "${{ steps.check.outputs.private_enabled }}"
    assert preflight["outputs"] == outputs
    [step] = preflight["steps"]
    assert step["id"] == "check"
    secret_flags = {
        "HAS_DATABASE_URL": "${{ secrets.DATABASE_URL != '' }}",
        "HAS_TELL_DATA_PUSH_TOKEN": "${{ secrets.TELL_DATA_PUSH_TOKEN != '' }}",
        "HAS_TELL_DATA_REPO": "${{ vars.TELL_DATA_REPO != '' }}",
    }
    if name == "ingest.yml":  # ADR 0029: the slot gate reads the event and the variable
        secret_flags |= {
            "HAS_TELL_PRIVATE_READ_TOKEN": "${{ secrets.TELL_PRIVATE_READ_TOKEN != '' }}",
            "EVENT_NAME": "${{ github.event_name }}",
            "FIRED_CRON": "${{ github.event.schedule }}",
            "RUNS_PER_DAY": "${{ vars.TELL_RUNS_PER_DAY }}",
            "EXTRA_SLOTS_CRON": EXTRA_SLOTS_CRON,
        }
    assert step["env"] == secret_flags
    script = str(step["run"])
    assert 'echo "enabled=$enabled" >> "$GITHUB_OUTPUT"' in script
    assert 'echo "publish_enabled=$publish_enabled" >> "$GITHUB_OUTPUT"' in script
    kind = name.removesuffix(".yml")
    notice = f"{kind} skipped: DATABASE_URL secret not set; see docs/CUTOVER_CHECKLIST.md"
    assert notice in script
    assert '>> "$GITHUB_STEP_SUMMARY"' in script
    assert "exit 1" not in script  # a missing secret skips; it never turns the run red
    assert (REPO_ROOT / "docs" / "CUTOVER_CHECKLIST.md").is_file()


@pytest.mark.parametrize("name", GUARDED_WORKFLOWS)
def test_every_job_needs_preflight_and_is_gated_on_it(name: str) -> None:
    jobs = _load(name)["jobs"]
    gated = [j for j in jobs if j != "preflight" and (name, j) not in UNGATED_JOBS]
    assert gated
    for job_name in gated:
        job = jobs[job_name]
        needs = job["needs"]
        assert isinstance(needs, list), f"{name}:{job_name}"
        assert needs[0] == "preflight", f"{name}:{job_name} needs {needs}"
        assert ENABLED in str(job["if"]), f"{name}:{job_name} if: {job['if']}"


def test_keepalive_runs_before_cutover() -> None:
    """ADR 0026: keepalive needs no secret and keeps the cron enabled until the secrets exist."""
    keepalive = _load("weekly.yml")["jobs"]["keepalive"]
    assert "needs" not in keepalive
    assert "if" not in keepalive


def test_resolve_score_publish_keeps_always_with_the_guard() -> None:
    """One broken vendor never blocks the digest, but nothing runs before cutover."""
    condition = str(_load("ingest.yml")["jobs"]["resolve_score_publish"]["if"])
    assert "always()" in condition
    assert f"&& {ENABLED}" in condition


@pytest.mark.parametrize(
    ("name", "job", "gated", "ungated"),
    [
        (
            "ingest.yml",
            "resolve_score_publish",
            ["tell publish --out", "tell prune --out", "git push"],
            # ADR 0036: notify needs only the database (nudges), so it is no longer gated.
            ["tell resolve", "tell score", "tell notify --slack", "tell deadman"],
        ),
        (
            "weekly.yml",
            "weekly",
            [
                "tell weekly --week",
                "tell backtest refresh",
                "git push",
                "$SLACK_WEEKLY_WEBHOOK_URL",
            ],
            ["tell runlog --week"],
        ),
    ],
)
def test_tell_data_steps_need_publish_config(
    name: str, job: str, gated: list[str], ungated: list[str]
) -> None:
    """Without TELL_DATA_PUSH_TOKEN / TELL_DATA_REPO nothing touches tell-data; prune in
    particular deletes what it archives, so it never runs without the checkout."""
    steps = _steps(_load(name), job)
    data_checkout = next(s for s in steps if s.get("with", {}).get("path") == "tell-data")
    assert PUBLISH_ENABLED in str(data_checkout["if"])
    for needle in gated:
        assert PUBLISH_ENABLED in str(steps[_index(steps, needle)].get("if", "")), needle
    for needle in ungated:
        assert "publish_enabled" not in str(steps[_index(steps, needle)].get("if", "")), needle


def test_no_secrets_in_any_if() -> None:
    """Actions cannot evaluate ``secrets`` in a job- or step-level ``if:``; the preflight
    outputs are the only way to branch on them."""
    for path in _workflow_paths():
        for job_name, job in _load(path.name)["jobs"].items():
            assert "secrets." not in str(job.get("if", "")), f"{path.name}:{job_name}"
            for step in job.get("steps", []):
                assert "secrets." not in str(step.get("if", "")), f"{path.name}:{job_name}"


@pytest.mark.parametrize("path", _workflow_paths(), ids=lambda p: p.name)
def test_action_majors_are_pinned(path: Path) -> None:
    uses = [
        str(step["uses"])
        for job in _load(path.name)["jobs"].values()
        for step in job.get("steps", [])
        if "uses" in step
    ]
    assert uses, path.name
    for ref in uses:
        action, _, version = ref.partition("@")
        assert action in ACTION_PINS, f"{path.name}: unreviewed action {ref}"
        assert version == ACTION_PINS[action], (
            f"{path.name}: {ref}, expected @{ACTION_PINS[action]}"
        )


# --- weekly.yml -------------------------------------------------------------------------


def test_weekly_schedule_and_steps() -> None:
    wf = _load("weekly.yml")
    on = _on(wf)
    assert on["schedule"] == [{"cron": "23 6 * * 1"}]
    assert "workflow_dispatch" in on
    steps = _steps(wf, "weekly")
    order = [
        _index(steps, "uv run tell weekly --week"),
        _index(steps, "uv run tell backtest"),
        _index(steps, "git push"),
    ]
    assert order == sorted(order)
    slack = steps[-1]
    assert "SLACK_WEEKLY_WEBHOOK_URL" in str(slack["if"])
    assert wf["jobs"]["weekly"]["env"]["SLACK_WEEKLY_WEBHOOK_URL"] == (
        "${{ secrets.SLACK_WEEKLY_WEBHOOK_URL }}"
    )


def test_weekly_never_commits_to_the_code_repo() -> None:
    """ADR 0025: reports go to tell-data; only tell-bot commits, only there."""
    for name in ("weekly.yml", "ingest.yml"):
        for job, script in _run_scripts(_load(name)):
            if "git push" in script or "git commit" in script:
                steps = _steps(_load(name), job)
                step = steps[_index(steps, "git push")]
                assert step.get("working-directory") == "tell-data", f"{name}:{job}"
    steps = _steps(_load("weekly.yml"), "weekly")
    backtest = str(steps[_index(steps, "uv run tell backtest")]["run"])
    assert "--out tell-data/" in backtest


# --- ci.yml -----------------------------------------------------------------------------


def test_ci_service_container_and_gates() -> None:
    wf = _load("ci.yml")
    on = _on(wf)
    assert "pull_request" in on
    assert on["push"]["branches"] == ["main"]
    test = wf["jobs"]["test"]
    assert test["services"]["postgres"]["image"] == "postgres:16"
    steps = test["steps"]
    for needle in (
        "uv sync --frozen",
        "uv run ruff check .",
        "uv run ruff format --check .",
        "uv run mypy --strict tell",
        "uv run alembic upgrade head",
        "--cov-fail-under=80",
        "scripts/check_coverage.py",
    ):
        _index(steps, needle)
    assert _index(steps, "alembic upgrade head") < _index(steps, "--cov-fail-under=80")


def test_ci_security_jobs() -> None:
    wf = _load("ci.yml")
    assert _on(wf)["schedule"] == [{"cron": "13 6 * * 1"}]
    scan = wf["jobs"]["secret-scan"]
    assert any(
        str(s.get("uses", "")).startswith("gitleaks/gitleaks-action@") for s in scan["steps"]
    )
    assert "if" not in scan  # every trigger, PRs included
    audit = wf["jobs"]["audit"]
    assert "schedule" in audit["if"]
    _index(audit["steps"], "pip-audit")


# --- publish-mcp.yml --------------------------------------------------------------------


def test_publish_mcp_trusted_publishing() -> None:
    wf = _load("publish-mcp.yml")
    assert _on(wf) == {"push": {"tags": ["tell-mcp-v*"]}}
    build = wf["jobs"]["build"]
    _index(build["steps"], "uv build --package tell-mcp")
    publish = wf["jobs"]["publish"]
    assert publish["permissions"] == {"id-token": "write"}
    assert publish["environment"]["name"] == "pypi"
    assert any(
        str(s.get("uses", "")).startswith("pypa/gh-action-pypi-publish@") for s in publish["steps"]
    )
    # No long-lived token: trusted publishing only.
    assert "password" not in (WORKFLOWS_DIR / "publish-mcp.yml").read_text(encoding="utf-8")


def test_publish_mcp_registry_listing() -> None:
    """docs/mcp.md "Listing in the MCP Registry": the listing is its own job after the PyPI
    upload, with GitHub OIDC and no secret, so a registry failure cannot fail the PyPI publish."""
    jobs = _load("publish-mcp.yml")["jobs"]
    assert list(jobs) == ["build", "publish", "registry"]
    # The PyPI jobs never wait for the registry; the registry reads the PyPI release, so it
    # waits for the upload and, with the default `if`, never runs after a failed one.
    assert "needs" not in jobs["build"]
    assert jobs["publish"]["needs"] == ["build"]
    registry = jobs["registry"]
    assert registry["needs"] == ["publish"]
    assert "if" not in registry
    # GitHub OIDC and a read-only checkout, nothing else. No environment: only the PyPI upload
    # runs in `pypi`, so a token minted here carries no `pypi` claim for the trusted publisher.
    assert registry["permissions"] == {"contents": "read", "id-token": "write"}
    assert "environment" not in registry
    assert jobs["publish"]["environment"]["name"] == "pypi"
    steps = registry["steps"]
    install = _index(steps, "releases/latest/download/mcp-publisher_")
    login = _index(steps, "mcp-publisher login")
    publish = _index(steps, "mcp-publisher publish")
    assert install < login < publish
    assert str(steps[login]["run"]).strip() == "./mcp-publisher login github-oidc"
    published = "./mcp-publisher publish packages/tell-mcp/server.json"
    assert str(steps[publish]["run"]).strip() == published
    assert (REPO_ROOT / "packages" / "tell-mcp" / "server.json").is_file()
    text = (WORKFLOWS_DIR / "publish-mcp.yml").read_text(encoding="utf-8")
    assert "secrets." not in text  # OIDC login: no PAT, no private key
    assert "--token" not in text


def test_weekly_keepalive_reenables_scheduled_workflows() -> None:
    """ADR 0026: the 60-day inactivity timer is reset through the API, never by a commit."""
    keepalive = _load("weekly.yml")["jobs"]["keepalive"]
    assert keepalive["permissions"] == {"actions": "write"}
    script = str(keepalive["steps"][0]["run"])
    assert "/enable" in script
    for name in ("ingest.yml", "weekly.yml"):
        assert name in script
    assert "git " not in script


# --- tell-data Pages workflow (copied into tell-data at cutover) --------------------------


def test_tell_data_pages_workflow() -> None:
    path = REPO_ROOT / "docs" / "deploy" / "tell-data-pages.yml"
    wf = yaml.safe_load(path.read_text(encoding="utf-8"))
    on = _on(wf)
    assert on["push"]["branches"] == ["main"]
    assert "workflow_dispatch" in on
    assert wf["permissions"] == {"contents": "read", "pages": "write", "id-token": "write"}
    steps = wf["jobs"]["deploy"]["steps"]
    upload = next(s for s in steps if str(s.get("uses", "")).startswith("actions/upload-pages-"))
    assert upload["with"]["path"] == "site"
    assert any(str(s.get("uses", "")).startswith("actions/deploy-pages@") for s in steps)


# --- tell-private intake workflow (copied into tell-private at cutover) ------------------


def test_tell_private_intake_workflow() -> None:
    """SPEC 15.6: the nightly reader runs where logs are private and commits only there."""
    wf = _load("tell-private-intake.yml")
    on = _on(wf)
    assert on["schedule"] == [{"cron": "41 3 * * *"}]
    assert "workflow_dispatch" in on
    assert wf["permissions"] == {"contents": "write"}
    assert wf["concurrency"] == {"group": "intake", "cancel-in-progress": False}
    assert wf["env"]["DATABASE_URL"] == "${{ secrets.DATABASE_URL }}"
    assert wf["env"]["TELL_INTAKE_CSV_URL"] == "${{ secrets.TELL_INTAKE_CSV_URL }}"
    job = wf["jobs"]["sync"]
    assert isinstance(job["timeout-minutes"], int)
    steps = job["steps"]
    checkouts = [s for s in steps if str(s.get("uses", "")).startswith("actions/checkout@")]
    assert [c["with"]["path"] for c in checkouts] == ["tell-private", "tell"]
    assert checkouts[1]["with"]["repository"] == "karank2512/tell-ai"
    sync = steps[_index(steps, "uv run tell intake sync")]
    assert sync["working-directory"] == "tell"
    assert "--private-dir ../tell-private" in str(sync["run"])
    commit = steps[_index(steps, "git push")]
    assert commit["working-directory"] == "tell-private"
    assert "git add watchlists" in str(commit["run"])
    assert _index(steps, "uv run tell intake sync") < _index(steps, "git push")
    # ADR 0039: the feedback reader runs in the same job, only when its secret exists.
    assert wf["env"]["TELL_FEEDBACK_CSV_URL"] == "${{ secrets.TELL_FEEDBACK_CSV_URL }}"
    feedback = steps[_index(steps, "uv run tell intake feedback-sync")]
    assert feedback["working-directory"] == "tell"
    assert feedback["if"] == "env.TELL_FEEDBACK_CSV_URL != ''"
    assert _index(steps, "uv run tell intake sync") < _index(
        steps, "uv run tell intake feedback-sync"
    )
    text = TEMPLATE_WORKFLOWS["tell-private-intake.yml"].read_text(encoding="utf-8")
    assert "tell-data" not in text  # nothing public is written by this job
