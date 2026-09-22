#!/usr/bin/env python3
"""codex_run.py — what a Command Center Codex run must be, read from its own rollout.

The Codex app holds each automation's model, effort, sandbox and approval
policy outside this repository, and it has written stale copies back over
edits made underneath it (LEARNINGS.md, 2026-09-06). So an automation file
says what a run was *meant* to get, not what it got. The run's rollout
records what it got: its `turn_context` records carry the effective `model`,
`effort`, `sandbox_policy`, `file_system_sandbox_policy`, `approval_policy`
and working directory. `funnel.py begin` compares those with the manifest
here before a Codex run may claim anything, and refuses on any difference
(#1316).

The run's rollout is found by id, not guessed: the app puts the thread id in
the run's environment (`CODEX_THREAD_ID`, with `CODEX_SESSION_ID` beside it),
and the rollout's file name ends in that id. The workspace is only a
cross-check.

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
import re
from typing import Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

#: The model and effort every Command Center Codex run uses (Nate,
#: 2026-09-22, #1315): GPT-6 Luna at `max` on both tiers. The tiers differ
#: in queue and in who may approve, not in model.
MODEL = "gpt-6-luna"
EFFORT = "max"

#: An automation runs unattended, so it must never stop to ask. `never` is
#: what the app records for a scheduled run; anything else is either a hand
#: session or an automation someone has changed.
APPROVAL_POLICY = "never"

#: The sandbox mode the paths below mean anything under. A full-access run
#: can write everywhere whatever its root list says.
SANDBOX_TYPE = "workspace-write"

#: The run clones its ticket's repository and pushes a branch, so it needs
#: the network. A run without it fails later and looks like a GitHub fault.
NETWORK_ACCESS = True

HOME = os.path.expanduser("~")

#: Where a run may write, as observed on a 2026-09-18 automation run. The
#: heartbeat spool is outside the session so a run's records survive it;
#: session workspaces are where the app puts each run's own directory; the
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

#: The temporary directories the sandbox always grants, named rather than
#: listed as paths in `file_system_sandbox_policy`.
SPECIAL_WRITES = frozenset({"slash_tmp", "tmpdir"})

#: The environment variables the app sets to the run's thread id, which is
#: also the id its rollout's file name ends in. Seen in automation runs'
#: `env` output on 2026-09-10 and 2026-09-17.
THREAD_ENVS = ("CODEX_THREAD_ID", "CODEX_SESSION_ID")
THREAD_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
                       r"[0-9a-f]{12}$")

#: Where rollouts live. Deliberately not overridable from the environment:
#: a run that could point this at a directory it can write could hand the
#: check a rollout of its own making. Tests patch the attribute instead.
DEFAULT_SESSIONS = os.path.join(HOME, ".codex", "sessions")


def sessions_root() -> str:
    return DEFAULT_SESSIONS


def thread_id(environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The run's thread id from its environment, or ``None``.

    Only a well-formed id is returned. The value goes into a file-name
    pattern, so anything else, a glob character included, is refused
    rather than searched for.
    """
    environ = os.environ if environ is None else environ
    for name in THREAD_ENVS:
        value = environ.get(name)
        if isinstance(value, str) and THREAD_ID.match(value.strip()):
            return value.strip()
    return None


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


def _has_parent_segment(path: str) -> bool:
    return ".." in path.replace("\\", "/").split("/")


def find_rollout(run: str, *, now: Optional[datetime.datetime] = None,
                 root: Optional[str] = None) -> Optional[str]:
    """This run's rollout: the file whose name ends in its thread id."""
    now = now or datetime.datetime.now()
    root = root or sessions_root()
    if not THREAD_ID.match(run or ""):
        return None
    for directory in _day_dirs(root, now):
        matches = glob.glob(os.path.join(
            directory, "rollout-*-{}.jsonl".format(run)))
        if matches:
            return max(matches, key=os.path.getmtime)
    return None


def read_rollout(path: str) -> Tuple[Optional[Dict], Optional[Dict]]:
    """``(session_meta, newest turn_context)`` payloads from one rollout.

    The newest turn context rather than the first: `begin` runs a few turns
    into the session, and a setting that changed in between is the one that
    governs the work to come.
    """
    meta = None
    latest = None
    for record in _records(path):
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if record.get("type") == "session_meta" and meta is None:
            meta = payload
        elif record.get("type") == "turn_context":
            latest = payload
    return meta, latest


def _allowed_root(root: str) -> bool:
    return any(_inside(root, prefix) for prefix in WRITABLE_PREFIXES)


def _automation_dir(root: str) -> bool:
    parent, name = os.path.split(os.path.normpath(root))
    return (os.path.normpath(parent) == os.path.normpath(AUTOMATIONS)
            and name.startswith(AUTOMATION_PREFIX))


def _root_drift(roots: object) -> List[str]:
    """Drift in ``sandbox_policy.writable_roots``."""
    if not isinstance(roots, list) or not all(
            isinstance(root, str) and root for root in roots):
        return ["writable roots: expected a list of paths, found {!r}".format(
            roots)]
    drift = []
    automations = set()
    for root in roots:
        if _has_parent_segment(root) or not os.path.isabs(root):
            drift.append("writable root is not a plain absolute path: {}"
                         .format(root))
        elif _allowed_root(root):
            continue
        elif _automation_dir(root):
            automations.add(os.path.normpath(root))
        else:
            drift.append("writable root outside the manifest: {}".format(root))
    if len(automations) > 1:
        drift.append("writable roots name {} automation directories, "
                     "expected at most one: {}".format(
                         len(automations), ", ".join(sorted(automations))))
    return drift


