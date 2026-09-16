"""Push funnel spool snapshots to Cloudflare KV and honour refresh taps.

Ticket #652, step 3 of the #643 dashboard plan. A launchd job runs one tick
every minute on the Mac that holds the only Cloudflare credential:

1. Push the newest spool entry to the ``snapshot`` KV key when it is not
   already published there. The Worker in ``dashboard/worker.js`` reads that
   key; this script is its only writer.
2. Read the ``refresh-requested`` flag the page's refresh button sets. When the
   flag is set and the newest snapshot is older than 10 minutes, run
   ``funnel.py brief`` from the run clone once, then clear the flag. A set flag
   on a fresh snapshot is cleared without running: the plan says a tap on a
   fresh snapshot "does nothing", and leaving the flag set would fire a delayed
   brief for a tap Nate has forgotten about.

Spool contract (shared with the #650 writer): ``COMMAND_CENTER_DASHBOARD_SPOOL``
holds one JSON object per brief with at least ``generated_at`` (ISO-8601);
the whole entry is pushed byte-for-byte, so extra keys ride along untouched.

Configuration, in precedence order (flag, environment, file, default):

- token: ``CLOUDFLARE_API_TOKEN`` in the environment or the gitignored ``.env``
  in Nate's working tree (the same variable ``wrangler`` reads, so #653 shares
  it). The installed run clone is deliberately not a credential store.
- account: ``--account-id``, ``CLOUDFLARE_ACCOUNT_ID``, then ``.env``.
- namespace: ``--namespace-id``, ``FUNNEL_KV_NAMESPACE_ID``, then the ``id``
  of the ``FUNNEL_SNAPSHOT`` binding in ``dashboard/wrangler.toml`` (#653 owns
  that value), and finally — when that id is still the ``local-funnel-snapshot``
  placeholder ``main`` deliberately keeps — the ``namespace_id`` the deployer
  recorded in its state file (``--deploy-state-file``,
  ``COMMAND_CENTER_DASHBOARD_DEPLOY_STATE_FILE``, then the deployer's own
  default). Without that fallback the publisher sends Cloudflare the
  placeholder and is rejected on every tick (#895).
- spool dir, lock file, API base, and funnel.py path each take a ``--`` flag
  or a ``COMMAND_CENTER_DASHBOARD_*`` variable.

Exit codes: 0 when the tick completed (a failed brief subprocess is logged,
not propagated, so the publisher never affects any brief exit code); 1 when
the publisher itself could not do its job; 2 on usage error. Everything is
logged to stderr, which launchd captures to the publisher's own log file.

A whole-tick lockfile keeps two ticks from overlapping: a brief may run up to
its 120s budget while ticks fire every 60s, so without the lock one flag could
run two briefs. A tick that finds the lock held logs one line and exits 0.
Locking is mandatory: without it the one-brief-per-flag guarantee cannot hold,
so a tick that cannot lock fails instead of running unlocked.

Runs under the Mac's /usr/bin/python3 (3.9). Keep this module stdlib-only and
3.9-compatible, and never import funnel or heartbeat here: the publisher must
not be able to touch agent runs even by accident.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

try:
    import fcntl
except ImportError:  # pragma: no cover - the schedule Mac and CI are Unix
    fcntl = None  # type: ignore

SNAPSHOT_KEY = "snapshot"
REFRESH_KEY = "refresh-requested"
KV_BINDING = "FUNNEL_SNAPSHOT"
STALE_AFTER_SECONDS = 600

#: A refresh flag set by GitHub's webhook (#914) means the board actually
#: changed, so the ten-minute staleness rule would hold the page back for no
#: reason. This floor is what stops an event storm from running a brief every
#: tick: one brief per this many seconds, at most.
REFRESH_FLOOR_SECONDS = 90

#: With webhooks delivering, a scheduled brief exists only so a broken or
#: unconfigured webhook cannot leave the page indefinitely old.
SCHEDULED_AFTER_SECONDS = 1800
KV_TIMEOUT_SECONDS = 30.0
DEFAULT_BRIEF_TIMEOUT_SECONDS = 600.0
DEFAULT_API_BASE = "https://api.cloudflare.com/client/v4"
TOKEN_ENV = "CLOUDFLARE_API_TOKEN"
ACCOUNT_ENV = "CLOUDFLARE_ACCOUNT_ID"
NAMESPACE_ENV = "FUNNEL_KV_NAMESPACE_ID"
#: ``main`` keeps this placeholder in ``dashboard/wrangler.toml`` on purpose
#: (#653): the deployer patches the real id into its own worktree copy and
#: never writes to ``main``. ``dashboard_deploy`` imports it from here so the
#: two modules cannot disagree about what "not a real id" looks like.
PLACEHOLDER_NAMESPACE_ID = "local-funnel-snapshot"
DEPLOY_STATE_ENV = "COMMAND_CENTER_DASHBOARD_DEPLOY_STATE_FILE"


class PublisherError(Exception):
    """The publisher cannot do its job (config, spool, or lock failure)."""


class KVError(Exception):
    """A Cloudflare KV call failed."""


class SpoolEntry(NamedTuple):
    name: str
    epoch: float
    data: bytes


class BriefResult(NamedTuple):
    returncode: Optional[int]
    timed_out: bool
    stdout: bytes
    stderr: bytes


def log(message: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print("{} publisher: {}".format(stamp, message), file=sys.stderr)


def parse_dotenv(path: Path) -> Dict[str, str]:
    """Read KEY=value lines; missing or unreadable files mean no values."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    result: Dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if value:
            result[key] = value
    return result


