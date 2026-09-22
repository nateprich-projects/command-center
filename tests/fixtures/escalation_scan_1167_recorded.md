## What it is

`escalation_reasons` scans the whole of an item's title and body for risk
vocabulary. That body includes the evidence a capture pastes in — log
excerpts, error strings, job names — and the alternatives a plan explicitly
rejects. So a report of a problem is scored on the words the problem used.

**The family, from the last four days**, each verified false before it was
overridden by hand. The pasted evidence:

```
Resource deadlock avoided
op=snapshot status=error while migrating the cache
runner=authorise-deploy-1 permission model unavailable
step=hard delete of the stuck branch skipped
```

Every line above is something that happened. None of it is a statement about
what this work will do: the change reads one region of a body instead of the
whole of it.

> An earlier draft proposed a keyword list instead, which would have been a
> hard delete of the gate's coverage.

That alternative is recorded and not taken.
