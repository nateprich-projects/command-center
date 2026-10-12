"""SSD-backed scratch for every automated test run (#2530).

``test_env.prepare`` is what the Muse runners and ``run_tests`` refuse
without: it roots pytest's basetemp and the bytecode cache on the
external volume, and raises instead of silently falling back to the
internal disk. The functional tests below run a real pytest and a real
import under the prepared environment and prove the debris lands on the
fixture volume.
"""

from __future__ import annotations

import os
import shlex
import stat
import subprocess
import sys

import pytest

import test_env


def _override(monkeypatch, path):
    monkeypatch.setenv(test_env.SSD_ROOT_ENV_VAR, str(path))


def test_default_root_is_the_external_scratch_volume():
    assert test_env.DEFAULT_SSD_ROOT == "/Volumes/ClaudeScratch"


def test_prepare_roots_temp_and_bytecode_on_the_volume(test_ssd_isolation,
                                                      monkeypatch):
    _override(monkeypatch, test_ssd_isolation)

    env = test_env.prepare()

    tmpdir = test_ssd_isolation / test_env.TMP_SUBDIR
    pycache = test_ssd_isolation / test_env.PYCACHE_SUBDIR
    assert env == {"TMPDIR": str(tmpdir),
                   "PYTHONPYCACHEPREFIX": str(pycache),
                   "PYTHONDONTWRITEBYTECODE": "1"}
    assert tmpdir.is_dir()
    assert pycache.is_dir()
    assert stat.S_IMODE(tmpdir.stat().st_mode) == 0o700
    assert stat.S_IMODE(pycache.stat().st_mode) == 0o700


def test_prepare_is_idempotent(test_ssd_isolation, monkeypatch):
    _override(monkeypatch, test_ssd_isolation)

    assert test_env.prepare() == test_env.prepare()


def test_missing_volume_refuses_loudly(tmp_path, monkeypatch):
    missing = tmp_path / "no-such-volume"
    _override(monkeypatch, missing)

    with pytest.raises(test_env.TestEnvError) as excinfo:
        test_env.prepare()

    assert str(missing) in str(excinfo.value)
    assert "refusing" in str(excinfo.value)


def test_file_at_the_root_refuses(tmp_path, monkeypatch):
    blocker = tmp_path / "test-ssd-file"
    blocker.write_text("not a directory\n")
    _override(monkeypatch, blocker)

    with pytest.raises(test_env.TestEnvError, match="not mounted"):
        test_env.prepare()


def test_unwritable_volume_refuses(test_ssd_isolation, monkeypatch):
    _override(monkeypatch, test_ssd_isolation)

    def denied(*args, **kwargs):
        raise PermissionError("Operation not permitted")

    monkeypatch.setattr(os, "makedirs", denied)
    with pytest.raises(test_env.TestEnvError, match="cannot create"):
        test_env.prepare()


def test_file_blocking_a_scratch_dir_refuses(test_ssd_isolation,
                                               monkeypatch):
    ssd = test_ssd_isolation
    (ssd / test_env.TMP_SUBDIR).write_text("not a directory\n")
    _override(monkeypatch, ssd)

    with pytest.raises(test_env.TestEnvError, match="cannot create"):
        test_env.prepare()


def test_failed_write_probe_refuses(test_ssd_isolation, monkeypatch):
    _override(monkeypatch, test_ssd_isolation)

    def probe_failed(**kwargs):
        raise OSError("Read-only file system")

    monkeypatch.setattr("tempfile.mkstemp", probe_failed)
    with pytest.raises(test_env.TestEnvError, match="not writable"):
        test_env.prepare()


def test_default_root_must_be_a_mount(tmp_path, monkeypatch):
    monkeypatch.delenv(test_env.SSD_ROOT_ENV_VAR)
    plain = tmp_path / "plain-dir"
    plain.mkdir()
    monkeypatch.setattr(test_env, "DEFAULT_SSD_ROOT", str(plain))

    with pytest.raises(test_env.TestEnvError, match="not a mounted volume"):
        test_env.prepare()