def _entry_drift(policy: object) -> List[str]:
    """Drift in ``file_system_sandbox_policy``'s write entries.

    `writable_roots` omits two things the sandbox grants anyway: the run's
    working directory and the temporary directories. The entry list names
    all of them, so it is the complete answer to "where may this run
    write". The working directory is held to the workspace rule separately.
    """
    if policy is None:
        return []
    if not isinstance(policy, dict) or not isinstance(
            policy.get("entries"), list):
        return ["file system policy: expected entries, found {!r}".format(
            policy)]
    drift = []
    for entry in policy["entries"]:
        if not isinstance(entry, dict) or entry.get("access") != "write":
            continue
        path = entry.get("path") if isinstance(entry.get("path"), dict) else {}
        if path.get("type") == "special":
            value = path.get("value") if isinstance(
                path.get("value"), dict) else {}
            if value.get("kind") not in SPECIAL_WRITES:
                drift.append("write access to special path {}".format(
                    value.get("kind")))
            continue
        target = path.get("path")
        if not isinstance(target, str) or _has_parent_segment(target) or \
                not os.path.isabs(target):
            drift.append("write entry is not a plain absolute path: {!r}"
                         .format(target))
        elif not (_allowed_root(target) or _automation_dir(target)):
            drift.append("write access outside the manifest: {}".format(target))
    return drift


def drift(settings: Mapping, session_cwd: Optional[str] = None) -> List[str]:
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
    # The working directory is writable whatever the root list says, so it
    # must be one of the app's own session workspaces.
    for label, where in (("working directory", settings.get("cwd")),
                         ("session directory", session_cwd)):
        if where is None and label == "session directory":
            continue
        if not isinstance(where, str) or _has_parent_segment(where) or \
                not _inside(where, SESSION_WORKSPACES) or \
                os.path.normpath(where) == os.path.normpath(
                    SESSION_WORKSPACES):
            found.append("{}: expected a workspace under {}, found {}".format(
                label, SESSION_WORKSPACES, where))
    sandbox = settings.get("sandbox_policy")
    if not isinstance(sandbox, dict):
        found.append("sandbox policy: expected a record, found {!r}".format(
            sandbox))
        return found
    if sandbox.get("type") != SANDBOX_TYPE:
        found.append("sandbox type: expected {}, found {}".format(
            SANDBOX_TYPE, sandbox.get("type")))
    if sandbox.get("network_access") is not NETWORK_ACCESS:
        found.append("network access: expected {}, found {}".format(
            NETWORK_ACCESS, sandbox.get("network_access")))
    found.extend(_root_drift(sandbox.get("writable_roots")))
    found.extend(_entry_drift(settings.get("file_system_sandbox_policy")))
    return found


def check(cwd: Optional[str] = None, *,
          now: Optional[datetime.datetime] = None,
          root: Optional[str] = None,
          environ: Optional[Mapping[str, str]] = None) -> Dict:
    """Whether this run is the run the manifest describes.

    Returns ``ok``, the ``rollout`` it read, the ``effective`` model and
    effort when they could be read, each ``drift`` line, and one ``why``
    sentence for the refusal.
    """
    cwd = cwd or os.getcwd()
    result: Dict = {"ok": False, "rollout": None, "effective": None,
                    "drift": []}
    run = thread_id(environ)
    if run is None:
        result["drift"] = ["no Codex thread id in the environment ({})".format(
            " or ".join(THREAD_ENVS))]
    else:
        try:
            path = find_rollout(run, now=now, root=root)
        except OSError as exc:
            path = None
            result["drift"] = ["rollouts unreadable: {}".format(exc)]
        if path is None and not result["drift"]:
            result["drift"] = ["no rollout for thread {} under {}".format(
                run, root or sessions_root())]
        elif path is not None:
            result["rollout"] = path
            try:
                meta, settings = read_rollout(path)
            except OSError as exc:
                meta = settings = None
                result["drift"] = ["rollout unreadable: {}".format(exc)]
            if not result["drift"]:
                result["drift"] = _rollout_drift(run, cwd, meta, settings)
                if settings is not None:
                    result["effective"] = {"model": settings.get("model"),
                                           "effort": settings.get("effort")}
    result["ok"] = not result["drift"]
    result["why"] = (
        "" if result["ok"] else
        "this Codex run is not the one the manifest describes ({}); "
        "begin ran in {}".format("; ".join(result["drift"]), cwd))
    return result


def _rollout_drift(run: str, cwd: str, meta: Optional[Dict],
                   settings: Optional[Dict]) -> List[str]:
    if meta is None:
        return ["no session_meta in the rollout for thread {}".format(run)]
    if meta.get("id") != run:
        return ["rollout for thread {} records id {}".format(
            run, meta.get("id"))]
    session_cwd = meta.get("cwd")
    if not isinstance(session_cwd, str) or not _inside(cwd, session_cwd):
        return ["begin ran outside its session's directory {}".format(
            session_cwd)]
    if settings is None:
        return ["no turn_context in the rollout for thread {}".format(run)]
    return drift(settings, session_cwd)


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Print the check for this run as JSON; exit 1 on drift."""
    del argv
    result = check()
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
