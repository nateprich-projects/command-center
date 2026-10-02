"""Fixture-only tests for the scheduled model watch composition."""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import nightly_watch  # noqa: E402
import price_watch  # noqa: E402


PRICE_FIXTURE = json.loads(
    (ROOT / "tests" / "fixtures" / "price_watch.json").read_text()
)
RELEASE_FIXTURE = json.loads(
    (ROOT / "tests" / "fixtures" / "release_responses.json").read_text()
)
RELEASE_SOURCE_FIXTURES = {
    "https://developers.openai.com/api/docs/models/all.md": (
        ROOT / "tests" / "fixtures" / "model_release_sources" / "openai.md"
    ).read_bytes(),
    "https://platform.claude.com/docs/en/models/overview.md": (
        ROOT / "tests" / "fixtures" / "model_release_sources" / "anthropic.md"
    ).read_bytes(),
    "https://dev.meta.ai/docs/models": (
        ROOT / "tests" / "fixtures" / "model_release_sources" / "meta.html"
    ).read_bytes(),
}
NOW = datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)
OPENAI_PRICE_URL = nightly_watch.PRICE_URLS["openai"]
OPENAI_CATALOGUE_URL = nightly_watch.MODEL_CATALOGUES["openai"]["url"]


def _write_rate_table(path):
    path.write_text(json.dumps({
        "schema_version": 1,
        "rates": PRICE_FIXTURE["current_gpt_6_luna_rates"],
    }))


def _fetcher(catalogue, *, fail_url=None):
    price_body = PRICE_FIXTURE["captured_gpt_6_luna_rates"]["body"].encode()
    model_ids = [
        row["id"] for row in catalogue.get("data", [])
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    ]
    model_page = "\n".join(
        "- [`{}`](/api/docs/models/{}.md)".format(model_id, model_id)
        for model_id in model_ids
    ).encode()

    def fetch(url, headers):
        if url == fail_url:
            raise OSError("fixture fetch failed")
        if url == OPENAI_PRICE_URL:
            return price_body
        if url == OPENAI_CATALOGUE_URL:
            return model_page
        raise AssertionError("unexpected URL: {}".format(url))

    return fetch


def _in_use(monkeypatch, models):
    monkeypatch.setattr(nightly_watch.release_watch, "models_in_use", lambda **kwargs: models)


def test_clean_run_keeps_price_table_unchanged_and_writes_no_watch_record(
    monkeypatch, tmp_path
):
    models = {"openai": ("gpt-6-luna",)}
    _in_use(monkeypatch, models)
    rates = tmp_path / "model_rates.json"
    _write_rate_table(rates)
    before = rates.read_bytes()
    record = tmp_path / "watch.jsonl"

    result = nightly_watch.run(
        now=NOW,
        record_path=record,
        env_file=tmp_path / "missing.env",
        environ={"OPENAI_API_KEY": "fixture-key"},
        current_models=models,
        fetcher=_fetcher({"data": [{"id": "gpt-6-luna"}]}),
        price_path=rates,
    )

    assert result.price_result.clean
    assert result.release_result.clean
    assert not result.recorded
    assert rates.read_bytes() == before
    assert not record.exists()
    assert nightly_watch.recent_entries(NOW, path=record) == {
        "model_releases": [],
        "watch_faults": [],
    }


def test_newer_model_is_recorded_once_across_repeated_runs(monkeypatch, tmp_path):
    models = {"openai": ("gpt-5.6-sol",)}
    _in_use(monkeypatch, models)
    monkeypatch.setattr(
        nightly_watch.price_watch,
        "watch_in_use",
        lambda *args, **kwargs: price_watch.WatchResult(),
    )
    record = tmp_path / "watch.jsonl"
    fetcher = _fetcher(RELEASE_FIXTURE["openai"])

    first = nightly_watch.run(
        now=NOW,
        record_path=record,
        env_file=tmp_path / "missing.env",
        environ={"OPENAI_API_KEY": "fixture-key"},
        current_models=models,
        fetcher=fetcher,
    )
    second = nightly_watch.run(
        now=NOW + timedelta(minutes=1),
        record_path=record,
        env_file=tmp_path / "missing.env",
        environ={"OPENAI_API_KEY": "fixture-key"},
        current_models=models,
        fetcher=fetcher,
    )

    assert [event["newer_model"] for event in first.model_releases] == ["gpt-5.7-sol"]
    assert first.model_releases[0]["source_url"] == OPENAI_CATALOGUE_URL
    assert second.model_releases == ()
    assert len(nightly_watch.recent_entries(NOW, path=record)["model_releases"]) == 1


