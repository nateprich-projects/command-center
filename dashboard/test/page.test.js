import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  STAGES, age, boardColumns, failureState, pipState, rowOwners, rowPrState, rowTier, shortRepo,
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

test("tier and owners describe open tickets only, without duplicates", () => {
  const tickets = [
    { state: "CLOSED", tier: "escalated", owner: "Muse" },
    { state: "OPEN", tier: "standard", owner: "Codex" },
    { state: "OPEN", tier: "escalated", owner: "Codex" },
  ];
  assert.equal(rowTier(tickets), "escalated");
  assert.deepEqual(rowOwners(tickets), ["Codex"]);
  assert.equal(rowTier([{ state: "CLOSED", tier: "escalated" }]), null);
  assert.deepEqual(rowOwners([{ state: "CLOSED", owner: "Muse" }]), []);
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
