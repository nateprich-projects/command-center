## What it is

FF's 08:00 PDT scheduled advisory has now failed on three consecutive scheduled runs and nothing announced any of them. The announcement path is not missing — it exists, it is wired, and it is reachable. It fired once, the delivery failed, and then it stopped firing.

**What the evidence says, dated.** `/var/tmp/ff-weekly-start-sit/daily.err.log` carries no timestamps, so each line was dated by matching its `report=` id to the mtime of the matching `data/ledger/failure-<id>.json`:

| run | mode | did it announce? |
| --- | --- | --- |
| 2026-09-18 08:03:04 PDT | daily | **attempted** — two `IMessageNotifier notification delivery failed (NotificationError)` lines |
| 2026-09-19 08:03:06 PDT | historical top-up | suppressed deliberately (`_notify_failure=False`, `operations.py:1368`) |
| 2026-09-19 08:06:06 PDT | daily | **no delivery line at all** |
| 2026-09-20 08:03:12 PDT | daily | **no delivery line at all** |

The send site is `operations.py:4826-4831`, inside the same block that writes the failure report:

```
if dispatcher is not None and dispatcher.notifiers and _notify_failure:
    _code, _message, reason = _public_failure_details(failure)
    dispatcher.send(failure_notification(reason))
```

The `advisory operation failed; report=…` line immediately above it is present for all four runs, so all four reached that block. `local-config.json` configures exactly one channel (`notifications.imessage_recipient`), so `dispatcher.notifiers` is non-empty. The two later `daily` runs therefore failed one of the other two conditions — and nothing the run writes can say which.

This corrects the reading in the capture above, which counted the two delivery-failure lines as spanning 09-16 → 09-20. The file appends and is never rotated; both lines belong to 09-18.

## The change

1. **Make the guard's own outcome observable.** At the send site, record which branch was taken: announced, refused because the mode suppresses it, refused because no channel is configured, or refused because no dispatcher was built. One line in the operator log and one field in the failure report. Without it, the next silent failure is diagnosed the same way this one was — by hand, days late.

2. **Then find and fix the two silent `daily` runs.** With (1) in place the cause is one scheduled run away, but it should be found now rather than waited for: the only two candidates are a `None` dispatcher or `_notify_failure` arriving false on a plain `daily`, and both are reachable by reading the call chain from the scheduled entry point down to `operate`.

3. **A delivery that fails must leave a mark the next reader finds.** `NotificationDispatcher.send` swallows every channel exception on purpose (`notifications.py:350`, "channel failures must not abort the run") and logs only `type(error).__name__`. Keeping the run alive is right; losing the fact is not. Record delivery outcome per channel in the run's own ledger entry, so "the advisory failed and could not say so" is a state something can read rather than a line in a log nobody opens.

4. **Timestamp the operator log.** `daily.err.log` is the only trace a failed scheduled run leaves, and dating a line in it currently requires cross-referencing ledger file mtimes. Prefix each line the wrapper writes with an ISO instant.

5. **Re-test on a forced failure**, not on a real one: run the daily against a source that refuses, and confirm one announcement is attempted, its outcome recorded either way, and the log line datable on its face.

## Watch for

- **The announcement must not need the thing the run just lost.** Every one of these failures is a provider refusal; a failure notice that itself calls the provider announces nothing.
- **Leave the historical top-up suppression alone.** `_notify_failure=False` at `operations.py:1368` is deliberate and the comment says why — that mode retries on a later tick and would repeat its text until the provider recovers. It is the one silent path here that is correct.
- **Whether iMessage is usable at all from a scheduled job is FF#223's question, not this one.** This ticket makes a failed run and a failed delivery both legible; it does not pick a replacement channel. If FF#223 concludes the channel cannot work unattended, the replacement is a new idea with its own preference question, not a decision buried in this fix.
- **`operations.py` is shared ground.** FF#90, #91 and #92 are all open against the same module; keep the change inside the failure block and the ledger writer rather than reshaping the operation loop.

## Overlap check

Checked: FF#86, FF#90, FF#91, FF#92, FF#93, FF#215, The-League#163, The-League#165, The-League#174, The-League#244, career-toolset#186, command-center#25, #162, #794, #1076, #1077, #1125, #1126, #1135, #1144, #1148 and #1149 (the other open plans)

Candidates:
- nateprich-projects/FF-Weekly-Start-Sit#225 and nateprich-projects/FF-Weekly-Start-Sit#86 both reference #91
- nateprich-projects/FF-Weekly-Start-Sit#225 and nateprich-projects/FF-Weekly-Start-Sit#86 both reference #92
- nateprich-projects/FF-Weekly-Start-Sit#225 and nateprich-projects/FF-Weekly-Start-Sit#90 both touch `local-config.json`
- nateprich-projects/FF-Weekly-Start-Sit#225 and nateprich-projects/FF-Weekly-Start-Sit#90 both touch `operations.py`
- nateprich-projects/FF-Weekly-Start-Sit#225 and nateprich-projects/FF-Weekly-Start-Sit#90 both reference #91
- nateprich-projects/FF-Weekly-Start-Sit#225 and nateprich-projects/FF-Weekly-Start-Sit#90 both reference #92
- nateprich-projects/FF-Weekly-Start-Sit#225 and nateprich-projects/FF-Weekly-Start-Sit#91 both touch `operations.py`
- nateprich-projects/FF-Weekly-Start-Sit#225 and nateprich-projects/FF-Weekly-Start-Sit#92 both touch `local-config.json`
- nateprich-projects/FF-Weekly-Start-Sit#225 and nateprich-projects/FF-Weekly-Start-Sit#92 both touch `operations.py`
- nateprich-projects/FF-Weekly-Start-Sit#225 and nateprich-projects/FF-Weekly-Start-Sit#92 both reference #91
- nateprich-projects/FF-Weekly-Start-Sit#225 and nateprich-projects/FF-Weekly-Start-Sit#93 both reference #91

Conclusion:
- The `operations.py` candidates (#90, #91, #92) share the file and none shares the mechanism: those plans work the advisory's content and its operation modes, while this one touches only the terminal-failure block and the ledger record it writes. Keep them separate and keep this change small enough not to collide — that constraint is repeated under **Watch for**.
- The `local-config.json` candidates (#90, #92) are a false positive for this plan. This plan reads the existing `notifications` section and changes nothing in it; the recipient value is neither moved nor printed. No coordination is owed.
- The `reference #91` / `reference #92` candidates match on cross-references inside the evidence rather than on shared ground. Nothing follows from them.
- The-League#247 is the same shape in the other repository (a scheduled job that degrades without saying so). Deliberately not merged with it: the repositories share no notification code, and #247 is about a run that *succeeds* degraded while this is about a run that fails outright.

## Proposed class: Broken

## Needs you

- Exposure: nothing outstanding. No secret is read or written. The one configured recipient value stays where it is and is never printed — the delivery record names the channel, not the destination.
- Gates: nothing outstanding. No gate ownership changes.
- Scope and priority: nothing outstanding. The scope is FF's own failure reporting; the provider strain that caused these particular failures belongs to FF#208 and The-League#249, and choosing a different notification channel belongs to a later idea if FF#223 calls for one.
- Preference: nothing outstanding.


<!-- command-center-provenance -->

```json
{
  "agent": "claude",
  "at": "2026-09-20T20:34:26.718129+00:00",
  "run": "bfd8abed09c7",
  "voice": "agent"
}
```

<!-- command-center-origin -->

```json
{
  "agent": null,
  "at": "2026-09-20T17:37:14.885387+00:00",
  "run": null,
  "voice": "agent"
}
```

