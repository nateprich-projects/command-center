"""Fixture-only tests for the read-only nightly model release watch."""

from __future__ import annotations

import copy
import json
import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import release_watch  # noqa: E402


FIXTURE = ROOT / "tests" / "fixtures" / "release_responses.json"


def responses():
    return json.loads(FIXTURE.read_text())


def test_models_in_use_comes_from_heartbeat_sources(monkeypatch):
    monkeypatch.setattr(
        release_watch.heartbeat,
        "MODEL_SOURCES",
        {"codex": "unused", "claude": "unused", "muse": "unused"},
    )

    observed = {
        "codex": {"provider": "openai", "model": "gpt-5.6-sol"},
        "claude": {"provider": "anthropic", "model": "claude-opus-5"},
        "muse": {"provider": "meta", "model": None},
    }

    assert release_watch.models_in_use(
        detector=lambda agent: observed[agent],
        providers={"codex": "openai", "claude": "anthropic", "muse": "meta"},
    ) == {
        "anthropic": ("claude-opus-5",),
        "openai": ("gpt-5.6-sol",),
    }


def test_a_new_version_emits_one_ticktick_task_and_changes_no_configuration(
    tmp_path,
):
    configuration = {"codex_model": "gpt-5.6-sol"}
    before = copy.deepcopy(configuration)
    notifications = []

    result = release_watch.watch(
        {"openai": ("gpt-5.6-sol",)},
        responses(),
        notifications.append,
    )

    assert result.faults == ()
    assert [task.newer_model for task in result.notifications] == ["gpt-5.7-sol"]
    assert notifications == list(result.notifications)
    assert notifications[0].title == "New openai model version: gpt-5.7-sol"
    assert "never changes configuration" in notifications[0].content
    assert configuration == before
    assert not list(tmp_path.iterdir()), "the watch must not create configuration files"


def test_unchanged_models_stay_silent():
    notifications = []

    result = release_watch.watch(
        {"zai": ("glm-5.3",)},
        responses(),
        notifications.append,
    )

    assert result.clean
    assert notifications == []


def test_only_the_same_model_family_is_compared():
    result = release_watch.watch(
        {"anthropic": ("claude-opus-5",)},
        responses(),
    )

    assert [task.newer_model for task in result.notifications] == ["claude-opus-5.1"]


def test_each_newer_version_gets_its_own_task():
    result = release_watch.watch(
        {"openai": ("gpt-5.6-sol",)},
        {"openai": {"data": [
            {"id": "gpt-5.6-sol"},
            {"id": "gpt-5.7-sol"},
            {"id": "gpt-5.8-sol"},
        ]}},
    )

    assert [task.newer_model for task in result.notifications] == [
        "gpt-5.7-sol", "gpt-5.8-sol",
    ]


@pytest.mark.parametrize(
    "response",
    [
        "not json",
        {"data": "not a list"},
        {"data": []},
        {"unexpected": [{"id": "gpt-5.7-sol"}]},
    ],
)
def test_an_unparseable_response_is_a_could_not_check_fault(response):
    result = release_watch.watch(
        {"openai": ("gpt-5.6-sol",)},
        {"openai": response},
    )

    assert result.notifications == ()
    assert len(result.faults) == 1
    assert result.faults[0].kind == "could-not-check"
    assert "openai" in str(result.faults[0])


def test_a_missing_provider_response_is_not_silence():
    result = release_watch.watch(
        {"meta": ("muse-spark-1.3",)},
        {},
    )

    assert result.notifications == ()
    assert result.faults == (
        release_watch.WatchFault("meta", "no model catalogue response"),
    )


def test_ticktick_payload_is_a_description_not_a_write():
    task = release_watch.TickTickTask("openai", "gpt-5.6-sol", "gpt-5.7-sol")

    assert task.payload("watch-project") == {
        "projectId": "watch-project",
        "title": "New openai model version: gpt-5.7-sol",
        "content": task.content,
    }
