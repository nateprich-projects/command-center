#!/usr/bin/env python3
"""Migrate routing policy from issue prose to canonical Project fields.

The first phase creates/extends the schema, backfills every open Project row,
and verifies the values while leaving legacy prose intact. The second phase
removes that prose after field-reading code is live. Both phases derive state
from GitHub and are safe to re-run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import funnel  # noqa: E402


class MigrationError(Exception):
    """The board cannot be migrated without guessing."""


FIELD_OPTIONS = {
    "Origin": funnel.ORIGIN_OPTIONS,
    "Risk": funnel.RISK_OPTIONS,
    "Needs": funnel.NEEDS_OPTIONS,
}

OPTION_STYLE = {
    "none": ("No outside dependency", "GRAY"),
    "agent": ("Waiting on an agent action or decision", "BLUE"),
    "human": ("Waiting on Nate", "RED"),
    "claude-code-environment": (
        "Requires the Claude Code environment", "PURPLE"),
    "external-event": ("Waiting on a named external condition", "YELLOW"),
}

RISK_LINE = re.compile(
    r"(?m)^Risk:[ \t]*(?P<risk>standard|escalated)"
    r"(?:[ \t]+[—-][ \t]*(?P<why>.*))?[ \t]*$"
)
NEEDS_SECTION = re.compile(
    r"(?ms)^## Needs (?:Nate|you)[ \t]*\n+(?P<body>.*?)(?=^## |\Z)"
)


def _run(args: Sequence[str]):
    proc = funnel._run_gh(
        list(args), capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise MigrationError(
            (proc.stderr or proc.stdout or "command failed").strip())
    return proc


def _fields() -> Dict[str, List[dict]]:
    data = funnel.gh_graphql(
        funnel.PROJECT_FIELDS_QUERY,
        login=funnel.PROJECT_OWNER,
        number=funnel.PROJECT_NUMBER,
    )
    rows = funnel._project_fields(data)
    if rows is None:
        raise MigrationError("the Command Center Project is not visible")
    result: Dict[str, List[dict]] = {}
    for row in rows:
        name = row.get("name")
        if isinstance(name, str):
            result.setdefault(name, []).append(row)
    return result


def _one_field(fields: Dict[str, List[dict]], name: str) -> Optional[dict]:
    rows = fields.get(name, [])
    if len(rows) > 1:
        raise MigrationError("Project has duplicate {} fields".format(name))
    return rows[0] if rows else None


def _create_field(name: str, options: Iterable[str]) -> None:
    _run([
        "gh", "project", "field-create", str(funnel.PROJECT_NUMBER),
        "--owner", funnel.PROJECT_OWNER,
        "--name", name,
        "--data-type", "SINGLE_SELECT",
        "--single-select-options", ",".join(options),
        "--format", "json",
    ])
    funnel.clear_project_field_cache()


def _graphql_string(value: object) -> str:
    return json.dumps(str(value), ensure_ascii=True)


def _update_options(field: dict, required: Sequence[str]) -> None:
    """Replace an option set while preserving every existing option ID."""
    existing = field.get("options")
    if not isinstance(existing, list) or not isinstance(field.get("id"), str):
        raise MigrationError("{} is not a readable single-select".format(
            field.get("name")))
    names = [row.get("name") for row in existing if isinstance(row, dict)]
    if len(names) != len(existing) or len(set(names)) != len(names):
        raise MigrationError("{} has ambiguous options".format(field["name"]))

    rows = []
    for option in existing:
        option_id = option.get("id")
        name = option.get("name")
        if not isinstance(option_id, str) or not option_id:
            raise MigrationError(
                "{} option {} has no ID; refusing to replace the set".format(
                    field["name"], name))
        rows.append({
            "id": option_id,
            "name": name,
            "description": option.get("description") or "",
            "color": option.get("color") or "GRAY",
        })
    for name in required:
        if name in names:
            continue
        description, color = OPTION_STYLE.get(name, ("", "GRAY"))
        rows.append({
            "id": None, "name": name,
            "description": description, "color": color,
        })

    rendered = []
    for row in rows:
        fields = []
        if row["id"] is not None:
            fields.append("id:{}".format(_graphql_string(row["id"])))
        fields.extend([
            "name:{}".format(_graphql_string(row["name"])),
            "description:{}".format(_graphql_string(row["description"])),
            "color:{}".format(row["color"]),
        ])
        rendered.append("{" + ",".join(fields) + "}")
    mutation = """
    mutation {
      updateProjectV2Field(input: {
        fieldId: %s,
        singleSelectOptions: [%s]
      }) {
        projectV2Field {
          ... on ProjectV2SingleSelectField {
            id name options { id name color description }
          }
        }
      }
    }
    """ % (_graphql_string(field["id"]), ",".join(rendered))
    funnel.gh_graphql(mutation)
    funnel.clear_project_field_cache()


def ensure_schema(*, apply: bool) -> List[str]:
    """Return schema changes, applying them when requested."""
    changes: List[str] = []
    fields = _fields()
    for name in ("Origin", "Risk"):
        field = _one_field(fields, name)
        if field is None:
            changes.append("create {} ({})".format(
                name, ", ".join(FIELD_OPTIONS[name])))
            if apply:
                _create_field(name, FIELD_OPTIONS[name])
                fields = _fields()
                field = _one_field(fields, name)
        if field is not None:
            actual = funnel._field_options(field)
            missing = [value for value in FIELD_OPTIONS[name]
                       if value not in actual]
            if missing:
                raise MigrationError(
                    "{} exists but is missing {}; repair it explicitly".format(
                        name, ", ".join(missing)))

    needs = _one_field(fields, "Needs")
    if needs is None:
        raise MigrationError("Project is missing the existing Needs field")
    missing_needs = [value for value in funnel.NEEDS_OPTIONS
                     if value not in funnel._field_options(needs)]
    if missing_needs:
        changes.append("extend Needs with {}".format(
            ", ".join(missing_needs)))
        if apply:
            _update_options(needs, funnel.NEEDS_OPTIONS)

    if apply:
        verified = _fields()
        for name, expected in FIELD_OPTIONS.items():
            field = _one_field(verified, name)
            if field is None or not set(expected).issubset(
                    funnel._field_options(field)):
                raise MigrationError(
                    "schema verification failed for {}".format(name))
    return changes


def parse_overrides(values: Sequence[str], *, choices: Sequence[str],
                    flag: str) -> Dict[str, str]:
    overrides: Dict[str, str] = {}
    for value in values:
        ref, separator, selected = value.partition("=")
        if not separator or not ref.strip() or selected not in choices:
            raise MigrationError(
                "bad {} {!r}; want owner/repo#n=<{}>".format(
                    flag, value, "|".join(choices)))
        overrides[ref.strip()] = selected
    return overrides


def infer_values(item: funnel.Item,
                 needs_overrides: Dict[str, str],
                 origin_overrides: Optional[Dict[str, str]] = None
                 ) -> Dict[str, str]:
    """Infer one open row from the exact legacy records, or refuse."""
    origin_overrides = origin_overrides or {}
    if item.ref in origin_overrides:
        origin = origin_overrides[item.ref]
    elif item.parent:
        origin = "agent"
    elif item.origin in funnel.ORIGIN_OPTIONS:
        origin = item.origin
    else:
        legacy = funnel.parse_origin(item.body or "")
        if legacy is None:
            raise MigrationError("{} has no readable Origin".format(item.ref))
        origin = "agent" if legacy["voice"] == "agent" else "Nate"

    risk = item.risk if item.risk in funnel.RISK_OPTIONS else funnel.required_tier(
        item.title, item.body or "")

    if item.ref in needs_overrides:
        needs = needs_overrides[item.ref]
    elif item.needs in (
            "agent", "human", "claude-code-environment", "external-event"):
        needs = item.needs
    elif item.is_blocked and (
            item.block_references or item.blocked_until is not None):
        needs = "external-event"
    elif item.is_blocked and item.parent:
        raise MigrationError(
            "{} is a blocked ticket with no canonical owner; pass "
            "--needs-override".format(item.ref))
    elif item.status == "Shaped" or (
            item.is_blocked and item.needs_decision):
        needs = "human"
    elif item.needs == "none":
        needs = "none"
    else:
        needs = "none"
    return {"Origin": origin, "Risk": risk, "Needs": needs}


def plan_backfill(items: Sequence[funnel.Item],
                  needs_overrides: Dict[str, str],
                  origin_overrides: Optional[Dict[str, str]] = None
                  ) -> List[Tuple[funnel.Item, dict]]:
    rows = []
    failures = []
    for item in items:
        if item.state == "OPEN" and item.item_id:
            try:
                values = infer_values(
                    item, needs_overrides, origin_overrides)
            except MigrationError as exc:
                failures.append(str(exc))
                continue
            rows.append((item, values))
    if failures:
        raise MigrationError(
            "unmappable rows: {}".format("; ".join(failures)))
    return rows


def plan_backfill_writes(
        rows: Sequence[Tuple[funnel.Item, dict]]
        ) -> List[Tuple[funnel.Item, str, str]]:
    """Return exactly the field mutations an apply run will issue."""
    writes = []
    for item, values in rows:
        for field, value in values.items():
            if getattr(item, field.lower()) == value:
                continue
            writes.append((item, field, value))
    return writes


def apply_backfill(
        writes: Sequence[Tuple[funnel.Item, str, str]]) -> None:
    for item, field, value in writes:
        funnel.write_project_select(item.item_id, field, value, item.ref)


def verify_backfill(expected: Sequence[Tuple[funnel.Item, dict]]) -> None:
    fresh = {item.ref: item for item in funnel.load_items(include_details=False)}
    failures = []
    for old, values in expected:
        item = fresh.get(old.ref)
        if item is None:
            failures.append("{} left the Project".format(old.ref))
            continue
        for field, value in values.items():
            if getattr(item, field.lower()) != value:
                failures.append("{} {} is {!r}, want {!r}".format(
                    old.ref, field, getattr(item, field.lower()), value))
    if failures:
        raise MigrationError("backfill verification failed: " + "; ".join(failures))


def trim_routing_prose(body: str) -> str:
    """Remove exact legacy routing copies while retaining explanations."""
    result = body or ""
    for _, block in funnel._marked_json_blocks(result, funnel.ORIGIN_MARKER):
        result = result.replace(block, "")

    def replace_risk(match: re.Match) -> str:
        why = (match.group("why") or "").strip()
        if match.group("risk") == "escalated" and why:
            return "## Risk rationale\n\n{}".format(why)
        return ""

    result = RISK_LINE.sub(replace_risk, result)

    def replace_needs(match: re.Match) -> str:
        kept = [line for line in match.group("body").splitlines()
                if "nothing outstanding" not in line.lower()]
        while kept and not kept[0].strip():
            kept.pop(0)
        while kept and not kept[-1].strip():
            kept.pop()
        if not kept:
            return ""
        return "## Needs Nate\n\n{}\n\n".format("\n".join(kept))

    result = NEEDS_SECTION.sub(replace_needs, result)
    result = re.sub(r"\n{3,}", "\n\n", result).strip()
    return result + "\n" if result else ""


def plan_prose_writes(
        items: Sequence[funnel.Item]
        ) -> List[Tuple[funnel.Item, str]]:
    """Return exactly the issue-body mutations an apply run will issue."""
    writes = []
    for item in items:
        if item.state != "OPEN":
            continue
        before = item.body or ""
        after = trim_routing_prose(before)
        if after.rstrip() == before.rstrip():
            continue
        writes.append((item, after))
    return writes


def apply_prose_writes(
        writes: Sequence[Tuple[funnel.Item, str]]) -> None:
    for item, body in writes:
        _run([
            "gh", "issue", "edit", str(item.number),
            "--repo", item.repo, "--body", body,
        ])


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true",
                        help="create/extend fields and backfill open rows")
    parser.add_argument(
        "--trim-prose", action="store_true",
        help=("include legacy routing-prose edits in the dry-run inventory; "
              "with --apply, perform them"))
    parser.add_argument("--needs-override", action="append", default=[],
                        metavar="REF=VALUE")
    parser.add_argument("--origin-override", action="append", default=[],
                        metavar="REF=VALUE")
    args = parser.parse_args(argv)
    try:
        needs_overrides = parse_overrides(
            args.needs_override, choices=funnel.NEEDS_OPTIONS,
            flag="--needs-override")
        origin_overrides = parse_overrides(
            args.origin_override, choices=funnel.ORIGIN_OPTIONS,
            flag="--origin-override")
        items = funnel.load_items()
        rows = plan_backfill(items, needs_overrides, origin_overrides)
        backfill_writes = plan_backfill_writes(rows)
        prose_writes = plan_prose_writes(items) if args.trim_prose else []
        changes = ensure_schema(apply=args.apply)
        print(json.dumps({
            "schema_changes": changes,
            "open_rows": len(rows),
            "backfill_writes": [
                {
                    "ref": item.ref,
                    "field": field,
                    "from": getattr(item, field.lower()),
                    "to": value,
                }
                for item, field, value in backfill_writes
            ],
            "prose_writes": [item.ref for item, _ in prose_writes],
        }, indent=2, sort_keys=True))
        if args.apply:
            apply_backfill(backfill_writes)
            verify_backfill(rows)
            if args.trim_prose:
                apply_prose_writes(prose_writes)
                print("trimmed routing prose from {} open issues".format(
                    len(prose_writes)))
    except (funnel.GitHubError, MigrationError) as exc:
        print("migrate-canonical-fields: {}".format(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
