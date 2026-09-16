import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  STAGES, age, boardColumns, failureState, nextOwner, pipState, rowPrState, rowTier, shortRepo,
} from "../public/app.js";

test("the board uses payload order within the fixed plan stages", () => {
  const input = [
    { status: "Building", title: "first" },
    { status: "Ideas", title: "idea" },
    { status: "Building", title: "second" },
  ];
  const columns = boardColumns(input);

  assert.deepEqual(columns.map((column) => column.stage), STAGES);
  assert.deepEqual(
    columns.find((column) => column.stage === "Building").items.map((item) => item.title),
    ["first", "second"],
  );
});

test("a pre-grouped board is rendered exactly in producer order", () => {
  const columns = [
    { stage: "Building", items: [{ title: "second" }, { title: "first" }] },
    { stage: "Ideas", items: [] },
  ];
  assert.equal(boardColumns({ columns }), columns);
});

test("a pip carries the ticket's furthest state", () => {
  assert.equal(pipState({ state: "CLOSED", pr: "merged" }), "closed");
  assert.equal(pipState({ state: "OPEN", pr: "approved" }), "approved");
  assert.equal(pipState({ state: "OPEN", pr: "submitted" }), "submitted");
  assert.equal(pipState({ state: "OPEN", blocked: true }), "blocked");
  assert.equal(pipState({ state: "OPEN" }), "open");
});

test("a row's PR flag is the furthest of its tickets, and closed work is not a flag", () => {
  assert.equal(rowPrState([{ state: "OPEN", pr: "submitted" }, { state: "OPEN", pr: "approved" }]), "approved");
  assert.equal(rowPrState([{ state: "CLOSED" }, { state: "OPEN", pr: "submitted" }]), "submitted");
  assert.equal(rowPrState([{ state: "CLOSED" }]), null);
});

test("tier describes open tickets only", () => {
  const tickets = [
    { state: "CLOSED", tier: "escalated" },
    { state: "OPEN", tier: "standard" },
    { state: "OPEN", tier: "escalated" },
  ];
  assert.equal(rowTier(tickets), "escalated");
  assert.equal(rowTier([{ state: "CLOSED", tier: "escalated" }]), null);
});

test("the next step is the producer's next owner, never a summary of owners", () => {
  assert.equal(nextOwner({ next_owner: "Muse", tickets: [{ state: "OPEN", owner: "Codex" }] }), "Muse");
  // Fallback for an older snapshot: the first open ticket, which the producer
  // already sorted into queue order.
  assert.equal(nextOwner({ tickets: [{ state: "CLOSED", owner: "Muse" }, { state: "OPEN", owner: "Codex" }] }), "Codex");
  assert.equal(nextOwner({ tickets: [{ state: "CLOSED", owner: "Muse" }] }), null);
});

test("snapshot age and last-failure status are explicit", () => {
  assert.equal(age("2026-09-13T08:00:00Z", Date.parse("2026-09-13T08:05:00Z")), "5m old");
  assert.equal(failureState({ last_brief_failed: true }), true);
  assert.equal(failureState({ status: { last_brief_failed: true } }), true);
  assert.equal(failureState({}), false);
});

test("board repository names omit the owner prefix", () => {
  assert.equal(shortRepo("nateprich-projects/command-center"), "command-center");
  assert.equal(shortRepo("command-center"), "command-center");
});

test("page code does not sort, filter, or reverse producer data", async () => {
  const source = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
  assert.doesNotMatch(source, /\.(?:sort|filter|reverse)\s*\(/);
});

test("the page renders no brief section other than the board and human steps", async () => {
  const source = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
  for (const dropped of [
    "missing", "working_tree_touched", "machine_local_steps", "closed_itself",
    "cleared_blocks", "unattended_approvals", "unattended_merges", "prose_dependencies",
    "stranded", "in_motion", "awaiting_breakdown", "agent_health", "degraded",
    "counts_by_gate", "maintenance_load", "disposal",
  ]) {
    assert.doesNotMatch(source, new RegExp(`brief\\.${dropped}\\b`), `page still reads ${dropped}`);
  }
});

test("the row toggle is bound once, so a chevron click does not cancel itself", async () => {
  const source = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
  const listeners = source.match(/addEventListener\("click", /g) || [];
  // One on the row (the chevron is inside it) and one on the group header.
  // The refresh button's listener went with the button. A second listener on
  // the chevron toggled twice and the row never opened (#902).
  assert.equal(listeners.length, 2);
  assert.doesNotMatch(source, /twisty\.addEventListener\("click"/);
});

test("the sub-issue bar fills its column rather than capping its pips", async () => {
  const app = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
  const css = await readFile(new URL("../public/styles.css", import.meta.url), "utf8");
  assert.doesNotMatch(app, /PIP_LIMIT/);
  assert.match(css, /\.pip-bar \.pip \{ flex: 1 1 0;/);
});

test("the page polls its own snapshot and re-renders only on a new timestamp", async () => {
  const source = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
  assert.match(source, /setInterval/);
  assert.match(source, /generatedAt === lastGeneratedAt/);
  // A hidden tab is not read, so it should not poll.
  assert.match(source, /visibilityState === "hidden"/);
});

test("expanded projects survive a re-render", async () => {
  const source = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
  assert.match(source, /const expanded = new Set\(\)/);
  assert.match(source, /expanded\.has\(item\.ref\)/);
});


test("the bar uses the producer's progress order, and the rows keep queue order", async () => {
  const source = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
  assert.match(source, /item\.pips/);
  // Still no client-side ordering: the producer sends both orders.
  assert.doesNotMatch(source, /\.(?:sort|filter|reverse)\s*\(/);
});

test("the refresh button is gone; the webhook and the scheduled brief replace it", async () => {
  const source = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
  const html = await readFile(new URL("../public/index.html", import.meta.url), "utf8");
  assert.doesNotMatch(source, /api\/refresh/);
  assert.doesNotMatch(html, /id="refresh"/);
});

test("the page ships its own favicon rather than borrowing a default", async () => {
  const html = await readFile(new URL("../public/index.html", import.meta.url), "utf8");
  assert.match(html, /rel="icon" href="\/favicon\.svg"/);
  const icon = await readFile(new URL("../public/favicon.svg", import.meta.url), "utf8");
  assert.match(icon, /<svg/);
});
