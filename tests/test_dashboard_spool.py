"""The dashboard snapshot is a best-effort consumer of ``funnel brief``."""

from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402


NOW = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)


def _item(number, status, *, repo="nateprich-projects/command-center", **kwargs):
    values = {
        "repo": repo,
        "number": number,
        "title": "Project {}".format(number),
        "url": "https://github.com/{}/issues/{}".format(repo, number),
        "state": "OPEN",
        "status": status,
        "klass": "New",
        "status_since": NOW - timedelta(days=number),
    }
    values.update(kwargs)
    return funnel.Item(**values)


def _brief_output(generated_at="2026-09-13T12:00:00+00:00"):
    return json.dumps({
        "generated_at": generated_at,
        "total_needing_nate": 0,
        "items": [],
    }, indent=2)


def _spooled(spool):
    paths = sorted(spool.glob("*.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text())


def test_dashboard_board_contains_ordered_parent_projects_and_recent_done_only():
    old_done = _item(
        1,
        "Done",
        state="CLOSED",
        closed_at=NOW - timedelta(days=8),
    )
    recent_done = _item(
        2,
        "Done",
        state="CLOSED",
        closed_at=NOW - timedelta(days=2),
    )
    older_ready = _item(8, "Ready")
    newer_ready = _item(3, "Ready")
    child = _item(4, "Building", parent=newer_ready.ref)
    parked = _item(
        5,
        "Parked",
        state="CLOSED",
        closed_at=NOW - timedelta(days=30),
    )

    board = funnel.dashboard_board(
        [newer_ready, child, old_done, parked, recent_done, older_ready], NOW
    )

    assert [column["stage"] for column in board["columns"]] == [
        "Ideas", "Shaped", "Ready", "Building", "Parked", "Done",
    ]
    ready = board["columns"][2]["items"]
    # A project with startable work leads its stage, in the engineers' queue
    # order (#902); projects with nothing startable keep time-at-gate order
    # behind it, which is why the older one is second here.
    assert [row["title"] for row in ready] == [
        newer_ready.title, older_ready.title,
    ]
    assert board["columns"][3]["items"] == []
    assert board["columns"][4]["items"][0]["title"] == parked.title
    assert [row["title"] for row in board["columns"][5]["items"]] == [
        recent_done.title,
    ]
    assert ready[1] == {
        "repo": "command-center",
        "ref": older_ready.ref,
        "title": older_ready.title,
        "url": older_ready.url,
        "class": "New",
        "pinned": False,
        "waited": "8 days",
        "tickets_closed": 0,
        "tickets_total": 0,
        "next_owner": None,
        "blocked": False,
        "blockers": [],
        "block_reason": None,
        "pips": [],
        "tickets": [],
    }


def test_successful_brief_spools_without_changing_stdout(
    monkeypatch, tmp_path, capsys
):
    spool = tmp_path / "dashboard-spool"
    monkeypatch.setenv(funnel.DASHBOARD_SPOOL_ENV, str(spool))
    # ``funnel.main`` reads the real clock for "waited", so anchor this item's
    # gate time to it rather than to the fixed NOW, or the expected "7 days"
    # goes stale at the next day rollover (it did, on 2026-09-14).
    project = _item(
        7, "Building", children_total=3, children_done=1,
        status_since=datetime.now(timezone.utc) - timedelta(days=7),
    )
    expected = _brief_output()
    monkeypatch.setattr(funnel, "load_items", lambda: [project])

    def fake_cmd_brief(items, now, **kwargs):
        assert items == [project]
        print(expected)
        return 0

    monkeypatch.setattr(funnel, "cmd_brief", fake_cmd_brief)

    assert funnel.main(["brief"]) == 0
    assert capsys.readouterr().out == expected + "\n"

    snapshot = _spooled(spool)
    assert snapshot["brief"] == json.loads(expected)
    assert snapshot["board"]["columns"][3]["items"] == [{
        "repo": "command-center",
        "ref": project.ref,
        "title": project.title,
        "url": project.url,
        "class": "New",
        "pinned": False,
        "waited": "7 days",
        "tickets_closed": 1,
        "tickets_total": 3,
        "next_owner": None,
        "blocked": False,
        "blockers": [],
        "block_reason": None,
        "pips": [],
        "tickets": [],
    }]
    assert snapshot["generated_at"] == json.loads(expected)["generated_at"]


def test_spool_write_failure_preserves_brief_output_and_exit_code(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv(funnel.DASHBOARD_SPOOL_ENV, str(tmp_path / "spool"))
    expected = _brief_output()
    monkeypatch.setattr(funnel, "load_items", lambda: [])

    def fake_cmd_brief(items, now, **kwargs):
        print(expected)
        return 0

    monkeypatch.setattr(funnel, "cmd_brief", fake_cmd_brief)

    def fail_to_spool(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(funnel, "write_dashboard_snapshot", fail_to_spool)

    assert funnel.main(["brief"]) == 0
    captured = capsys.readouterr()
    assert captured.out == expected + "\n"
    assert "could not spool dashboard brief: disk full" in captured.err


def test_nonzero_brief_is_not_spooled(monkeypatch, tmp_path, capsys):
    spool = tmp_path / "spool"
    monkeypatch.setenv(funnel.DASHBOARD_SPOOL_ENV, str(spool))
    expected = _brief_output()
    monkeypatch.setattr(funnel, "load_items", lambda: [])

    def fake_cmd_brief(items, now, **kwargs):
        print(expected)
        return 2

    monkeypatch.setattr(funnel, "cmd_brief", fake_cmd_brief)

    assert funnel.main(["brief"]) == 2
    assert capsys.readouterr().out == expected + "\n"
    assert not spool.exists()


def test_unreadable_project_brief_is_not_spooled(monkeypatch, tmp_path, capsys):
    spool = tmp_path / "spool"
    monkeypatch.setenv(funnel.DASHBOARD_SPOOL_ENV, str(spool))

    def fail_to_load():
        raise funnel.GitHubError("Project offline")

    monkeypatch.setattr(funnel, "load_items", fail_to_load)

    assert funnel.main(["brief"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["missing"][0]["error"] == "Project offline"
    assert not spool.exists()


def _write_snapshots(count):
    return [
        funnel.write_dashboard_snapshot(
            {"n": index}, {"columns": []}, "2026-09-13T12:00:00+00:00")
        for index in range(count)
    ]


def test_spool_keeps_only_the_newest_entries(monkeypatch, tmp_path):
    """Every reader parses every entry, so the spool is bounded (#979)."""
    spool = tmp_path / "spool"
    monkeypatch.setenv(funnel.DASHBOARD_SPOOL_ENV, str(spool))
    monkeypatch.setattr(funnel, "DASHBOARD_SPOOL_KEEP", 3)

    written = _write_snapshots(5)

    assert sorted(p.name for p in spool.glob("*.json")) == sorted(
        p.name for p in written[-3:])
    data, _ = funnel._newest_snapshot_entry(spool)
    assert json.loads(data)["brief"] == {"n": 4}


def test_spool_prune_leaves_unrelated_files(monkeypatch, tmp_path):
    spool = tmp_path / "spool"
    spool.mkdir()
    (spool / "notes.json").write_text("{}")
    (spool / ".brief-1-ab.json.tmp").write_text("")
    monkeypatch.setenv(funnel.DASHBOARD_SPOOL_ENV, str(spool))
    monkeypatch.setattr(funnel, "DASHBOARD_SPOOL_KEEP", 1)

    _write_snapshots(2)

    names = sorted(p.name for p in spool.iterdir())
    assert "notes.json" in names
    assert ".brief-1-ab.json.tmp" in names
    assert len([n for n in names if n.startswith("brief-")]) == 1


def test_spool_prune_failure_keeps_the_brief_and_the_snapshot(
    monkeypatch, tmp_path, capsys
):
    spool = tmp_path / "spool"
    monkeypatch.setenv(funnel.DASHBOARD_SPOOL_ENV, str(spool))
    expected = _brief_output()
    monkeypatch.setattr(funnel, "load_items", lambda: [])

    def fake_cmd_brief(items, now, **kwargs):
        print(expected)
        return 0

    monkeypatch.setattr(funnel, "cmd_brief", fake_cmd_brief)
    real_listdir = funnel.os.listdir

    def failing_listdir(path):
        if pathlib.Path(path) == spool:
            raise PermissionError("denied")
        return real_listdir(path)

    monkeypatch.setattr(funnel.os, "listdir", failing_listdir)

    assert funnel.main(["brief"]) == 0
    captured = capsys.readouterr()
    assert captured.out == expected + "\n"
    assert "could not prune dashboard spool: denied" in captured.err
    assert "could not spool" not in captured.err
    monkeypatch.setattr(funnel.os, "listdir", real_listdir)
    assert _spooled(spool)["brief"] == json.loads(expected)
