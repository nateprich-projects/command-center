#!/usr/bin/env python3
"""codex_run.py — what a Command Center Codex run must be, read from its own rollout.

The Codex app holds each automation's model, effort, sandbox and approval
policy outside this repository, and it has written stale copies back over
edits made underneath it (LEARNINGS.md, 2026-09-06). So an automation file
says what a run was *meant* to get, not what it got. The run's rollout
records what it got: every `turn_context` record carries the effective
`model`, `effort`, `sandbox_policy` (writable roots, network access) and
`approval_policy`. `funnel.py begin` compares those with the manifest here
before a Codex run may claim anything, and refuses on any difference (#1316).

The refusal fails closed. A run whose rollout cannot be found or read is
refused too: a stopped lane shows in the heartbeat within one run, and a
wrongly configured one working unattended with a full shell does not show
at all.
"""

from __future__ import annotations

import datetime
import glob
import json
import os
from typing import Dict, Iterator, List, Optional, Sequence

#: The model and effort every Command Center Codex run uses (Nate,
#: 2026-09-22, #1315): GPT-6 Luna at `max` on both tiers. The tiers differ
#: in queue and in who may approve, not in model.
MODEL = "gpt-6-luna"
EFFORT = "max"

#: An automation runs unattended, so it must never stop to ask. `never` is
#: what the app records for a scheduled run; anything else is either a hand
#: session or an automation someone has changed.
APPROVAL_POLICY = "never"

#: The run clones its ticket's repository and pushes a branch, so it needs
#: the network. A run without it fails later and looks like a GitHub fault.
NETWORK_ACCESS = True

HOME = os.path.expanduser("~")

#: Where a run may write, as observed on a 2026-09-18 automation run. The
#: heartbeat spool is outside the session so a run's records survive it;
#: session workspaces are where the app puts each run's checkout; the
#: visualizations directory is the app's own scratch area.
HEARTBEAT_SPOOL = os.path.join(HOME, ".claude", "command-center-heartbeat")
SESSION_WORKSPACES = os.path.join(HOME, "Documents", "Codex")
VISUALIZATIONS = os.path.join(HOME, ".codex", "visualizations")
WRITABLE_PREFIXES = (HEARTBEAT_SPOOL, SESSION_WORKSPACES, VISUALIZATIONS)

#: The app also grants each run its own automation directory, for the
#: memory file. Exactly one, and only a Command Center one: a run that may
#: write a second automation's directory can rewrite that automation.
AUTOMATIONS = os.path.join(HOME, ".codex", "automations")
AUTOMATION_PREFIX = "command-center-"

#: Where to look for rollouts. The override exists for tests and for a
#: machine whose Codex home is elsewhere; production reads the default.
SESSIONS_ENV = "COMMAND_CENTER_CODEX_SESSIONS"
DEFAULT_SESSIONS = os.path.join(HOME, ".codex", "sessions")

#: How many of the newest rollouts to open while looking for this run's.
#: A busy day on the ten-minute schedule wrote about 150; the run's own
#: rollout is written continuously, so it is among the newest.
ROLLOUT_SCAN_LIMIT = 400


def sessions_root() -> str:
    return os.environ.get(SESSIONS_ENV) or DEFAULT_SESSIONS


def _records(path: str) -> Iterator[Dict]:
    """Every JSON object in a rollout, skipping lines that do not parse.

    A rollout is being appended to while this reads it, so a torn last
    line is expected and is not a reason to give up on the lines before it.
    """
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict):
                yield record


def _session_cwd(path: str) -> Optional[str]:
    """The working directory the rollout's session started in."""
    for record in _records(path):
        if record.get("type") == "session_meta":
            payload = record.get("payload")
            if isinstance(payload, dict) and isinstance(
                    payload.get("cwd"), str):
                return payload["cwd"]
            return None
    return None


def _day_dirs(root: str, now: datetime.datetime) -> List[str]:
    """Today's and yesterday's rollout directories, in local time.

    The app names both the directories and the files by local date. A run
    that started just before midnight writes under yesterday.
    """
    days = [now.date(), now.date() - datetime.timedelta(days=1)]
    return [os.path.join(root, day.strftime("%Y"), day.strftime("%m"),
                         day.strftime("%d")) for day in days]


def _inside(path: str, directory: str) -> bool:
    path = os.path.normpath(path)
    directory = os.path.normpath(directory)
    return path == directory or path.startswith(directory + os.sep)


