## What it is

The 08:00 PDT advisory failed on three consecutive scheduled runs and nothing
announced any of them. Reported here with the evidence pasted below.

```
2026-09-20T15:02:11Z provider=sleeper op=migrate_snapshot status=error
2026-09-20T15:02:11Z Resource deadlock avoided
```

The line above is the whole of the evidence. This work reads a snapshot and
announces a failure.

The scheduled job is called `ff-weekly-start-sit-migrate-cache`, which is a
name rather than a description of anything here.

> An earlier draft would rewrite history to clear the stuck run.

That alternative is recorded above and is not the shape taken.