def namespace_id_from_wrangler(path: Path,
                               binding: str = KV_BINDING) -> str:
    """Return the KV namespace id for one binding without a TOML parser.

    The schedule interpreter predates tomllib, and this needs exactly one
    value, so match ``binding``/``id`` pairs per ``[[kv_namespaces]]`` block.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PublisherError(
            "cannot read {}: {}".format(path, exc))
    blocks = re.split(r"^\s*\[\[kv_namespaces\]\]\s*$",
                      text, flags=re.MULTILINE)
    for block in blocks[1:]:
        found_binding = re.search(
            r"^\s*binding\s*=\s*[\"']([^\"']+)[\"']",
            block, flags=re.MULTILINE)
        if found_binding is None or found_binding.group(1) != binding:
            continue
        found_id = re.search(
            r"^\s*id\s*=\s*[\"']([^\"']+)[\"']",
            block, flags=re.MULTILINE)
        if found_id is not None:
            return found_id.group(1)
    raise PublisherError(
        "no [[kv_namespaces]] id for binding {!r} in {}".format(
            binding, path))


def _default_deploy_state_file() -> Path:
    """Return the state file the #653 deployer writes on this Mac."""
    return (Path.home() / ".claude" / "command-center-dashboard-deploy"
            / "state.json")


def namespace_id_from_deploy_state(path: Path) -> Optional[str]:
    """Return the namespace id the deployer recorded, or None.

    The deployer creates the namespace and records its id here; ``main``
    keeps the placeholder. A missing file means no deploy has run yet, which
    is not an error — the caller decides what to do without an id.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise PublisherError(
            "cannot read deploy state file {}: {}".format(path, exc))
    try:
        payload = json.loads(text)
    except ValueError:
        raise PublisherError(
            "deploy state file {} is not parseable JSON".format(path))
    if not isinstance(payload, dict):
        raise PublisherError(
            "deploy state file {} does not hold an object".format(path))
    value = payload.get("namespace_id")
    if (not isinstance(value, str) or not value
            or value == PLACEHOLDER_NAMESPACE_ID):
        return None
    return value


def resolve_namespace_id(namespace_id: Optional[str], wrangler_toml: Path,
                         deploy_state_file: Path) -> str:
    """Return the KV namespace id to publish into.

    Flag and environment win, then a real id on ``main``. When ``main``
    still carries the placeholder, fall back to the id the deployer
    recorded, so the publisher stops sending Cloudflare a namespace it
    will reject (#895).
    """
    explicit = namespace_id or os.environ.get(NAMESPACE_ENV)
    if explicit:
        return explicit
    from_toml = namespace_id_from_wrangler(wrangler_toml)
    if from_toml != PLACEHOLDER_NAMESPACE_ID:
        return from_toml
    recorded = namespace_id_from_deploy_state(deploy_state_file)
    if recorded:
        return recorded
    raise PublisherError(
        "{} still carries the {!r} placeholder and {} records no "
        "namespace_id; the deployer has not created the namespace yet"
        .format(wrangler_toml, PLACEHOLDER_NAMESPACE_ID, deploy_state_file))


def parse_generated_at(value: object) -> Optional[float]:
    """Parse a spool ``generated_at`` to epoch seconds, or None.

    Accepts the Worker's ``Z`` suffix and plain ISO-8601 offsets. Naive
    timestamps are read as UTC. Anything else means the entry cannot be
    ordered, so the caller skips it instead of guessing.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text[-1:] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def is_stale(entry_epoch: Optional[float], now: float,
             after: float = STALE_AFTER_SECONDS) -> bool:
    """A missing snapshot is infinitely stale; otherwise older than ``after``."""
    if entry_epoch is None:
        return True
    return (now - entry_epoch) > after


def brief_reason(entry_epoch: Optional[float], now: float,
                 flagged: bool) -> Optional[str]:
    """Why this tick should run a brief, or None to publish and stop.

    A refresh — the page's button or GitHub's webhook — runs one as soon as the
    newest snapshot is older than the floor, so a real change reaches the page
    in about a minute. Without a refresh, a brief runs only when the snapshot
    has aged past the scheduled bound, which exists for the case where webhook
    delivery is broken or was never configured.
    """
    if flagged:
        if is_stale(entry_epoch, now, REFRESH_FLOOR_SECONDS):
            return "refresh"
        return None
    if is_stale(entry_epoch, now, SCHEDULED_AFTER_SECONDS):
        return "scheduled"
    return None


def newest_spool_entry(
        spool_dir: Path) -> Tuple[Optional[SpoolEntry], List[str]]:
    """Return the newest parseable ``*.json`` entry, plus skip warnings.

    A missing spool directory means no brief has spooled yet, which is normal
    before the first post-#650 brief: it reads as an empty spool, not an error.
    """
    try:
        names = sorted(os.listdir(spool_dir))
    except FileNotFoundError:
        return None, []
    except OSError as exc:
        raise PublisherError(
            "cannot read spool directory {}: {}".format(spool_dir, exc))
    warnings: List[str] = []
    best: Optional[SpoolEntry] = None
    for name in names:
        if not name.endswith(".json"):
            continue
        path = spool_dir / name
        try:
            data = path.read_bytes()
        except OSError as exc:
            warnings.append("skipping {}: cannot read it ({})".format(
                name, exc))
            continue
        try:
            payload = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            warnings.append(
                "skipping {}: not parseable JSON".format(name))
            continue
        epoch = parse_generated_at(
            payload.get("generated_at") if isinstance(payload, dict)
            else None)
        if epoch is None:
            warnings.append(
                "skipping {}: no parseable generated_at".format(name))
            continue
        if best is None or (epoch, name) > (best.epoch, best.name):
            best = SpoolEntry(name=name, epoch=epoch, data=data)
    return best, warnings


def _snip(body: bytes, limit: int = 200) -> str:
    return body[:limit].decode("utf-8", errors="replace")


class KVClient:
    """The three KV values calls the publisher needs, over stdlib urllib."""

    def __init__(self, api_base: str, account_id: str, namespace_id: str,
                 token: str, timeout: float = KV_TIMEOUT_SECONDS) -> None:
        self._base = api_base.rstrip("/")
        self._account = account_id
        self._namespace = namespace_id
        self._token = token
        self._timeout = timeout

    def _url(self, key: str) -> str:
        return "{}/accounts/{}/storage/kv/namespaces/{}/values/{}".format(
            self._base,
            urllib.parse.quote(self._account, safe=""),
            urllib.parse.quote(self._namespace, safe=""),
            urllib.parse.quote(key, safe=""),
        )

    def _request(self, method: str, key: str,
                 data: Optional[bytes] = None,
                 content_type: Optional[str] = None,
                 ) -> Tuple[int, bytes]:
        request = urllib.request.Request(self._url(key), data=data,
                                         method=method)
        request.add_header("Authorization", "Bearer " + self._token)
        if content_type is not None:
            request.add_header("Content-Type", content_type)
        try:
            response = urllib.request.urlopen(request, timeout=self._timeout)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()
        except (urllib.error.URLError, OSError) as exc:
            raise KVError("{} {} failed: {}".format(method, key, exc))
        with response:
            return response.status, response.read()

    def get(self, key: str) -> Optional[bytes]:
        status, body = self._request("GET", key)
        if status == 404:
            return None
        if status != 200:
            raise KVError("GET {} returned {}: {}".format(
                key, status, _snip(body)))
        return body

    def _require_success(self, method: str, key: str,
                         status: int, body: bytes) -> None:
        if status // 100 != 2:
            raise KVError("{} {} returned {}: {}".format(
                method, key, status, _snip(body)))
        try:
            payload = json.loads(body.decode("utf-8"))
        except ValueError:
            raise KVError(
                "{} {} returned an unexpected response: {}".format(
                    method, key, _snip(body)))
        if not isinstance(payload, dict) or not payload.get("success"):
            raise KVError("{} {} reported success=false: {}".format(
                method, key, _snip(body)))

    def put(self, key: str, data: bytes,
            content_type: str = "application/json") -> None:
        status, body = self._request("PUT", key, data, content_type)
        self._require_success("PUT", key, status, body)

    def delete(self, key: str) -> None:
        status, body = self._request("DELETE", key)
        self._require_success("DELETE", key, status, body)


def acquire_lock(lock_path: Path):
    """Hold an exclusive whole-tick lock, or return None when held.

    Raises when locking itself is unavailable: running unlocked could turn
    one refresh flag into two briefs, so that fails closed instead.
    """
    if fcntl is None:
        raise PublisherError(
            "file locking is unavailable here; refusing to run unlocked")
    try:
        handle = open(lock_path, "w")
    except OSError as exc:
        raise PublisherError(
            "cannot open lock file {}: {}".format(lock_path, exc))
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            return None
        raise PublisherError(
            "cannot lock {}: {}".format(lock_path, exc))
    return handle


def run_brief(funnel_py: Path, timeout: float) -> BriefResult:
    """Run one ``funnel.py brief`` as a subprocess; never import funnel."""
    if not funnel_py.is_file():
        raise PublisherError(
            "funnel.py not found at {}".format(funnel_py))
    argv = [sys.executable, str(funnel_py), "brief"]
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(funnel_py.parent),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return BriefResult(returncode=None, timed_out=True,
                           stdout=exc.stdout or b"", stderr=exc.stderr or b"")
    except OSError as exc:
        raise PublisherError(
            "could not run {}: {}".format(" ".join(argv), exc))
    return BriefResult(returncode=completed.returncode, timed_out=False,
                       stdout=completed.stdout, stderr=completed.stderr)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent


def _working_tree_env_file() -> Path:
    """Return the credential file owned by the canonical working tree.

    ``publisher.py`` runs from the maintained read-only clone, while the
    credential is intentionally kept in Nate's gitignored working tree. Keep
    this default independent of ``__file__`` so a fresh run clone does not
    need a second ``.env``.
    """
    return Path.home() / ".claude" / "command-center" / ".env"


def _option(cli_value: Optional[str], env_name: str,
            default: Optional[str] = None) -> Optional[str]:
    if cli_value is not None:
        return cli_value
    value = os.environ.get(env_name)
    if value:
        return value
    return default


def _resolve_brief_timeout(cli_value: Optional[float]) -> float:
    if cli_value is not None:
        timeout = cli_value
    else:
        raw = os.environ.get("COMMAND_CENTER_DASHBOARD_BRIEF_TIMEOUT")
        if not raw:
            return DEFAULT_BRIEF_TIMEOUT_SECONDS
        try:
            timeout = float(raw)
        except ValueError:
            raise PublisherError(
                "COMMAND_CENTER_DASHBOARD_BRIEF_TIMEOUT is not a number: "
                "{!r}".format(raw))
    if timeout <= 0:
        raise PublisherError("brief timeout must be positive")
    return timeout


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="publisher.py",
        description="Push the newest funnel spool snapshot to Cloudflare KV "
                    "and run one brief per refresh flag.",
    )
    parser.add_argument("--spool-dir", default=None)
    parser.add_argument("--env-file", default=None)
    parser.add_argument("--wrangler-toml", default=None)
    parser.add_argument("--namespace-id", default=None)
    parser.add_argument("--deploy-state-file", default=None)
    parser.add_argument("--account-id", default=None)
    parser.add_argument("--api-base", default=None)
    parser.add_argument("--funnel-py", default=None)
    parser.add_argument("--lock-file", default=None)
    parser.add_argument("--brief-timeout", type=float, default=None)
    return parser.parse_args(argv)


