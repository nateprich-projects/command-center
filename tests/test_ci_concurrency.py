"""CI concurrency guard for #1110 (parent #1076).

The `pytest` job runs on the shared hobby-linux runner. It must carry
job-level concurrency scoped to the branch/ref so a superseded run on the
same PR branch cancels instead of occupying the runner, while runs on
distinct branches are unaffected and main-branch runs never cancel.
"""

from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
TESTS_YML = ROOT / ".github" / "workflows" / "tests.yml"
WATCHDOG_YML = ROOT / ".github" / "workflows" / "watchdog.yml"


def _pytest_job_text() -> str:
    """Return the raw YAML of the `pytest` job block (up to `steps:`)."""
    text = TESTS_YML.read_text()
    start = text.index("  pytest:")
    steps = text.index("    steps:", start)
    return text[start:steps]


def test_concurrency_is_job_level_not_workflow_level():
    text = TESTS_YML.read_text()
    assert "\nconcurrency:" not in text, (
        "concurrency must be job-level (indented under the job), not "
        "top-level, so it scopes to the hobby-linux job only"
    )
    assert "concurrency:" in _pytest_job_text()


def test_concurrency_group_scopes_to_branch_or_ref():
    job = _pytest_job_text()
    match = re.search(r"^\s*group:\s*(.+)$", job, re.MULTILINE)
    assert match, "pytest job concurrency must define a group"
    group = match.group(1)
    assert "github.ref" in group, (
        "concurrency group must scope to the branch/ref so distinct branches "
        "are unaffected, got: {}".format(group)
    )


def test_main_branch_never_cancels():
    job = _pytest_job_text()
    match = re.search(r"^\s*cancel-in-progress:\s*(.+)$", job, re.MULTILINE)
    assert match, "pytest job concurrency must define cancel-in-progress"
    value = match.group(1).strip()
    assert value != "true", (
        "cancel-in-progress must not be unconditional: main-branch runs "
        "must never cancel"
    )
    assert "refs/heads/main" in value, (
        "cancel-in-progress must exempt main, got: {}".format(value)
    )


def test_hobby_linux_label_unchanged():
    job = _pytest_job_text()
    assert "hobby-linux" in job, (
        "the pytest job must stay on the hobby-linux runner labels"
    )


def test_watchdog_has_no_concurrency():
    # watchdog.yml runs on ubuntu-latest (GitHub-hosted) on a schedule; it
    # never touches hobby-linux, so it is out of scope for #1110.
    assert "concurrency" not in WATCHDOG_YML.read_text()
