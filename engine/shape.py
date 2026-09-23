#!/usr/bin/env python3
"""Shape one idea from a structured answer (#810, Phase 2 of #794).

The shape runner shows the model a packet and nothing else: the idea, its
origin, plan.md, AGENTS.md, and the sibling plans in the same repo. The
model returns one structured answer; this module validates that answer
against the plan schema, renders the issue body from the fields, and
applies the self-approval rule mechanically.

The decision never parses a Needs section: ``needs_nate``'s four fields
are the open-question record, and ``decide`` feeds them to the shared
``self_approval_eligible`` predicate. The rendered body keeps the stable
four-category section as the human-readable record of those fields.

Two entry points share this module: ``shape-packet`` is read-only, while
``shape-apply`` performs the shaping writes.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import funnel  # noqa: E402


class ShapeError(Exception):
    """A shape answer failed validation, or shaping cannot proceed."""


#: Malformed answer on a runner-protocol attempt below the final one: the
#: runner feeds the parse error back to the model and calls again.
RETRY_EXIT = 3

#: Attempts past the first are final: a malformed answer then exits 1
#: with nothing recorded, and the runner finishes the run errored. The
#: idea keeps its needs-shaping label, so the next run asks again.
FINAL_ATTEMPT = 2


def validation_exit(attempt: Optional[int]) -> int:
    """The exit for a malformed answer, recording nothing either way.

    Without ``--attempt`` the single-shot legacy exit 1 stands. With an
    explicit attempt the runner protocol applies: retryable 3 below the
    final attempt, 1 at it.
    """
    if attempt is None:
        return 1
    return RETRY_EXIT if attempt < FINAL_ATTEMPT else 1


#: The answer keys shape-apply accepts — exactly these, no extras. From
#: the Shape row of #794, plus the model-declared escalated-risk list
#: (#1034) that the decision unions with the wording scan, plus the
#: sequencing-dependency list (#1053) that sequencing questions become
#: instead of Needs Nate entries.
ANSWER_KEYS = frozenset({
    "decided_from_precedent",
    "decided_by_agent",
    "needs_nate",
    "proposed_class",
    "plan_markdown",
    "escalated_risk",
    "depends_on",
})

#: A sequencing dependency: owner/repo#n, the only shape accepted
#: (#1053). The packet's sibling plans carry full refs, so the model
#: copies them verbatim; a bare #n is a confused-model shape and fails.
REF_RE = re.compile(
    r"\A(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)"
    r"#(?P<number>[1-9][0-9]*)\Z"
)

#: The needs_nate fields, each mapped to the Needs-section category it
#: renders as. The category spellings are the stable headings the skill
#: and the old parser agree on.
NEEDS_FIELDS = (
    ("exposure", "Exposure"),
    ("gates", "Gates"),
    ("scope", "Scope and priority"),
    ("preference", "Preference"),
)

#: The all-clear rendering per category, verbatim from skills/shape: the
#: bare answer is ``nothing outstanding`` and the elaboration follows
#: after a period.
ALL_CLEAR = {
    "Exposure": "nothing outstanding. "
                "No new credentials or reachable surface.",
    "Gates": "nothing outstanding. No gate ownership changes.",
    "Scope and priority": "nothing outstanding. "
                          "The scoped change is documented.",
    "Preference": "nothing outstanding. "
                  "No user-facing choice remains.",
}


def _require_text(value: object, where: str) -> str:
    """Return stripped text, or raise when it is missing or blank."""
    if not isinstance(value, str) or not value.strip():
        raise ShapeError("{} must be a non-empty string".format(where))
    return value.strip()


def _require_line(value: object, where: str) -> str:
    """Return one line of text, or raise when it is missing or blank.

    Decision fields render as Markdown list items, so inner newlines are
    collapsed: a multi-line claim would otherwise break the list form.
    """
    return re.sub(r"\s+", " ", _require_text(value, where))


def _check_keys(entry: object, keys: Sequence[str], where: str) -> None:
    """Reject a mapping that is missing keys or carries unknown ones."""
    if not isinstance(entry, dict):
        raise ShapeError("{} must be an object".format(where))
    missing = sorted(set(keys) - set(entry))
    extra = sorted(set(entry) - set(keys))
    if missing:
        raise ShapeError("{} is missing {}".format(
            where, ", ".join(missing)))
    if extra:
        raise ShapeError("{} has unknown fields: {}".format(
            where, ", ".join(extra)))


def _validate_precedent(entries: object) -> List[Dict[str, str]]:
    """Validate the decided-from-precedent list. It may be empty: a
    genuinely novel idea cites nothing, and an empty list says so
    honestly rather than inventing a source."""
    if not isinstance(entries, list):
        raise ShapeError("decided_from_precedent must be a list")
    validated = []
    for index, entry in enumerate(entries):
        where = "decided_from_precedent[{}]".format(index)
        _check_keys(entry, ("claim", "source"), where)
        validated.append({
            "claim": _require_line(entry["claim"], where + ".claim"),
            "source": _require_line(entry["source"], where + ".source"),
        })
    return validated


def _validate_agent_decisions(entries: object) -> List[Dict[str, str]]:
    """Validate the decided-by-agent list. Each entry keeps its
    reasoning and its rejected alternative, in the same shape as the
    plan's Rejected section."""
    if not isinstance(entries, list):
        raise ShapeError("decided_by_agent must be a list")
    validated = []
    for index, entry in enumerate(entries):
        where = "decided_by_agent[{}]".format(index)
        _check_keys(entry, ("decision", "alternative", "why"), where)
        validated.append({
            "decision": _require_line(
                entry["decision"], where + ".decision"),
            "alternative": _require_line(
                entry["alternative"], where + ".alternative"),
            "why": _require_line(entry["why"], where + ".why"),
        })
    return validated


