# Brokered Codex caller attestation — #2341 design receipt

**Scope:** Design and read-only source review only. This receipt does not authorize a listener, ACL, permission, credential, protected rollout read, or app/schedule change. It does not change #1998's live acceptance bar.

## Evidence available under current access

The current [`credential_broker.py`](../credential_broker.py) authenticates its Unix-socket peer with `getpeereid` and checks for UID 506. Apple's [`getpeereid(3)` manual](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man3/getpeereid.3.html) says this returns the peer's effective UID and GID. That identifies the OS user, not a Codex Scheduled run.

The installed MacOSX 26.5 SDK declares `SOL_LOCAL` and `LOCAL_PEERTOKEN` in `sys/un.h`; Apple's [XNU socket header](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/un.h) describes this option as returning the peer audit token. Decode that token with the SDK's `bsm/libbsm.h` helpers, including `audit_token_to_euid`, `audit_token_to_pid`, and `audit_token_to_pidversion`; do not parse its storage directly. Apple's [Endpoint Security session](https://developer.apple.com/videos/play/wwdc2020/10159/) also demonstrates extracting process identity from an audit token. These sources identify the peer process instance at connection time, but do not carry a Codex thread ID or workspace.

Apple's [`sys/proc_info.h`](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/sys/proc_info.h) defines `PROC_PIDTBSDINFO` and fields for PID, parent PID, UID, and process start time. However, the installed [`libproc.h`](https://github.com/apple-oss-distributions/xnu/blob/main/libsyscall/wrappers/libproc/libproc.h) states that its process-information interfaces are private and subject to change. Earlier in-thread diagnostic notes recorded `EPERM` for cross-process `proc_pidinfo` and for a temporary Unix listener bind; this run did not repeat those probes. Do not make private or denied process reads a required production identity source without separate security review.

[`codex_run.py`](../codex_run.py) finds a rollout from `CODEX_THREAD_ID` in the caller's environment and checks that rollout's effective settings and workspace. That is useful for a process checking its own run. The broker cannot trust the caller's environment, `cwd`, `argv`, executable path, matching timestamp or filename, self-reported `codex_run.check()` result, or a synthetic `do:stop` packet: an unrelated process under UID 506 can supply those values. A rollout read alone does not authenticate the process that claims it.

**Finding:** The reviewed, currently authorized OS evidence can identify a socket peer as a UID and process instance. It does not independently bind that process to exactly one live Codex Scheduled thread and workspace. No such trusted process-to-run source was established in this design review. A broader session-file ACL would not close that authenticity gap.

## Required binding and fail-closed behavior

A future implementation may dispatch privileged `begin` only if it obtains an independently verifiable, OS-authenticated association from the accepted peer's audit-token process instance to exactly one current UID-506 in-app Scheduled rollout, then verifies the rollout's thread, workspace, and effective settings. At minimum:

1. Require `getpeereid` UID 506 and audit-token euid 506, a positive PID, and a nonzero PID version.
2. Preserve the token's process-instance identity. Refuse if the peer exited, the PID/version is stale or mismatched, or required process evidence is unavailable.
3. Require exactly one independently matched current rollout and verify its thread, workspace, and effective settings against the manifest.
4. Only then continue to the existing live-run/claim checks. A client self-check is supporting diagnostic data, never broker authorization.

Refuse before `begin`, with no claim and no retry, for a missing or malformed token, UID mismatch, exited/recycled process, denied or incomplete ancestry evidence, missing/unreadable/malformed/stale rollout, zero or multiple rollout matches, workspace/settings mismatch, or a same-UID process without the trusted process-to-thread link. If the supported OS sources cannot provide that link, the broker design is not implementable as specified; do not replace it with caller assertions.

## Minimal test plan

- **Positive fixture:** inject an OS-sourced peer token and a separately trusted process-to-thread/workspace association to one live UID-506 rollout with manifest-correct effective settings; verify exactly one `begin` dispatch.
- **Negative fixtures:** wrong UID; unrelated UID-506 Python caller forging thread/cwd/environment; malformed token; stale PID or changed PID version; peer exit; denied parent/process read; missing, malformed, stale, or duplicate rollouts; wrong workspace/settings; `codex_run.check() == ok` without independent binding; and mocked `do:stop`. Each must refuse before `begin` and create no claim.
- **Live test:** use a genuine UID-506 in-app Scheduled run. `sudo -u`, a headless Codex CLI, an interactive run with different settings, and mocked responses are not substitutes.

## Access approval and staging

Current access does not authorize the broker to read `/Users/codex/.codex/sessions` or inspect another UID's process ancestry. Before any diagnostic, Nate/security owner must separately review and approve its exact one-off scope: a read-only peer/ancestor inspection method (including explicit acceptance of any private `libproc` use), the exact rollout file rather than a broad home/session grant, and temporary socket traversal/connect ACLs with cleanup and retained-field limits. These approvals are separate from #1999's app-UI sign-in. No such approval is requested or applied here.

Safe order: keep the current automations and direct path in place; implement and unit-test the binding/refusal contract under #1998 only after the proof source and access are approved; stage the versioned listener and minimal socket/checkout ACL while the current automation identity remains in use; then let Nate perform #1999's atomic automations-only sign-in; run the genuine in-app UID-506 brokered claim and finish path; record actual claim, push, PR, and release evidence; and retain #1998's CI and independent escalated review. If any proof fails, keep or restore the direct path and leave #1998 blocked and PR #2323 draft. This design does not authorize a cutover or live trial.