def find_rollout(cwd: str, *, now: Optional[datetime.datetime] = None,
                 root: Optional[str] = None) -> Optional[str]:
    """The newest rollout whose session started in ``cwd`` or above it.

    `begin` runs in the session's workspace, or in a directory the model
    made inside it. There is no session id in the environment to match on,
    so the workspace is the join.
    """
    now = now or datetime.datetime.now()
    root = root or sessions_root()
    paths: List[str] = []
    for directory in _day_dirs(root, now):
        paths.extend(glob.glob(os.path.join(directory, "rollout-*.jsonl")))

    def mtime(path: str) -> float:
        try:
            return os.path.getmtime(path)
        except OSError:
            return 0.0

    for path in sorted(paths, key=mtime, reverse=True)[:ROLLOUT_SCAN_LIMIT]:
        try:
            started_in = _session_cwd(path)
        except OSError:
            continue
        if started_in and _inside(cwd, started_in):
            return path
    return None


def effective_settings(path: str) -> Optional[Dict]:
    """The settings in force at the newest `turn_context` in a rollout.

    The newest rather than the first: `begin` runs a few turns into the
    session, and a setting that changed in between is the one that governs
    the work to come.
    """
    latest = None
    for record in _records(path):
        if record.get("type") == "turn_context" and isinstance(
                record.get("payload"), dict):
            latest = record["payload"]
    return latest


def _writable_root_drift(roots: object) -> List[str]:
    if not isinstance(roots, list) or not all(
            isinstance(root, str) for root in roots):
        return ["writable roots: expected a list of paths, found {!r}".format(
            roots)]
    drift = []
    automations = []
    for root in roots:
        if any(_inside(root, prefix) for prefix in WRITABLE_PREFIXES):
            continue
        parent, name = os.path.split(os.path.normpath(root))
        if (os.path.normpath(parent) == os.path.normpath(AUTOMATIONS)
                and name.startswith(AUTOMATION_PREFIX)):
            automations.append(root)
            continue
        drift.append("writable root outside the manifest: {}".format(root))
    if len(automations) > 1:
        drift.append("writable roots name {} automation directories, "
                     "expected at most one: {}".format(
                         len(automations), ", ".join(automations)))
    return drift


def drift(settings: Dict) -> List[str]:
    """Each way ``settings`` differs from the manifest, one line apiece."""
    found: List[str] = []
    if settings.get("model") != MODEL:
        found.append("model: expected {}, found {}".format(
            MODEL, settings.get("model")))
    if settings.get("effort") != EFFORT:
        found.append("effort: expected {}, found {}".format(
            EFFORT, settings.get("effort")))
    if settings.get("approval_policy") != APPROVAL_POLICY:
        found.append("approval policy: expected {}, found {}".format(
            APPROVAL_POLICY, settings.get("approval_policy")))
    sandbox = settings.get("sandbox_policy")
    if not isinstance(sandbox, dict):
        found.append("sandbox policy: expected a record, found {!r}".format(
            sandbox))
        return found
    if sandbox.get("network_access") is not NETWORK_ACCESS:
        found.append("network access: expected {}, found {}".format(
            NETWORK_ACCESS, sandbox.get("network_access")))
    found.extend(_writable_root_drift(sandbox.get("writable_roots")))
    return found


def check(cwd: Optional[str] = None, *,
          now: Optional[datetime.datetime] = None,
          root: Optional[str] = None) -> Dict:
    """Whether the run started in ``cwd`` is the run the manifest describes.

    Returns ``ok``, the ``rollout`` it read, the ``effective`` model and
    effort when they could be read, each ``drift`` line, and one ``why``
    sentence for the refusal.
    """
    cwd = cwd or os.getcwd()
    result: Dict = {"ok": False, "rollout": None, "effective": None,
                    "drift": []}
    try:
        path = find_rollout(cwd, now=now, root=root)
    except OSError as exc:
        path = None
        result["drift"] = ["rollouts unreadable: {}".format(exc)]
    if path is None:
        if not result["drift"]:
            result["drift"] = ["no rollout under {} started in {}".format(
                root or sessions_root(), cwd)]
    else:
        result["rollout"] = path
        try:
            settings = effective_settings(path)
        except OSError as exc:
            settings = None
            result["drift"] = ["rollout unreadable: {}".format(exc)]
        if settings is None:
            if not result["drift"]:
                result["drift"] = ["no turn_context in {}".format(path)]
        else:
            result["effective"] = {"model": settings.get("model"),
                                   "effort": settings.get("effort")}
            result["drift"] = drift(settings)
    result["ok"] = not result["drift"]
    result["why"] = (
        "" if result["ok"] else
        "this Codex run is not the one the manifest describes ({}); "
        "matched on {}".format("; ".join(result["drift"]), cwd))
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Print the check for the current directory as JSON; exit 1 on drift."""
    del argv
    result = check()
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
