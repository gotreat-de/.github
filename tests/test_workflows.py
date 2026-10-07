"""The rules the shared workflows have to keep - each of them invisible in a caller's diff.

``deploy.yml`` holds the Dokploy API key and must run the deploy script of its own ref, never code of the
calling repo. These tests turn a slip into a red ``check``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
DEPLOY = WORKFLOWS / "deploy.yml"
ALL_WORKFLOWS = sorted(WORKFLOWS.glob("*.yml"))
# The workflows other repos call; ci.yml is this repo's own.
SHARED_WORKFLOWS = [DEPLOY]
PINNED_ACTION = re.compile(r"^\s+(- )?uses: [\w.-]+/[\w.-]+@[0-9a-f]{40} # v\d+\.\d+\.\d+$")
RUN_BLOCK = re.compile(r"^(?P<indent>\s*)(- )?run: \|$")


def _lines(workflow: Path) -> list[str]:
    return workflow.read_text().splitlines()


def _run_block_lines(workflow: Path) -> list[str]:
    script: list[str] = []
    block_indent: int | None = None
    for line in _lines(workflow):
        if block_indent is not None and (not line.strip() or len(line) - len(line.lstrip()) > block_indent):
            script.append(line)
            continue
        match = RUN_BLOCK.match(line)
        block_indent = len(match["indent"]) if match else None
    return script


def _ids(workflows: list[Path]) -> list[str]:
    return [workflow.name for workflow in workflows]


@pytest.mark.parametrize("workflow", ALL_WORKFLOWS, ids=_ids(ALL_WORKFLOWS))
def test_every_action_is_pinned_by_sha(workflow: Path) -> None:
    uses = [line for line in _lines(workflow) if line.lstrip().removeprefix("- ").startswith("uses:")]

    assert uses
    assert [line for line in uses if not PINNED_ACTION.match(line)] == []


@pytest.mark.parametrize("workflow", ALL_WORKFLOWS, ids=_ids(ALL_WORKFLOWS))
def test_no_expression_reaches_a_shell(workflow: Path) -> None:
    assert [line for line in _run_block_lines(workflow) if "${{" in line] == []


@pytest.mark.parametrize("workflow", SHARED_WORKFLOWS, ids=_ids(SHARED_WORKFLOWS))
def test_a_shared_workflow_is_only_ever_called(workflow: Path) -> None:
    # A trigger of its own would run the workflow in this repo, which has no `production` environment.
    lines = _lines(workflow)
    start = lines.index("on:") + 1
    end = next(i for i, line in enumerate(lines[start:], start) if line and not line.startswith(" "))
    triggers = [line.strip() for line in lines[start:end] if re.match(r"^  \w", line)]

    assert triggers == ["workflow_call:"]


def test_deploy_checks_out_nothing_but_its_own_ref() -> None:
    lines = _lines(DEPLOY)
    checkouts = [i for i, line in enumerate(lines) if "actions/checkout" in line]

    assert len(checkouts) == 1
    step = [line.strip() for line in lines[checkouts[0] : checkouts[0] + 5]]
    assert "repository: ${{ job.workflow_repository }}" in step
    assert "ref: ${{ job.workflow_sha }}" in step
    assert "persist-credentials: false" in step


def test_deploy_hands_dokploy_the_callers_compose_of_the_released_commit() -> None:
    # Dokploy runs the compose it stores; only the released compose.yml may become that.
    lines = [line.strip() for line in _lines(DEPLOY)]

    assert "RELEASE_SHA: ${{ inputs.sha || github.sha }}" in lines
    assert any('"repos/$REPOSITORY/contents/compose.yml?ref=$RELEASE_SHA"' in line for line in lines)
    assert "COMPOSE_FILE: ${{ runner.temp }}/compose.yml" in lines


def test_deploy_serializes_on_the_job_and_never_cancels() -> None:
    lines = _lines(DEPLOY)

    # A workflow-level group would make a caller that declares the same group wait for itself.
    assert [line for line in lines if line.startswith("concurrency:")] == []
    assert "      group: deploy-production" in lines
    assert "      cancel-in-progress: false" in lines


def test_deploy_only_runs_for_main_in_the_production_environment() -> None:
    lines = _lines(DEPLOY)

    assert "    if: github.ref == 'refs/heads/main'" in lines
    assert "    environment: production" in lines