def test_mounted_default_root_passes(tmp_path, monkeypatch):
    monkeypatch.delenv(test_env.SSD_ROOT_ENV_VAR)
    plain = tmp_path / "plain-dir"
    plain.mkdir()
    monkeypatch.setattr(test_env, "DEFAULT_SSD_ROOT", str(plain))
    monkeypatch.setattr(os.path, "ismount", lambda path: True)

    env = test_env.prepare()

    assert env["TMPDIR"] == str(plain / test_env.TMP_SUBDIR)


def test_wedged_volume_refuses_within_the_timeout(test_ssd_isolation,
                                                    monkeypatch):
    _override(monkeypatch, test_ssd_isolation)

    def wedged(root, *, override):
        import time
        time.sleep(30)
        raise AssertionError("a wedged volume must not answer")

    monkeypatch.setattr(test_env, "_prepare_under", wedged)
    with pytest.raises(test_env.TestEnvError, match="did not answer"):
        test_env.prepare(timeout=0.05)


def test_export_prints_evalable_assignments(test_ssd_isolation, monkeypatch,
                                             capsys):
    _override(monkeypatch, test_ssd_isolation)

    assert test_env.export_main(["--export", "PROBE"]) == 0

    out = capsys.readouterr().out.splitlines()
    assert out == [
        "PROBE_TMPDIR={}".format(shlex.quote(str(
            test_ssd_isolation / test_env.TMP_SUBDIR))),
        "PROBE_PYCACHE={}".format(shlex.quote(str(
            test_ssd_isolation / test_env.PYCACHE_SUBDIR))),
    ]


def test_export_fails_loudly_without_the_volume(tmp_path, monkeypatch,
                                                capsys):
    _override(monkeypatch, tmp_path / "no-such-volume")

    assert test_env.export_main(["--export", "PROBE"]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert "refusing" in captured.err


def test_export_rejects_a_non_variable_prefix(capsys):
    with pytest.raises(SystemExit) as excinfo:
        test_env.export_main(["--export", "not a name"])
    assert excinfo.value.code == 2


def test_pytest_debris_lands_on_the_volume(tmp_path, test_ssd_isolation,
                                            monkeypatch):
    """A real pytest under the prepared env numbers its dirs on SSD paths."""
    _override(monkeypatch, test_ssd_isolation)
    env = test_env.prepare()
    suite = tmp_path / "suite"
    suite.mkdir()
    (suite / "test_marker.py").write_text(
        "def test_leaves_a_marker(tmp_path):\n"
        "    (tmp_path / 'marker.txt').write_text('here\\n')\n")

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         str(suite / "test_marker.py")],
        env=dict(os.environ, **env),
        cwd=str(suite),
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    markers = list(
        (test_ssd_isolation / test_env.TMP_SUBDIR).rglob("marker.txt"))
    assert len(markers) == 1


def test_bytecode_lands_on_the_volume(tmp_path, test_ssd_isolation,
                                       monkeypatch):
    """A real import under the prefix writes its cache to SSD paths."""
    _override(monkeypatch, test_ssd_isolation)
    env = test_env.prepare()
    del env["PYTHONDONTWRITEBYTECODE"]
    # The lane itself runs with the no-cache guard set; the child must
    # not inherit it or no .pyc is written and the prefix proves nothing.
    child = dict(os.environ, **env)
    child.pop("PYTHONDONTWRITEBYTECODE", None)
    package = tmp_path / "package"
    package.mkdir()
    (package / "mod_probe_ssd.py").write_text("VALUE = 42\n")

    proc = subprocess.run(
        [sys.executable, "-c", "import mod_probe_ssd"],
        env=child,
        cwd=str(package),
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert proc.returncode == 0, proc.stderr
    # The prefix catches every import including stdlib ones; the proof
    # is our module's own cache plus nothing beside its source.
    caches = [
        path for path in
        (test_ssd_isolation / test_env.PYCACHE_SUBDIR).rglob("*.pyc")
        if path.name.startswith("mod_probe_ssd.")]
    assert len(caches) == 1
    assert not list(package.rglob("__pycache__"))