def _validate_needs_nate(needs: object) -> Dict[str, Optional[List[str]]]:
    """Validate the open-question record: the four fields, each null or
    a non-empty list of single questions (#1053).

    Atomic questions per category: a compound splits before it is asked,
    so a settled half cannot drag its genuine half to Nate. A bare
    string is the old single-question shape and fails — the runner
    retries once with the error fed back, and the model answers again
    with a list.
    """
    fields = [field for field, _ in NEEDS_FIELDS]
    _check_keys(needs, fields, "needs_nate")
    assert isinstance(needs, dict)
    validated: Dict[str, Optional[List[str]]] = {}
    for field, _ in NEEDS_FIELDS:
        value = needs[field]
        if value is None:
            validated[field] = None
        elif not isinstance(value, list) or not value:
            raise ShapeError(
                "needs_nate.{} must be null or a non-empty list of "
                "questions".format(field))
        else:
            questions = []
            for index, entry in enumerate(value):
                where = "needs_nate.{}.{}".format(field, index)
                questions.append(_require_line(entry, where))
            validated[field] = questions
    return validated


def _validate_depends_on(entries: object) -> List[str]:
    """Validate the sequencing-dependency list (#1053).

    Sequencing is never a Needs Nate question: waiting on a named
    sibling, priority against named tickets, or a separate pin or
    activation is recorded here as owner/repo#n refs, and the apply
    step writes the native blocked-by edges. Empty when the plan waits
    on nothing — an empty list says so honestly.
    """
    if not isinstance(entries, list):
        raise ShapeError("depends_on must be a list")
    validated = []
    for index, entry in enumerate(entries):
        where = "depends_on[{}]".format(index)
        if not isinstance(entry, str) or not REF_RE.match(entry.strip()):
            raise ShapeError(
                "{} must be an owner/repo#n ref".format(where))
        validated.append(entry.strip())
    return validated


def _validate_escalated_risk(entries: object) -> List[Dict[str, str]]:
    """Validate the model-declared escalated-risk list (#1034).

    A list of ``{"reason", "why"}`` objects, possibly empty. Each reason
    names one of ``funnel.ESCALATION_PATTERNS`` exactly; each why is one
    non-empty line saying what in the plan carries that risk. The model
    judges the plan itself here, not its wording: the decision holds on
    this list or the wording scan, whichever fires, so the declaration
    can only hold, never release.
    """
    if not isinstance(entries, list):
        raise ShapeError("escalated_risk must be a list")
    validated = []
    for index, entry in enumerate(entries):
        where = "escalated_risk[{}]".format(index)
        _check_keys(entry, ("reason", "why"), where)
        reason = _require_line(entry["reason"], where + ".reason")
        if reason not in funnel.ESCALATION_PATTERNS:
            raise ShapeError(
                "{} is not an escalation reason; choose one of "
                "{}".format(where + ".reason",
                             ", ".join(sorted(
                                 funnel.ESCALATION_PATTERNS))))
        validated.append({
            "reason": reason,
            "why": _require_line(entry["why"], where + ".why"),
        })
    return validated


