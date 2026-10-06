# Side Chats as Spaces: a Muse implementation guide

How to build ChatGPT-Spaces-style persistent context on Muse today, with no
new product features: one side chat per life domain, a file-backed "space"
behind each chat, and scheduled workers that do the thinking in the background
while the chat stays a clean decision surface.

**Scope.** This covers personal-life domains (finance, career, health) run as
Muse side chats. It is not a proposal to change the command-center funnel —
`plan.md` and its rejected alternatives stand. The philosophy here *is* the
command-center philosophy (deterministic filtering before judgment, the human
as the gate on irreversible actions, everything logged); only the machinery
differs, because these domains proved they work better in side chats than in
the repo. _(confirmed by Nate 2026-10-04)_

**Rule provenance.** Per this repo's rule-authority convention: rules tagged
`_(confirmed by Nate YYYY-MM-DD)_` were decided with Nate in conversation.
Rules tagged `_(agent rule, unconfirmed — advisory)_` are the author's
synthesis and remain advice until he confirms them.

## The architecture

Three pieces per domain:

1. **Side chat** — the domain surface. One chat per domain (💰 Finances,
   💼 Career, 😴 Health). The chat is a *decision inbox*, never a workbench.
   You never watch work happen here; you only ever see conclusions.
2. **Space** — the persistent state, in two layers:
   - *Canon store*: a folder on OneDrive (e.g. `Finance/Family/`) holding
     README, data files, and a change log. This is the source of truth, and
     it is deliberately agent-neutral — any AI (or human) can read it.
   - *Chat memory*: `~/side-chats/<chat-id>/MEMORY.md` — the distilled
     standing rules and current state that chat's agent starts from.
3. **Workers** — scheduled cron jobs, each owned by a goal, each delivering
   to the side chat. They run the sweeps, filters, and scans in the
   background. The chat only ever shows what needs your judgment.

## The rules that make it work

1. **Canon rule.** Facts live in the space's files, not in chat memory.
   Before asserting an amount, date, cancellation, or any other fact, check
   the files. Never assert from chat memory alone.
   _(confirmed by Nate 2026-10-04)_
2. **Recency rule.** When Muse and any side chat disagree on a fact, whoever
   has the most recent information wins. Date every claim; compare the dates;
   newer is canon. If recency is unclear, ask the human instead of guessing.
   _(confirmed by Nate 2026-10-04)_
3. **Freshness contract** (in each space's README): every entry carries a
   written-on and a last-verified date. Re-verify on a schedule (quarterly
   works). Staleness rule: never answer from an entry older than ~6 months
   without saying so and checking live data first.
   _(confirmed by Nate 2026-10-03)_
4. **Append, don't rewrite.** Corrections are new dated entries, not edits
   over old ones. Every change lands in the change log (`changes.csv`) in
   the same session it was decided. _(confirmed by Nate 2026-10-03)_
5. **Silence by default.** Workers stay quiet unless there is something
   decision-worthy. One daily touchpoint per domain — a single briefing —
   not a drip of notifications. _(confirmed by Nate 2026-10-04)_

## Trust boundaries (standing authority)

- **Pre-authorize the reversible**: auto-reject on hard exclusions, staging
  candidates, read-only scans, note-taking. The worker does these without
  asking, every time. _(confirmed by Nate 2026-10-04)_
- **Hard-gate the irreversible**: sends, posts, purchases, reservations,
  deletes, settings changes, task/calendar writes, anything touching someone
  else's account (LinkedIn actions are always the human's), and TTL deletions
  of staged items. Each needs the human's explicit word, every time.
  _(confirmed by Nate 2026-10-04)_
- A 👍 is not approval. The human clicks final submit — on applications,
  on purchases, on everything irreversible. _(confirmed by Nate 2026-10-04)_

## The memory loop (why the chats don't go stale)

Side chats degrade over long sessions: the transcript gets long, attention
dilutes, the agent starts contradicting its own earlier statements. Three
layers defend against it, in reliability order:

1. **In-the-moment writes (primary).** The session agent updates the chat's
   MEMORY.md and the canon store *during* the session, before anything can
   be lost. Every correction, every decision, written down while it's fresh.
   _(agent rule, unconfirmed — advisory)_
2. **Distiller backstop.** One scheduled job across all side chats (daily,
   early morning, silent unless it fails). It lists the chats, checks which
   had real user engagement since its watermark, and distills durable
   decisions/corrections/preferences into each chat's MEMORY.md. Quiet chats
   are skipped. This is the piece that should be built into the product —
   a feature request is filed; until then, the cron does it.
   _(confirmed by Nate 2026-10-05)_
3. **Compaction (automatic).** The runtime summarizes long transcripts on
   its own. This is safe *because* state lives in files, not the transcript.
   Never rely on the transcript as the record.
   _(agent rule, unconfirmed — advisory)_

Each side chat's MEMORY.md also carries a self-maintenance instruction:
after any substantive session, distill the session into the file and date
the changes. The transcript rots; the file persists.
_(agent rule, unconfirmed — advisory)_

## The automation pattern

- Each domain gets 1–4 workers. Each worker is owned by a goal, has one
  job, and delivers to the side chat (or stays silent).
