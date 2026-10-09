import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  claudeWeekStartMs,
  countUrgent,
  expectedPacePercent,
  museWeekStartMs,
  paceFigures,
  renderTopStrip,
  topStrip,
} from "../public/topstrip.js";

// Friday 2026-10-09 12:00 UTC; the Muse week opened Monday 2026-10-05 00:00 UTC.
const FRIDAY_NOON_UTC = Date.UTC(2026, 9, 9, 12, 0, 0);
const MONDAY_MIDNIGHT_UTC = Date.UTC(2026, 9, 5, 0, 0, 0);

test("the Muse week opens Monday 00:00 UTC", () => {
  assert.equal(museWeekStartMs(FRIDAY_NOON_UTC), MONDAY_MIDNIGHT_UTC);
  assert.equal(museWeekStartMs(MONDAY_MIDNIGHT_UTC), MONDAY_MIDNIGHT_UTC);
  // Sunday 2026-10-11 12:00 UTC is still the same week: 6.5 days elapsed.
  const sunday = Date.UTC(2026, 9, 11, 12, 0, 0);
  assert.equal(museWeekStartMs(sunday), MONDAY_MIDNIGHT_UTC);
});

test("the Claude week opens Saturday 12:00 local, within the last 7 days", () => {
  const start = claudeWeekStartMs(FRIDAY_NOON_UTC);
  const at = new Date(start);
  assert.equal(at.getDay(), 6);
  assert.equal(at.getHours(), 12);
  assert.ok(start <= FRIDAY_NOON_UTC);
  assert.ok(FRIDAY_NOON_UTC - start < 7 * 24 * 3600 * 1000);
});

test("weekly usage over days elapsed matches the even-burn pace", () => {
  // Half the week gone: 50% used is exactly on pace.
  assert.deepEqual(paceFigures(50, 3.5), {
    state: "known",
    used: 50,
    expected: 50,
    delta: 0,
    elapsedDays: 3.5,
  });
  // End of the week: the line reads 100.
  assert.deepEqual(paceFigures(15, 7), {
    state: "known",
    used: 15,
    expected: 100,
    delta: -85,
    elapsedDays: 7,
  });
  assert.equal(expectedPacePercent(0), 0);
  assert.deepEqual(paceFigures("15", 3.5), { state: "unavailable" });
  assert.deepEqual(paceFigures(15, Number.NaN), { state: "unavailable" });
});

function stripSnapshot() {
  return {
    generated_at: "2026-10-09T12:00:00Z",
    brief: { total_needing_nate: 4 },
    board: {
      columns: [
        { stage: "Building", items: [
          { ref: "o/a#1", class: "Broken" },
          { ref: "o/a#2", class: "Bug" },
          { ref: "o/a#3", class: "New" },
        ] },
        { stage: "Ideas", items: [{ ref: "o/a#4", class: "Investigate" }] },
        { stage: "Ready", items: [{ ref: "o/a#5", class: "Maintenance" }] },
        // Done is history: a finished Broken project is not urgent work.
        { stage: "Done", items: [{ ref: "o/a#6", class: "Broken" }] },
      ],
    },
    usage: {
      muse: {
        spent_dollars: 30,
        cap_dollars: 200,
        used_percent: 15,
        calls: 10,
        captured_at: 1728470400,
        source: "Local Muse session journal estimate",
      },
      claude: { u: { sd: 20 }, t: FRIDAY_NOON_UTC - 30 * 60 * 1000 },
    },
  };
}

test("the strip shows needing_nate with urgent plus Bugs counts", () => {
  const summary = topStrip(stripSnapshot(), FRIDAY_NOON_UTC);
  assert.equal(summary.needingNate, 4);
  assert.equal(summary.urgent, 3);
  assert.equal(summary.bugs, 1);

  assert.equal(summary.muse.state, "known");
  assert.equal(summary.muse.used, 15);
  assert.ok(Math.abs(summary.muse.elapsedDays - 4.5) < 1e-9);
  assert.ok(Math.abs(summary.muse.expected - (4.5 / 7) * 100) < 1e-9);
  assert.ok(Math.abs(summary.muse.delta - (15 - (4.5 / 7) * 100)) < 1e-9);

  assert.equal(summary.claude.state, "known");
  assert.equal(summary.claude.used, 20);
  assert.ok(summary.claude.elapsedDays >= 0 && summary.claude.elapsedDays < 7);
  assert.ok(Math.abs(
    summary.claude.expected - (summary.claude.elapsedDays / 7) * 100,
  ) < 1e-9);
});

test("the strip tolerates an unreadable brief, board and usage", () => {
  const summary = topStrip({}, FRIDAY_NOON_UTC);
  assert.equal(summary.needingNate, null);
  assert.deepEqual(
    { urgent: summary.urgent, bugs: summary.bugs },
    { urgent: 0, bugs: 0 },
  );
  assert.equal(summary.muse.state, "unavailable");
  assert.equal(summary.claude.state, "unavailable");

  assert.equal(topStrip({ brief: { total_needing_nate: "lots" } }).needingNate, null);
  assert.deepEqual(countUrgent(null), { urgent: 0, bugs: 0 });
});

class StubNode {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.className = "";
    this.textContent = "";
  }

  append(...children) {
    this.children.push(...children);
  }

  replaceChildren() {
    this.children = [];
  }
}

function stubDocument() {
  const previous = globalThis.document;
  globalThis.document = {
    createElement: (tagName) => new StubNode(tagName),
  };
  return () => {
    if (previous === undefined) delete globalThis.document;
    else globalThis.document = previous;
  };
}

function allText(node) {
  const parts = [];
  if (node.textContent) parts.push(node.textContent);
  for (const child of node.children || []) parts.push(allText(child));
  return parts.join(" ");
}

test("the rendered strip names the waiting total, both usages and the counts", () => {
  const restore = stubDocument();
  try {
    const container = new StubNode("section");
    renderTopStrip(container, topStrip(stripSnapshot(), FRIDAY_NOON_UTC));
    const text = allText(container);
    assert.match(text, /4 waiting on you/);
    assert.match(text, /Muse 15\.0%/);
    assert.match(text, /Claude 20\.0%/);
    assert.match(text, /day [\d.]+ of 7/);
    assert.match(text, /3 urgent/);
    assert.match(text, /1 bugs/);

    const unreadable = new StubNode("section");
    renderTopStrip(unreadable, topStrip({}, FRIDAY_NOON_UTC));
    const missing = allText(unreadable);
    assert.match(missing, /could not be read/);
    assert.match(missing, /unavailable/);
  } finally {
    restore();
  }
});

test("the strip reads only the snapshot it is handed and keeps no store", async () => {
  const source = await readFile(new URL("../public/topstrip.js", import.meta.url), "utf8");
  assert.doesNotMatch(source, /fetch\s*\(/);
  assert.doesNotMatch(source, /XMLHttpRequest/);
  assert.doesNotMatch(source, /localStorage|sessionStorage|indexedDB/);
  assert.match(source, /\/api\/snapshot/);
});
