# #2016 send-back premises

Recorded during Codex run `2c8e638a7b6a`, against `origin/main` at
`e4be5f8e12bf66f8c9865b9232c375c7fccb07c8`, before proceeding with the
send-back implementation.

## shape-apply intake

Command: `git show origin/main:engine/shape.py | nl -ba | sed -n '1341,1351p'`

```python
1341    # The packet and decision may be minutes old. Re-read immediately before
1342    # the first issue mutation; a moved stage or newly added child makes this
1343    # answer stale. Body-only edits deliberately do not block the apply.
1344    fresh_status, fresh_children = _read_fresh_shape_facts(item)
1345    if fresh_status != "Ideas" or fresh_children > 0:
1346        status_label = fresh_status if fresh_status is not None else "missing"
1347        print("{} ref={} fresh Status={} children={}".format(
1348            SKIPPED_STALE_SHAPE_OUTCOME, item.ref, status_label,
1349            fresh_children,
1350        ))
1351        return 0
```

The fresh Status guard skips shape-apply for every Status other than `Ideas`
(and also skips when the item has children).

## Baseline funnel verbs

Command, run from `main` at the recorded `origin/main` commit:
`python3 funnel.py --help`

The complete verb list was:

`queue, next, brief, snapshot, ideas, doctor, main-ci, show, approve, accept, capture, promote, claim, release, pin, unpin, park, answer-gates, comment, hold, reject, review, merge, begin, session-server, next-review`

There was no `send-back` verb. Both ticket premises held, so the ticket was
eligible to proceed.
