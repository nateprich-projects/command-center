#!/usr/bin/env python3
"""overseer_filing.py — file an overseer-created issue into Project 2.

The Claude Code Projects overseers (#2418) file work over repo-scoped REST,
which cannot reach user-owned Projects (probe 2 failed, #2438). This script
runs on hobby-linux under `.github/workflows/overseer-filing.yml`, adds the
filed issue to Project 2, and sets Status, Origin, Risk, Needs and Domain
from the routing block in the issue body, using PROJECT_FILING_TOKEN.

Routing block contract (written by the overseer skills, #2441-#2443):

    <!-- command-center-routing -->
    ```json
    {"Risk": "standard", "Needs": "agent", "Domain": "League"}
    ```

Status is always Ideas and Origin is always agent; Risk, Needs and Domain
come from the block and must each be a known option. Field and option IDs
are resolved live from the Project: nothing is hardcoded.

Fail-closed rules, in order:

- No routing marker: not an overseer-filed issue. Exit quietly with no
  writes and no comment. Every human- and agent-filed issue in the repo
  passes through here and must be untouched.
- Marker present but the block is malformed, a value is missing, or a value
  is unknown: the issue is added to Project 2 with NO fields set, and a
  comment says why. Routing is never defaulted.
- Any transport failure: no comment is trusted, the run exits nonzero.

Triggers (see the workflow): `issues: opened` in this repo carries the body
in the event; `repository_dispatch` type `overseer-filed` and
`workflow_dispatch` carry `issue_repo` plus `issue_number` and the body is
refetched. Dispatches for repos outside OVERSEER_REPOS are refused.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))))
import funnel  # noqa: E402  — RISK_OPTIONS, NEEDS_OPTIONS, DOMAIN_OPTIONS

ROUTING_MARKER = "<!-- command-center-routing -->"
FAILURE_MARKER = "<!-- command-center-filing-failure -->"

#: Repos whose overseers file through this workflow (#2418): The League
#: project attaches The-League and Fantasy-GM, AFL attaches AFL and
#: Fantasy-GM, command-center attaches command-center.
OVERSEER_REPOS = frozenset({
    "nateprich-projects/command-center",
    "nateprich-projects/Fantasy-GM",
    "nateprich-projects/The-League",
    "nateprich-projects/AFL",
})

FIXED_STATUS = "Ideas"
FIXED_ORIGIN = "agent"
ROUTING_FIELDS = ("Risk", "Needs", "Domain")

ALLOWED_VALUES = {
    "Risk": tuple(funnel.RISK_OPTIONS),
    "Needs": tuple(funnel.NEEDS_OPTIONS),
    "Domain": tuple(funnel.DOMAIN_OPTIONS),
}

_LINE_LEADING_MARKER_RE = re.compile(r"(?m)^[ \t]*<!-- command-center-")
_FENCED_JSON_RE = re.compile(r"```json[ \t]*\r?\n(.*?)(\r?\n```)", re.DOTALL)

PROJECT_QUERY = """
query($login: String!, $number: Int!) {
  user(login: $login) {
    projectV2(number: $number) {
      id
      fields(first: 100) {
        nodes {
          ... on ProjectV2SingleSelectField {
            id name
            options { id name }
          }
        }
      }
    }
  }
}
"""

ISSUE_QUERY = """
query($owner: String!, $repo: String!, $number: Int!) {
  repository(owner: $owner, name: $repo) {
    issue(number: $number) {
      id
      projectItems(first: 20) {
        nodes { id project { id } }
      }
    }
  }
}
"""

SET_FIELD = """
mutation($project: ID!, $item: ID!, $field: ID!, $option: String!) {
  updateProjectV2ItemFieldValue(input: {
    projectId: $project, itemId: $item, fieldId: $field,
    value: {singleSelectOptionId: $option}
  }) { projectV2Item { id } }
}
"""


def extract_routing_block(body: str) -> Tuple[Optional[Dict], Optional[str]]:
    """Read the routing block, distinguishing absent from malformed.

    Returns ``(block, error)``. ``(None, None)`` means no marker: not an
    overseer-filed issue, skip silently. Any error string means the marker
    is present but unusable: fail closed with a comment.
    """
    if ROUTING_MARKER not in body:
        return None, None
    rest = body.split(ROUTING_MARKER, 1)[1]
    # Only a marker that begins a line starts another block, as in
    # funnel's marked-JSON reader: quoted marker text inside JSON must
    # not cut the block.
    next_marker = _LINE_LEADING_MARKER_RE.search(rest)
    if next_marker:
        rest = rest[:next_marker.start()]
    fenced = _FENCED_JSON_RE.search(rest)
    if not fenced:
        return None, (
            "no fenced ```json block found after {}".format(ROUTING_MARKER)
        )
    try:
        found = json.loads(fenced.group(1))
    except (TypeError, ValueError) as exc:
        return None, "routing block is not valid JSON ({})".format(exc)
    if not isinstance(found, dict):
        return None, "routing block must be a JSON object"
    return found, None


def validate_routing(block: Dict) -> Tuple[Optional[Dict[str, str]],
                                           Optional[str]]:
    """Check the block's values against the live Project options.

    Returns ``(values, error)`` with exactly Risk, Needs and Domain on
    success. Extra keys are ignored: only routing values fail closed.
    """
    values: Dict[str, str] = {}
    for field in ROUTING_FIELDS:
        if field not in block:
            return None, "routing block is missing {!r}".format(field)
        value = block[field]
        if not isinstance(value, str):
            return None, "routing block has non-string {} {!r}".format(
                field, value)
        if value not in ALLOWED_VALUES[field]:
            return None, "routing block has unknown {} {!r}; expected one of: {}".format(
                field, value, ", ".join(ALLOWED_VALUES[field]))
        values[field] = value
    return values, None


def gh(*args: str) -> str:
    proc = subprocess.run(["gh"] + list(args),
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout).strip())
    return proc.stdout


def gh_graphql(query: str, **variables) -> dict:
    cmd = ["gh", "api", "graphql", "-f", "query=" + query]
    for key, value in variables.items():
        flag = "-F" if isinstance(value, (int, bool)) else "-f"
        cmd += [flag, "{}={}".format(key, value)]
    payload = json.loads(gh(*cmd[1:]))
    if isinstance(payload, dict) and payload.get("errors"):
        raise RuntimeError("GraphQL errors: {}".format(payload["errors"]))
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("GraphQL response held no data")
    return data


def resolve_event() -> Tuple[str, int, Optional[str]]:
    """Return ``(issue_repo, issue_number, body_or_None)`` for this run."""
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    event_path = os.environ.get("GITHUB_EVENT_PATH", "")
    with open(event_path, encoding="utf-8") as handle:
        event = json.load(handle)
    if event_name == "issues":
        issue = event.get("issue") or {}
        repository = event.get("repository") or {}
        return (
            str(repository.get("full_name") or ""),
            int(issue.get("number")),
            issue.get("body"),
        )
    if event_name == "workflow_dispatch":
        inputs = event.get("inputs") or {}
        return (
            str(inputs.get("issue_repo") or ""),
            int(inputs.get("issue_number")),
            None,
        )
    if event_name == "repository_dispatch":
        payload = event.get("client_payload") or {}
        return (
            str(payload.get("issue_repo") or ""),
            int(payload.get("issue_number")),
            None,
        )
    raise RuntimeError("unsupported event {}".format(event_name or "unknown"))


def fetch_issue_body(repo: str, number: int) -> str:
    payload = json.loads(gh("api", "repos/{}/issues/{}".format(repo, number)))
    return str(payload.get("body") or "")


def project_selects() -> Tuple[str, Dict[str, Dict[str, str]]]:
    """Return the live ``(project_id, {field: {option: id}})`` for filing."""
    data = gh_graphql(
        PROJECT_QUERY, login=funnel.PROJECT_OWNER,
        number=funnel.PROJECT_NUMBER)
    project = (data.get("user") or {}).get("projectV2") or {}
    project_id = project.get("id")
    nodes = ((project.get("fields") or {}).get("nodes")) or []
    if not isinstance(project_id, str) or not project_id:
        raise RuntimeError("Project {}/{} is missing or not visible".format(
            funnel.PROJECT_OWNER, funnel.PROJECT_NUMBER))
    selects: Dict[str, Dict[str, str]] = {}
    for node in nodes:
        if not isinstance(node, dict) or not isinstance(node.get("id"), str):
            continue
        options = node.get("options")
        if not isinstance(options, list):
            continue
        by_name = {
            option.get("name"): option.get("id")
            for option in options if isinstance(option, dict)
        }
        if (len(by_name) != len(options) or not by_name
                or any(not isinstance(value, str) or not value
                       for value in by_name.values())):
            raise RuntimeError(
                "Project field {} has ambiguous or incomplete options".format(
                    node.get("name")))
        selects[str(node.get("name"))] = {
            "field_id": node["id"], "options": by_name,
        }
    return project_id, selects


def existing_item_id(owner: str, repo: str, number: int,
                     project_id: str) -> Optional[str]:
    data = gh_graphql(ISSUE_QUERY, owner=owner, repo=repo, number=number)
    issue = (data.get("repository") or {}).get("issue")
    if not isinstance(issue, dict):
        return None
    nodes = (issue.get("projectItems") or {}).get("nodes") or []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        project = node.get("project") or {}
        if project.get("id") == project_id and node.get("id"):
            return str(node.get("id"))
    return None


def ensure_item_id(repo: str, number: int, project_id: str) -> str:
    """Add the issue to Project 2, reusing its item when already filed."""
    owner, _, short = repo.partition("/")
    found = existing_item_id(owner, short, number, project_id)
    if found:
        return found
    url = "https://github.com/{}/issues/{}".format(repo, number)
    payload = json.loads(gh(
        "project", "item-add", str(funnel.PROJECT_NUMBER),
        "--owner", funnel.PROJECT_OWNER,
        "--url", url, "--format", "json",
    ))
    item_id = payload.get("id")
    if not isinstance(item_id, str) or not item_id:
        raise RuntimeError("project item-add returned no item id")
    return item_id


def write_field(project_id: str, item_id: str, selects: dict,
                field: str, value: str, ref: str) -> None:
    select = selects.get(field)
    if select is None:
        raise RuntimeError("Project has no field {}".format(field))
    option = select["options"].get(value)
    if option is None:
        raise RuntimeError(
            "Project field {} has no option {}".format(field, value))
    response = gh_graphql(
        SET_FIELD, project=project_id, item=item_id,
        field=select["field_id"], option=option)
    mutation = response.get("updateProjectV2ItemFieldValue") or {}
    item = mutation.get("projectV2Item") or {}
    if item.get("id") != item_id:
        raise RuntimeError(
            "GitHub did not confirm the {} update for {} to {}".format(
                field, ref, value))


def post_comment(repo: str, number: int, body: str) -> None:
    gh("api", "-X", "POST",
       "repos/{}/issues/{}/comments".format(repo, number),
       "-f", "body=" + body)


def failure_comment(reason: str, routing_fault: bool = True) -> str:
    if routing_fault:
        remedy = (
            "The issue was added to the Project with no fields set; routing "
            "is never defaulted. Fix the `{}` block — a fenced ```json "
            "object with Risk ({}), Needs ({}) and Domain ({}) — then "
            "re-run the `overseer-filing` workflow for this issue "
            "(`workflow_dispatch` with `issue_repo` and `issue_number`)."
        ).format(
            ROUTING_MARKER,
            ", ".join(ALLOWED_VALUES["Risk"]),
            ", ".join(ALLOWED_VALUES["Needs"]),
            ", ".join(ALLOWED_VALUES["Domain"]),
        )
    else:
        # A Project-side write failure, not a routing fault: the block was
        # valid, so the fix is a re-run, not a body edit. The run stops at
        # the first failed write, so some fields may already be set;
        # re-running rewrites all five idempotently.
        remedy = (
            "The filing run stopped before the field set was confirmed, so "
            "some fields may already be set. Re-run the `overseer-filing` "
            "workflow for this issue (`workflow_dispatch` with `issue_repo` "
            "and `issue_number`) to complete the set; no body edit is "
            "needed."
        )
    return (
        "{}\n\n"
        "Command Center could not file this issue into Project 2: {}.\n\n"
        "{}"
    ).format(FAILURE_MARKER, reason, remedy)


def main() -> int:
    if not os.environ.get("GH_TOKEN"):
        print("overseer_filing: GH_TOKEN (PROJECT_FILING_TOKEN) is not set")
        return 2
    try:
        repo, number, body = resolve_event()
    except (KeyError, TypeError, ValueError, RuntimeError) as exc:
        print("overseer_filing: cannot resolve the filed issue: {}".format(exc))
        return 2
    if repo not in OVERSEER_REPOS:
        print("overseer_filing: refusing {}; not an overseer repo".format(
            repo or "unknown repo"))
        return 2
    ref = "{}/issues/{}".format(repo, number)
    if body is None:
        try:
            body = fetch_issue_body(repo, number)
        except (RuntimeError, ValueError) as exc:
            print("overseer_filing: cannot read {}: {}".format(ref, exc))
            return 1
    block, block_error = extract_routing_block(body or "")
    if block is None and block_error is None:
        print("overseer_filing: {} carries no routing block; skipping".format(
            ref))
        return 0
    values: Optional[Dict[str, str]] = None
    reason = block_error
    if reason is None:
        assert block is not None
        values, reason = validate_routing(block)
    try:
        project_id, selects = project_selects()
        item_id = ensure_item_id(repo, number, project_id)
    except (RuntimeError, ValueError) as exc:
        print("overseer_filing: cannot file {}: {}".format(ref, exc))
        return 1
    if values is None:
        assert reason is not None
        try:
            post_comment(repo, number, failure_comment(reason))
        except (RuntimeError, ValueError) as exc:
            print("overseer_filing: {} failed closed ({}) but the failure "
                  "comment could not post: {}".format(ref, reason, exc))
            return 1
        print("overseer_filing: {} failed closed: {}".format(ref, reason))
        return 0
    writes: List[Tuple[str, str]] = [
        ("Status", FIXED_STATUS),
        ("Origin", FIXED_ORIGIN),
        ("Risk", values["Risk"]),
        ("Needs", values["Needs"]),
        ("Domain", values["Domain"]),
    ]
    # Every value resolves before any write: a renamed option fails closed
    # with no fields, never a partial set.
    try:
        for field, value in writes:
            select = selects.get(field)
            if select is None or value not in select["options"]:
                raise RuntimeError(
                    "Project field {} has no option {}".format(field, value))
        for field, value in writes:
            write_field(project_id, item_id, selects, field, value, ref)
    except (RuntimeError, ValueError) as exc:
        detail = "a Project field write failed: {}".format(exc)
        try:
            post_comment(repo, number, failure_comment(detail,
                                                     routing_fault=False))
        except (RuntimeError, ValueError) as comment_exc:
            print("overseer_filing: {} write failed ({}) and the failure "
                  "comment could not post: {}".format(ref, exc, comment_exc))
            return 1
        print("overseer_filing: {} write failed: {}".format(ref, exc))
        return 1
    print("overseer_filing: {} filed as Status=Ideas Origin=agent "
          "Risk={} Needs={} Domain={}".format(
              ref, values["Risk"], values["Needs"], values["Domain"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
