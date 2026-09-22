## What it is

FF's 08:00 PDT scheduled advisory has now failed on three consecutive
scheduled runs and nothing announced any of them. One line, copied from the
operator log, is the whole of the evidence:

```
2026-09-20T15:02:11Z provider=sleeper op=migrate status=error
```

This work reads a snapshot and announces a failure. It has no other property.