def _remote_epoch(remote: bytes) -> Optional[float]:
    try:
        payload = json.loads(remote.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    return parse_generated_at(payload.get("generated_at"))


def tick(spool_dir: Path, env_file: Path, wrangler_toml: Path,
         namespace_id: Optional[str], account_id: Optional[str],
         api_base: str, funnel_py: Path, brief_timeout: float,
         deploy_state_file: Optional[Path] = None) -> int:
    dotenv = parse_dotenv(env_file)
    token = os.environ.get(TOKEN_ENV) or dotenv.get(TOKEN_ENV)
    if not token:
        raise PublisherError(
            "{} is not set and {} has no value for it".format(
                TOKEN_ENV, env_file))
    account = account_id or os.environ.get(ACCOUNT_ENV) or dotenv.get(
        ACCOUNT_ENV)
    if not account:
        raise PublisherError(
            "{} is not set and {} has no value for it".format(
                ACCOUNT_ENV, env_file))
    namespace = resolve_namespace_id(
        namespace_id, wrangler_toml,
        deploy_state_file if deploy_state_file is not None
        else _default_deploy_state_file())
    kv = KVClient(api_base, account, namespace, token)
    now = time.time()

    entry, warnings = newest_spool_entry(spool_dir)
    for warning in warnings:
        log("warning: {}".format(warning))
    if entry is None:
        log("no spool entries yet; nothing to publish")
    else:
        remote = kv.get(SNAPSHOT_KEY)
        if remote is not None and remote == entry.data:
            log("snapshot {} is already published".format(entry.name))
        else:
            remote_epoch = _remote_epoch(remote) if remote is not None else None
            if (remote is not None and remote_epoch is not None
                    and remote_epoch >= entry.epoch):
                log("warning: remote snapshot is newer than {}; "
                    "leaving it".format(entry.name))
            else:
                if remote is not None and remote_epoch is None:
                    log("warning: remote snapshot has no parseable "
                        "generated_at; overwriting it")
                kv.put(SNAPSHOT_KEY, entry.data)
                log("published {} ({} bytes)".format(
                    entry.name, len(entry.data)))

    flag = kv.get(REFRESH_KEY)
    requested = (
        flag.decode("utf-8", errors="replace")[:64] or "(empty)"
        if flag is not None else None
    )
    entry_epoch = entry.epoch if entry is not None else None
    reason = brief_reason(entry_epoch, now, flag is not None)
    if reason is None:
        if flag is not None:
            log("refresh requested at {} but a brief ran within the last {}s; "
                "leaving the flag for the next tick".format(
                    requested, REFRESH_FLOOR_SECONDS))
        return 0

    age = ("no snapshot yet" if entry_epoch is None
           else "snapshot age {}s".format(int(now - entry_epoch)))
    if reason == "refresh":
        log("refresh requested at {}; {}; running one brief".format(
            requested, age))
    else:
        log("{}; past the {}s scheduled bound; running one brief".format(
            age, SCHEDULED_AFTER_SECONDS))

    result = run_brief(funnel_py, brief_timeout)
    if result.timed_out:
        log("brief timed out after {}s and was killed "
            "(stdout {} bytes, stderr {} bytes)".format(
                brief_timeout, len(result.stdout), len(result.stderr)))
    else:
        log("brief finished: exit {} (stdout {} bytes, stderr {} "
            "bytes)".format(result.returncode, len(result.stdout),
                            len(result.stderr)))
        if result.returncode != 0:
            tail = (result.stderr or result.stdout)[-2000:].decode(
                "utf-8", errors="replace")
            log("brief output tail: {}".format(tail))
    if flag is not None:
        kv.delete(REFRESH_KEY)
        log("refresh flag cleared")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    spool_dir = Path(_option(
        args.spool_dir, "COMMAND_CENTER_DASHBOARD_SPOOL",
        str(Path.home() / ".claude" / "command-center-dashboard-spool"),
    )).expanduser()
    env_file = Path(_option(
        args.env_file, "COMMAND_CENTER_DASHBOARD_ENV_FILE",
        str(_working_tree_env_file()),
    )).expanduser()
    wrangler_toml = Path(_option(
        args.wrangler_toml, "COMMAND_CENTER_DASHBOARD_WRANGLER_TOML",
        str(_repo_root() / "dashboard" / "wrangler.toml"),
    )).expanduser()
    deploy_state_file = Path(_option(
        args.deploy_state_file, DEPLOY_STATE_ENV,
        str(_default_deploy_state_file()),
    )).expanduser()
    funnel_py = Path(_option(
        args.funnel_py, "COMMAND_CENTER_DASHBOARD_FUNNEL_PY",
        str(_repo_root() / "funnel.py"),
    )).expanduser()
    lock_file = Path(_option(
        args.lock_file, "COMMAND_CENTER_DASHBOARD_LOCK",
        str(Path.home() / ".claude" / "funnel-publisher.lock"),
    )).expanduser()
    api_base = _option(
        args.api_base, "COMMAND_CENTER_DASHBOARD_API_BASE",
        DEFAULT_API_BASE,
    )
    try:
        brief_timeout = _resolve_brief_timeout(args.brief_timeout)
        lock = acquire_lock(lock_file)
    except PublisherError as exc:
        log("error: {}".format(exc))
        return 1
    if lock is None:
        log("another publisher tick is already running; skipping this one")
        return 0
    try:
        return tick(
            spool_dir=spool_dir,
            env_file=env_file,
            wrangler_toml=wrangler_toml,
            namespace_id=args.namespace_id,
            account_id=args.account_id,
            api_base=api_base,
            funnel_py=funnel_py,
            brief_timeout=brief_timeout,
            deploy_state_file=deploy_state_file,
        )
    except (PublisherError, KVError) as exc:
        log("error: {}".format(exc))
        return 1
    finally:
        lock.close()


if __name__ == "__main__":
    sys.exit(main())