def test_each_newer_model_gets_its_own_brief_entry(monkeypatch, tmp_path):
    models = {"openai": ("gpt-5.6-sol", "gpt-5.6-mini")}
    _in_use(monkeypatch, models)
    monkeypatch.setattr(
        nightly_watch.price_watch,
        "watch_in_use",
        lambda *args, **kwargs: price_watch.WatchResult(),
    )
    record = tmp_path / "watch.jsonl"

    result = nightly_watch.run(
        now=NOW,
        record_path=record,
        env_file=tmp_path / "missing.env",
        environ={"OPENAI_API_KEY": "fixture-key"},
        current_models=models,
        fetcher=_fetcher(RELEASE_FIXTURE["openai"]),
    )

    assert {
        (event["provider"], event["current_model"], event["newer_model"])
        for event in result.model_releases
    } == {
        ("openai", "gpt-5.6-sol", "gpt-5.7-sol"),
        ("openai", "gpt-5.6-mini", "gpt-5.7-mini"),
    }
    assert len(nightly_watch.recent_entries(NOW, path=record)["model_releases"]) == 2


@pytest.mark.parametrize(
    ("failed_url", "failed_source"),
    [
        (OPENAI_PRICE_URL, "pricing"),
        (OPENAI_CATALOGUE_URL, "release"),
    ],
)
def test_one_failed_capture_is_recorded_while_the_other_check_runs(
    monkeypatch, tmp_path, failed_url, failed_source
):
    if failed_source == "pricing":
        models = {"openai": ("gpt-5.6-sol",)}
        catalogue = RELEASE_FIXTURE["openai"]
    else:
        models = {"openai": ("gpt-6-luna",)}
        catalogue = {"data": [{"id": "gpt-6-luna"}]}
    _in_use(monkeypatch, models)
    rates = tmp_path / "model_rates.json"
    _write_rate_table(rates)
    record = tmp_path / "watch.jsonl"
    result = nightly_watch.run(
        now=NOW,
        record_path=record,
        env_file=tmp_path / "missing.env",
        environ={"OPENAI_API_KEY": "fixture-key"},
        current_models=models,
        fetcher=_fetcher(catalogue, fail_url=failed_url),
        price_path=rates,
    )

    assert len(result.watch_faults) == 1
    assert result.watch_faults[0]["source"] == failed_source
    if failed_source == "pricing":
        assert result.price_result.faults
        assert [event["newer_model"] for event in result.model_releases] == [
            "gpt-5.7-sol"
        ]
    else:
        assert result.price_result.clean
        assert result.release_result.faults
        assert result.release_result.faults[0].source_url == OPENAI_CATALOGUE_URL
        assert result.watch_faults[0]["source_url"] == OPENAI_CATALOGUE_URL


def test_recent_entries_expire_after_seven_days(tmp_path):
    record = tmp_path / "watch.jsonl"
    stale = {
        "recorded_at": (NOW - timedelta(days=8)).isoformat(),
        "model_releases": [{
            "provider": "openai",
            "current_model": "gpt-5.6-sol",
            "newer_model": "gpt-5.7-sol",
        }],
        "watch_faults": [{"source": "release", "provider": "openai"}],
    }
    recent = {
        "recorded_at": (NOW - timedelta(days=7)).isoformat(),
        "model_releases": [],
        "watch_faults": [{"source": "pricing", "provider": "anthropic"}],
    }
    record.write_text(json.dumps(stale) + "\n" + json.dumps(recent) + "\n")

    entries = nightly_watch.recent_entries(NOW, path=record)

    assert entries["model_releases"] == []
    assert [fault["provider"] for fault in entries["watch_faults"]] == ["anthropic"]


def test_a_failed_public_catalogue_fetch_keeps_source_for_release_fault():
    responses = nightly_watch.capture_release_responses(
        {"openai": ("gpt-6-luna",)},
        {"OPENAI_API_KEY": "fixture-key"},
        fetcher=lambda *_args: (_ for _ in ()).throw(OSError("offline")),
    )

    assert responses == {
        "openai": {
            "source_url": OPENAI_CATALOGUE_URL,
            "error": "public model page fetch failed",
        },
    }
    result = nightly_watch.release_watch.watch(
        {"openai": ("gpt-6-luna",)}, responses,
    )
    assert len(result.faults) == 1
    assert result.faults[0].kind == "could-not-check"
    assert result.faults[0].source_url == OPENAI_CATALOGUE_URL