def validate_answer(data: object) -> Dict:
    """Validate a shape answer against the plan schema.

    Returns a normalized copy: surrounding whitespace stripped, inner
    newlines in one-line fields collapsed. Anything malformed raises
    ``ShapeError`` before any write, so a confused model cannot leave a
    half-shaped idea behind.
    """
    _check_keys(data, sorted(ANSWER_KEYS), "the answer")
    assert isinstance(data, dict)
    proposed = _require_line(data["proposed_class"], "proposed_class")
    if proposed not in funnel.LADDER:
        raise ShapeError(
            "proposed_class {!r} is not a ladder class; choose one of "
            "{}".format(proposed, ", ".join(funnel.LADDER)))
    return {
        "decided_from_precedent": _validate_precedent(
            data["decided_from_precedent"]),
        "decided_by_agent": _validate_agent_decisions(
            data["decided_by_agent"]),
        "needs_nate": _validate_needs_nate(data["needs_nate"]),
        "proposed_class": proposed,
        "plan_markdown": _require_text(
            data["plan_markdown"], "plan_markdown"),
        "escalated_risk": _validate_escalated_risk(
            data["escalated_risk"]),
        "depends_on": _validate_depends_on(data["depends_on"]),
    }


def render_plan(answer: Dict) -> str:
    """Render the issue body from validated answer fields.

    The plan narrative and proposed class come first, followed by a
    durable Risk line when the model declared a risk, then the
    runner-owned decision record: what precedent settled, what the
    agent decided itself, the sequencing dependencies where any wait
    (#1053), and the four Needs Nate categories (each open list joined
    on one line, the stable all-clear line where null). Needs stays
    last so the section holds only its category lines. Takes a
    validated answer; ``apply_shape`` validates before calling.
    """
    lines = [answer["plan_markdown"].rstrip(), "",
             "Proposed class: {}".format(answer["proposed_class"]), ""]
    if answer["escalated_risk"]:
        # The sweep must be able to re-run the exact self-approval condition
        # after the typed answer is gone. Keep a model-declared risk visible in
        # the durable plan so the shared plan scan continues to hold it.
        declared = "; ".join(
            "{}: {}".format(entry["reason"], entry["why"])
            for entry in answer["escalated_risk"]
        )
        lines.extend(["Risk: escalated — {}".format(declared), ""])
    lines.extend(["## Decided from precedent", ""])
    precedent = answer["decided_from_precedent"]
    if precedent:
        for entry in precedent:
            lines.append("- {} (source: {})".format(
                entry["claim"], entry["source"]))
    else:
        lines.append("None recorded.")
    lines.extend(["", "## Decided by the agent", ""])
    by_agent = answer["decided_by_agent"]
    if by_agent:
        for entry in by_agent:
            lines.append("- {} (rejected: {}; {})".format(
                entry["decision"], entry["alternative"], entry["why"]))
    else:
        lines.append("None recorded.")
    depends = answer.get("depends_on", [])
    if depends:
        lines.extend(["", "## Sequencing", "",
                      "Depends on: {}".format(", ".join(depends))])
    lines.extend(["", "## Needs Nate", ""])
    needs = answer["needs_nate"]
    for field, category in NEEDS_FIELDS:
        questions = needs[field]
        lines.append("- {}: {}".format(
            category,
            "; ".join(questions)
            if questions is not None else ALL_CLEAR[category]))
    lines.append("")
    return "\n".join(lines)


def open_need_categories(answer: Dict) -> List[str]:
    """The Needs Nate categories holding a question, in field order."""
    return [category for field, category in NEEDS_FIELDS
            if answer["needs_nate"][field] is not None]


def needs_nate_open(answer: Dict) -> bool:
    """Whether the answer's open-question record asks Nate anything.

    This boolean is what the old Needs-section parser used to supply.
    One non-null field holds the plan at Shaped.
    """
    return bool(open_need_categories(answer))


