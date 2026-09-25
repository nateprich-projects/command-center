#!/usr/bin/env python3
"""Break a plan into tickets through one structured answer (#809, #794 Phase 2).

The breakdown runner shows the model a packet — the plan body, the existing
sibling tickets, and the sizing standard — and nothing else. The model
returns one JSON answer with either a ticket list or a needs-decision
question. This module validates that answer against the ticket schema and
performs every side effect; the model runs no command.

Packet (read-only)::

    breakdown-packet <project>

prints the plan body, sibling tickets, and sizing standard as JSON.

Apply (writes)::

    breakdown-apply <project> --answer -

reads ``{"tickets": [...], "needs_decision": null | "question"}`` where each
ticket carries ``title``, ``body``, ``risk``, ``depends_on`` (sibling
indices or ``owner/repo#n``), and ``needs``. Validation is total before the
first mutation: at least one ticket or a question but never both, both enums
exact, every dependency resolving to a sibling index or an existing open
issue, and no dependency cycles. The apply path resumes matching sub-issues
from GitHub by title, creates missing ones with native blocked-by edges,
writes the canonical Origin, Risk and Needs fields, and posts the
coverage comment; the question path posts the
needs-decision comment and labels the project blocked — unless the plan
body already carries a valid answered-Gates marker (#1274), in which case
the question is settled and nothing is posted or labelled.

The runner protocol (#811) rides on ``--attempt``: a malformed answer
exits 3 below attempt 2 so the runner retries once with the error fed
back, and 1 at 2 or later with nothing recorded. Without ``--attempt``
the single-shot exit 2 stands. ``--validate-only`` validates without
creating anything and prints the validated answer, for the shadow path.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import funnel  # noqa: E402


class BreakdownError(Exception):
    """A local breakdown failure: bad ref, bad file, unparsable answer."""


#: The risk enum, owned here: the answer schema's vocabulary, not GitHub's.
RISK_OPTIONS = ("standard", "escalated")

#: The needs enum lives on the Project (Nate 2026-09-13, #794); the schema
#: reuses funnel's record of it rather than keeping a second copy.
NEEDS_OPTIONS = funnel.NEEDS_OPTIONS

#: The sizing standard is the judgement slice of the breakdown skill: the
#: unit, inferred-premise routing, and sizing rules, without the protocol the
#: runner owns. Keep premise routing before SIZING_END so the model receives it.
SIZING_SKILL_PATH = os.path.join("skills", "breakdown", "SKILL.md")
SIZING_START = "## The unit"
SIZING_END = "## Ordering and independence"

#: An external dependency: owner/repo#n, the only string shape accepted.
REF_RE = re.compile(
    r"\A(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)"
    r"#(?P<number>[1-9][0-9]*)\Z"
)

#: Issue URLs accepted wherever a project ref is accepted.
ISSUE_URL_RE = re.compile(
    r"\Ahttps?://[^/]+/(?P<owner>[A-Za-z0-9_.-]+)/"
    r"(?P<repo>[A-Za-z0-9_.-]+)/issues/(?P<number>[1-9][0-9]*)/*\Z"
)

#: Exit code for a schema failure. No effect has happened: validation is
#: total before the first mutation, so 2 always means nothing was created.
EXIT_VALIDATION = 2

#: Malformed answer on a runner-protocol attempt below the final one: the
#: runner feeds the parse error back to the model and calls again.
RETRY_EXIT = 3

#: Attempts past the first are final: a malformed answer then exits 1
#: with nothing recorded, and the runner finishes the run errored. The
#: project stays awaiting breakdown, so the next run asks again.
FINAL_ATTEMPT = 2


def parse_project_ref(value: str, default_repo: Optional[str] = None,
                      ) -> Tuple[str, int]:
    """Split a project ref into its repo and issue number.

    Accepts a bare number (resolved against ``default_repo``), an
    ``owner/repo#n`` ref, or an issue URL. Anything else is a local error,
    raised before any GitHub call.
    """
    text = (value or "").strip()
    if text.isdigit() and int(text) >= 1:
        if not default_repo:
            raise BreakdownError(
                "a bare issue number needs --repo to name its owner/repo")
        return default_repo, int(text)
    match = REF_RE.match(text)
    if match:
        return ("{}/{}".format(match.group("owner"), match.group("repo")),
                int(match.group("number")))
    match = ISSUE_URL_RE.match(text)
    if match:
        return ("{}/{}".format(match.group("owner"), match.group("repo")),
                int(match.group("number")))
    raise BreakdownError(
        "not a project ref: {!r} — want <n>, owner/repo#n, or an issue URL"
        .format(value))


def sizing_standard(skill_text: Optional[str] = None) -> str:
    """Return the sizing slice of the breakdown skill, verbatim.

    Reads ``skills/breakdown/SKILL.md`` from the checkout unless ``skill_text``
    is given (the tests' seam). Fails loudly when the section markers move
    rather than silently handing the model the wrong judgement text.
    """
    if skill_text is None:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, SIZING_SKILL_PATH),
                  encoding="utf-8") as handle:
            skill_text = handle.read()
    lines = skill_text.splitlines()
    try:
        start = next(index for index, line in enumerate(lines)
                     if line.strip() == SIZING_START)
        end = next(index for index, line in enumerate(lines)
                   if line.strip() == SIZING_END)
    except StopIteration:
        raise BreakdownError(
            "cannot find the sizing standard in {}: want {!r} through {!r}"
            .format(SIZING_SKILL_PATH, SIZING_START, SIZING_END))
    if end <= start:
        raise BreakdownError(
            "the sizing standard in {} is empty: {!r} comes after {!r}"
            .format(SIZING_SKILL_PATH, SIZING_END, SIZING_START))
    return "\n".join(lines[start:end]).strip() + "\n"


def fetch_plan(repo: str, number: int) -> dict:
    """The project issue behind the breakdown: identity plus its plan body."""
    data = funnel._gh_json(
        "gh", "issue", "view", str(number), "--repo", repo, "--json",
        "number,title,url,body,state")
    if not data:
        raise funnel.GitHubError(
            "could not read project {}#{}".format(repo, number))
    data["ref"] = "{}#{}".format(repo, number)
    return data


def fetch_siblings(repo: str, number: int) -> List[dict]:
    """The project's existing sub-issues, so the model does not re-plan them.

    Number, title, and state are the anti-duplication facts; bodies would
    bloat the packet fifty times over for no decision the question needs.
    """
    owner, _, name = repo.partition("/")
    data = funnel.gh_graphql(
        funnel.SUB_ISSUES, owner=owner, name=name, number=number)
    issue = (data.get("repository") or {}).get("issue") or {}
    nodes = (issue.get("subIssues") or {}).get("nodes") or []
    siblings = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        child_repo = (node.get("repository") or {}).get("nameWithOwner")
        child_number = node.get("number")
        if not child_repo or not child_number:
            continue
        siblings.append({
            "ref": "{}#{}".format(child_repo, child_number),
            "repo": child_repo,
            "number": child_number,
            "title": node.get("title"),
            "state": node.get("state"),
        })
    siblings.sort(key=lambda entry: entry["number"])
    return siblings


def build_packet(*, repo: str, number: int, plan: dict,
                 siblings: Sequence[dict], sizing: str,
                 collected_at: str) -> Dict:
    """Assemble the packet from already-fetched pieces. Pure: no IO."""
    plan = plan or {}
    return {
        "project": {
            "ref": "{}#{}".format(repo, number),
            "repo": repo,
            "number": number,
            "title": plan.get("title"),
            "url": plan.get("url"),
            "body": plan.get("body"),
            "state": plan.get("state"),
        },
        "siblings": list(siblings or []),
        "sizing_standard": sizing,
        "sizing_standard_source": SIZING_SKILL_PATH,
        "collected_at": collected_at,
    }


def collect(project: str, repo: Optional[str] = None, *,
            now: Optional[datetime] = None) -> Dict:
    """Fetch every packet piece. Reads only, no writes."""
    # A bare number needs its repo resolved; a full ref or URL does not,
    # so the checkout lookup runs only when the ref cannot stand alone.
    text = (project or "").strip()
    default = funnel.resolve_repo(repo) if text.isdigit() else repo
    resolved_repo, number = parse_project_ref(project, default)
    return build_packet(
        repo=resolved_repo,
        number=number,
        plan=fetch_plan(resolved_repo, number),
        siblings=fetch_siblings(resolved_repo, number),
        sizing=sizing_standard(),
        collected_at=(now or datetime.now(timezone.utc)).isoformat(),
    )


def packet_main(argv: Optional[Sequence[str]] = None) -> int:
    """Print the breakdown packet for one project as JSON."""
    parser = argparse.ArgumentParser(
        description="assemble one read-only breakdown packet for a project")
    parser.add_argument("project",
                        help="issue number, owner/repo#n, or issue URL")
    parser.add_argument("--repo", default=None,
                        help="owner/name; required with a bare number when "
                             "ambiguous")
    args = parser.parse_args(argv)
    try:
        packet = collect(args.project, args.repo)
    except (funnel.GitHubError, BreakdownError) as exc:
        print("breakdown-packet: {}".format(exc), file=sys.stderr)
        return 1
    print(json.dumps(packet, indent=2, sort_keys=True))
    return 0


def _is_blank(value: object) -> bool:
    """Whether a required text field came back missing or whitespace-only."""
    return not isinstance(value, str) or not value.strip()


def _find_cycle(edges: Dict[int, List[int]]) -> Optional[List[int]]:
    """Return one dependency cycle as an index path, or None when acyclic.

    ``edges[i]`` lists the sibling indices ticket ``i`` is blocked by. A
    self-edge is reported by validation, not here, so it is skipped.
    """
    visiting: Dict[int, int] = {}
    done = set()
    for root in sorted(edges):
        if root in done:
            continue
        stack = [(root, iter(sorted(edges.get(root, []))))]
        visiting[root] = len(stack) - 1
        while stack:
            node, children = stack[-1]
            advanced = False
            for child in children:
                if child == node or child not in edges:
                    continue
                if child in visiting:
                    path = [entry[0] for entry in stack[visiting[child]:]]
                    return path + [child]
                if child not in done:
                    visiting[child] = len(stack)
                    stack.append((child, iter(sorted(edges.get(child, [])))))
                    advanced = True
                    break
            if not advanced:
                done.add(node)
                del visiting[node]
                stack.pop()
    return None


def validate_answer(answer: object, issue_state: Callable[[str], Optional[str]]
                    ) -> Tuple[List[str], Optional[dict]]:
    """Validate one breakdown answer against the ticket schema.

    Returns ``(errors, normalized)``. ``errors`` is empty exactly when the
    answer is applicable; ``normalized`` then holds the tickets with their
    dependencies resolved to ``("sibling", index)`` / ``("external", ref)``
    pairs in answer order, plus the stripped needs-decision question or
    None. ``issue_state`` maps an ``owner/repo#n`` ref to its issue state
    (``"OPEN"``/``"CLOSED"``) or None when no such issue exists; each unique
    ref is looked up at most once.
    """
    if not isinstance(answer, dict):
        return ["the answer must be a JSON object with tickets and "
                "needs_decision"], None
    raw_tickets = answer.get("tickets", [])
    if not isinstance(raw_tickets, list):
        return ["tickets must be a list of ticket objects"], None
    for index, raw in enumerate(raw_tickets):
        if not isinstance(raw, dict):
            return ["ticket {} must be an object with title, body, risk, "
                    "depends_on, and needs".format(index)], None

    raw_question = answer.get("needs_decision")
    if raw_question is None:
        question = None
    elif isinstance(raw_question, str):
        question = raw_question.strip() or None
    else:
        return ["needs_decision must be null or a question string"], None

    errors: List[str] = []
    if question is not None and raw_tickets:
        errors.append("the answer must either list tickets or ask a "
                      "needs-decision question, not both")
    if question is None and not raw_tickets:
        errors.append("the answer must list at least one ticket or ask a "
                      "needs-decision question")

    tickets: List[dict] = []
    dep_errors = 0
    known: Dict[str, Optional[str]] = {}
    for index, raw in enumerate(raw_tickets):
        label = "ticket {}".format(index)
        title = raw.get("title")
        if _is_blank(title):
            errors.append("{} needs a non-empty title".format(label))
        body = raw.get("body")
        if _is_blank(body):
            errors.append("{} needs a non-empty body".format(label))
        risk = raw.get("risk")
        if risk not in RISK_OPTIONS:
            errors.append("{} has risk {!r}; want one of {}".format(
                label, risk, ", ".join(RISK_OPTIONS)))
        needs = raw.get("needs")
        if needs not in NEEDS_OPTIONS:
            errors.append("{} has needs {!r}; want one of {}".format(
                label, needs, ", ".join(NEEDS_OPTIONS)))
        depends_on = raw.get("depends_on", [])
        if not isinstance(depends_on, list):
            errors.append("{} has depends_on {!r}; want a list of sibling "
                           "indices and owner/repo#n refs".format(
                               label, depends_on))
            depends_on = []
        resolved = []
        for entry in depends_on:
            if isinstance(entry, bool):
                errors.append("{} depends on {!r}; want a sibling index or "
                              "owner/repo#n".format(label, entry))
                dep_errors += 1
            elif isinstance(entry, int):
                if entry == index:
                    errors.append("{} depends on itself".format(label))
                    dep_errors += 1
                elif entry < 0 or entry >= len(raw_tickets):
                    errors.append(
                        "{} depends on sibling {}; only 0..{} exist".format(
                            label, entry, len(raw_tickets) - 1))
                    dep_errors += 1
                else:
                    resolved.append(("sibling", entry))
            elif isinstance(entry, str) and REF_RE.match(entry.strip()):
                ref = entry.strip()
                if ref not in known:
                    known[ref] = issue_state(ref)
                state = known[ref]
                if state is None:
                    errors.append("{} depends on {}, which is not an "
                                  "existing issue".format(label, ref))
                    dep_errors += 1
                elif str(state).upper() != "OPEN":
                    errors.append("{} depends on {}, which is not open"
                                  .format(label, ref))
                    dep_errors += 1
                else:
                    resolved.append(("external", ref))
            else:
                errors.append("{} depends on {!r}; want a sibling index or "
                              "owner/repo#n".format(label, entry))
                dep_errors += 1
        clean_body = body
        if isinstance(body, str):
            clean_body = funnel.RISK_LINE.sub("", body)
            clean_body = re.sub(r"\n{3,}", "\n\n", clean_body).strip()
            if not clean_body:
                errors.append(
                    "{} needs body content beyond the canonical Risk field"
                    .format(label))
        tickets.append({
            "title": title.strip() if isinstance(title, str) else title,
            "body": clean_body,
            "risk": risk,
            "needs": needs,
            "depends_on": resolved,
        })

    if not dep_errors:
        edges = {
            index: [target for kind, target in ticket["depends_on"]
                    if kind == "sibling"]
            for index, ticket in enumerate(tickets)
        }
        cycle = _find_cycle(edges)
        if cycle is not None:
            errors.append("tickets depend in a cycle: {}".format(
                " -> ".join("ticket {}".format(i) for i in cycle)))

    if errors:
        return errors, None
    return [], {"tickets": tickets, "needs_decision": question}


def creation_order(tickets: Sequence[dict]) -> List[int]:
    """List ticket indices so every blocker is created before its dependent.

    Kahn's algorithm, stable in answer order: among the tickets whose
    blockers all exist, the earliest answer wins. External refs point
    outside the answer, so only sibling edges order anything.
    """
    remaining = {
        index: {target for kind, target in ticket.get("depends_on", [])
                if kind == "sibling"}
        for index, ticket in enumerate(tickets)
    }
    order = []
    while remaining:
        ready = sorted(index for index, blockers in remaining.items()
                       if blockers <= set(order))
        if not ready:
            raise BreakdownError(
                "tickets cannot be ordered; validation missed a cycle")
        order.append(ready[0])
        del remaining[ready[0]]
    return order


def issue_url(ref: str) -> str:
    """Render an owner/repo#n ref as the issue URL `gh --blocked-by` takes."""
    match = REF_RE.match(ref.strip())
    if not match:
        raise BreakdownError(
            "not an owner/repo#n ref: {!r}".format(ref))
    return "https://github.com/{}/{}/issues/{}".format(
        match.group("owner"), match.group("repo"), match.group("number"))


def blocked_by_values(ticket: dict, created_numbers: Dict[int, int]) -> List[str]:
    """Render one ticket's dependencies as `gh --blocked-by` values.

    Sibling indices become the created issue numbers in the ticket repo;
    external refs become issue URLs, since a bare number would point at the
    wrong repository. Answer order is preserved.
    """
    values = []
    for kind, target in ticket.get("depends_on", []):
        if kind == "sibling":
            values.append(str(created_numbers[target]))
        else:
            values.append(issue_url(target))
    return values


def display_blockers(ticket: dict, created_refs: Dict[int, str],
                     repo: str) -> List[str]:
    """Render one ticket's blockers for the human-readable coverage comment.

    Same-repo blockers read as ``#n``; cross-repo ones keep their full ref.
    """
    rendered = []
    for kind, target in ticket.get("depends_on", []):
        if kind == "sibling":
            ref = created_refs[target]
        else:
            ref = target
        if ref.startswith(repo + "#"):
            rendered.append("#" + ref.split("#", 1)[1])
        else:
            rendered.append(ref)
    return rendered


def coverage_comment_body(project_ref: str, created: Sequence[dict]) -> str:
    """Render the deterministic coverage comment posted on the project.

    Each entry names what the runner wrote — the ref, the code-owned risk,
    the Needs field, and the native edges — so a later reader sees the
    breakdown without opening every ticket.
    """
    lines = ["Breakdown of {} created {} ticket{}:".format(
        project_ref, len(created), "" if len(created) == 1 else "s")]
    for ticket in created:
        entry = "- {}: {} (Risk: {}, Needs: {}".format(
            ticket["ref"], ticket["title"], ticket["risk"], ticket["needs"])
        if ticket.get("blocked_by"):
            entry += "; blocked by {}".format(
                ", ".join(ticket["blocked_by"]))
        lines.append(entry + ")")
    return "\n".join(lines)


def parse_created_number(output: str) -> int:
    """Read the created issue number from `gh issue create` output.

    `gh` prints the issue URL; the number is its last path segment. Only the
    last non-empty line is read, so progress chatter above it is harmless.
    """
    lines = [line.strip() for line in (output or "").splitlines()
             if line.strip()]
    if not lines:
        raise funnel.GitHubError(
            "gh issue create printed nothing; the ticket may not exist")
    match = re.search(r"/issues/([1-9][0-9]*)\s*$", lines[-1])
    if not match:
        raise funnel.GitHubError(
            "could not read the created issue number from: {}".format(
                lines[-1][:120]))
    return int(match.group(1))


#: The `gh issue view` failure that means the issue is genuinely absent,
#: rather than a transient GitHub failure one retry might cure.
ISSUE_MISSING_SIGNALS = ("could not resolve", "http 404", "not found")


def fetch_issue_state(ref: str) -> Optional[str]:
    """Return an issue's state, or None when no such issue exists.

    The dependency check behind validation. A missing issue reads as None;
    any other `gh` failure raises, so a transient outage can never masquerade
    as a bad dependency and mislabel the answer.
    """
    match = REF_RE.match(ref.strip())
    if not match:
        return None
    proc = funnel._run_gh(
        ["gh", "issue", "view", match.group("number"), "--repo",
         "{}/{}".format(match.group("owner"), match.group("repo")),
         "--json", "number,state"],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        if any(signal in detail.lower()
               for signal in ISSUE_MISSING_SIGNALS):
            return None
        raise funnel.GitHubError(
            "could not read the state of {}: {}".format(ref, detail))
    try:
        data = json.loads(proc.stdout or "")
    except ValueError:
        raise funnel.GitHubError(
            "could not read the state of {}: gh answered no JSON".format(
                ref))
    if not isinstance(data, dict):
        raise funnel.GitHubError(
            "could not read the state of {}: gh answered no JSON".format(
                ref))
    return data.get("state")


def create_ticket(repo: str, parent_number: int, ticket: dict,
                  blocked_by: Sequence[str]) -> Tuple[int, str, str]:
    """Create one sub-issue with its native blocked-by edges. One mutation.

    Returns the number, the ``owner/repo#n`` ref, and the issue URL the
    Project add takes.
    """
    body = ticket.get("body") or ""
    command = ["gh", "issue", "create", "--repo", repo,
               "--parent", str(parent_number)]
    if list(blocked_by):
        command += ["--blocked-by", ",".join(blocked_by)]
    command += ["--title", ticket["title"], "--body", body]
    proc = funnel._run_gh(command, capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise funnel.GitHubError(
            "could not create {!r} under {}#{}: {}".format(
                ticket["title"], repo, parent_number,
                (proc.stderr or "").strip()))
    number = parse_created_number(proc.stdout or "")
    url = [line.strip() for line in (proc.stdout or "").splitlines()
           if line.strip()][-1]
    return number, "{}#{}".format(repo, number), url


PROJECT_ITEM_ADD_ALREADY_EXISTS = "content already exists in this project"


def existing_project_item_id(url: str) -> Optional[str]:
    """Read the Project row for ``url`` and return its item id, if present.

    ``Issue.projectItems`` is empty for org-repo issues in this user-owned
    Project, so membership is confirmed from the Project's own item list.
    """
    target = url.rstrip("/")
    matches = [
        item for item in funnel.load_items(include_details=False)
        if isinstance(getattr(item, "url", None), str)
        and item.url.rstrip("/") == target
    ]
    if len(matches) > 1:
        raise funnel.GitHubError(
            "Project read found multiple rows for {}".format(url))
    if not matches:
        return None
    item_id = getattr(matches[0], "item_id", None)
    if not isinstance(item_id, str) or not item_id:
        raise funnel.GitHubError(
            "Project read found {} without an item id".format(url))
    return item_id


def add_to_project(url: str) -> str:
    """Return a new ticket's Project item id, where its Needs field lives.

    The id comes from `gh project item-add`, as `cmd_capture` does: the add
    answers the Project row's id, for a fresh ticket or one already on the
    board. If GitHub reports the content is already on the board, confirm
    membership from the Project's own item list before returning its id.
    Never query `Issue.projectItems` for it — LEARNINGS.md (2026-09-05,
    measured) records that connection as empty for org-repo issues in this
    user-owned Project, which is exactly what a breakdown creates.
    """
    proc = funnel._run_gh(
        ["gh", "project", "item-add", str(funnel.PROJECT_NUMBER),
         "--owner", funnel.PROJECT_OWNER, "--url", url, "--format", "json"],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        if PROJECT_ITEM_ADD_ALREADY_EXISTS in detail.lower():
            try:
                item_id = existing_project_item_id(url)
            except Exception as exc:
                raise funnel.GitHubError(
                    "created {} but gh project item-add reported already "
                    "exists and Project membership could not be confirmed: "
                    "{} (membership read failed: {})".format(
                        url, detail, exc)) from exc
            if item_id:
                return item_id
            raise funnel.GitHubError(
                "created {} but gh project item-add reported already "
                "exists and the Project read did not confirm membership: "
                "{}".format(url, detail))
        raise funnel.GitHubError(
            "created {} but could not add it to the Project: {}".format(
                url, detail))
    try:
        item_id = json.loads(proc.stdout or "").get("id")
    except (ValueError, AttributeError):
        item_id = None
    if not item_id:
        raise funnel.GitHubError(
            "created {} but gh project item-add answered no item id".format(
                url))
    return item_id


def write_needs(item_id: str, needs: str, ref: str) -> None:
    """Write one ticket's Needs single-select. One mutation."""
    funnel.write_project_select(item_id, "Needs", needs, ref)


def post_comment(repo: str, number: int, body: str, *,
                 run: Optional[str] = None,
                 agent: Optional[str] = None) -> None:
    """Post one runner-owned comment with the agent voice stamped on it."""
    proc = funnel._run_gh(
        ["gh", "issue", "comment", str(number), "--repo", repo,
         "--body", funnel.append_provenance(
             body, "agent", at=datetime.now(timezone.utc),
             run=run, agent=agent)],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise funnel.GitHubError(
            "could not comment on {}#{}: {}".format(
                repo, number, (proc.stderr or "").strip()))


def apply_blocked_label(repo: str, number: int) -> None:
    """Label one issue blocked, so it leaves the breakdown queue."""
    proc = funnel._run_gh(
        ["gh", "issue", "edit", str(number), "--repo", repo,
         "--add-label", "blocked"],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise funnel.GitHubError(
            "recorded the needs-decision comment on {}#{}, but could not "
            "add its blocked label: {}".format(
                repo, number, (proc.stderr or "").strip()))


def match_existing_siblings(tickets: Sequence[dict],
                            siblings: Sequence[dict]) -> Dict[int, dict]:
    """Match a planned ticket to its existing child only by an unambiguous title.

    Titles that occur more than once in either list are left unmatched rather
    than guessed by position. A dependency on one of those existing issues
    can still name its explicit ``owner/repo#n`` ref in the answer.
    """
    planned_by_title: Dict[str, List[int]] = {}
    existing_by_title: Dict[str, List[dict]] = {}
    for index, ticket in enumerate(tickets):
        planned_by_title.setdefault(ticket["title"], []).append(index)
    for sibling in siblings:
        title = sibling.get("title")
        if isinstance(title, str):
            existing_by_title.setdefault(title, []).append(sibling)

    matched = {}
    for title, indexes in planned_by_title.items():
        candidates = existing_by_title.get(title, [])
        if len(indexes) == 1 and len(candidates) == 1:
            matched[indexes[0]] = candidates[0]
    return matched


def apply_create(repo: str, parent_number: int, tickets: Sequence[dict], *,
                 run: Optional[str] = None,
                 agent: Optional[str] = None) -> List[dict]:
    """Resume or create each ticket, then cover the parent.

    Blockers go first, so every native edge points at an issue that already
    exists. Existing children are matched by unique title from GitHub; their
    Project add and Needs write are repeated so a later attempt repairs a
    half-applied sequence. The returned rows follow creation order, so the
    coverage comment lists each blocker before its dependents. A failure
    names the issues already present, so the next attempt starts from
    GitHub's truth rather than this run's memory.
    """
    created_numbers: Dict[int, int] = {}
    created_refs: Dict[int, str] = {}
    order = creation_order(tickets)
    existing = match_existing_siblings(
        tickets, fetch_siblings(repo, parent_number))
    try:
        for index in order:
            ticket = tickets[index]
            sibling = existing.get(index)
            if sibling is None:
                number, ref, url = create_ticket(
                    repo, parent_number, ticket,
                    blocked_by_values(ticket, created_numbers))
            else:
                number = sibling["number"]
                ref = sibling["ref"]
                url = issue_url(ref)
            created_numbers[index] = number
            created_refs[index] = ref
            item_id = add_to_project(url)
            funnel.write_project_select(item_id, "Origin", "agent", ref)
            funnel.write_project_select(item_id, "Risk", ticket["risk"], ref)
            write_needs(item_id, ticket["needs"], ref)
        created = [{
            "ref": created_refs[index],
            "number": created_numbers[index],
            "title": tickets[index]["title"],
            "risk": tickets[index]["risk"],
            "needs": tickets[index]["needs"],
            "blocked_by": display_blockers(
                tickets[index], created_refs, repo),
        } for index in order]
        post_comment(
            repo, parent_number,
            coverage_comment_body(
                "{}#{}".format(repo, parent_number), created),
            run=run, agent=agent)
        return created
    except funnel.GitHubError as exc:
        if created_refs:
            progress = "already created: {}".format(", ".join(
                created_refs[index] for index in order
                if index in created_refs))
        else:
            progress = "no tickets were created"
        raise funnel.GitHubError("{} ({})".format(exc, progress))


def apply_question(repo: str, parent_number: int, question: str, *,
                   run: Optional[str] = None,
                   agent: Optional[str] = None) -> None:
    """Post the breakdown's question and park the project on Nate's answer.

    Creates no tickets: a ticket built on an invented decision is worse
    than no ticket, because someone will implement it.
    """
    post_comment(
        repo, parent_number,
        "{} {}".format(funnel.NEEDS_DECISION_PREFIX, question),
        run=run, agent=agent)
    url = "https://github.com/{}/issues/{}".format(repo, parent_number)
    funnel.write_project_select(
        add_to_project(url), "Needs", "human",
        "{}#{}".format(repo, parent_number))
    apply_blocked_label(repo, parent_number)


def apply(repo: str, number: int, normalized: dict, *,
          run: Optional[str] = None,
          agent: Optional[str] = None) -> dict:
    """Perform one validated answer's effects. Reads, then writes, in order."""
    project_ref = "{}#{}".format(repo, number)
    plan = fetch_plan(repo, number)
    if str(plan.get("state") or "").upper() != "OPEN":
        raise funnel.GitHubError(
            "project {} is {}; a closed project takes no breakdown".format(
                project_ref, plan.get("state")))
    question = normalized.get("needs_decision")
    if question is not None:
        # An answered Gates question is settled, and asking it again is the
        # #1167 defect: Nate answered at 21:53:02Z, the block cleared, and a
        # breakdown lane posted the same question and re-blocked the item four
        # minutes later. The plan body is already in hand from fetch_plan, so
        # this costs no extra read, and it goes through funnel's own reader
        # rather than a second parse — two parsers of one marker drift.
        answered = funnel.parse_gates_answer(plan.get("body") or "")
        if answered is not None:
            return {"project": project_ref,
                    "needs_decision": question,
                    "already_answered": answered}
        apply_question(repo, number, question, run=run, agent=agent)
        return {"project": project_ref, "needs_decision": question}
    created = apply_create(repo, number, normalized.get("tickets", []),
                           run=run, agent=agent)
    return {"project": project_ref, "created": created}


def read_answer(source: str) -> object:
    """Read one JSON answer from a path, or from stdin when given `-`."""
    if source == "-":
        text = sys.stdin.read()
    else:
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
    try:
        return json.loads(text)
    except ValueError as exc:
        raise BreakdownError(
            "the answer is not valid JSON: {}".format(exc))


def validation_exit(attempt: Optional[int]) -> int:
    """The exit for a malformed answer, recording nothing either way.

    Without ``--attempt`` the single-shot legacy exit 2 stands. With an
    explicit attempt the runner protocol applies: retryable 3 below the
    final attempt, 1 at it — the project stays awaiting breakdown, so
    the next run asks again.
    """
    if attempt is None:
        return EXIT_VALIDATION
    return RETRY_EXIT if attempt < FINAL_ATTEMPT else 1


def apply_main(argv: Optional[Sequence[str]] = None) -> int:
    """Validate one breakdown answer and perform its effects."""
    parser = argparse.ArgumentParser(
        description="validate one breakdown answer and create its tickets")
    parser.add_argument("project",
                        help="issue number, owner/repo#n, or issue URL")
    parser.add_argument("--answer", required=True,
                        help="path to the JSON answer, or - for stdin")
    parser.add_argument("--repo", default=None,
                        help="owner/name; required with a bare number when "
                             "ambiguous")
    parser.add_argument("--run", default=None,
                        help="heartbeat run id stamped on runner comments")
    parser.add_argument("--agent", default=None,
                        help="agent stamped on runner comments")
    parser.add_argument("--attempt", type=int, default=None,
                        help="attempt number in the runner protocol: a "
                             "malformed answer exits 3 below attempt 2 "
                             "(the runner retries once) and 1 at 2 or "
                             "later. Without --attempt, exit 2.")
    parser.add_argument("--validate-only", action="store_true",
                        help="validate without creating anything; print the "
                             "validated answer as JSON")
    args = parser.parse_args(argv)
    if args.attempt is not None and args.attempt < 1:
        parser.error("--attempt must be at least 1")
    try:
        text = (args.project or "").strip()
        default = funnel.resolve_repo(args.repo) if text.isdigit() \
            else args.repo
        repo, number = parse_project_ref(args.project, default)
    except (funnel.GitHubError, BreakdownError) as exc:
        print("breakdown-apply: {}".format(exc), file=sys.stderr)
        return 1
    try:
        answer = read_answer(args.answer)
    except OSError as exc:
        print("breakdown-apply: cannot read the answer: {}".format(exc),
              file=sys.stderr)
        return 1
    except BreakdownError as exc:
        print("breakdown-apply: {}".format(exc), file=sys.stderr)
        return validation_exit(args.attempt)
    try:
        errors, normalized = validate_answer(answer, fetch_issue_state)
    except funnel.GitHubError as exc:
        print("breakdown-apply: {}".format(exc), file=sys.stderr)
        return 1
    if errors:
        for error in errors:
            print("breakdown-apply: {}".format(error), file=sys.stderr)
        return validation_exit(args.attempt)
    assert normalized is not None
    if args.validate_only:
        print(json.dumps(normalized, indent=2, sort_keys=True))
        return 0
    try:
        result = apply(repo, number, normalized,
                       run=args.run, agent=args.agent)
    except (funnel.GitHubError, BreakdownError) as exc:
        print("breakdown-apply: {}".format(exc), file=sys.stderr)
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0