- Workers are read-only unless the job's purpose is a sanctioned write, and
  sanctioned writes are enumerated in the job body (e.g. "may write
  top5-latest.json and nothing else"). _(confirmed by Nate 2026-10-04)_
- The rhythm that works: a **quiet morning worker** sweeps, filters, scores,
  auto-rejects hard exclusions, and stages survivors — saying nothing. A
  **single afternoon briefing** ranks what's staged, diffs against yesterday,
  reports what was auto-rejected and why, and ends with at most one next
  step. Readable in under a minute. _(confirmed by Nate 2026-10-04)_
- **Close the feedback loop**: reject → ask why → tune the filters → better
  results tomorrow. Rejection reasons are data; distill them into the
  preference patterns. _(agent rule, unconfirmed — advisory)_

## Worked example: the career chat

**Setup.** The 💼 Career side chat owns the job search. Behind it: a
`career-sweep` skill (executable checklists — fetch from keyless sources,
keyword-match titles, apply the denylist gate, write one JSON per surviving
role), preferences in `job-preferences.md` on OneDrive (downloaded fresh
before every run), and a bullet bank that is the *sole* source of truth for
anything that goes on a resume. Five workers: quiet morning intake, 4pm
briefing, LinkedIn triage, archive watcher, and a 3am process retro.

**Automations.** At 9:38 AM the intake worker sweeps job sources, scores
each role 1–10 against standing preferences ($225k+ base target, FTE only,
no staffing firms, remote preferred, leadership seats over IC), auto-rejects
hard exclusions (staffing-firm denylist, contract/temp, relocation-required,
under the $150k comp floor), and stages survivors — silently. At 4pm the
briefing worker ranks the top 5 (commute math on the high end vs a
170-min/week baseline, public RSUs at face vs heavily-discounted startup
paper, "player coach" language flagged), diffs against yesterday's top 5,
checks open applications' ages, scans email for recruiter/ATS mail, and
attaches "who you know" from a cached connections file. One message, under
a minute, at most one next step. Nothing new → it says so briefly and stays
quiet.

**The 3am process retro** closes the learning loop. While the memory
distiller captures *what* happened (decisions, facts, preferences), the retro
captures *how* the work went: it reads the last 24 hours of chat and
workspace changes and files durable process lessons — ATS quirks, reusable
patterns, mistakes not to repeat — into a package-build playbook and the
assistant's own operating manual. Bar for inclusion is high: concrete
evidence, generally applicable, not already recorded. It runs silently and
never touches memory files; the what/how split keeps the two jobs from
stepping on each other. _(confirmed by Nate 2026-10-05)_

**The user experience.** The human never watches any of this. No
"fetching 50 listings… filtering… 12 survived" theater — that all happens in
background workers. The chat is a decision inbox: approve (triggers a
package build), reject (prompts "why," which tunes the filters), or ignore
(nudged at 2d/4d/weekly with a fatigue guard). Contrast with ChatGPT/Claude,
where the chain-of-thought and tool calls play out in front of you: here the
work surface and the reporting surface are separated.

**The VM browser pattern** (for what can't be automated). Two modes:
headless/CLI for sweeps; the VM's real Chromium for interactive sites. When
a role is approved, the agent builds the package (tailored resume + cover
letter, every claim traceable to the bullet bank), drives the browser to the
employer's portal, and handles login via saved credentials. The human
watches or takes over live — and does the parts the agent can't: MFA codes,
CAPTCHAs, and the final submit click. The boundary is absolute: the agent
does everything up to the irreversible line, then stops.
_(confirmed by Nate 2026-10-04; three applications submitted this way)_

## What to borrow from ChatGPT Spaces (and what not to)

ChatGPT's Spaces (DevDay 2026) made this pattern legible: persistent shared
context decoupled from transcripts, long-running chats synced to spaces,
spaces shareable person-to-person. Worth borrowing:

- **Visible canon.** Make the distilled state inspectable, not just
  trusted. (Answers "what if the wrong side of the drift gets chosen.")
- **Sync as a primitive.** Chat↔space distillation built into the platform,
  not a cron the user maintains.
- **Granular sharing.** Page/space-level, view-vs-edit permissions — the
  model a two-human household needs.

Keep the substrate agent-neutral (a cloud drive, not a walled garden):
portability beats convenience when more than one AI is in the picture.
Skip wikilink-style cross-linking until the corpus outgrows a README index
or you open it in real Obsidian — in a plain cloud drive the links don't
resolve, and dated plain-text pointers ("see health-profile.md, verified
2026-10-05") do the same job. _(agent rule, unconfirmed — advisory)_

## Setup checklist _(agent rule, unconfirmed — advisory)_

1. Create the side chat; note its chat ID.
2. Create the space: OneDrive folder with a README (what's-here table, data
   authorities, freshness contract, ground rules) plus the domain's data
   files and change log.
3. Create `~/side-chats/<chat-id>/MEMORY.md`: canon rule, recency rule,
   standing rules, self-maintenance instruction, current state — everything
   dated.
4. Write the skill: executable checklists with `bin/` scripts, not a
   framework. Keep it keyless and repo-free where possible.
5. Create the workers: quiet intake + single daily briefing, silent on
   success, each owned by a goal. Enumerate sanctioned writes in the body.
6. Set up the distiller (one job across all side chats, watermark-gated,
   skips quiet chats) — or wait for the platform to ship it natively.
7. Seed the canon with current state. Date everything. Verify the human can
   see it all without asking the agent.
