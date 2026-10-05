#!/usr/bin/env python3
"""Credential broker for the Codex implementation runner.

The socket protocol exposes only two operations: begin and finish. The server
owns the GitHub credentials; callers provide fixed begin options or a finish
answer, never a command, checkout path, repository, remote, or refspec.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import pathlib
import pwd
import re
import socket
import stat
import subprocess
import sys
from typing import Callable, Dict, Mapping, Optional, Sequence, Tuple


BROKER_VERSION = 1
RUNNER_ROOT = pathlib.Path("/Users/nateprich/.claude/command-center-run")
BROKER_INSTALL_DIR = pathlib.Path(
    "/Users/nateprich/.claude/command-center-broker"
)
BROKER_SOURCE_NAME = "credential_broker.py"
MANIFEST_NAME = "manifest.json"
EMPTY_HOOKS_NAME = "hooks-empty"
GIT_PROXY_NAME = "git"
DEFAULT_SOCKET = pathlib.Path(
    "/Users/Shared/command-center-broker/broker.sock"
)
CODEX_USER = "codex"
CODEX_UID = 506
CODEX_CHECKOUT_ROOT = pathlib.Path(
    "/Users/codex/.claude/command-center-heartbeat/codex-runs"
)
MAX_REQUEST_BYTES = 1024 * 1024
MAX_ANSWER_BYTES = 64 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
RUN_ID_RE = re.compile(r"[0-9a-f]{12}\Z")
TICKET_DIR_RE = re.compile(
    r"ticket-([1-9][0-9]*)-([0-9]{8}T[0-9]{12}Z)\Z"
)
WORK_RE = re.compile(
    r"([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)#([1-9][0-9]*)\Z"
)
FIXED_PATH = (
    "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
)


class BrokerError(Exception):
    """A request was refused before an unsafe effect could occur."""


def verify_installed_copy(
    module_file: Optional[pathlib.Path] = None,
    manifest_file: Optional[pathlib.Path] = None,
    *,
    require_location: bool = False,
) -> None:
    """Fail closed when the installed broker has drifted or runs from source."""
    module_path = pathlib.Path(module_file or __file__).resolve()
    manifest_path = pathlib.Path(
        manifest_file or module_path.parent / MANIFEST_NAME
    )
    if require_location:
        expected = (BROKER_INSTALL_DIR / BROKER_SOURCE_NAME).resolve()
        if module_path != expected:
            raise BrokerError(
                "refusing to serve from a checkout; run the versioned installer"
            )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        actual = hashlib.sha256(module_path.read_bytes()).hexdigest()
    except (OSError, ValueError, UnicodeError) as exc:
        raise BrokerError("installed broker manifest is unreadable") from exc
    if (
        not isinstance(manifest, dict)
        or manifest.get("version") != BROKER_VERSION
        or manifest.get("sha256") != actual
    ):
        raise BrokerError(
            "installed broker drift detected; run the versioned installer"
        )


def _valid_run_id(value: object) -> bool:
    return isinstance(value, str) and RUN_ID_RE.fullmatch(value) is not None


def validate_request(payload: object) -> Dict[str, object]:
    """Validate the complete public request shape before dispatch."""
    if not isinstance(payload, dict):
        raise BrokerError("request must be a JSON object")
    verb = payload.get("verb")
    if verb == "begin":
        if set(payload) != {"verb", "tier"}:
            raise BrokerError("begin accepts only the tier option")
        tier = payload.get("tier")
        if tier not in ("standard", "escalated"):
            raise BrokerError("begin tier must be standard or escalated")
        return {"verb": "begin", "tier": tier}
    if verb == "finish":
        if set(payload) != {"verb", "run", "answer"}:
            raise BrokerError("finish accepts only run and answer")
        run = payload.get("run")
        answer = payload.get("answer")
        if not _valid_run_id(run):
            raise BrokerError("finish requires a valid heartbeat run id")
        if not isinstance(answer, dict):
            raise BrokerError("finish answer must be a JSON object")
        if len(json.dumps(answer, ensure_ascii=False).encode("utf-8")) > MAX_ANSWER_BYTES:
            raise BrokerError("finish answer is too large")
        return {"verb": "finish", "run": run, "answer": answer}
    if isinstance(verb, str):
        raise BrokerError("unsupported broker verb: {}".format(verb))
    raise BrokerError("request must name the begin or finish verb")


def work_ref(work: object) -> Tuple[str, int, str]:
    """Parse a heartbeat ticket binding into repo, issue number, and ref."""
    if not isinstance(work, str):
        raise BrokerError("run has no ticket binding")
    match = WORK_RE.fullmatch(work)
    if match is None:
        raise BrokerError("run ticket binding is malformed")
    repo = "{}/{}".format(match.group(1), match.group(2))
    number = int(match.group(3))
    return repo, number, "{}#{}".format(repo, number)


def validate_finish_snapshot(
    run: str, snapshot: Mapping[str, object]
) -> Dict[str, object]:
    """Require an open Codex run whose live Project claim it still owns."""
    start = snapshot.get("start")
    binding = snapshot.get("binding")
    if (
        not isinstance(start, dict)
        or start.get("run") != run
        or start.get("agent") != "codex"
        or snapshot.get("pairing") != "open"
    ):
        raise BrokerError("finish run is not an open Codex run")
    if not isinstance(binding, dict) or binding.get("do") != "ticket":
        raise BrokerError("finish run is not bound to a ticket")
    repo, number, ref = work_ref(binding.get("work"))
    if snapshot.get("claim_state") != "owned":
        raise BrokerError("finish run no longer owns the live ticket claim")
    return {"run": run, "repo": repo, "number": number, "ref": ref}


_LIVE_CONTEXT_SCRIPT = r'''
import json
import sys
import heartbeat
from engine import implement

run = sys.argv[1]
records = heartbeat.read_github_strict("codex")
view = heartbeat.run_view(records, run)
if not isinstance(view, dict):
    raise SystemExit("heartbeat run is unreadable")
start = view.get("start")
binding = view.get("binding")
if not isinstance(start, dict) or start.get("agent") != "codex":
    raise SystemExit("heartbeat run is not a Codex start")
if view.get("pairing") != "open":
    raise SystemExit("heartbeat run is already finished")
if not isinstance(binding, dict) or binding.get("do") != "ticket":
    raise SystemExit("heartbeat run is not bound to a ticket")
work = binding.get("work")
if not isinstance(work, str):
    raise SystemExit("heartbeat ticket binding is unreadable")
state, _items = implement._claim_state(work, run, "codex")
print(json.dumps({
    "start": start,
    "binding": binding,
    "pairing": view.get("pairing"),
    "claim_state": state,
}))
'''


def _home_for_runner() -> pathlib.Path:
    try:
        return pathlib.Path(pwd.getpwnam("nateprich").pw_dir)
    except KeyError as exc:
        raise BrokerError("broker owner account nateprich is unavailable") from exc


def runner_environment(
    *,
    home: Optional[pathlib.Path] = None,
    runner_root: pathlib.Path = RUNNER_ROOT,
) -> Dict[str, str]:
    """Build a small environment from trusted paths, never caller variables."""
    trusted_home = pathlib.Path(home or _home_for_runner())
    tmpdir = pathlib.Path("/private/tmp")
    if not tmpdir.is_dir():
        tmpdir = pathlib.Path("/tmp")
    return {
        "HOME": str(trusted_home),
        "PATH": FIXED_PATH,
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "TMPDIR": str(tmpdir),
        "GH_CONFIG_DIR": str(trusted_home / ".config" / "gh"),
        "GH_PROMPT_DISABLED": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(trusted_home / ".gitconfig"),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_SSH": "/usr/bin/false",
        "GIT_SSH_COMMAND": "/usr/bin/false",
        "GIT_ALLOW_PROTOCOL": "https",
        "PYTHONPATH": str(pathlib.Path(runner_root)),
    }


def trusted_git_identity(home: pathlib.Path) -> Tuple[str, str]:
    """Read only the two commit identity fields from Nate's trusted config."""
    env = {
        "HOME": str(home),
        "PATH": FIXED_PATH,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(pathlib.Path(home) / ".gitconfig"),
    }
    values = []
    for key in ("user.name", "user.email"):
        result = subprocess.run(
            ["/usr/bin/git", "config", "--global", "--get", key],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        value = (result.stdout or "").strip()
        if result.returncode != 0 or not value:
            raise BrokerError("trusted Git commit identity is unavailable")
        values.append(value)
    return values[0], values[1]


def _repo_url(repo: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise BrokerError("bound repository name is malformed")
    return "https://github.com/{}.git".format(repo)


def verify_git_proxy(directory: pathlib.Path) -> None:
    """Require a private proxy linked to the drift-checked broker module."""
    directory = pathlib.Path(directory)
    module = directory / BROKER_SOURCE_NAME
    proxy = directory / GIT_PROXY_NAME
    if (not proxy.is_symlink() or proxy.readlink() != pathlib.Path(BROKER_SOURCE_NAME)
            or not module.is_file()):
        raise BrokerError("installed trusted Git proxy is unavailable")
    verify_installed_copy(module, directory / MANIFEST_NAME)


def finish_environment(
    checkout: pathlib.Path,
    repo: str,
    number: int,
    hooks_path: pathlib.Path,
    *,
    home: Optional[pathlib.Path] = None,
    runner_root: pathlib.Path = RUNNER_ROOT,
    remote_url: Optional[str] = None,
    credential_helper: str = "!gh auth git-credential",
    allowed_protocols: str = "https",
    git_proxy_dir: Optional[pathlib.Path] = None,
) -> Dict[str, str]:
    """Pin Git and GitHub CLI behavior for a hostile model-writable checkout."""
    checkout = pathlib.Path(checkout).resolve()
    branch = "ticket/{}".format(number)
    url = remote_url or _repo_url(repo)
    trusted_home = pathlib.Path(home or _home_for_runner())
    env = runner_environment(home=trusted_home, runner_root=runner_root)
    proxy_dir = pathlib.Path(git_proxy_dir or BROKER_INSTALL_DIR)
    verify_git_proxy(proxy_dir)
    git_name, git_email = trusted_git_identity(trusted_home)
    # Ignore every global safe.directory, alias, and executable setting. The
    # two trusted identity fields are copied below; auth uses the fixed helper.
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    # Git has no environment switch that discards .git/config. Route every
    # ordinary `git` call from finish-ticket through the installed, verified
    # broker copy, which admits built-in commands only. Local aliases cannot
    # become commands even if the model rewrites .git/config during finish.
    env["PATH"] = "{}:{}".format(
        proxy_dir, FIXED_PATH
    )
    env.update({
        "GIT_DIR": str(checkout / ".git"),
        "GIT_WORK_TREE": str(checkout),
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_ALLOW_PROTOCOL": allowed_protocols,
        "GH_PAGER": "cat",
    })
    pairs = [
        ("core.hooksPath", str(pathlib.Path(hooks_path).resolve())),
        ("core.fsmonitor", "false"),
        ("core.sshCommand", "/usr/bin/false"),
        # Empty resets any repo-local helpers; the following helper is trusted.
        ("credential.helper", ""),
        ("credential.helper", credential_helper),
        ("safe.directory", str(checkout)),
        ("remote.origin.url", url),
        ("remote.origin.pushurl", url),
        ("remote.origin.fetch", "+refs/heads/main:refs/remotes/origin/main"),
        ("remote.origin.fetch",
         "+refs/heads/{0}:refs/remotes/origin/{0}".format(branch)),
        ("remote.origin.push",
         "refs/heads/{0}:refs/heads/{0}".format(branch)),
        ("branch.{}.remote".format(branch), "origin"),
        ("branch.{}.merge".format(branch), "refs/heads/{}".format(branch)),
        ("user.name", git_name),
        ("user.email", git_email),
    ]
    env["GIT_CONFIG_COUNT"] = str(len(pairs))
    for index, (key, value) in enumerate(pairs):
        env["GIT_CONFIG_KEY_{}".format(index)] = key
        env["GIT_CONFIG_VALUE_{}".format(index)] = value
    return env


def _empty_directory(path: pathlib.Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISDIR(info.st_mode) and not path.is_symlink() and not any(path.iterdir())


def _git_call(
    args: Sequence[str],
    checkout: pathlib.Path,
    env: Mapping[str, str],
    *,
    runner: Callable = subprocess.run,
) -> subprocess.CompletedProcess:
    return runner(
        ["/usr/bin/git"] + list(args),
        cwd=str(checkout),
        env=dict(env),
        capture_output=True,
        text=True,
        check=False,
    )


def verify_checkout(
    checkout: pathlib.Path,
    repo: str,
    number: int,
    hooks_path: pathlib.Path,
    *,
    expected_uid: Optional[int] = None,
    home: Optional[pathlib.Path] = None,
    runner_root: pathlib.Path = RUNNER_ROOT,
    remote_url: Optional[str] = None,
    allowed_protocols: str = "https",
    git_proxy_dir: Optional[pathlib.Path] = None,
    runner: Callable = subprocess.run,
) -> Dict[str, str]:
    """Check the exact ticket checkout and fixed effective remote before finish."""
    checkout = pathlib.Path(checkout)
    if checkout.is_symlink():
        raise BrokerError("bound checkout cannot be a symlink")
    try:
        resolved = checkout.resolve(strict=True)
        info = resolved.stat()
        git_info = (resolved / ".git").lstat()
    except OSError as exc:
        raise BrokerError("bound ticket checkout is unavailable") from exc
    if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise BrokerError("bound checkout is not an owner-only directory")
    if expected_uid is not None and info.st_uid != expected_uid:
        raise BrokerError("bound checkout is not owned by the Codex user")
    if not stat.S_ISDIR(git_info.st_mode) or stat.S_ISLNK(git_info.st_mode):
        raise BrokerError("bound checkout is not a regular Git clone")
    if not _empty_directory(pathlib.Path(hooks_path)):
        raise BrokerError("installed empty hooks directory is unavailable")
    env = finish_environment(
        resolved, repo, number, hooks_path,
        home=home, runner_root=runner_root, remote_url=remote_url,
        allowed_protocols=allowed_protocols,
        git_proxy_dir=git_proxy_dir,
    )
    branch = _git_call(
        ["branch", "--show-current"], resolved, env, runner=runner
    )
    top = _git_call(["rev-parse", "--show-toplevel"], resolved, env, runner=runner)
    remote = _git_call(
        ["remote", "get-url", "--push", "origin"],
        resolved, env, runner=runner,
    )
    expected_url = remote_url or _repo_url(repo)
    if (
        branch.returncode != 0
        or branch.stdout.strip() != "ticket/{}".format(number)
        or top.returncode != 0
        or pathlib.Path(top.stdout.strip()).resolve() != resolved
        or remote.returncode != 0
        or remote.stdout.strip() != expected_url
    ):
        raise BrokerError(
            "bound checkout branch or effective origin does not match the live claim"
        )
    return env


def find_ticket_checkout(
    repo: str,
    number: int,
    *,
    root: pathlib.Path = CODEX_CHECKOUT_ROOT,
    expected_uid: Optional[int] = CODEX_UID,
    hooks_path: pathlib.Path,
    home: Optional[pathlib.Path] = None,
    runner_root: pathlib.Path = RUNNER_ROOT,
    runner: Callable = subprocess.run,
) -> pathlib.Path:
    """Resolve one owner-only checkout by the GitHub-bound ticket number."""
    root = pathlib.Path(root)
    if root.is_symlink():
        raise BrokerError("Codex checkout root cannot be a symlink")
    try:
        root_info = root.stat()
    except OSError as exc:
        raise BrokerError("Codex checkout root is unavailable") from exc
    if not stat.S_ISDIR(root_info.st_mode) or root_info.st_mode & 0o077:
        raise BrokerError("Codex checkout root must be owner-only")
    if expected_uid is not None and root_info.st_uid != expected_uid:
        raise BrokerError("Codex checkout root has the wrong owner")
    candidates = []
    for path in root.iterdir():
        match = TICKET_DIR_RE.fullmatch(path.name)
        if match and int(match.group(1)) == number:
            candidates.append(path)
    if len(candidates) != 1:
        raise BrokerError(
            "expected exactly one live checkout for the bound ticket"
        )
    verify_checkout(
        candidates[0], repo, number, hooks_path,
        expected_uid=expected_uid, home=home,
        runner_root=runner_root, runner=runner,
    )
    return candidates[0]


def _run_json_command(
    args: Sequence[str],
    *,
    cwd: pathlib.Path,
    env: Mapping[str, str],
    runner: Callable = subprocess.run,
) -> subprocess.CompletedProcess:
    try:
        return runner(
            list(args), cwd=str(cwd), env=dict(env),
            capture_output=True, text=True, check=False,
        )
    except OSError as exc:
        raise BrokerError("trusted runner command could not start") from exc


def read_live_context(
    run: str,
    *,
    runner_root: pathlib.Path = RUNNER_ROOT,
    home: Optional[pathlib.Path] = None,
    runner: Callable = subprocess.run,
) -> Dict[str, object]:
    """Read GitHub heartbeat binding and live Project claim for every finish."""
    if not _valid_run_id(run):
        raise BrokerError("finish requires a valid heartbeat run id")
    env = runner_environment(home=home, runner_root=runner_root)
    result = _run_json_command(
        [sys.executable, "-c", _LIVE_CONTEXT_SCRIPT, run],
        cwd=runner_root,
        env=env,
        runner=runner,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise BrokerError(
            "could not verify the live GitHub claim{}".format(
                ": " + detail[:2000] if detail else ""
            )
        )
    try:
        snapshot = json.loads(result.stdout)
    except ValueError as exc:
        raise BrokerError("live claim response was not valid JSON") from exc
    if not isinstance(snapshot, dict):
        raise BrokerError("live claim response was not an object")
    return validate_finish_snapshot(run, snapshot)


def run_begin(
    tier: str,
    *,
    runner_root: pathlib.Path = RUNNER_ROOT,
    home: Optional[pathlib.Path] = None,
    runner: Callable = subprocess.run,
) -> Dict[str, object]:
    """Run begin once with fixed agent and bounded caller options."""
    if tier not in ("standard", "escalated"):
        raise BrokerError("begin tier must be standard or escalated")
    result = _run_json_command(
        [
            sys.executable, str(pathlib.Path(runner_root) / "funnel.py"),
            "begin", "--agent", "codex", "--tier", tier,
        ],
        cwd=runner_root,
        env=runner_environment(home=home, runner_root=runner_root),
        runner=runner,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return {
            "ok": False,
            "error": detail[-MAX_RESPONSE_BYTES:] or "begin failed",
            "returncode": result.returncode,
        }
    try:
        packet = json.loads(result.stdout)
    except ValueError as exc:
        raise BrokerError(
            "begin returned no readable packet; do not retry the request"
        ) from exc
    if not isinstance(packet, dict):
        raise BrokerError(
            "begin returned no readable packet; do not retry the request"
        )
    return {"ok": True, "packet": packet}


def run_finish(
    context: Mapping[str, object],
    answer: Mapping[str, object],
    *,
    hooks_path: pathlib.Path,
    runner_root: pathlib.Path = RUNNER_ROOT,
    home: Optional[pathlib.Path] = None,
    remote_url: Optional[str] = None,
    credential_helper: str = "!gh auth git-credential",
    allowed_protocols: str = "https",
    git_proxy_dir: Optional[pathlib.Path] = None,
    runner: Callable = subprocess.run,
) -> Dict[str, object]:
    """Run finish-ticket only for the claim-derived checkout and branch."""
    checkout = pathlib.Path(str(context["checkout"])).resolve()
    repo = str(context["repo"])
    number = int(context["number"])
    run = str(context["run"])
    env = verify_checkout(
        checkout, repo, number, hooks_path,
        expected_uid=context.get("expected_uid"),
        home=home, runner_root=runner_root, remote_url=remote_url,
        allowed_protocols=allowed_protocols, runner=runner,
        git_proxy_dir=git_proxy_dir,
    )
    if credential_helper != "!gh auth git-credential":
        env = finish_environment(
            checkout, repo, number, hooks_path,
            home=home, runner_root=runner_root, remote_url=remote_url,
            credential_helper=credential_helper,
            allowed_protocols=allowed_protocols,
            git_proxy_dir=git_proxy_dir,
        )
    encoded_answer = json.dumps(
        dict(answer), ensure_ascii=False, separators=(",", ":")
    )
    result = _run_json_command(
        [
            sys.executable,
            str(pathlib.Path(runner_root) / "finish-ticket"),
            "--run", run,
            "--agent", "codex",
            "--repo", repo,
            "--answer", encoded_answer,
        ],
        cwd=checkout,
        env=env,
        runner=runner,
    )
    return {
        "ok": result.returncode == 0,
        "returncode": result.returncode,
        "stdout": (result.stdout or "")[-MAX_RESPONSE_BYTES:],
        "stderr": (result.stderr or "")[-MAX_RESPONSE_BYTES:],
    }


def dispatch_request(
    payload: object,
    *,
    peer_uid: int,
    expected_uid: int = CODEX_UID,
    runner_root: pathlib.Path = RUNNER_ROOT,
    checkout_root: pathlib.Path = CODEX_CHECKOUT_ROOT,
    hooks_path: Optional[pathlib.Path] = None,
    home: Optional[pathlib.Path] = None,
    runner: Callable = subprocess.run,
    live_context_reader: Callable = read_live_context,
    checkout_finder: Callable = find_ticket_checkout,
    begin_runner: Callable = run_begin,
    finish_runner: Callable = run_finish,
) -> Dict[str, object]:
    """Authenticate and dispatch one complete broker request."""
    if peer_uid != expected_uid:
        raise BrokerError("socket peer is not the isolated Codex user")
    request = validate_request(payload)
    if request["verb"] == "begin":
        return begin_runner(
            str(request["tier"]),
            runner_root=runner_root, home=home, runner=runner,
        )
    context = live_context_reader(
        str(request["run"]),
        runner_root=runner_root, home=home, runner=runner,
    )
    if hooks_path is None:
        hooks_path = BROKER_INSTALL_DIR / EMPTY_HOOKS_NAME
    checkout = checkout_finder(
        str(context["repo"]), int(context["number"]),
        root=checkout_root, expected_uid=expected_uid,
        hooks_path=hooks_path, home=home,
        runner_root=runner_root, runner=runner,
    )
    full_context = dict(context)
    full_context.update({
        "checkout": str(checkout),
        "expected_uid": expected_uid,
    })
    return finish_runner(
        full_context,
        request["answer"],
        hooks_path=hooks_path,
        runner_root=runner_root,
        home=home,
        runner=runner,
    )


def _peer_uid(connection: socket.socket) -> int:
    """Read the Unix socket peer UID with macOS getpeereid; fail closed."""
    try:
        function = ctypes.CDLL(None, use_errno=True).getpeereid
    except AttributeError as exc:
        raise BrokerError("getpeereid is unavailable; refusing socket peer") from exc
    uid = ctypes.c_uint()
    gid = ctypes.c_uint()
    function.argtypes = [
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_uint),
        ctypes.POINTER(ctypes.c_uint),
    ]
    function.restype = ctypes.c_int
    result = function(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid))
    if result != 0:
        raise BrokerError("could not authenticate socket peer")
    return int(uid.value)


def _read_request(connection: socket.socket) -> object:
    chunks = bytearray()
    while b"\n" not in chunks:
        block = connection.recv(min(65536, MAX_REQUEST_BYTES + 1 - len(chunks)))
        if not block:
            break
        chunks.extend(block)
        if len(chunks) > MAX_REQUEST_BYTES:
            raise BrokerError("request exceeds the size limit")
    if b"\n" in chunks:
        line, trailing = bytes(chunks).split(b"\n", 1)
        if trailing.strip():
            raise BrokerError("one request is allowed per socket connection")
    else:
        line = bytes(chunks)
    if not line:
        raise BrokerError("request is empty")
    try:
        return json.loads(line.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise BrokerError("request is not valid JSON") from exc


def _socket_acl(path: pathlib.Path, principal: str, permission: str) -> None:
    result = subprocess.run(
        ["/bin/chmod", "+a", "{} allow {}".format(principal, permission), str(path)],
        env=runner_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise BrokerError("could not apply the broker socket ACL")


def _verify_socket_directory(path: pathlib.Path, owner_uid: int) -> None:
    if path.is_symlink():
        raise BrokerError("broker socket directory cannot be a symlink")
    try:
        info = path.stat()
    except OSError as exc:
        raise BrokerError(
            "broker socket directory is missing; run the cutover installer"
        ) from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != owner_uid
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise BrokerError("broker socket directory owner or mode is unsafe")
    listing = subprocess.run(
        ["/bin/ls", "-lde", str(path)],
        capture_output=True, text=True, check=False,
    )
    if listing.returncode != 0 or "codex allow search" not in listing.stdout:
        raise BrokerError("broker socket directory lacks the Codex search ACL")


def _handle_connection(
    connection: socket.socket,
    *,
    expected_uid: int,
    dispatch: Callable,
    peer_uid_reader: Callable = _peer_uid,
) -> None:
    try:
        peer = peer_uid_reader(connection)
        if peer != expected_uid:
            raise BrokerError("socket peer is not the isolated Codex user")
        request = _read_request(connection)
        response = dispatch(request, peer_uid=peer, expected_uid=expected_uid)
    except BrokerError as exc:
        response = {"ok": False, "error": str(exc)}
    except Exception:
        response = {"ok": False, "error": "broker request failed closed"}
    encoded = json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n"
    if len(encoded) > MAX_RESPONSE_BYTES:
        encoded = json.dumps({
            "ok": False, "error": "broker response exceeds the size limit",
        }).encode("utf-8") + b"\n"
    connection.sendall(encoded)


def serve_forever(
    socket_path: Optional[pathlib.Path] = None,
    *,
    server_factory: Callable = socket.socket,
    peer_uid_reader: Callable = _peer_uid,
    dispatch: Callable = dispatch_request,
) -> None:
    """Serve serial requests; no local run table, queue, or retry is kept."""
    owner = pwd.getpwnam("nateprich").pw_uid
    if os.geteuid() != owner:
        raise BrokerError("broker server must run as nateprich")
    codex_uid = pwd.getpwnam(CODEX_USER).pw_uid
    if codex_uid != CODEX_UID:
        raise BrokerError("Codex UID does not match the installed access policy")
    path = pathlib.Path(
        socket_path or os.environ.get("BROKER_SOCKET") or DEFAULT_SOCKET
    )
    _verify_socket_directory(path.parent, owner)
    if path.exists() or path.is_symlink():
        raise BrokerError("broker socket path already exists; refusing to replace it")
    server = server_factory(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        server.bind(str(path))
        os.chmod(path, 0o600)
        _socket_acl(path, CODEX_USER, "write")
        server.listen(1)
        while True:
            connection, _address = server.accept()
            with connection:
                _handle_connection(
                    connection,
                    expected_uid=codex_uid,
                    dispatch=dispatch,
                    peer_uid_reader=peer_uid_reader,
                )
    finally:
        server.close()


def socket_request(
    payload: Mapping[str, object],
    *,
    socket_path: Optional[pathlib.Path] = None,
) -> Dict[str, object]:
    """Send one request; never retry or fall back to direct GitHub access."""
    path = pathlib.Path(
        socket_path or os.environ.get("BROKER_SOCKET") or DEFAULT_SOCKET
    )
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(30 * 60)
    try:
        connection.connect(str(path))
    except OSError as exc:
        connection.close()
        raise BrokerError(
            "credential broker is unavailable; no fallback or retry was attempted"
        ) from exc
    try:
        connection.sendall(
            json.dumps(dict(payload), ensure_ascii=False).encode("utf-8") + b"\n"
        )
        chunks = bytearray()
        while b"\n" not in chunks:
            block = connection.recv(65536)
            if not block:
                break
            chunks.extend(block)
            if len(chunks) > MAX_RESPONSE_BYTES:
                raise BrokerError("broker response exceeds the size limit")
    except OSError as exc:
        raise BrokerError(
            "broker response was lost; inspect heartbeat state before retrying"
        ) from exc
    finally:
        connection.close()
    try:
        response = json.loads(bytes(chunks).split(b"\n", 1)[0].decode("utf-8"))
    except (IndexError, UnicodeError, ValueError) as exc:
        raise BrokerError(
            "broker response is unreadable; inspect heartbeat state before retrying"
        ) from exc
    if not isinstance(response, dict):
        raise BrokerError("broker returned an invalid response")
    return response


def client_main(argv: Sequence[str]) -> int:
    """Uncredentialed client interface; only begin and finish are accepted."""
    if not argv:
        print("credential-broker: use begin or finish", file=sys.stderr)
        return 2
    verb = argv[0]
    if verb not in ("begin", "finish"):
        print("credential-broker: unsupported broker verb: {}".format(verb),
              file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(prog="credential-broker {}".format(verb))
    if verb == "begin":
        parser.add_argument("--tier", required=True, choices=("standard", "escalated"))
    else:
        parser.add_argument("--run", required=True)
        answer_group = parser.add_mutually_exclusive_group(required=True)
        answer_group.add_argument("--answer", help="structured JSON answer")
        answer_group.add_argument("--answer-file", help="path to JSON answer")
    args = parser.parse_args(list(argv[1:]))
    if verb == "begin":
        payload = {"verb": "begin", "tier": args.tier}
    else:
        try:
            raw = (
                pathlib.Path(args.answer_file).read_text(encoding="utf-8")
                if args.answer_file else args.answer
            )
            answer = json.loads(raw)
        except (OSError, UnicodeError, ValueError) as exc:
            print("credential-broker: answer is not readable JSON: {}".format(exc),
                  file=sys.stderr)
            return 2
        payload = {"verb": "finish", "run": args.run, "answer": answer}
    try:
        response = socket_request(payload)
    except BrokerError as exc:
        print("credential-broker: {}".format(exc), file=sys.stderr)
        return 1
    if verb == "begin" and response.get("ok"):
        print(json.dumps(response.get("packet"), ensure_ascii=False, indent=2))
        return 0
    if verb == "finish":
        if response.get("stdout"):
            sys.stdout.write(str(response["stdout"]))
        if response.get("stderr"):
            sys.stderr.write(str(response["stderr"]))
        return 0 if response.get("ok") is True else 1
    print(
        response.get("error", "credential broker request failed"),
        file=sys.stderr,
    )
    return 1


def _git_subcommand(args: Sequence[str]) -> Optional[str]:
    """Find the command after the global Git options used by finish/tests."""
    index = 0
    while index < len(args):
        option = args[index]
        if option in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            index += 2
        elif (option.startswith(("-C", "-c", "--git-dir=", "--work-tree=",
                                 "--namespace=")) and option not in ("-C", "-c")):
            index += 1
        elif option in ("--version", "--help"):
            return option
        elif option.startswith("-"):
            return None
        else:
            return option
    return None


def trusted_git_main(args: Sequence[str]) -> int:
    """Execute Git built-ins only; never expand a checkout's local alias."""
    try:
        verify_installed_copy()
        listed = subprocess.run(
            ["/usr/bin/git", "--list-cmds=builtins"],
            cwd="/", env={"PATH": FIXED_PATH, "HOME": "/",
                          "GIT_CONFIG_NOSYSTEM": "1",
                          "GIT_CONFIG_GLOBAL": "/dev/null"},
            capture_output=True, text=True, check=True, timeout=10,
        )
    except (BrokerError, OSError, subprocess.SubprocessError) as exc:
        print("credential-broker: trusted Git proxy unavailable: {}".format(exc),
              file=sys.stderr)
        return 1
    command = _git_subcommand(args)
    if command not in set(listed.stdout.split()) | {"--version", "--help"}:
        print("credential-broker: Git command is not a built-in", file=sys.stderr)
        return 1
    os.execv("/usr/bin/git", ["/usr/bin/git", *args])
    return 1


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if pathlib.Path(sys.argv[0]).name == GIT_PROXY_NAME:
        return trusted_git_main(args)
    if args:
        return client_main(args)
    try:
        verify_installed_copy(require_location=True)
        hooks = BROKER_INSTALL_DIR / EMPTY_HOOKS_NAME
        if not _empty_directory(hooks):
            raise BrokerError("installed empty hooks directory is missing or dirty")
        serve_forever()
    except (BrokerError, KeyError) as exc:
        print("credential-broker: {}".format(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
