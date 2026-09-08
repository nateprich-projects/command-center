"""The routine hash helper emits a literal that ``funnel begin`` can verify."""

from __future__ import annotations

import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import funnel  # noqa: E402


spec = importlib.util.spec_from_file_location(
    "paste_routine_sha", ROOT / "scripts" / "paste_routine_sha.py"
)
paste = importlib.util.module_from_spec(spec)
spec.loader.exec_module(paste)


BEGIN = (
    "python3 /Users/nateprich/.claude/command-center/funnel.py begin "
    "--agent zcode --tier standard --breakdown\n"
)


def _routine(tmp_path, body):
    path = tmp_path / "zcode.md"
    path.write_text(body, encoding="utf-8")
    return path


def test_paste_command_uses_funnel_hash_without_an_existing_literal(tmp_path):
    path = _routine(tmp_path, "# routine\n\n" + BEGIN)

    command = paste.paste_command(path)

    assert command == BEGIN.rstrip() + " --routine-sha " + funnel.routine_sha(path)


def test_paste_command_replaces_an_old_literal_with_the_current_hash(tmp_path):
    old = "0" * 64
    path = _routine(
        tmp_path,
        "# routine\n\n" + BEGIN.replace(
            "--breakdown", "--breakdown --routine-sha " + old
        ),
    )

    command = paste.paste_command(path)
    expected = funnel.routine_sha(path)

    assert command.endswith("--routine-sha " + expected)
    assert command.count("--routine-sha") == 1
    assert command.startswith(BEGIN.rstrip())

    repasted = path.read_text(encoding="utf-8").replace(
        path.read_text(encoding="utf-8").splitlines()[-1], command
    )
    repasted_path = tmp_path / "repasted-zcode.md"
    repasted_path.write_text(repasted, encoding="utf-8")
    assert funnel.routine_sha(repasted_path) == expected


def test_main_prints_only_the_pasteable_command(tmp_path, capsys):
    path = _routine(tmp_path, "# routine\n\n" + BEGIN)

    assert paste.main([str(path)]) == 0

    assert capsys.readouterr().out == paste.paste_command(path) + "\n"
