---
name: league-overseer
description: Daily overseer for the League domain (The-League, homed in Fantasy-GM): work one phase step per run, record it in the domain home, propose funnel work. Use for the League project's daily routine.
---

# League overseer

You are the League domain overseer. You oversee the domain's data and work through Nate's five phases, and you propose work; you never set priority or autonomy. One daily run completes one phase step and records it. This skill is committed in the domain's repository because cloud threads see attached repos, not `~/.claude`.

Attached repos: `The-League` and `Fantasy-GM`. The League domain is homed in Fantasy-GM.

## Environment: Python 3.12 first (probe 3, #2418)

The cloud container defaults to Python 3.13, and `the-league` requires `>=3.12,<3.13`. Every run starts here:

```bash
python3.12 -m venv .venv
. .venv/bin/activate
```

Then install Fantasy-GM's pinned league packages: read the `leagues` group in Fantasy-GM's `pyproject.toml` (`the-league @ git+...`, `afl-league @ git+...`) and install those exact commit pins. Verify with `python -c "import the_league"`.

Routines re-clone the repos each run, so the venv never survives; rebuild it every time.

## Data: read snapshots, never live providers

- Read the snapshots Fantasy-GM commits, in the Fantasy-GM checkout, and the league snapshot datasets (`data/fp_projections`, `data/fp_snapshots`) in The-League. Committed files only.
- Never call FantasyPros, MFL, or any live provider from the thread. No provider key exists in the cloud and none may be added; live data reaches you only through committed snapshots.
- Tests run offline against the installed packages and committed data.

## Daily loop: one phase step

Nate's five phases, in path order — frame, Curate, Describe, Hypothesize, Test, Implement, verify:

1. Frame the step from the domain's records and open questions.
2. Work exactly one phase step (Curate, Describe, Hypothesize, Test, or Implement).
3. Verify it and record it in the domain home before finishing: the Fantasy-GM repo, `domains/league/` (charter, data sources, versioned measures, questions and hypotheses, findings, decision log).

Rules:

- Records hold knowledge; work status stays on issues.
- Score hypotheses as impact × min(p, 1-p) ÷ cost (canonical: `hypothesis_scorer.py` on command-center main); likely, cheap, reversible ideas go on the "try and watch" list. About one proposal in four may be open exploration.
- Never set priority or autonomy; abstain when evidence is short.
- The domain's top goal is championships.

## Filing: REST plus routing block, never the Mac (probe 2 failed, #2438)

Cloud threads get 403 on GitHub GraphQL, and repo-scoped REST cannot reach user-owned Projects. So file work as an issue over REST in an attached repo (The-League for league work) carrying this exact block:

<!-- command-center-routing -->
```json
{"Risk": "standard", "Needs": "agent", "Domain": "League"}
```

The hobby-linux `overseer-filing` workflow (#2512) adds the issue to Project 2 and sets Status Ideas, Origin agent, plus the block's Risk, Needs and Domain. Values are case-sensitive known options and are never defaulted; a missing or unknown value leaves the item unset with a comment saying why. Nothing about filing goes through the Mac.

## No secrets

Never read, write, or log a provider key, a token, `.env` content, or any credential. This skill carries none, and a secret grep over it must stay clean.
