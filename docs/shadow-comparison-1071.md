# Shadow comparison exclusions for #1071

This note records the GitHub-only reproduction for the shadow window in
command-center #1044. It does not change #806 or #813.

Window: `2026-09-17T08:55:06Z` through `2026-09-17T20:50:00Z`.

| population | count |
| --- | ---: |
| shadow jobs | 41 |
| live jobs | 46 |
| matched pairs | 35 |
| compared pairs | 5 |
| matched but not compared | 30 |

The 30 exclusions are mutually exclusive:

| exclusion reason | pairs |
| --- | ---: |
| live side has no parseable recorded verdict; shadow side does | 28 |
| both sides have no parseable recorded verdict | 1 |
| shadow side has no parseable recorded verdict; live side does | 1 |
| **total** | **30** |

The dominant cause is the live review finish path not recording a parseable
structured verdict. This is not a no-op-tick population: every one of the 35
matched live finishes and 41 shadow finishes has `outcome=done`, so no matched
pair has `outcome=nothing-to-do`.

The #972/#980 family is relevant precedent for durable live outcomes, but it is
the separate breakdown/shape population, not the source of these review-mode
exclusions.

## Reproduction

From the command-center checkout:

```sh
gh api 'repos/nateprich-projects/command-center/contents/muse.jsonl?ref=heartbeat' \
  -H 'Accept: application/vnd.github.raw' > /tmp/muse-heartbeat-1071.jsonl
python3 - <<'PY'
import json
from collections import Counter
from engine import shadow_report as s

records = [json.loads(line) for line in open('/tmp/muse-heartbeat-1071.jsonl')
           if line.strip()]
start = s._timestamp('2026-09-17T08:55:06Z')
end = s._timestamp('2026-09-17T20:50:00Z')
shadow_rows, live_rows = s.partition_records(records)
shadow = s.jobs_from_records(shadow_rows, since=start, until=end)
live = s.jobs_from_records(live_rows, since=start, until=end)
pairs = s._pair_jobs(shadow, live)
report = s.build_report(
    records, since=start, until=end, now=end, live_verdicts={}
)

def missing(job):
    return job.get('decision') not in {'approved', 'rejected'}

classes = Counter()
for shadow_job, live_job in pairs:
    shadow_missing = missing(shadow_job)
    live_missing = missing(live_job)
    if shadow_missing and live_missing:
        classes['both sides missing structured verdict'] += 1
    elif live_missing:
        classes['live only missing structured verdict'] += 1
    elif shadow_missing:
        classes['shadow only missing structured verdict'] += 1

print(report['jobs'])
print(classes)
print(Counter(job['finish'].get('outcome') for _, job in pairs))
PY
```

Expected output:

```text
{'shadow': 41, 'live': 46, 'matched': 35, 'compared': 5}
Counter({
    'live only missing structured verdict': 28,
    'both sides missing structured verdict': 1,
    'shadow only missing structured verdict': 1,
})
Counter({'done': 35})
```