def test_public_model_pages_check_all_three_providers_without_credentials():
    models = {
        "openai": ("gpt-5.6-sol",),
        "anthropic": ("claude-opus-5",),
        "meta": ("muse-spark-1.2-contributor",),
    }
    requests = []

    def fetch(url, headers):
        requests.append((url, headers))
        return RELEASE_SOURCE_FIXTURES.get(url)

    responses = nightly_watch.capture_release_responses(models, {}, fetcher=fetch)
    result = nightly_watch.release_watch.watch(models, responses)

    # Before the keyless sources were wired in, this yielded three
    # missing-key could-not-check faults at the capture/parse seam.
    assert result.faults == (), [str(fault) for fault in result.faults]
    assert set(requests) == {(url, None) for url in RELEASE_SOURCE_FIXTURES}
    assert {
        (check.provider, check.status, check.source_url)
        for check in result.checks
    } == {
        ("openai", "changed", "https://developers.openai.com/api/docs/models/all.md"),
        ("anthropic", "changed", "https://platform.claude.com/docs/en/models/overview.md"),
        ("meta", "changed", "https://dev.meta.ai/docs/models"),
    }
    assert {
        (task.provider, task.current_model, task.newer_model, task.source_url)
        for task in result.notifications
    } == {
        ("openai", "gpt-5.6-sol", "gpt-6-sol", "https://developers.openai.com/api/docs/models/all.md"),
        ("openai", "gpt-5.6-sol", "gpt-6.1-sol", "https://developers.openai.com/api/docs/models/all.md"),
        ("anthropic", "claude-opus-5", "claude-opus-5-5", "https://platform.claude.com/docs/en/models/overview.md"),
        ("meta", "muse-spark-1.2-contributor", "muse-spark-1.3-contributor", "https://dev.meta.ai/docs/models"),
    }
    assert all(task.source_url in task.content for task in result.notifications)


def test_latest_public_models_produce_sourced_unchanged_checks():
    models = {
        "openai": ("gpt-6-luna",),
        "anthropic": ("claude-opus-5-5",),
        "meta": ("muse-spark-1.3-contributor",),
    }
    responses = nightly_watch.capture_release_responses(
        models,
        fetcher=lambda url, _headers: RELEASE_SOURCE_FIXTURES[url],
    )

    result = nightly_watch.release_watch.watch(models, responses)

    assert result.faults == ()
    assert result.notifications == ()
    assert {
        (check.provider, check.status, check.source_url)
        for check in result.checks
    } == {
        ("openai", "unchanged", "https://developers.openai.com/api/docs/models/all.md"),
        ("anthropic", "unchanged", "https://platform.claude.com/docs/en/models/overview.md"),
        ("meta", "unchanged", "https://dev.meta.ai/docs/models"),
    }


def test_unrecognized_public_pages_are_sourced_could_not_check_faults():
    models = {
        "openai": ("gpt-6-luna",),
        "anthropic": ("claude-opus-5-5",),
        "meta": ("muse-spark-1.3-contributor",),
    }
    requests = []

    def fetch(url, headers):
        requests.append((url, headers))
        return b"public page without a recognizable model list"

    responses = nightly_watch.capture_release_responses(
        models,
        {
            "OPENAI_API_KEY": "unused",
            "ANTHROPIC_API_KEY": "unused",
            "MODEL_API_KEY": "unused",
        },
        fetcher=fetch,
    )
    result = nightly_watch.release_watch.watch(models, responses)

    assert set(requests) == {(url, None) for url in RELEASE_SOURCE_FIXTURES}
    assert result.notifications == ()
    assert result.checks == ()
    assert {fault.provider for fault in result.faults} == set(models)
    assert all(fault.kind == "could-not-check" for fault in result.faults)
    assert {
        (fault.provider, fault.source_url)
        for fault in result.faults
    } == {
        ("openai", "https://developers.openai.com/api/docs/models/all.md"),
        ("anthropic", "https://platform.claude.com/docs/en/models/overview.md"),
        ("meta", "https://dev.meta.ai/docs/models"),
    }
