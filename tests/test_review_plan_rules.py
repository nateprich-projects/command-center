"""plan.md and the plan bind a diff only as rules it must not break (#1970).

On 2026-09-29 the lister turned plan.md's statements about the touched area
into work the diff must do: PR #1941 was blocked on plan.md:682-689 (the
shaping gate) and PR #1959 on plan.md:81 (watch_gates), behaviour those
tickets were told not to change or another ticket delivers.
"""

from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENGINE = (ROOT / "scripts" / "muse-review-engine").read_text()
ROUTINE = (ROOT / "routines" / "muse-review.md").read_text()


def _heredoc(name):
    return ENGINE.split("{}() {{".format(name), 1)[1].split("\nHEADER", 1)[0]


def _flat(text):
    return " ".join(text.split())


def test_the_lister_takes_must_do_requirements_from_the_tickets():
    lister = _flat(_heredoc("lister_header"))

    assert ("What it must do comes from the tickets it closes: their Do and "
            "Accept, as amended by Nate-voiced comments.") in lister
    assert "the requirements the ticket, its parent plan, and the " \
        "repository's stated rules place on it" not in lister


def test_the_lister_lists_plan_rules_only_as_must_not_break_rows():
    lister = _flat(_heredoc("lister_header"))

    assert ("`plan_md`, the parent plan and the repository's stated rules add "
            "only what the diff must not break or contradict") in lister
    assert '"Does not break: <the rule>"' in lister
    assert ("Never list a rule as something the diff must implement unless a "
            "ticket asks for it.") in lister


def test_the_judge_meets_a_must_not_break_row_unless_a_line_breaks_it():
    judge = _flat(_heredoc("judge_header"))

    assert ("A `Does not break:` requirement is met unless a diff line breaks "
            "or contradicts the rule.") in judge
    assert ("That the diff does not implement the rule is never a reason for "
            "unmet or unsure.") in judge


def test_the_routine_asks_for_ticket_work_and_unbroken_plan_rules():
    prompt = _flat(ROUTINE.split("\n---\n", 1)[1])

    assert ("Does this diff do what its tickets ask, avoid what the plan "
            "rejected, and break nothing the plan, `plan_md` or the "
            "repository's rules require?") in prompt
    assert ("`plan_md` and the plan bind only as rules the diff must not "
            "break") in prompt
    assert "A `Does not break:` row is met unless a diff line breaks it." \
        in prompt
    assert "Walk each ticket and plan requirement" not in prompt


def test_the_lister_names_the_plans_rejected_options():
    # #1980: a rejected option is the plan rule a diff most plausibly breaks.
    lister = _flat(_heredoc("lister_header"))

    assert ("must not break or contradict, including anything the plan "
            "rejected") in lister


def test_engine_comments_no_longer_describe_plan_sourced_must_do_work():
    assert "what the ticket, its parent plan and the" not in ENGINE
    assert "drawn from the ticket, its\n# parent plan" not in ENGINE
