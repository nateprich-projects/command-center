"""Runners and routines read the published snapshot, not a live brief.

``funnel snapshot`` prints the newest dashboard spool entry byte for byte.
Only the publisher runs ``brief`` (#824); everything else reads this
artifact, so this command must never touch GitHub.
"""

from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import funnel  # noqa: E402


def _write(spool, name, payload):
    path = spool / name
    path.write_text(json.dumps(payload) + "\n")
    return path


def test_snapshot_prints_the_newest_entry_byte_for_byte(
    monkeypatch, tmp_path, capsys
):
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setenv(funnel.DASHBOARD_SPOOL_ENV, str(spool))
    _write(spool, "brief-1-old.json", {
        "generated_at": "2026-09-14T00:00:00+00:00",
        "brief": {"total_needing_nate": 1},
        "board": {},
    })
    newest = _write(spool, "brief-2-new.json", {
        "generated_at": "2026-09-15T00:00:00+00:00",
        "brief": {"total_needing_nate": 0},
        "board": {},
    })

    assert funnel.main(["snapshot"]) == 0
    captured = capsys.readouterr()

    assert captured.out == newest.read_text()
    assert captured.err == ""


def test_snapshot_orders_by_generated_at_not_filename(
    monkeypatch, tmp_path, capsys
):
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setenv(funnel.DASHBOARD_SPOOL_ENV, str(spool))
    _write(spool, "brief-9-misnamed.json", {
        "generated_at": "2026-09-14T00:00:00Z",
        "brief": {},
    })
    newest = _write(spool, "brief-1-renamed.json", {
        "generated_at": "2026-09-15T00:00:00+00:00",
        "brief": {"total_needing_nate": 2},
    })

    assert funnel.main(["snapshot"]) == 0

    assert json.loads(capsys.readouterr().out) == json.loads(
        newest.read_text()
    )


def test_snapshot_skips_unparseable_entries_with_a_warning(
    monkeypatch, tmp_path, capsys
):
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setenv(funnel.DASHBOARD_SPOOL_ENV, str(spool))
    (spool / "half-written.json").write_text('{"brief": ')
    _write(spool, "no-timestamp.json", {"brief": {}})
    good = _write(spool, "brief-3-good.json", {
        "generated_at": "2026-09-15T00:00:00Z",
        "brief": {"total_needing_nate": 3},
    })

    assert funnel.main(["snapshot"]) == 0
    captured = capsys.readouterr()

    assert captured.out == good.read_text()
    assert "skipping half-written.json: not parseable JSON" in captured.err
    assert (
        "skipping no-timestamp.json: no parseable generated_at"
        in captured.err
    )


def test_snapshot_with_no_spool_says_so_and_fails(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv(
        funnel.DASHBOARD_SPOOL_ENV, str(tmp_path / "missing-spool")
    )

    assert funnel.main(["snapshot"]) == 1
    captured = capsys.readouterr()

    assert captured.out == ""
    assert "no published snapshot yet" in captured.err


def test_snapshot_with_no_parseable_entry_says_so_and_fails(
    monkeypatch, tmp_path, capsys
):
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setenv(funnel.DASHBOARD_SPOOL_ENV, str(spool))
    (spool / "half-written.json").write_text('{"brief": ')

    assert funnel.main(["snapshot"]) == 1
    captured = capsys.readouterr()

    assert captured.out == ""
    assert "no published snapshot yet" in captured.err


def test_snapshot_reads_no_github_state(monkeypatch, tmp_path, capsys):
    """The snapshot is a local read: no Project load precedes it."""
    spool = tmp_path / "spool"
    spool.mkdir()
    monkeypatch.setenv(funnel.DASHBOARD_SPOOL_ENV, str(spool))
    entry = _write(spool, "brief-1-only.json", {
        "generated_at": "2026-09-15T00:00:00Z",
        "brief": {"total_needing_nate": 4},
    })

    def fail_to_load():
        raise AssertionError("snapshot must not load the Project")

    monkeypatch.setattr(funnel, "load_items", fail_to_load)

    assert funnel.main(["snapshot"]) == 0
    assert capsys.readouterr().out == entry.read_text()