def decide(answer: Dict, *,
           klass: Optional[str],
           origin_voice: Optional[str],
           override_target: Optional[str] = None,
           escalation_reasons: Sequence[str] = (),
           state: Optional[str] = None) -> Tuple[str, str]:
    """Apply the self-approval rule to validated fields. Pure: no IO.

    Returns the status and its reason: ``Ready`` only when the four
    needs_nate fields are all null, the class is self-approvable, the
    effective shaper is agents, and the plan carries no escalated risk.
    The rule itself is the shared ``self_approval_eligible`` predicate,
    so the packet path cannot drift from the shaping path; only the
    needs_nate input comes from the fields instead of the parser.

    Escalated risk is the union of the model's ``escalated_risk``
    declaration and the wording scan passed as ``escalation_reasons``
    (#1034): either one holding is enough, so a plan-worded risk the
    scan misses still holds, and a scan hit the model omitted still
    holds. There is no standard override: an empty declaration never
    clears a scan hit.

    Sequencing dependencies never hold (#1053): a plan that waits on
    a named sibling but asks Nate nothing self-approves, and the
    dependency is recorded as a native edge, not a question.
    """
    declared = [entry["reason"] for entry in answer.get("escalated_risk", [])
                if isinstance(entry, dict)
                and isinstance(entry.get("reason"), str)]
    reasons = sorted(set(escalation_reasons or ()) | set(declared))
    open_categories = open_need_categories(answer)
    if funnel.self_approval_eligible(
            klass, origin_voice, override_target,
            needs_nate=bool(open_categories),
            escalated=bool(reasons),
            state=state):
        if origin_voice == "agent":
            owner_basis = "origin agent"
        else:
            owner_basis = "origin override to agents"
        return ("Ready",
                "needs_nate all null; class {} self-approvable; "
                "{}".format(klass, owner_basis))
    failed = []
    if state is not None and str(state).upper() != "OPEN":
        failed.append("the issue is {} on GitHub".format(
            str(state).upper()))
    if klass not in funnel.SELF_APPROVABLE_CLASSES:
        failed.append("class {} is not self-approvable".format(
            klass or "unset"))
    if funnel.effective_shape_owner(
            origin_voice, override_target) != "agents":
        failed.append("origin is Nate's")
    failed.extend("open question under {}".format(category)
                  for category in open_categories)
    if reasons:
        failed.append("escalated risk ({})".format(", ".join(reasons)))
    return "Shaped", "; ".join(failed)


def preview_decision(items: list, item, answer: Dict) -> Tuple[str, str]:
    """The status one validated answer would record, without writing.

    The same inputs the live path decides from — the effective class
    with the class-missing recovery, the origin voice and override, the
    model's escalated-risk declaration inside the answer, and the
    escalated-risk scan of the rendered plan — so ``--validate-only``
    reports the status the live path would write. Pure apart from its
    arguments; takes a validated answer.
    """
    original_body = item.body or ""
    origin = funnel.parse_origin(original_body)
    origin_voice = origin["voice"] if origin is not None else None
    override = funnel.parse_origin_override(original_body)
    override_target = override["target"] if override is not None else None
    by_ref = {candidate.ref: candidate for candidate in items}
    effective_klass = funnel.effective_class(item, by_ref)
    if item.klass not in funnel.LADDER and origin_voice == "agent":
        effective_klass = answer["proposed_class"]
    scan_answer = dict(answer)
    # `decide` consumes the typed declaration separately. Keep it out of the
    # wording scan here so the durable Risk line added by `render_plan` does
    # not report the same declaration twice.
    scan_answer["escalated_risk"] = []
    return decide(
        answer,
        klass=effective_klass,
        origin_voice=origin_voice,
        override_target=override_target,
        escalation_reasons=funnel.plan_is_escalated(
            render_plan(scan_answer)
        ),
        state=item.state,
    )


def issue_url(ref: str) -> str:
    """Render an owner/repo#n ref as the issue URL `gh` takes for edges."""
    match = REF_RE.match(ref.strip())
    if not match:
        raise ShapeError("not an owner/repo#n ref: {!r}".format(ref))
    return "https://github.com/{}/{}/issues/{}".format(
        match.group("owner"), match.group("repo"), match.group("number"))


def blocked_by_values(depends_on: Sequence[str], repo: str) -> List[str]:
    """Render sequencing deps as `gh --add-blocked-by` values (#1053).

    Same-repo refs become bare numbers; cross-repo ones become issue
    URLs, since a bare number would point at the wrong repository.
    Answer order is preserved. Takes validated refs; anything else
    fails closed rather than guessing an edge target.
    """
    values = []
    for ref in depends_on or []:
        match = REF_RE.match(ref.strip())
        if not match:
            raise ShapeError(
                "not an owner/repo#n ref: {!r}".format(ref))
        if "{}/{}".format(match.group("owner"),
                          match.group("repo")) == repo:
            values.append(match.group("number"))
        else:
            values.append(issue_url(ref))
    return values


