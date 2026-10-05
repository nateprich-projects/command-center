"""Fail-closed assessment for the dormant #2297 Mac path."""

import mac_capability


REF = "nateprich-projects/command-center#2297"
RECEIPT = mac_capability.MacCapabilityReceipt(
    ticket_ref=REF,
    capability="read-owner-local-log",
    evidence_url=(
        "https://github.com/nateprich-projects/command-center/"
        "issues/2297#issuecomment-12345"
    ),
)


def assess(monkeypatch, **changes):
    monkeypatch.setattr(
        mac_capability, "_mac_runtime_check", lambda: {"ok": True}
    )
    values = dict(
        ticket_ref=REF,
        needs="claude-code-environment",
        agent="codex",
        capability="read-owner-local-log",
        receipt=RECEIPT,
    )
    values.update(changes)
    return mac_capability.assess_native_mac_step(**values)


def test_exact_mac_receipt_is_only_eligible_for_review(monkeypatch):
    assert assess(monkeypatch) == "eligible-for-review"


def test_human_and_other_needs_never_use_the_mac_path(monkeypatch):
    assert assess(monkeypatch, needs="human") == "human"
    assert assess(monkeypatch, needs="none") == "wrong-route"
    assert assess(monkeypatch, agent="claude") == "wrong-route"


def test_receipt_is_bound_to_one_ticket_and_capability(monkeypatch):
    assert assess(monkeypatch, receipt=None) == "unproved-capability"
    assert assess(monkeypatch, ticket_ref="nateprich-projects/command-center#2298") == (
        "unproved-capability"
    )
    assert assess(monkeypatch, capability="sudo-as-service-user") == "unproved-capability"
    assert assess(monkeypatch, receipt=mac_capability.MacCapabilityReceipt(
        REF, "read-owner-local-log", "https://example.com/claim"
    )) == "unproved-capability"


def test_failed_real_mac_check_cannot_be_replaced_by_a_cloud_claim(monkeypatch):
    assess(monkeypatch)
    monkeypatch.setattr(
        mac_capability, "_mac_runtime_check",
        lambda: {"ok": False, "profile": "cloud"},
    )
    assert mac_capability.assess_native_mac_step(
        ticket_ref=REF, needs="claude-code-environment", agent="codex",
        capability="read-owner-local-log", receipt=RECEIPT,
    ) == (
        "unverified-mac-runtime"
    )


def test_unproved_step_never_reads_runtime_or_github(monkeypatch):
    def unexpected_check():
        raise AssertionError("no runtime read without a specific receipt")

    assess(monkeypatch)
    monkeypatch.setattr(mac_capability, "_mac_runtime_check", unexpected_check)
    assert mac_capability.assess_native_mac_step(
        ticket_ref=REF, needs="claude-code-environment", agent="codex",
        capability="read-owner-local-log", receipt=None,
    ) == "unproved-capability"
