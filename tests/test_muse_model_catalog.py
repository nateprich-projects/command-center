"""Validate every pinned Muse model against the local provider catalog."""

from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402


def write(root: pathlib.Path, name: str, body: str) -> pathlib.Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


def catalog(root: pathlib.Path, rows) -> pathlib.Path:
    directory = root / "model-catalog"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "fixture.json").write_text(json.dumps({"rows": rows}))
    return directory


def check_for_pin(tmp_path, model, catalog_dir):
    write(tmp_path, "runner.sh", "muse exec --model {} --json\n".format(model))
    return funnel.check_muse_model_pins([tmp_path], catalog_dir=catalog_dir)


def test_visible_catalog_id_is_verified_at_its_callsite(tmp_path):
    path = write(tmp_path, "runner.sh",
                 "muse exec --model fixture-visible --json\n")
    catalog_dir = catalog(tmp_path, [{
        "model_id": "fixture-visible", "visibility": "visible",
    }])

    check = funnel.check_muse_model_pins([tmp_path], catalog_dir=catalog_dir)

    assert check.ok
    assert "1 verified, 0 failed, 0 unknown" in check.found
    assert "VERIFIED model id 'fixture-visible'" in check.found
    assert "{}:1 (muse exec --model fixture-visible)".format(path) in check.found


def test_id_absent_from_readable_catalog_is_unknown_and_fails(tmp_path):
    catalog_dir = catalog(tmp_path, [{
        "model_id": "fixture-visible", "visibility": "visible",
    }])

    check = check_for_pin(tmp_path, "totally-made-up-model", catalog_dir)

    assert not check.ok
    assert "UNKNOWN/INVALID model id 'totally-made-up-model'" in check.found
    assert "0 verified, 1 failed, 0 unknown" in check.found
    assert "visible model" in check.fix


def test_invisible_catalog_id_is_reported_as_stale(tmp_path):
    catalog_dir = catalog(tmp_path, [{
        "model_id": "fixture-retired", "visibility": "hidden",
    }])

    check = check_for_pin(tmp_path, "fixture-retired", catalog_dir)

    assert not check.ok
    assert "STALE model id 'fixture-retired' is not visible" in check.found
    assert "0 verified, 1 failed, 0 unknown" in check.found


def test_missing_catalog_is_explicitly_unknown_without_failed_pins(tmp_path):
    missing = tmp_path / "model-catalog"

    check = check_for_pin(tmp_path, "fixture-visible", missing)

    assert check.ok
    assert "model catalog unknown" in check.found
    assert "0 verified, 0 failed, 1 unknown" in check.found
    assert "catalog unavailable" in check.found
    assert not check.fix


def test_unreadable_catalog_degrades_the_whole_check(tmp_path):
    catalog_dir = tmp_path / "model-catalog"
    catalog_dir.mkdir()
    (catalog_dir / "broken.json").write_text("not json")

    check = check_for_pin(tmp_path, "fixture-visible", catalog_dir)

    assert check.ok
    assert "model catalog unknown" in check.found
    assert "0 verified, 0 failed, 1 unknown" in check.found
    assert "broken.json is unreadable" in check.found


def test_python_argv_model_value_and_dynamic_expression_are_collected(tmp_path):
    source = write(tmp_path, "runner.py", (
        'subprocess.run([MUSE_COMMAND, "exec", "--model", '
        '"fixture-visible", "--json"])\n'
        'subprocess.run([MUSE_COMMAND, "exec", "--model", '
        'MUSE_MODEL, "--json"])\n'
    ))

    invocations = funnel.muse_model_invocations([tmp_path])

    assert [(item.line, item.model) for item in invocations] == [
        (1, "fixture-visible"), (2, "<dynamic MUSE_MODEL>"),
    ]
    assert all(item.path == str(source) for item in invocations)


def test_shell_model_on_continuation_is_collected(tmp_path):
    source = write(tmp_path, "runner", (
        '"$MUSE_BIN" exec \\\n'
        '  --reasoning-effort max \\\n'
        '  --model fixture-visible \\\n'
        '  --json\n'
    ))

    invocations = funnel.muse_model_invocations([tmp_path])

    assert [(item.line, item.model) for item in invocations] == [
        (1, "fixture-visible"),
    ]
    assert invocations[0].path == str(source)