def fetch_repo_text(repo: str, path: str) -> Tuple[str, bool]:
    """One text file at the repo's default branch, or ("", True).

    Missing is a fact about the repo, not a fetch failure: the packet
    stays valid and says so, because the shape question is still
    answerable without it.
    """
    proc = funnel._run_gh(
        ["gh", "api", "repos/{}/contents/{}".format(repo, path),
         "-H", "Accept: application/vnd.github.raw"],
        capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        if "404" in (proc.stderr or ""):
            return "", True
        raise funnel.GitHubError(
            "could not read {} in {}: {}".format(
                path, repo, (proc.stderr or "").strip()))
    return proc.stdout or "", False


def sibling_plan_items(items: Sequence, idea) -> list:
    """The other open project plans in the idea's repo, sorted by ref.

    Open plans are parentless, open, Shaped/Ready/Building items —
    narrowed to the idea's own repo, which is what the shape packet
    promises.
    """
    return sorted(
        (row for row in items
         if row.ref != idea.ref
         and row.repo == idea.repo
         and row.parent is None
         and row.state == "OPEN"
         and row.status in funnel.SHAPING_PLAN_STATUSES),
        key=lambda row: row.ref)


def _idea_packet(item) -> Dict:
    """The idea's identity, captured note, labels, and Project fields."""
    return {
        "ref": item.ref,
        "number": item.number,
        "title": item.title,
        "url": item.url,
        "body": item.body,
        "labels": sorted(item.labels or []),
        "status": item.status,
        "klass": item.klass,
    }


def _sibling_packet(item) -> Dict:
    """One sibling plan: identity, stage, class, and the plan itself."""
    return {
        "ref": item.ref,
        "number": item.number,
        "title": item.title,
        "status": item.status,
        "klass": item.klass,
        "body": item.body,
    }


def build_packet(*, repo: str, idea: Dict,
                 origin_voice: Optional[str],
                 override_target: Optional[str],
                 plan_md: str, plan_md_missing: bool,
                 agents_md: str, agents_md_missing: bool,
                 siblings: Sequence[Dict],
                 collected_at: str) -> Dict:
    """Assemble the packet from already-fetched pieces. Pure: no IO.

    Everything the shape question needs in one JSON-serialisable dict:
    the idea, its origin, the repo's plan.md and AGENTS.md, and the
    sibling plans the model cites as precedent.
    """
    return {
        "repo": repo,
        "idea": dict(idea),
        "origin": {
            "voice": origin_voice,
            "override_target": override_target,
        },
        "plan_md": plan_md,
        "plan_md_missing": plan_md_missing,
        "agents_md": agents_md,
        "agents_md_missing": agents_md_missing,
        "sibling_plans": [dict(row) for row in siblings],
        "collected_at": collected_at,
    }


def collect(repo: Optional[str], idea_number: int, *,
            items_loader: Optional[Callable[[], list]] = None,
            now: Optional[datetime] = None) -> Dict:
    """Fetch every piece and build the packet. Reads only, no writes."""
    resolved = funnel.resolve_repo(repo)
    items = (items_loader or funnel.load_items)()
    idea_item = funnel.find(items, "{}#{}".format(resolved, idea_number))
    origin = funnel.parse_origin(idea_item.body or "")
    override = funnel.parse_origin_override(idea_item.body or "")
    plan_md, plan_md_missing = fetch_repo_text(resolved, "plan.md")
    agents_md, agents_md_missing = fetch_repo_text(resolved, "AGENTS.md")
    return build_packet(
        repo=resolved,
        idea=_idea_packet(idea_item),
        origin_voice=origin["voice"] if origin is not None else None,
        override_target=(override["target"]
                         if override is not None else None),
        plan_md=plan_md,
        plan_md_missing=plan_md_missing,
        agents_md=agents_md,
        agents_md_missing=agents_md_missing,
        siblings=[_sibling_packet(row)
                  for row in sibling_plan_items(items, idea_item)],
        collected_at=(now or datetime.now(timezone.utc)).isoformat(),
    )


def packet_main(argv: Optional[Sequence[str]] = None) -> int:
    """Print the shape packet for one idea as JSON."""
    parser = argparse.ArgumentParser(
        description="assemble one read-only shape packet for an idea")
    parser.add_argument("idea", type=int, help="idea issue number")
    parser.add_argument("--repo", default=None,
                        help="owner/name; required when ambiguous")
    args = parser.parse_args(argv)
    try:
        packet = collect(args.repo, args.idea)
    except funnel.GitHubError as exc:
        print("shape-packet: {}".format(exc), file=sys.stderr)
        return 1
    print(json.dumps(packet, indent=2, sort_keys=True))
    return 0


def apply_shape(items: list, now: datetime, ref: str,
                answer_data: object,
                run: Optional[str] = None,
                agent: Optional[str] = None) -> int:
    """Validate one shape answer and record the plan it carries.

    Renders the issue body from the answer fields, applies the
    self-approval rule mechanically, writes Shaped or Ready, and posts
    the self-approval record as a code-owned comment on a Ready
    transition. Validation runs before the first write, so a malformed
    answer leaves the idea untouched.

    Two deliberate departures from ``funnel shaped``: the open-question
    record comes from the needs_nate fields rather than a Needs-section
    parse, and a manually placed origin-override block is carried over
    verbatim with the origin block — otherwise the override term of the
    rule could never fire on this path, and the packet would report an
    override the apply step ignores.

    Sequencing dependencies ride the body write as native blocked-by
    edges (#1053): one ``gh issue edit`` carries the rendered plan and
    the edges together, so a ref GitHub cannot resolve fails the whole
    write instead of recording a plan whose dependency is missing.
    """
    item = funnel.find(items, ref)
    answer = validate_answer(answer_data)

    original_body = item.body or ""
    origin = funnel.parse_origin(original_body)
    origin_voice = origin["voice"] if origin is not None else None
    carried_blocks = []
    for marker in (funnel.ORIGIN_MARKER,
                   funnel.ORIGIN_OVERRIDE_MARKER):
        block = funnel._marked_json_block(original_body, marker)
        if block is not None:
            carried_blocks.append(block)
    class_missing = item.klass not in funnel.LADDER

    rendered = render_plan(answer)
    # Decided before the first write from the same inputs as the shadow
    # path: the decision is pure, and previewing it early changes nothing
    # the writes below can observe.
    status, reason = preview_decision(items, item, answer)
    authority_signals = funnel.needs_nate_signals(rendered)
    body = funnel.append_provenance(
        rendered, "agent", at=now, run=run, agent=agent)
    for block in carried_blocks:
        body = "{}\n\n{}".format(body, block)

    command = ["gh", "issue", "edit", str(item.number), "--repo",
               item.repo, "--body", body]
    blocked_by = blocked_by_values(answer.get("depends_on", []), item.repo)
    if blocked_by:
        command += ["--add-blocked-by", ",".join(blocked_by)]
    out = funnel._run_gh(command, capture_output=True, text=True)
    if out.returncode != 0:
        raise funnel.GitHubError(out.stderr.strip())
    # The session keeps this object after the issue-body write. Keep its
    # body aligned with GitHub before a same-session reader evaluates it.
    item.body = body

    if not item.item_id:
        raise funnel.GitHubError("{} is not in the Project".format(item.ref))
    if class_missing and origin_voice == "agent":
        # This recovery write is the only place shaping may assign a
        # Class. Keep it immediately before the Status mutation so the
        # latter never makes an unclassed idea look like it advanced
        # cleanly.
        funnel.gh_graphql(
            funnel.SET_FIELD, project=funnel.PROJECT_ID,
            item=item.item_id, field=funnel.CLASS_FIELD_ID,
            option=funnel._option_id(funnel.CLASS_FIELD_ID,
                                     answer["proposed_class"]))
    status_error = funnel._write_status(item, status, now)
    if status_error is not None:
        # The body is durable, but the stage is not confirmed. Do not
        # clear the shaping label or post a self-approval marker, both
        # of which would make the item look further along than its known
        # Project state.
        persisted_status = item.status or "unknown"
        print("{} → {}\n{}".format(item.ref, persisted_status, item.url))
        print(
            "could not confirm requested Status {} for {}; retaining the "
            "previously observed stage {}: {}".format(
                status, item.ref, persisted_status, status_error
            ),
            file=sys.stderr,
        )
        return 1

    label = funnel._run_gh(
        ["gh", "issue", "edit", str(item.number), "--repo", item.repo,
         "--remove-label", "needs-shaping"],
        capture_output=True, text=True,
    )
    if label.returncode == 0:
        item.labels = [
            value for value in item.labels if value != "needs-shaping"
        ]
    if status == "Ready":
        basis = "{}; no escalated risk".format(reason)
        if authority_signals:
            basis += "; authority signals: {}".format(
                ", ".join(authority_signals)
            )
        comment = funnel._run_gh(
            ["gh", "issue", "comment", str(item.number),
             "--repo", item.repo,
             "--body", funnel.self_approval_comment(
                 basis, at=now, run=run, agent=agent
             )],
            capture_output=True, text=True,
        )
        if comment.returncode != 0:
            raise funnel.GitHubError(comment.stderr.strip())
    print("{} → {}\n{}".format(item.ref, status, item.url))
    if status == "Ready":
        print("advanced to Ready: {}".format(reason))
    else:
        print("held at Shaped: {}".format(reason))
    if authority_signals:
        print("\n--- self-approval advisory ---")
        print("Authority signals are recorded in the Self-approved basis:")
        descriptions = {
            "policy authority": (
                "cites plan.md or AGENTS.md on a gate, membership, "
                "or who may write"
            ),
            "unattended authority": "changes what an agent may do unattended",
            "gate authority": "changes a gate's question, answer, or owner",
            "field authority": (
                "changes who may set a field that other rules act on"
            ),
        }
        for signal in authority_signals:
            print("  {}: {}".format(
                signal, descriptions.get(signal, signal)
            ))
    if status != "Ready":
        print("\nIt now waits on you: is the plan good? "
              "Answer by moving it to Ready.")
    return 0


def apply_main(argv: Optional[Sequence[str]] = None) -> int:
    """Validate a shape answer from a file or stdin and record it."""
    parser = argparse.ArgumentParser(
        description="validate one shape answer and record the plan")
    parser.add_argument("idea", type=int, help="idea issue number")
    parser.add_argument("--repo", default=None,
                        help="owner/name; required when ambiguous")
    parser.add_argument("--answer", required=True,
                        help="answer JSON file, or - for stdin")
    parser.add_argument("--run", default=None,
                        help="run id recorded in provenance blocks")
    parser.add_argument("--agent", default=None,
                        help="agent name recorded in provenance blocks")
    parser.add_argument("--attempt", type=int, default=None,
                        help="attempt number in the runner protocol: a "
                             "malformed answer exits 3 below attempt 2 "
                             "(the runner retries once) and 1 at 2 or "
                             "later. Without --attempt, exit 1.")
    parser.add_argument("--validate-only", action="store_true",
                        help="validate and decide without writing anything; "
                             "print the status, reason, and answer as JSON")
    args = parser.parse_args(argv)
    if args.attempt is not None and args.attempt < 1:
        parser.error("--attempt must be at least 1")
    try:
        if args.answer == "-":
            raw = sys.stdin.read()
        else:
            try:
                raw = pathlib.Path(args.answer).read_text()
            except OSError as exc:
                raise ShapeError(
                    "cannot read {}: {}".format(args.answer, exc))
    except (ShapeError, funnel.GitHubError) as exc:
        # An unreadable answer is a local failure, not a malformed one:
        # retrying the same read could not help.
        print("shape-apply: {}".format(exc), file=sys.stderr)
        return 1
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        print("shape-apply: the answer is not valid JSON: {}".format(exc),
              file=sys.stderr)
        return validation_exit(args.attempt)
    try:
        answer = validate_answer(data)
    except ShapeError as exc:
        print("shape-apply: {}".format(exc), file=sys.stderr)
        return validation_exit(args.attempt)
    try:
        resolved = funnel.resolve_repo(args.repo)
        items = funnel.load_items()
        ref = "{}#{}".format(resolved, args.idea)
        if args.validate_only:
            item = funnel.find(items, ref)
            status, reason = preview_decision(items, item, answer)
            print(json.dumps({"status": status, "reason": reason,
                              "answer": answer},
                             indent=2, sort_keys=True))
            return 0
        return apply_shape(
            items, datetime.now(timezone.utc), ref, data,
            run=args.run, agent=args.agent)
    except (ShapeError, funnel.GitHubError) as exc:
        print("shape-apply: {}".format(exc), file=sys.stderr)
        return 1
