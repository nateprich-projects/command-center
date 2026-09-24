"""scripts/zai-exec asks GLM-5.3 one tool-less question and fails closed.

The z.ai half of the engine's standard tier until 2026-10-06 09:00 PDT
(Nate, 2026-09-23). A local HTTP server stands in for z.ai's
Anthropic-compatible endpoint, so every refusal below is exercised against
real HTTP status codes and bodies rather than a patched function.
"""

from __future__ import annotations

import http.server
import importlib.machinery
import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import threading

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "zai-exec"
sys.path.insert(0, str(ROOT))

import heartbeat  # noqa: E402
import session_usage  # noqa: E402


def _load():
    loader = importlib.machinery.SourceFileLoader("zai_exec", str(SCRIPT))
    spec = importlib.util.spec_from_loader("zai_exec", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


zai_exec = _load()


def _reply(text="{\"ok\": true}", model="glm-5.3", stop="end_turn", **extra):
    content = [{"type": "thinking", "thinking": "private reasoning"}]
    if text is not None:
        content.append({"type": "text", "text": text})
    reply = {
        "id": "msg_test", "type": "message", "role": "assistant",
        "model": model, "content": content, "stop_reason": stop,
        "usage": {"input_tokens": 27, "output_tokens": 98,
                  "cache_read_input_tokens": 5},
    }
    reply.update(extra)
    return reply


class _Server:
    """Answers each POST from a queue of (status, body) and keeps requests."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("content-length", "0"))
                outer.requests.append({
                    "path": self.path,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "body": json.loads(self.rfile.read(length)),
                })
                status, body = outer.responses.pop(0)
                if status == "drop":
                    # Close without a response: the client sees the server
                    # disconnect mid-exchange (RemoteDisconnected).
                    self.close_connection = True
                    return
                if status == "redirect":
                    self.send_response(307)
                    self.send_header("Location", body)
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
                raw = (body if isinstance(body, str)
                       else json.dumps(body)).encode("utf-8")
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:{}/api/anthropic/v1/messages".format(
            self.httpd.server_address[1])
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def run(tmp_path, monkeypatch, capsys):
    """Run zai-exec in process against a stub server on a fake clock.

    A retry's wait advances the clock instead of sleeping, so the deadline
    arithmetic is exercised exactly and the suite stays fast.
    """
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Answer with one JSON object: the packet goes here.")
    logs = tmp_path / "logs"
    monkeypatch.setenv("ZAI_API_KEY", "test-key")
    monkeypatch.setenv("ZAI_EXEC_LOG_DIR", str(logs))
    monkeypatch.setenv("ZCODE_SESSION_ID", "session-1")

    class FakeClock:
        def __init__(self):
            self.now = 1000.0
            self.sleeps = []

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.sleeps.append(seconds)
            self.now += seconds

    clock = FakeClock()
    monkeypatch.setattr(zai_exec, "time", clock)

    def invoke(responses, *args):
        with _Server(responses) as server:
            monkeypatch.setenv("ZAI_EXEC_URL", server.url)
            status = zai_exec.main(
                ["--prompt-file", str(prompt), "--timeout", "30"] + list(args))
        out, err = capsys.readouterr()
        return status, out, err, server.requests

    invoke.logs = logs
    invoke.prompt = prompt
    invoke.clock = clock
    return invoke


def test_a_finished_answer_is_the_text_blocks_alone(run):
    status, out, err, requests = run([(200, _reply())])

    assert status == 0, err
    assert out == '{"ok": true}'
    assert "private reasoning" not in out
    assert len(requests) == 1
    sent = requests[0]
    assert sent["path"] == "/api/anthropic/v1/messages"
    assert sent["headers"]["x-api-key"] == "test-key"
    assert sent["headers"]["anthropic-version"] == "2023-06-01"
    body = sent["body"]
    assert body["model"] == "glm-5.3"
    assert body["max_tokens"] >= 16384, "GLM-5.3 reasons before it answers"
    assert body["messages"] == [{
        "role": "user",
        "content": "Answer with one JSON object: the packet goes here."}]
    # Tool-less by construction: nothing is offered, so nothing can be called.
    assert set(body) == {"model", "max_tokens", "messages"}


def test_the_reply_from_another_model_is_refused(run):
    """z.ai remaps ids server-side (LEARNINGS.md, 2026-09-06); a Flash answer
    must not be recorded as GLM-5.3's judgement."""
    status, out, err, _ = run([(200, _reply(model="glm-5.3-flash"))])

    assert status == 1
    assert out == ""
    assert "model mismatch" in err and "glm-5.3-flash" in err


def test_a_reply_cut_off_at_max_tokens_is_refused(run):
    status, out, err, _ = run([(200, _reply(text='{"requirements": [',
                                            stop="max_tokens"))])

    assert status == 1
    assert out == ""
    assert "max_tokens" in err


def test_a_reply_with_no_text_is_refused(run):
    status, out, err, _ = run([(200, _reply(text=None))])

    assert status == 1
    assert out == ""
    assert "no text" in err


@pytest.mark.parametrize("status_code, body", (
    (429, {"error": {"code": "1308", "message":
                     "Usage limit reached for 5 hour. Your limit will reset "
                     "at 2026-09-24 04:00:00"}}),
    (429, {"error": {"code": "1310", "message": "Weekly Limit Exhausted"}}),
    (429, {"error": {"code": "9999", "message": "something new"}}),
    (429, "not json"),
    (400, {"error": {"code": 1113, "message": "Insufficient balance"}}),
))
def test_a_spent_quota_has_its_own_status_and_marker(run, status_code, body):
    status, out, err, requests = run([(status_code, body)])

    assert status == zai_exec.EXIT_QUOTA == 75
    assert out == ""
    assert err.startswith("zai-exec: quota-exhausted: HTTP {}".format(
        status_code))
    assert len(requests) == 1, "a spent window is not retried"


BUSY = (429, {"error": {"code": "1302", "message": "High concurrency"}})


def test_a_concurrency_refusal_is_retried_until_the_deadline_not_a_count(run):
    """#1411: three quick retries (about 65 seconds) could not outlast
    another GLM-5.3 call holding the Lite plan's slot for minutes. Retries
    continue, with backoff, for as long as the deadline leaves room."""
    responses = [BUSY] * 6 + [
        (500, {"error": {"message": "upstream"}}),
        (429, {"error": {"code": "1305", "message": "overloaded"}}),
        (200, _reply()),
    ]
    status, out, err, requests = run(responses, "--timeout", "1080")

    assert status == 0, err
    assert out == '{"ok": true}'
    assert len(requests) == 9
    assert run.clock.sleeps == [5.0, 10.0, 20.0, 40.0, 60.0, 60.0, 60.0, 60.0]
    assert "retrying" in err


def test_no_retry_waits_past_the_callers_deadline(run):
    """The engine's bound kills a call that outlives it; a retry that would
    leave too little time for the call itself stops instead, and says why."""
    # 60 seconds: waits of 5 and 10 leave room for a call; 20 more would not.
    status, _, err, requests = run([BUSY] * 10, "--timeout", "60")

    assert status == 1
    assert len(requests) == 3
    assert run.clock.sleeps == [5.0, 10.0]
    assert "HTTP 429 code 1302" in err
    assert "no time left to retry" in err
    assert "quota-exhausted" not in err


def test_a_dropped_connection_is_retried_not_a_traceback(run):
    status, out, err, requests = run(
        [("drop", None), ("drop", None), (200, _reply())], "--timeout", "600")

    assert status == 0, err
    assert out == '{"ok": true}'
    assert len(requests) == 3
    assert "Traceback" not in err
    assert "connection failed" in err


def test_a_redirect_is_refused_and_the_key_goes_nowhere_else(run):
    with _Server([(200, _reply())]) as elsewhere:
        status, out, err, requests = run(
            [("redirect", elsewhere.url)], "--timeout", "600")
    assert status == 1
    assert out == ""
    assert len(requests) == 1
    assert elsewhere.requests == [], "x-api-key must not follow a redirect"
    assert "HTTP 307" in err


def test_an_unexpected_error_is_one_line_not_a_traceback(run, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("something odd")

    monkeypatch.setattr(zai_exec, "_post", boom)
    status, _, err, _ = run([])

    assert status == 1
    assert err == "zai-exec: unexpected RuntimeError: something odd\n"


def test_a_client_error_fails_closed(run):
    status, _, err, requests = run([(401, {"error": {
        "code": "1002", "message": "invalid token"}})])

    assert status == 1
    assert "HTTP 401 code 1002" in err
    assert len(requests) == 1


def test_an_unreachable_endpoint_fails_closed(run, monkeypatch, capsys):
    with _Server([]) as server:
        url = server.url
    monkeypatch.setenv("ZAI_EXEC_URL", url)
    status = zai_exec.main(["--prompt-file", str(run.prompt), "--timeout", "5"])
    assert status == 1
    assert "quota-exhausted" not in capsys.readouterr().err


def test_an_empty_prompt_is_a_usage_error(run):
    run.prompt.write_text("  \n")
    status, _, err, requests = run([])
    assert status == 2
    assert "empty" in err
    assert requests == []


def test_the_key_comes_from_the_keychain_reader_usage_already_has(
        run, monkeypatch):
    import usage

    monkeypatch.delenv("ZAI_API_KEY")
    monkeypatch.setattr(usage, "_zai_key", lambda: "keychain-key")
    status, _, err, requests = run([(200, _reply())])

    assert status == 0, err
    assert requests[0]["headers"]["x-api-key"] == "keychain-key"


def test_no_key_is_a_usage_error_not_a_call(run, monkeypatch):
    import usage

    monkeypatch.delenv("ZAI_API_KEY")
    monkeypatch.setattr(usage, "_zai_key", lambda: None)
    status, _, err, requests = run([])

    assert status == 2
    assert "no z.ai key" in err
    assert requests == []


def test_the_call_log_holds_metadata_and_never_the_prompt_or_answer(run):
    status, _, err, _ = run([(200, _reply(
        text="the secret answer",
        usage={"input_tokens": 27, "output_tokens": 98,
               "cache_read_input_tokens": 5,
               "cache_creation_input_tokens": 10}))])

    assert status == 0, err
    path = run.logs / "model-io-session-1.jsonl"
    raw = path.read_text()
    assert "the packet goes here" not in raw
    assert "the secret answer" not in raw
    assert "private reasoning" not in raw
    row = json.loads(raw)
    assert row["type"] == "model_io"
    assert row["model"] == "glm-5.3"
    # input_tokens is the uncached input alone on this endpoint; cache reads
    # and writes sit beside it. The four kinds are disjoint — a cache write is
    # counted once, not also as fresh input (#1411) — and sum to the total.
    usage = row["response"]["usage"]
    assert usage == {
        "total_input_tokens": 42, "fresh_input_tokens": 27,
        "cache_read_input_tokens": 5, "cache_write_input_tokens": 10,
        "output_tokens": 98,
    }
    assert usage["total_input_tokens"] == sum(
        usage[kind] for kind in ("fresh_input_tokens",
                                 "cache_read_input_tokens",
                                 "cache_write_input_tokens"))


def test_heartbeat_reads_zcodes_model_and_input_from_the_call_log(
        run, monkeypatch):
    """Not the retired zcode app's rollout, whose newest file is from
    2026-09-09 and would stamp that session onto every engine run."""
    run([(200, _reply(id="msg_lister"))])
    run([(200, _reply(id="msg_judge"))])
    pattern = str(run.logs / "*.jsonl")
    monkeypatch.setitem(heartbeat.MODEL_SOURCES, "zcode", pattern)
    monkeypatch.setitem(session_usage.SESSION_GLOBS, "zcode", pattern)

    assert heartbeat.detect_model("zcode")["model"] == "glm-5.3"
    assert heartbeat.input_usage("zcode") == {
        "total_input_tokens": 64, "fresh_input_tokens": 54,
        "ratio": 64 / 54,
    }
    assert session_usage.usage_for_session("zcode", "session-1") == {
        "fresh_input_tokens": 54, "cache_read_input_tokens": 10,
        "cache_write_input_tokens": 0, "output_tokens": 196,
    }


def test_a_run_reads_only_its_own_call_log(run, monkeypatch):
    """#1411: a run that stopped before any model call must not inherit the
    previous run's model and tokens from the newest file."""
    run([(200, _reply(id="msg_earlier"))])            # session-1's call
    pattern = str(run.logs / "*.jsonl")
    monkeypatch.setitem(heartbeat.MODEL_SOURCES, "zcode", pattern)
    monkeypatch.setitem(session_usage.SESSION_GLOBS, "zcode", pattern)

    # A later run, session-2, made no model call: nothing is known.
    monkeypatch.setenv("ZCODE_SESSION_ID", "session-2")
    assert heartbeat.detect_model("zcode")["model"] is None
    assert heartbeat.input_usage("zcode") is None
    assert session_usage.usage_for_session("zcode", "session-2") is None
    # A session id that merely contains another's is not that session.
    assert session_usage.usage_for_session("zcode", "session") is None

    # No session id at all reads as unknown, never as the newest file.
    monkeypatch.delenv("ZCODE_SESSION_ID")
    assert heartbeat.detect_model("zcode")["model"] is None
    assert heartbeat.input_usage("zcode") is None

    monkeypatch.setenv("ZCODE_SESSION_ID", "session-1")
    assert heartbeat.detect_model("zcode")["model"] == "glm-5.3"


def test_zcode_no_longer_reads_the_retired_apps_rollout():
    for source in (heartbeat.MODEL_SOURCES["zcode"],
                   session_usage.SESSION_GLOBS["zcode"]):
        assert ".zcode" not in source
        assert "zai-exec" in source


def test_the_script_runs_as_an_executable(tmp_path):
    """The engine executes it directly, so the mode and shebang matter."""
    assert os.access(SCRIPT, os.X_OK)
    assert SCRIPT.read_text().startswith("#!/usr/bin/env python3\n")
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("hello")
    with _Server([(200, _reply(text="hi"))]) as server:
        env = dict(os.environ, ZAI_API_KEY="k", ZAI_EXEC_URL=server.url,
                   ZAI_EXEC_LOG_DIR=str(tmp_path / "logs"))
        proc = subprocess.run(
            [str(SCRIPT), "--prompt-file", str(prompt), "--timeout", "20"],
            env=env, capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == "hi"
