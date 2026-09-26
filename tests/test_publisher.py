"""The funnel publisher pushes spool snapshots and honours refresh taps.

Ticket #652: every minute the launchd job pushes the newest spool entry to the
Cloudflare KV snapshot the Worker in `dashboard/worker.js` reads, and polls the
`refresh-requested` flag. A set flag runs one `funnel.py brief` only when the
newest snapshot is older than 10 minutes. A publishable brief clears the flag;
an exit-0 unpublishable refresh records its attempt in the flag and waits one
cadence interval before retrying. Failures go to the publisher's own log
(stderr under launchd) and never touch agent runs.

Every test runs against a fake KV server, fixture spool entries, and a fake
brief script. No test reads the real `.env`, the real spool, or GitHub.
"""

from __future__ import annotations

import http.server
import json
import os
import socketserver
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import publisher

ROOT = Path(__file__).resolve().parent.parent
FAKE_TOKEN = "test-token-not-a-credential"
FAKE_ACCOUNT = "test-account-id"
FAKE_NAMESPACE = "test-namespace-id"


@pytest.fixture(autouse=True)
def home_is_tmp(monkeypatch, tmp_path):
    """A forgotten path override must land in tmp, never in ~/.claude."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return tmp_path


@pytest.fixture()
def kv():
    """A fake Cloudflare KV API speaking just the values endpoints."""
    server = FakeKVServer()
    server.start()
    yield server
    server.stop()


class FakeKVServer:
    """Minimal in-memory stand-in for the KV values REST API."""

    def __init__(self):
        self.values = {}
        self.calls = []
        self.auth_headers = []
        self.fail_next = None  # (status, body) returned once, then cleared
        self._server = None
        self._thread = None

    def start(self):
        handler = self._handler()
        self._server = socketserver.TCPServer(("127.0.0.1", 0), handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever)
        self._thread.start()

    def stop(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    @property
    def api_base(self):
        host, port = self._server.server_address
        return "http://{}:{}".format(host, port)

    def puts_to(self, key):
        return [c for c in self.calls if c[0] == "PUT" and c[1] == key]

    def deletes_of(self, key):
        return [c for c in self.calls if c[0] == "DELETE" and c[1] == key]

    def _handler(self):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, status, body, content_type="application/json"):
                payload = body if isinstance(body, bytes) else body.encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _route(self):
                # /accounts/<a>/storage/kv/namespaces/<n>/values/<key>
                parts = self.path.split("?", 1)[0].strip("/").split("/")
                if (
                    len(parts) == 7
                    and parts[0] == "accounts"
                    and parts[2] == "storage"
                    and parts[3] == "kv"
                    and parts[4] == "namespaces"
                    and parts[6].startswith("values/")
                ):
                    return parts[1], parts[5], parts[6][len("values/"):]
                if (
                    len(parts) == 8
                    and parts[0] == "accounts"
                    and parts[2] == "storage"
                    and parts[3] == "kv"
                    and parts[4] == "namespaces"
                    and parts[6] == "values"
                ):
                    return parts[1], parts[5], parts[7]
                return None, None, None

            def _handle(self):
                account, namespace, key = self._route()
                outer.auth_headers.append(self.headers.get("Authorization"))
                if outer.fail_next is not None:
                    status, body = outer.fail_next
                    outer.fail_next = None
                    self._send(status, body)
                    return
                if account != FAKE_ACCOUNT or namespace != FAKE_NAMESPACE:
                    self._send(404, json.dumps({"success": False}))
                    return
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.calls.append((self.command, key, body))
                if self.command == "GET":
                    if key in outer.values:
                        self._send(200, outer.values[key],
                                   "application/octet-stream")
                    else:
                        self._send(404, json.dumps({"success": False}))
                elif self.command == "PUT":
                    outer.values[key] = body
                    self._send(200, json.dumps({"success": True}))
                elif self.command == "DELETE":
                    outer.values.pop(key, None)
                    self._send(200, json.dumps({"success": True}))
                else:
                    self._send(405, json.dumps({"success": False}))

            do_GET = _handle
            do_PUT = _handle
            do_DELETE = _handle

        return Handler


def iso(seconds_ago=0):
    return datetime.fromtimestamp(
        time.time() - seconds_ago, tz=timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def write_spool_entry(spool_dir, name, seconds_ago, extra=None):
    payload = {
        "brief": {"items": []},
        "board": {"columns": []},
        "generated_at": iso(seconds_ago),
    }
    if extra:
        payload.update(extra)
    path = spool_dir / name
    path.write_text(json.dumps(payload))
    return path, payload


def write_env_file(path, token=FAKE_TOKEN, account=FAKE_ACCOUNT):
    lines = []
    if token is not None:
        lines.append("CLOUDFLARE_API_TOKEN={}".format(token))
    if account is not None:
        lines.append("CLOUDFLARE_ACCOUNT_ID={}".format(account))
    path.write_text("\n".join(lines) + "\n")
    return path


def write_wrangler_toml(path, namespace=FAKE_NAMESPACE):
    path.write_text(
        'name = "command-center-funnel"\n'
        "[[kv_namespaces]]\n"
        'binding = "FUNNEL_SNAPSHOT"\n'
        'id = "{}"\n'.format(namespace)
    )
    return path


def write_fake_brief(path, exit_code=0, sleep=0, write_spool_to=None,
                     missing=None):
    """A stand-in for funnel.py that records runs instead of reading GitHub."""
    path.write_text(
        "import json, sys, time\n"
        "from pathlib import Path\n"
        "here = Path(__file__).parent\n"
        'with (here / "brief-runs.log").open("a") as fh:\n'
        '    fh.write("ran\\n")\n'
        "time.sleep({})\n".format(sleep)
        + (
            "entry = {{\"brief\": {{}}, \"board\": {{}}, "
            '"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", '
            "time.gmtime())}}\n"
            '(Path(r"""{}""") / "from-brief.json").write_text(__import__("json").dumps(entry))\n'.format(
                write_spool_to
            )
            if write_spool_to is not None
            else ""
        )
        + "print(json.dumps({\"generated_at\": time.strftime(\"%Y-%m-%dT%H:%M:%SZ\", time.gmtime()), \"missing\": "
        + repr(missing if missing is not None else [])
        + "}))\n"
        + "sys.exit({})\n".format(exit_code)
    )
    return path


def brief_run_count(fake_brief):
    log = fake_brief.parent / "brief-runs.log"
    if not log.exists():
        return 0
    return len(log.read_text().splitlines())


def base_argv(tmp_path, kv, spool_dir):
    env_file = write_env_file(tmp_path / "test.env")
    wrangler = write_wrangler_toml(tmp_path / "wrangler.toml")
    fake_brief = write_fake_brief(tmp_path / "funnel.py")
    fake_metrics = tmp_path / "metrics-series.py"
    write_fake_series(fake_metrics, exit_code=1)
    return (
        [
            "--spool-dir",
            str(spool_dir),
            "--env-file",
            str(env_file),
            "--wrangler-toml",
            str(wrangler),
            "--api-base",
            kv.api_base,
            "--funnel-py",
            str(fake_brief),
            "--metrics-py",
            str(fake_metrics),
            "--deploy-state-file",
            str(tmp_path / "deploy-state.json"),
        ],
        fake_brief,
    )


def write_fake_series(path, payload=None, exit_code=0):
    if payload is None:
        script = "import sys\nprint('series unavailable', file=sys.stderr)\nsys.exit({})\n".format(exit_code)
    else:
        script = "import json\nimport sys\nprint(json.dumps({!r}))\nsys.exit({})\n".format(
            payload, exit_code,
        )
    path.write_text(script)
    return path


def run_publisher(argv, monkeypatch, capsys, with_metrics=False):
    """Run one tick with every path overridden; the real Mac is untouched."""
    for name in (
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_ACCOUNT_ID",
        "FUNNEL_KV_NAMESPACE_ID",
        "COMMAND_CENTER_DASHBOARD_SPOOL",
        "COMMAND_CENTER_DASHBOARD_ENV_FILE",
        "COMMAND_CENTER_DASHBOARD_WRANGLER_TOML",
        "COMMAND_CENTER_DASHBOARD_API_BASE",
        "COMMAND_CENTER_DASHBOARD_FUNNEL_PY",
        "COMMAND_CENTER_DASHBOARD_METRICS_PY",
        "COMMAND_CENTER_DASHBOARD_DEPLOY_STATE_FILE",
    ):
        monkeypatch.delenv(name, raising=False)
    if not with_metrics:
        # The legacy publisher cases focus on snapshot/refresh behavior. The
        # new metrics publication path has its own subprocess/KV integration
        # case below.
        monkeypatch.setattr(publisher, "publish_metrics_series", lambda *_: None)
    code = publisher.main(argv)
    out, err = capsys.readouterr()
    return code, out, err


def test_publishes_the_newest_entry_once(tmp_path, kv, monkeypatch, capsys):
    spool = tmp_path / "spool"
    spool.mkdir()
    write_spool_entry(spool, "old.json", seconds_ago=3600)
    newest_path, newest = write_spool_entry(spool, "new.json", seconds_ago=60)
    argv, _ = base_argv(tmp_path, kv, spool)

    code, _, _ = run_publisher(argv, monkeypatch, capsys)
    assert code == 0
    code, _, _ = run_publisher(argv, monkeypatch, capsys)
    assert code == 0

    puts = kv.puts_to("snapshot")
    assert len(puts) == 1
    assert json.loads(puts[0][2].decode()) == newest
    assert puts[0][2] == newest_path.read_bytes()


def test_publishes_metrics_series_under_its_own_key(
    tmp_path, kv, monkeypatch, capsys
):
    spool = tmp_path / "spool"
    spool.mkdir()
    entry_path, snapshot = write_spool_entry(spool, "entry.json", seconds_ago=60)
    argv, _ = base_argv(tmp_path, kv, spool)
    series = {
        "schema_version": 1,
        "as_of": "2026-09-24",
        "days": ["2026-09-24"],
        "metrics": {"A": {"A1": {"total": {"daily": [3]}}}},
    }
    metrics_path = Path(argv[argv.index("--metrics-py") + 1])
    write_fake_series(metrics_path, series)

    code, _, err = run_publisher(argv, monkeypatch, capsys, with_metrics=True)

    assert code == 0, err
    assert json.loads(kv.values["snapshot"]) == snapshot
    assert kv.values["snapshot"] == entry_path.read_bytes()
    assert json.loads(kv.values["metrics"]) == series
    assert len(kv.puts_to("metrics")) == 1


def test_skips_publish_when_remote_snapshot_is_current(
    tmp_path, kv, monkeypatch, capsys
):
    spool = tmp_path / "spool"
    spool.mkdir()
    _, payload = write_spool_entry(spool, "entry.json", seconds_ago=60)
    kv.values["snapshot"] = json.dumps(payload).encode()
    argv, _ = base_argv(tmp_path, kv, spool)

    code, _, _ = run_publisher(argv, monkeypatch, capsys)

    assert code == 0
    assert kv.puts_to("snapshot") == []


def test_publishes_when_remote_snapshot_is_missing(
    tmp_path, kv, monkeypatch, capsys
):
    spool = tmp_path / "spool"
    spool.mkdir()
    _, payload = write_spool_entry(spool, "entry.json", seconds_ago=60)
    argv, _ = base_argv(tmp_path, kv, spool)

    code, _, _ = run_publisher(argv, monkeypatch, capsys)

    assert code == 0
    puts = kv.puts_to("snapshot")
    assert len(puts) == 1
    assert json.loads(puts[0][2].decode()) == payload


def test_unparseable_spool_files_are_skipped_not_fatal(
    tmp_path, kv, monkeypatch, capsys
):
    spool = tmp_path / "spool"
    spool.mkdir()
    (spool / "half-written.json").write_text('{"brief": ')
    (spool / "no-timestamp.json").write_text(json.dumps({"brief": {}}))
    _, payload = write_spool_entry(spool, "good.json", seconds_ago=60)
    argv, _ = base_argv(tmp_path, kv, spool)

    code, _, err = run_publisher(argv, monkeypatch, capsys)

    assert code == 0
    assert "half-written.json" in err
    assert "no-timestamp.json" in err
    puts = kv.puts_to("snapshot")
    assert len(puts) == 1
    assert json.loads(puts[0][2].decode()) == payload


def test_empty_spool_publishes_nothing(tmp_path, kv, monkeypatch, capsys):
    spool = tmp_path / "spool"
    spool.mkdir()
    argv, _ = base_argv(tmp_path, kv, spool)

    code, _, _ = run_publisher(argv, monkeypatch, capsys)

    assert code == 0
    assert kv.puts_to("snapshot") == []


def test_missing_spool_dir_is_an_empty_spool(tmp_path, kv, monkeypatch, capsys):
    argv, _ = base_argv(tmp_path, kv, tmp_path / "no-such-spool")

    code, _, _ = run_publisher(argv, monkeypatch, capsys)

    assert code == 0
    assert kv.puts_to("snapshot") == []


def test_refresh_with_stale_snapshot_runs_one_brief_and_clears(
    tmp_path, kv, monkeypatch, capsys
):
    spool = tmp_path / "spool"
    spool.mkdir()
    write_spool_entry(spool, "entry.json", seconds_ago=11 * 60)
    kv.values["refresh-requested"] = iso().encode()
    argv, fake_brief = base_argv(tmp_path, kv, spool)

    code, _, _ = run_publisher(argv, monkeypatch, capsys)

    assert code == 0
    assert brief_run_count(fake_brief) == 1
    assert len(kv.deletes_of("refresh-requested")) == 1
    assert "refresh-requested" not in kv.values


def test_exit_zero_missing_items_envelope_keeps_refresh_flag(
    tmp_path, kv, monkeypatch, capsys
):
    spool = tmp_path / "spool"
    spool.mkdir()
    entry_path, _ = write_spool_entry(spool, "entry.json", seconds_ago=11 * 60)
    kv.values["snapshot"] = entry_path.read_bytes()
    kv.values["refresh-requested"] = iso().encode()
    argv, fake_brief = base_argv(tmp_path, kv, spool)
    write_fake_brief(
        fake_brief,
        missing=[{"section": "items", "error": "Project unavailable"}],
    )

    code, _, err = run_publisher(argv, monkeypatch, capsys)

    assert code == 0
    assert brief_run_count(fake_brief) == 1
    assert kv.deletes_of("refresh-requested") == []
    assert "refresh-requested" in kv.values
    assert "did not produce a publishable snapshot" in err
    stored_flag = json.loads(kv.values["refresh-requested"].decode())
    assert stored_flag["requested_at"] == iso()
    assert publisher.parse_generated_at(stored_flag["attempted_at"]) == pytest.approx(
        time.time(), abs=1
    )


def test_unpublishable_refresh_retries_only_after_the_shared_interval(
    tmp_path, kv, monkeypatch, capsys
):
    spool = tmp_path / "spool"
    spool.mkdir()
    start = 1_800_000_000.0
    now = [start]
    monkeypatch.setattr(publisher.time, "time", lambda: now[0])
    entry_path, _ = write_spool_entry(
        spool, "entry.json", seconds_ago=publisher.STALE_AFTER_SECONDS + 1
    )
    kv.values["snapshot"] = entry_path.read_bytes()
    kv.values["refresh-requested"] = iso().encode()
    argv, fake_brief = base_argv(tmp_path, kv, spool)
    write_fake_brief(
        fake_brief,
        missing=[{"section": "items", "error": "Project unavailable"}],
    )

    code, _, _ = run_publisher(argv, monkeypatch, capsys)

    assert code == 0
    assert brief_run_count(fake_brief) == 1
    stored_flag = json.loads(kv.values["refresh-requested"].decode())
    assert publisher.parse_generated_at(stored_flag["attempted_at"]) == start

    now[0] = start + publisher.STALE_AFTER_SECONDS - 1
    code, _, _ = run_publisher(argv, monkeypatch, capsys)

    assert code == 0
    assert brief_run_count(fake_brief) == 1
    assert "refresh-requested" in kv.values
    assert json.loads(kv.values["refresh-requested"].decode()) == stored_flag

    now[0] = start + publisher.STALE_AFTER_SECONDS
    code, _, _ = run_publisher(argv, monkeypatch, capsys)

    assert code == 0
    assert brief_run_count(fake_brief) == 2
    assert "refresh-requested" in kv.values


def test_publishable_refresh_clears_the_attempt_timestamp_with_the_flag(
    tmp_path, kv, monkeypatch, capsys
):
    spool = tmp_path / "spool"
    spool.mkdir()
    write_spool_entry(spool, "entry.json", seconds_ago=11 * 60)
    kv.values["refresh-requested"] = json.dumps({
        "requested_at": iso(),
        "attempted_at": iso(seconds_ago=publisher.STALE_AFTER_SECONDS + 1),
    }).encode()
    argv, fake_brief = base_argv(tmp_path, kv, spool)

    code, _, _ = run_publisher(argv, monkeypatch, capsys)

    assert code == 0
    assert brief_run_count(fake_brief) == 1
    assert len(kv.deletes_of("refresh-requested")) == 1
    assert "refresh-requested" not in kv.values


def test_refresh_within_the_floor_waits_and_keeps_the_flag(
    tmp_path, kv, monkeypatch, capsys
):
    """A second event moments after a brief must not run another (#914).

    The flag stays so the next tick honours it once the floor has passed;
    clearing it here would drop a change that arrived during the brief.
    """
    spool = tmp_path / "spool"
    spool.mkdir()
    write_spool_entry(spool, "entry.json", seconds_ago=30)
    kv.values["refresh-requested"] = iso().encode()
    argv, fake_brief = base_argv(tmp_path, kv, spool)

    code, _, _ = run_publisher(argv, monkeypatch, capsys)

    assert code == 0
    assert brief_run_count(fake_brief) == 0
    assert kv.deletes_of("refresh-requested") == []
    assert "refresh-requested" in kv.values


def test_a_webhook_refresh_past_the_floor_runs_one_brief(
    tmp_path, kv, monkeypatch, capsys
):
    spool = tmp_path / "spool"
    spool.mkdir()
    write_spool_entry(
        spool, "entry.json", seconds_ago=publisher.STALE_AFTER_SECONDS + 1
    )
    kv.values["refresh-requested"] = iso().encode()
    argv, fake_brief = base_argv(tmp_path, kv, spool)

    code, _, _ = run_publisher(argv, monkeypatch, capsys)

    assert code == 0
    assert brief_run_count(fake_brief) == 1
    assert len(kv.deletes_of("refresh-requested")) == 1


def test_refresh_floor_uses_the_shared_regeneration_interval(monkeypatch):
    monkeypatch.setattr(publisher, "STALE_AFTER_SECONDS", 900)
    now = 10_000.0

    assert publisher.brief_reason(now - 899, now, flagged=True) is None
    assert publisher.brief_reason(now - 901, now, flagged=True) == "refresh"
    assert publisher.brief_reason(
        now - 901, now, flagged=True, last_attempt_epoch=now - 899
    ) is None
    assert publisher.brief_reason(
        now - 901, now, flagged=True, last_attempt_epoch=now - 900
    ) == "refresh"


def test_five_minute_refresh_bursts_follow_the_configured_interval(
    tmp_path, kv, monkeypatch, capsys
):
    spool = tmp_path / "spool"
    spool.mkdir()
    start = 1_800_000_000.0
    now = [start]
    monkeypatch.setattr(publisher.time, "time", lambda: now[0])
    write_spool_entry(
        spool, "entry.json", seconds_ago=publisher.STALE_AFTER_SECONDS + 1
    )
    argv, fake_brief = base_argv(tmp_path, kv, spool)
    regeneration_times = []
    last_run_count = 0

    for minutes in range(0, 31, 5):
        now[0] = start + minutes * 60
        kv.values["refresh-requested"] = iso().encode()
        code, _, _ = run_publisher(argv, monkeypatch, capsys)

        assert code == 0
        run_count = brief_run_count(fake_brief)
        if run_count > last_run_count:
            regeneration_times.append(now[0])
            write_spool_entry(spool, "entry.json", seconds_ago=0)
            last_run_count = run_count

    assert [when - regeneration_times[0] for when in regeneration_times] == [
        0,
        15 * 60,
        30 * 60,
    ]
    assert all(
        later - earlier >= publisher.STALE_AFTER_SECONDS
        for earlier, later in zip(regeneration_times, regeneration_times[1:])
    )


def test_no_flag_and_a_recent_snapshot_runs_no_brief(
    tmp_path, kv, monkeypatch, capsys
):
    spool = tmp_path / "spool"
    spool.mkdir()
    write_spool_entry(spool, "entry.json", seconds_ago=10 * 60)
    argv, fake_brief = base_argv(tmp_path, kv, spool)

    code, _, _ = run_publisher(argv, monkeypatch, capsys)

    assert code == 0
    assert brief_run_count(fake_brief) == 0
    assert kv.deletes_of("refresh-requested") == []


def test_an_old_snapshot_runs_a_scheduled_brief_without_any_flag(
    tmp_path, kv, monkeypatch, capsys
):
    """The backstop for a webhook that is broken or never configured (#914)."""
    spool = tmp_path / "spool"
    spool.mkdir()
    write_spool_entry(spool, "entry.json", seconds_ago=60 * 60)
    argv, fake_brief = base_argv(tmp_path, kv, spool)

    code, _, err = run_publisher(argv, monkeypatch, capsys)

    assert code == 0
    assert brief_run_count(fake_brief) == 1
    assert kv.deletes_of("refresh-requested") == []
    assert "scheduled bound" in err


def test_scheduled_regeneration_is_not_held_by_a_refresh_retry(
    tmp_path, kv, monkeypatch, capsys
):
    spool = tmp_path / "spool"
    spool.mkdir()
    now = [1_800_000_000.0]
    monkeypatch.setattr(publisher.time, "time", lambda: now[0])
    write_spool_entry(
        spool, "entry.json", seconds_ago=publisher.SCHEDULED_AFTER_SECONDS + 1
    )
    kv.values["refresh-requested"] = json.dumps({
        "requested_at": iso(),
        "attempted_at": iso(),
    }).encode()
    argv, fake_brief = base_argv(tmp_path, kv, spool)
    write_fake_brief(
        fake_brief,
        missing=[{"section": "items", "error": "Project unavailable"}],
    )

    code, _, err = run_publisher(argv, monkeypatch, capsys)

    assert code == 0
    assert brief_run_count(fake_brief) == 1
    assert "scheduled bound" in err
    assert "refresh-requested" in kv.values
    stored_flag = json.loads(kv.values["refresh-requested"].decode())
    assert stored_flag["attempted_at"] == iso()


def test_failed_brief_still_clears_the_flag_and_exits_zero(
    tmp_path, kv, monkeypatch, capsys
):
    spool = tmp_path / "spool"
    spool.mkdir()
    write_spool_entry(spool, "entry.json", seconds_ago=11 * 60)
    kv.values["refresh-requested"] = iso().encode()
    argv, fake_brief = base_argv(tmp_path, kv, spool)
    write_fake_brief(fake_brief, exit_code=3)

    code, _, err = run_publisher(argv, monkeypatch, capsys)

    assert code == 0
    assert brief_run_count(fake_brief) == 1
    assert len(kv.deletes_of("refresh-requested")) == 1
    assert "exit 3" in err


def test_kv_failure_exits_one_and_runs_no_brief(
    tmp_path, kv, monkeypatch, capsys
):
    spool = tmp_path / "spool"
    spool.mkdir()
    write_spool_entry(spool, "entry.json", seconds_ago=60)
    kv.values["refresh-requested"] = iso().encode()
    kv.fail_next = (500, json.dumps({"success": False}))
    argv, fake_brief = base_argv(tmp_path, kv, spool)

    code, _, err = run_publisher(argv, monkeypatch, capsys)

    assert code == 1
    assert err.strip() != ""
    assert brief_run_count(fake_brief) == 0
    assert "refresh-requested" in kv.values


def test_missing_token_exits_one_and_names_the_env_file(
    tmp_path, kv, monkeypatch, capsys
):
    spool = tmp_path / "spool"
    spool.mkdir()
    argv, _ = base_argv(tmp_path, kv, spool)
    env_index = argv.index("--env-file") + 1
    Path(argv[env_index]).write_text("CLOUDFLARE_ACCOUNT_ID=x\n")

    code, _, err = run_publisher(argv, monkeypatch, capsys)

    assert code == 1
    assert "CLOUDFLARE_API_TOKEN" in err
    assert argv[env_index] in err


def test_missing_account_exits_one_without_logging_the_token(
    tmp_path, kv, monkeypatch, capsys
):
    spool = tmp_path / "spool"
    spool.mkdir()
    argv, _ = base_argv(tmp_path, kv, spool)
    env_index = argv.index("--env-file") + 1
    Path(argv[env_index]).write_text(
        "CLOUDFLARE_API_TOKEN={}\n".format(FAKE_TOKEN)
    )

    code, _, err = run_publisher(argv, monkeypatch, capsys)

    assert code == 1
    assert "CLOUDFLARE_ACCOUNT_ID" in err
    assert FAKE_TOKEN not in err


def test_default_env_file_uses_working_tree_not_run_clone(
    tmp_path, kv, monkeypatch, capsys
):
    """An installed run-clone publisher reads the existing working-tree env."""
    spool = tmp_path / "spool"
    spool.mkdir()
    write_spool_entry(spool, "entry.json", seconds_ago=60)
    argv, _ = base_argv(tmp_path, kv, spool)
    env_index = argv.index("--env-file")
    del argv[env_index:env_index + 2]

    run_clone = tmp_path / "run-clone"
    run_clone.mkdir()
    monkeypatch.setattr(publisher, "_repo_root", lambda: run_clone)
    working_tree = Path(os.environ["HOME"]) / ".claude" / "command-center"
    working_tree.mkdir(parents=True)
    write_env_file(working_tree / ".env")

    code, _, err = run_publisher(argv, monkeypatch, capsys)

    assert code == 0, err
    assert len(kv.puts_to("snapshot")) == 1
    assert (working_tree / ".env").exists()
    assert not (run_clone / ".env").exists()


def test_token_reaches_cloudflare_but_never_the_log(
    tmp_path, kv, monkeypatch, capsys
):
    spool = tmp_path / "spool"
    spool.mkdir()
    write_spool_entry(spool, "entry.json", seconds_ago=60)
    argv, _ = base_argv(tmp_path, kv, spool)

    code, out, err = run_publisher(argv, monkeypatch, capsys)

    assert code == 0
    assert kv.auth_headers
    assert all(h == "Bearer " + FAKE_TOKEN for h in kv.auth_headers)
    assert FAKE_TOKEN not in out
    assert FAKE_TOKEN not in err


def test_brief_timeout_kills_the_run_and_clears_the_flag(
    tmp_path, kv, monkeypatch, capsys
):
    spool = tmp_path / "spool"
    spool.mkdir()
    write_spool_entry(spool, "entry.json", seconds_ago=11 * 60)
    kv.values["refresh-requested"] = iso().encode()
    argv, fake_brief = base_argv(tmp_path, kv, spool)
    write_fake_brief(fake_brief, sleep=30)
    argv += ["--brief-timeout", "1"]

    code, _, err = run_publisher(argv, monkeypatch, capsys)

    assert code == 0
    assert brief_run_count(fake_brief) == 1
    assert len(kv.deletes_of("refresh-requested")) == 1
    assert "timed out" in err


def test_a_brief_run_is_published_on_the_next_tick(
    tmp_path, kv, monkeypatch, capsys
):
    spool = tmp_path / "spool"
    spool.mkdir()
    entry_path, _ = write_spool_entry(spool, "entry.json", seconds_ago=11 * 60)
    kv.values["snapshot"] = entry_path.read_bytes()
    kv.values["refresh-requested"] = iso().encode()
    argv, fake_brief = base_argv(tmp_path, kv, spool)
    write_fake_brief(fake_brief, write_spool_to=str(spool))

    code, _, _ = run_publisher(argv, monkeypatch, capsys)
    assert code == 0
    assert brief_run_count(fake_brief) == 1
    code, _, _ = run_publisher(argv, monkeypatch, capsys)
    assert code == 0

    puts = kv.puts_to("snapshot")
    assert len(puts) == 1
    published_at = publisher.parse_generated_at(
        json.loads(puts[0][2].decode())["generated_at"]
    )
    assert published_at is not None
    assert abs(published_at - time.time()) < 120


def test_parse_generated_at_accepts_worker_and_python_shapes():
    now = time.time()
    assert publisher.parse_generated_at("2026-09-13T08:00:00Z") == pytest.approx(
        datetime(2026, 9, 13, 8, 0, tzinfo=timezone.utc).timestamp()
    )
    assert publisher.parse_generated_at("2026-09-13T08:00:00+00:00") == (
        pytest.approx(
            datetime(2026, 9, 13, 8, 0, tzinfo=timezone.utc).timestamp()
        )
    )
    assert abs(publisher.parse_generated_at(iso(0)) - now) < 5
    assert publisher.parse_generated_at("not a timestamp") is None
    assert publisher.parse_generated_at("") is None
    assert publisher.parse_generated_at(None) is None
    assert publisher.parse_generated_at(12345) is None


def test_is_stale_uses_the_configured_regeneration_interval():
    now = 1_000_000.0
    interval = publisher.STALE_AFTER_SECONDS
    assert publisher.is_stale(now - interval, now) is False
    assert publisher.is_stale(now - interval - 1, now) is True
    assert publisher.is_stale(None, now) is True


def test_parse_dotenv_reads_assignments_and_ignores_noise(tmp_path):
    path = tmp_path / ".env"
    path.write_text(
        "# a comment\n"
        "\n"
        "CLOUDFLARE_API_TOKEN=abc123\n"
        "  CLOUDFLARE_ACCOUNT_ID = \"quoted\"  \n"
        "export IGNORED=no\n"
        "EMPTY=\n"
    )
    parsed = publisher.parse_dotenv(path)
    assert parsed["CLOUDFLARE_API_TOKEN"] == "abc123"
    assert parsed["CLOUDFLARE_ACCOUNT_ID"] == "quoted"
    assert "IGNORED" not in parsed
    assert "EMPTY" not in parsed
    assert publisher.parse_dotenv(tmp_path / "missing") == {}


def test_namespace_id_comes_from_the_snapshot_binding(tmp_path):
    path = tmp_path / "wrangler.toml"
    path.write_text(
        "[[kv_namespaces]]\n"
        'binding = "OTHER"\n'
        'id = "wrong"\n'
        "[[kv_namespaces]]\n"
        "binding = 'FUNNEL_SNAPSHOT'\n"
        'id = "right"\n'
    )
    assert publisher.namespace_id_from_wrangler(path) == "right"


def test_namespace_id_missing_binding_raises(tmp_path):
    path = tmp_path / "wrangler.toml"
    path.write_text('name = "x"\n')
    with pytest.raises(publisher.PublisherError):
        publisher.namespace_id_from_wrangler(path)


def write_deploy_state(path, namespace_id):
    path.write_text(
        json.dumps({"deployed_sha": "a" * 40, "namespace_id": namespace_id})
        + "\n"
    )
    return path


def test_namespace_falls_back_to_the_id_the_deployer_recorded(tmp_path):
    """main keeps the placeholder on purpose, so the deployer's state file is
    the only place the real id exists (#895)."""
    wrangler = write_wrangler_toml(
        tmp_path / "wrangler.toml",
        namespace=publisher.PLACEHOLDER_NAMESPACE_ID,
    )
    state = write_deploy_state(tmp_path / "state.json", "real-namespace-id")
    assert publisher.resolve_namespace_id(None, wrangler, state) == (
        "real-namespace-id")


def test_a_real_wrangler_id_wins_over_the_deploy_state(tmp_path):
    wrangler = write_wrangler_toml(tmp_path / "wrangler.toml")
    state = write_deploy_state(tmp_path / "state.json", "stale-id")
    assert publisher.resolve_namespace_id(None, wrangler, state) == (
        FAKE_NAMESPACE)


def test_an_explicit_namespace_wins_over_both(tmp_path):
    wrangler = write_wrangler_toml(tmp_path / "wrangler.toml")
    state = write_deploy_state(tmp_path / "state.json", "stale-id")
    assert publisher.resolve_namespace_id("flag-id", wrangler, state) == (
        "flag-id")


def test_the_placeholder_with_no_deploy_state_raises(tmp_path):
    wrangler = write_wrangler_toml(
        tmp_path / "wrangler.toml",
        namespace=publisher.PLACEHOLDER_NAMESPACE_ID,
    )
    with pytest.raises(publisher.PublisherError) as excinfo:
        publisher.resolve_namespace_id(
            None, wrangler, tmp_path / "missing.json")
    assert publisher.PLACEHOLDER_NAMESPACE_ID in str(excinfo.value)


def test_a_state_file_still_holding_the_placeholder_is_no_id(tmp_path):
    state = write_deploy_state(
        tmp_path / "state.json", publisher.PLACEHOLDER_NAMESPACE_ID)
    assert publisher.namespace_id_from_deploy_state(state) is None
    assert publisher.namespace_id_from_deploy_state(
        tmp_path / "missing.json") is None


def test_a_placeholder_tick_fails_without_calling_cloudflare(
    tmp_path, kv, monkeypatch, capsys
):
    """The wedge this fixes: 400s every minute against the placeholder."""
    spool = tmp_path / "spool"
    spool.mkdir()
    write_spool_entry(spool, "entry.json", seconds_ago=30)
    argv, _ = base_argv(tmp_path, kv, spool)
    write_wrangler_toml(
        tmp_path / "wrangler.toml",
        namespace=publisher.PLACEHOLDER_NAMESPACE_ID,
    )

    code, _, err = run_publisher(argv, monkeypatch, capsys)

    assert code == 1
    assert kv.calls == []
    assert publisher.PLACEHOLDER_NAMESPACE_ID in err


def test_publisher_imports_no_agent_code():
    """The publisher talks to Cloudflare and a brief subprocess only; it must
    never import funnel or heartbeat, so it cannot touch agent runs."""
    probe = (
        "import sys; sys.path.insert(0, %r); import publisher; "
        "leftover = [m for m in ('funnel', 'heartbeat') if m in sys.modules]; "
        "assert not leftover, leftover; print('clean')" % str(ROOT)
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "clean" in result.stdout


def test_publisher_exits_two_on_usage_error(monkeypatch, capsys):
    with pytest.raises(SystemExit) as exc:
        publisher.main(["--no-such-flag"])
    assert exc.value.code == 2
