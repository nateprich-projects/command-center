import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  STAGES, age, boardColumns, boardTabCounts, boardTabFromUrl, boardTabUrl, tabColumns,
  failureState, museUsageText, musePanelUsage, museEstimate,
  claudeUsage, claudeUsageChanged, snapshotNeedsRender,
  renderUsage, nextOwner, ownerCell,
  phoneState, pipState, projectBlocked, projectHold, holdChip, renderPhoneBoard, ticketHold, unblocksChip,
  repoLabels, repoOf,
  repoOptions, rowTier, shortRepo, visible, renderExecutionTiles, requestMetrics,
  renderMetricChart, renderBudgetMetrics, renderRunMetrics,
  renderAttentionMetrics, renderChurnMetrics,
  renderOutputPanel,
  renderQualityMetrics,
  renderWaiting,
  CHART_WINDOW_DAYS,
  tabFromUrl, tabUrl,
} from "../public/app.js";

class TestNode {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.attributes = new Map();
    this.className = "";
    this.classList = {
      add: (...names) => {
        const classes = new Set(this.className.split(/\s+/).filter(Boolean));
        for (const name of names) classes.add(name);
        this.className = [...classes].join(" ");
      },
    };
  }

  append(...children) {
    for (const child of children) {
      if (child === undefined || child === null) continue;
      this.children.push(child);
    }
  }

  replaceChildren(...children) {
    this.children = [];
    this.append(...children);
  }

  set textContent(value) {
    this.children = [String(value)];
  }

  get textContent() {
    return this.children.map((child) => (
      child instanceof TestNode ? child.textContent : String(child)
    )).join("");
  }

  get childElementCount() {
    return this.children.filter((child) => child instanceof TestNode).length;
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
    // As in a browser, the class attribute and className are one value.
    if (name === "class") this.className = String(value);
  }

  addEventListener() {}

  *walk() {
    yield this;
    for (const child of this.children) {
      if (child instanceof TestNode) yield* child.walk();
    }
  }

  querySelectorAll(selector) {
    const className = selector.startsWith(".") ? selector.slice(1) : null;
    return [...this.walk()].filter((node) => (
      className && node.className.split(/\s+/).includes(className)
    ));
  }

  querySelector(selector) {
    return this.querySelectorAll(selector)[0] || null;
  }
}

class TestDocument {
  createElement(tagName) {
    return new TestNode(tagName);
  }

  createElementNS(namespace, tagName) {
    const node = new TestNode(tagName);
    node.namespaceURI = namespace;
    return node;
  }
}

function renderWaitingFixture(brief) {
  const container = new TestNode("div");
  const emptyState = {
    content: {
      cloneNode() {
        const fragment = new TestNode("fragment");
        const message = new TestNode("p");
        message.className = "empty";
        message.textContent = "Nothing is waiting on you.";
        fragment.append(message);
        return fragment;
      },
    },
  };
  const previousDocument = globalThis.document;
  globalThis.document = {
    createElement(tagName) { return new TestNode(tagName); },
    querySelector(selector) {
      if (selector === "#waiting") return container;
      if (selector === "#empty-state") return emptyState;
      return null;
    },
  };
  try {
    renderWaiting(brief);
    return container;
  } finally {
    if (previousDocument === undefined) delete globalThis.document;
    else globalThis.document = previousDocument;
  }
}

function renderUsageFixture(usage, nowMs) {
  const container = new TestNode("div");
  const previousDocument = globalThis.document;
  globalThis.document = {
    createElement(tagName) { return new TestNode(tagName); },
    querySelector(selector) {
      assert.equal(selector, "#usage");
      return container;
    },
  };
  try {
    renderUsage(usage, nowMs);
    return container;
  } finally {
    if (previousDocument === undefined) delete globalThis.document;
    else globalThis.document = previousDocument;
  }
}

function usageRow(container, marker) {
  return container.querySelectorAll(".usage").find((row) => (
    row.className.split(/\s+/).includes(marker)
  ));
}

function nodesByTag(root, tagName) {
  return [...root.walk()].filter((node) => node.tagName === tagName);
}

// The page's one sort orders the repository dropdown's names, which are not
// producer rows; its one filter is the viewer's repository choice, in
// visible(), which keeps producer order (Nate, 2026-09-24). Everything else
// stays free of client-side ordering.
function withoutRepoOptions(source) {
  const start = source.indexOf("function repoOptions(");
  const body = source.slice(start, source.indexOf("\n}\n", start));
  assert.match(body, /\.sort\(/);
  return source.replace(body, "");
}

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

test("the board splits into Parked, In Progress and Bugs in producer order (Nate, 2026-09-28)", () => {
  const columns = [
    { stage: "Ideas", items: [{ title: "idea", class: "Bug" }, { title: "improve", class: "Improve" }] },
    { stage: "Building", items: [
      { title: "broken", class: "Broken" }, { title: "bug", class: "Bug" }, { title: "new", class: "New" },
    ] },
    { stage: "Parked", items: [{ title: "parked bug", class: "Bug" }, { title: "parked", class: null }] },
    { stage: "Done", items: [{ title: "done", class: "Broken" }, { title: "done bug", class: "Bug" }] },
  ];
  const titles = (tab) => tabColumns(columns, tab).map((column) => [
    column.stage, column.items.map((item) => item.title),
  ]);

  assert.deepEqual(titles("in-progress"), [
    ["Ideas", ["improve"]], ["Building", ["broken", "new"]], ["Parked", []], ["Done", ["done"]],
  ]);
  assert.deepEqual(titles("bugs"), [
    ["Ideas", ["idea"]], ["Building", ["bug"]], ["Parked", []], ["Done", ["done bug"]],
  ]);
  assert.deepEqual(titles("parked"), [
    ["Ideas", []], ["Building", []], ["Parked", ["parked bug", "parked"]], ["Done", []],
  ]);
  // Counts are open projects only, so Done never inflates a tab.
  assert.deepEqual(boardTabCounts(columns, null), { parked: 2, "in-progress": 3, bugs: 2 });
});

test("the board tab counts follow the repository filter", () => {
  const columns = [{ stage: "Ready", items: [
    { ref: "o/a#1", class: "Bug" }, { ref: "o/b#2", class: "Bug" }, { ref: "o/a#3", class: "Improve" },
  ] }];
  assert.deepEqual(boardTabCounts(columns, "o/a"), { parked: 0, "in-progress": 1, bugs: 1 });
});

test("In Progress is the default board tab and the others live in the URL", async () => {
  assert.equal(boardTabFromUrl("/"), "in-progress");
  assert.equal(boardTabFromUrl("/?board=nonsense"), "in-progress");
  assert.equal(boardTabFromUrl("/?repo=o%2Fa&board=bugs"), "bugs");
  assert.equal(boardTabUrl("parked", "/?repo=o%2Fa"), "/?repo=o%2Fa&board=parked");
  assert.equal(boardTabUrl("in-progress", "/?board=bugs&tab=funnel"), "/?tab=funnel");
  const html = await readFile(new URL("../public/index.html", import.meta.url), "utf8");
  const tabs = html.slice(html.indexOf('id="board-tabs"'), html.indexOf('<div class="legend"'));
  assert.deepEqual([...tabs.matchAll(/data-board-tab="([^"]+)"/g)].map((match) => match[1]),
    ["parked", "in-progress", "bugs"]);
  assert.match(tabs, /data-board-tab="in-progress"\s+aria-selected="true"/);
  assert.equal((tabs.match(/data-count/g) || []).length, 3);
});

test("a pip carries the ticket's furthest state", () => {
  assert.equal(pipState({ state: "CLOSED", pr: "merged" }), "closed");
  assert.equal(pipState({ state: "OPEN", pr: "approved" }), "approved");
  assert.equal(pipState({ state: "OPEN", pr: "changes requested" }), "changes-requested");
  assert.equal(pipState({ state: "OPEN", pr: "submitted" }), "submitted");
  assert.equal(pipState({ state: "OPEN", pr_stale: true }), "stale");
  assert.equal(pipState({ state: "OPEN", pr_unknown: true }), "unknown");
  assert.equal(pipState({ state: "OPEN", blocked: true }), "blocked");
  assert.equal(
    pipState({ state: "OPEN", blocked: true, blocked_by_siblings: true }), "queued",
  );
  assert.equal(pipState({ state: "OPEN" }), "open");
});

test("a carried PR fact has a stale pip and visible age on the phone board", () => {
  const previousDocument = globalThis.document;
  globalThis.document = new TestDocument();
  try {
    const board = renderPhoneBoard([{
      stage: "Building",
      items: [{
        ref: "repo#1",
        title: "Plan",
        tickets: [{
          ref: "repo#2",
          number: 2,
          title: "Review PR",
          state: "OPEN",
          pr_stale: true,
          pr_stale_state: "approved",
          pr_stale_age: "5h",
        }],
      }],
    }]);
    const staleRow = board.querySelectorAll(".phone-row").find(
      (row) => row.className.split(/\s+/).includes("phone-row-stale"),
    );

    assert.ok(staleRow);
    assert.ok(staleRow.querySelector(".pip")?.className.split(/\s+/).includes("pip-stale"));
    assert.equal(staleRow.querySelector(".pr-stale-age")?.textContent, "stale approved · 5h");
  } finally {
    if (previousDocument === undefined) delete globalThis.document;
    else globalThis.document = previousDocument;
  }
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
  assert.doesNotMatch(withoutRepoOptions(source), /\.(?:sort|filter|reverse)\s*\(/);
});

test("decision rows display the producer's waiting reason", async () => {
  const source = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
  const start = source.indexOf("function decisionRow(");
  const row = source.slice(start, source.indexOf("\n}\n\nfunction humanStepRow", start));
  assert.match(row, /item\.waiting_reason/);
  assert.match(row, /chip\(item\.waiting_reason, "chip-reason"\)/);
});

test("the repository filter keeps producer order and drops only other repos", () => {
  const rows = [
    { ref: "o/b#3", repo: "b" },
    { ref: "o/a#1", repo: "a" },
    { ref: "o/b#2" },
    { ref: "other/a#4", repo: "a" },
    { repo: "o/a" },
  ];
  assert.deepEqual(visible(rows, "o/a").map((row) => row.ref), ["o/a#1", undefined]);
  assert.deepEqual(visible(rows, "o/b").map((row) => row.ref), ["o/b#3", "o/b#2"]);
  assert.equal(visible(rows, null).length, 5);
  assert.deepEqual(visible(undefined, "o/a"), []);
  // The ref's full owner/repo wins over a board row's short `repo`, so two
  // owners' same-named repositories stay apart.
  assert.equal(repoOf({ ref: "owner/member-repo#12", repo: "member-repo" }), "owner/member-repo");
  assert.equal(repoOf({ repo: "nateprich-projects/workbench" }), "nateprich-projects/workbench");
});

test("the waiting panel distinguishes unreadable sections from completed empty values", async () => {
  const empty = JSON.parse(await readFile(
    new URL("../fixtures/snapshot-empty-brief.json", import.meta.url), "utf8",
  ));
  const unreadable = JSON.parse(await readFile(
    new URL("../fixtures/snapshot-unreadable-brief.json", import.meta.url), "utf8",
  ));

  const emptyPanel = renderWaitingFixture(empty.brief);
  assert.match(emptyPanel.textContent, /Nothing is waiting on you\./);
  assert.doesNotMatch(emptyPanel.textContent, /could not be read/i);
  assert.equal(emptyPanel.querySelectorAll(".waiting-section").length, 0);

  const unreadablePanel = renderWaitingFixture(unreadable.brief);
  assert.match(unreadablePanel.textContent, /Decisions waiting on you/);
  assert.match(unreadablePanel.textContent, /Actions waiting on you/);
  assert.match(unreadablePanel.textContent, /Could not be read\./);
  assert.match(unreadablePanel.textContent, /Counts by gate could not be read\./);
  assert.match(unreadablePanel.textContent, /Machine-local steps could not be read\./);
  assert.match(unreadablePanel.textContent, /Status\/state mismatches could not be read\./);
  assert.doesNotMatch(unreadablePanel.textContent, /Maintenance load could not be read/);
  assert.match(unreadablePanel.textContent, /The total waiting on you could not be read\./);

  const unreadableDecisions = renderWaitingFixture({
    ...empty.brief,
    total_needing_nate: null,
    items: null,
    human_steps: [],
  });
  assert.match(unreadableDecisions.textContent, /Decisions waiting on you/);
  assert.match(unreadableDecisions.textContent, /Could not be read\./);
  assert.doesNotMatch(unreadableDecisions.textContent, /Actions waiting on you/);
  assert.doesNotMatch(unreadableDecisions.textContent, /No actions are waiting on you/);

  const unreadableActions = renderWaitingFixture({
    ...empty.brief,
    total_needing_nate: null,
    items: [],
    human_steps: null,
  });
  assert.doesNotMatch(unreadableActions.textContent, /Decisions waiting on you/);
  assert.match(unreadableActions.textContent, /Actions waiting on you/);
  assert.match(unreadableActions.textContent, /Could not be read\./);
  assert.doesNotMatch(unreadableActions.textContent, /No decisions are waiting on you/);

  const everySectionUnreadable = renderWaitingFixture({
    ...empty.brief,
    total_needing_nate: null,
    items: null,
    human_steps: null,
    machine_local_steps: null,
    blocked: null,
    blocked_human_steps: null,
    status_state_mismatches: null,
    counts_by_gate: null,
    maintenance_load: null,
    disposal: null,
    resend_ratio: null,
    rejected_merges: null,
  });
  for (const message of [
    "Machine-local steps could not be read.",
    "Blocked items could not be read.",
    "Blocked human steps could not be read.",
    "Status/state mismatches could not be read.",
    "Counts by gate could not be read.",
    "Maintenance load could not be read.",
    "Disposal could not be read.",
    "Resend ratio could not be read.",
    "Rejected merges could not be read.",
    "The total waiting on you could not be read.",
  ]) {
    assert.ok(everySectionUnreadable.textContent.includes(message), message);
  }
});

test("the dropdown lists every repository once, alphabetically, and keeps the choice", () => {
  const snapshot = {
    board: { columns: [
      { stage: "Building", items: [
        { ref: "owner/zeta#1", repo: "zeta" }, { ref: "owner/Alpha#2", repo: "Alpha" },
      ] },
      { stage: "Done", items: [{ ref: "owner/zeta#3", repo: "zeta" }] },
    ] },
    brief: {
      items: [{ ref: "owner/mid#5", repo: "owner/mid" }],
      human_steps: [{ ref: "owner/beta#4" }, { ref: "other/beta#6" }],
    },
  };
  const names = repoOptions(snapshot, null);
  assert.deepEqual(names, ["owner/Alpha", "other/beta", "owner/beta", "owner/mid", "owner/zeta"]);
  assert.deepEqual(repoOptions(snapshot, "owner/gone").length, 6);
  assert.deepEqual(repoOptions({ brief: { items: null, human_steps: null } }, null), []);
  // Short names, unless two owners share one.
  assert.deepEqual(repoLabels(names).map(([, label]) => label),
    ["Alpha", "other/beta", "owner/beta", "mid", "zeta"]);
});

test("the dropdown is rebuilt only when its list changes", async () => {
  const source = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
  assert.match(source, /if \(signature !== renderedOptions\)/);
});

test("the filter sits at the top of the page and lives in the URL", async () => {
  const html = await readFile(new URL("../public/index.html", import.meta.url), "utf8");
  const source = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
  const masthead = html.slice(html.indexOf('<header class="masthead">'), html.indexOf("</header>"));
  assert.match(masthead, /<select id="repo-filter">/);
  assert.match(source, /searchParams\.set\("repo", repo\)/);
  assert.match(source, /get\("repo"\)/);
});

test("a ticket others wait on says which, in Nate's phrasing", () => {
  const previousDocument = globalThis.document;
  globalThis.document = new TestDocument();
  try {
    const ref = (n) => `nateprich-projects/command-center#${n}`;
    assert.equal(unblocksChip({ unblocks: [] }), null);
    assert.equal(unblocksChip({ unblocks: [ref(1465)] }).textContent, "unblocks #1465");
    assert.equal(unblocksChip({ unblocks: [ref(1465), ref(1466)] }).textContent,
      "unblocks #1465 & #1466");
    assert.equal(unblocksChip({ unblocks: [ref(1465), ref(1466), ref(1467)] }).textContent,
      "unblocks #1465, #1466, & #1467");
    assert.equal(
      unblocksChip({ unblocks: [ref(1465)], unblocks_later: [ref(1466), ref(1467)] }).textContent,
      "unblocks #1465, then #1466 & #1467",
    );
    assert.equal(
      unblocksChip({ unblocks: [ref(1)], unblocks_later: [2, 3, 4, 5, 6].map(ref) }).textContent,
      "unblocks #1, then #2, #3, #4, & 2 more",
    );
    assert.equal(unblocksChip({ unblocks: [], unblocks_later: [ref(2)] }), null);
  } finally {
    if (previousDocument === undefined) delete globalThis.document;
    else globalThis.document = previousDocument;
  }
});

test("a ticket row shows the class it ranks as", async () => {
  const source = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
  const row = source.slice(source.indexOf("function ticketRow("), source.indexOf("function phoneChildren("));
  assert.match(row, /classChip\(ticket\.state === "OPEN" \? ticket\.class : null\)/);
  assert.match(row, /unblocksChip\(ticket\)/);
});

test("a row nobody can act on says Blocked where the owner would be", () => {
  const previousDocument = globalThis.document;
  globalThis.document = new TestDocument();
  try {
    assert.equal(ownerCell(null, true).textContent, "Blocked");
    assert.ok(ownerCell(null, true).className.includes("owner-blocked"));
    assert.equal(ownerCell(null, false).textContent, "—");
    assert.equal(ownerCell("Muse", true).textContent, "Muse");
    assert.equal(ownerCell(null, "blocked").textContent, "Blocked");
    assert.equal(ownerCell(null, "queued").textContent, "Queued");
    assert.ok(ownerCell(null, "queued").className.includes("owner-queued"));
  } finally {
    if (previousDocument === undefined) delete globalThis.document;
    else globalThis.document = previousDocument;
  }
  assert.equal(projectBlocked({ next_step_blocked: true, blocked: false }), true);
  assert.equal(projectBlocked({ next_step_blocked: false, blocked: false }), false);
  // An older snapshot without the flag falls back to the project's own block.
  assert.equal(projectBlocked({ blocked: true }), true);
  // Queued uses the pip's definition: every blocker is a sibling ticket.
  assert.equal(ticketHold({ state: "OPEN", blocked: true, blocked_by_siblings: true }), "queued");
  assert.equal(ticketHold({ state: "OPEN", blocked: true }), "blocked");
  assert.equal(ticketHold({ state: "OPEN", blocked: false }), null);
  assert.equal(ticketHold({ state: "CLOSED", blocked: true }), null);
});

test("engine holds the Project fields do not show read on the row (Nate, 2026-09-25)", () => {
  const until = "2026-09-26T08:09:44+00:00";
  assert.equal(ticketHold({ state: "OPEN", paused_until: until }), "paused");
  // A block outranks a pause: the pause only matters once the block lifts.
  assert.equal(ticketHold({ state: "OPEN", blocked: true, paused_until: until }), "blocked");
  assert.equal(projectHold({ next_step_blocked: false, next_step_paused_until: until }), "paused");
  assert.equal(projectHold({ next_step_blocked: true, next_step_paused_until: until }), "blocked");
  assert.equal(projectHold({ next_step_blocked: false }), null);
  const previousDocument = globalThis.document;
  globalThis.document = new TestDocument();
  try {
    const paused = ownerCell(null, "paused", "until Sat 1:09 AM");
    assert.equal(paused.textContent, "Paused");
    assert.ok(paused.className.includes("owner-paused"));
    const chipNode = holdChip({ state: "OPEN", paused_until: until, paused_failures: 6 });
    assert.match(chipNode.textContent, /^paused until /);
    assert.doesNotMatch(chipNode.textContent, /UTC|Z$/);
    assert.match(chipNode.title, /6 failed runs in a row/);
    assert.equal(holdChip({ state: "OPEN", finished_by_comments: true }).textContent,
      "finished — close it");
    assert.equal(holdChip({ state: "CLOSED", finished_by_comments: true }), null);
    assert.equal(holdChip({ state: "OPEN" }), null);
  } finally {
    if (previousDocument === undefined) delete globalThis.document;
    else globalThis.document = previousDocument;
  }
});

test("the page does not render values from other brief sections", async () => {
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
  const board = source.slice(source.indexOf("function projectRow("), source.indexOf("function decisionRow("));
  const listeners = board.match(/addEventListener\("click", /g) || [];
  // One on the row (the chevron is inside it) and one on the group header.
  // The view navigation has its own listener; a second listener on the
  // chevron toggled twice and the row never opened (#902).
  assert.equal(listeners.length, 2);
  assert.match(source, /nav\.addEventListener\("click", /);
  assert.doesNotMatch(source, /twisty\.addEventListener\("click"/);
});

test("the sub-issue bar fills its column rather than capping its pips", async () => {
  const app = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
  const css = await readFile(new URL("../public/styles.css", import.meta.url), "utf8");
  assert.doesNotMatch(app, /PIP_LIMIT/);
  assert.match(css, /\.pip-bar \.pip \{ flex: 1 1 0;/);
});

test("the page follows the system light/dark setting (#996)", async () => {
  const css = await readFile(new URL("../public/styles.css", import.meta.url), "utf8");
  const html = await readFile(new URL("../public/index.html", import.meta.url), "utf8");
  assert.match(html, /<meta name="color-scheme" content="light dark">/);
  assert.match(css, /color-scheme: light dark;/);
  assert.match(css, /@media \(prefers-color-scheme: light\) \{\s+:root \{/);
  // Outside the two token blocks, no rule carries a colour literal.
  const rules = css
    .replace(/:root \{[^}]*\}/g, "")
    .replace(/\/\*[\s\S]*?\*\//g, "");
  assert.doesNotMatch(rules, /#[0-9a-fA-F]{3,8}\b|rgba?\(/);
});

test("changes requested has a themed pip and legend entry", async () => {
  const css = await readFile(new URL("../public/styles.css", import.meta.url), "utf8");
  const html = await readFile(new URL("../public/index.html", import.meta.url), "utf8");
  assert.match(css, /--pip-changes-requested:/);
  assert.match(css, /\.pip-changes-requested \{ background: var\(--pip-changes-requested-pip\); \}/);
  assert.match(html, /pip pip-changes-requested[^<]*<\/i>\s*changes requested/);
});

test("pip collisions use scoped colours and a textured blocked state", async () => {
  const css = await readFile(new URL("../public/styles.css", import.meta.url), "utf8");
  const html = await readFile(new URL("../public/index.html", import.meta.url), "utf8");
  assert.match(css, /--pip-changes-requested-pip:/);
  assert.match(css, /--pip-blocked-pip:/);
  assert.match(css, /--pip-blocked-pip-stripe:/);
  assert.match(css, /\.pip-changes-requested \{ background: var\(--pip-changes-requested-pip\); \}/);
  assert.match(css, /\.pip-blocked \{[\s\S]*repeating-linear-gradient\(\s*135deg,/);
  assert.match(css, /\.chip-tier-escalated \{ color: var\(--pip-blocked\); \}/);
  assert.match(css, /\.chip-class-maintenance \{ color: var\(--pip-blocked\); \}/);
  assert.match(css, /\.chip-class-broken \{ color: var\(--danger\); \}/);
  assert.match(css, /\.pip-queued \{ background: var\(--pip-open\); \}/);
  assert.match(html, /pip pip-open[^<]*<\/i>\s*open or queued/);
  assert.match(html, /pip pip-blocked[^<]*<\/i>\s*blocked</);
  assert.doesNotMatch(html, /pip-queued|waiting on a sibling|from outside/);
  assert.doesNotMatch(html, /striped/);
});

test("the phone progress shows its count once (#994)", async () => {
  const source = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
  const progress = source.slice(
    source.indexOf("function phoneProgress("), source.indexOf("function phoneDetails("),
  );
  assert.match(progress, /pips\(item, children, closed, total\)/);
  assert.doesNotMatch(progress, /`\$\{closed\}\/\$\{total\}`\)\);/);
  assert.match(source, /element\("span", "pip-count", `\$\{closed\}\/\$\{total\}`\)/);
});

test("the page poll refreshes usage age and detects changed provider fields", async () => {
  const source = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
  assert.match(source, /setInterval/);
  const poll = source.slice(
    source.indexOf("async function loadSnapshot("), source.indexOf("\nfunction activateTab("),
  );
  assert.match(poll, /snapshotNeedsRender\(lastSnapshot, snapshot, lastGeneratedAt\)/);
  assert.match(poll, /renderUsage\(snapshot\.usage \|\| \{\}\)/);
  // A hidden tab is not read, so it should not poll.
  assert.match(source, /visibilityState === "hidden"/);
});

test("the wide board has no PR column, and every grid template matches its visible cells (#997)", async () => {
  const app = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
  const css = await readFile(new URL("../public/styles.css", import.meta.url), "utf8");
  assert.doesNotMatch(app, /"cell-pr"/);
  const tracks = [...css.matchAll(/--cols: ([^;]+);/g)].map((m) => m[1].match(/minmax\([^)]*\)|\S+/g).length);
  // 8 wide cells; 7 with repository hidden; 4 with repository, tier, class and age hidden.
  assert.deepEqual(tracks, [8, 7, 4]);
});

test("expanded projects survive a re-render", async () => {
  const source = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
  assert.match(source, /const expanded = new Set\(\)/);
  assert.match(source, /expanded\.has\(item\.ref\)/);
});

test("the phone board keeps four summary fields and discloses details recursively", async () => {
  const app = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
  const css = await readFile(new URL("../public/styles.css", import.meta.url), "utf8");
  assert.match(app, /element\("div", "phone-board"\)/);
  assert.match(app, /element\("details", "phone-row"\)/);
  assert.match(app, /element\("span", "phone-counter", `\$\{closed\}\/\$\{total\}`\)/);
  assert.match(app, /for \(const child of children\) \{\s+childList\.append\(phoneRow\(child, className\)\);/);
  assert.match(app, /row\.addEventListener\("toggle",/);
  assert.match(css, /@media \(max-width: 600px\)/);
  assert.match(css, /\.table \{ display: none; \}/);
  assert.match(css, /\.phone-board \{ display: block; \}/);
  // Two-row summary (#1065): row 1 is the disclosure arrow plus the title at
  // the full summary width, and row 2 is the repository, class chip and
  // progress counter trailing beneath it. This replaced the single-row
  // 24px/minmax/76px/auto/auto pin, under which long titles squeezed the rest.
  assert.match(css, /grid-template-columns: 24px minmax\(0, 1fr\) auto auto;/);
  assert.match(css, /grid-template-rows: auto auto;/);
  assert.match(css, /\.phone-title \{ grid-column: 2 \/ -1; grid-row: 1;/);
  assert.match(css, /\.phone-repo \{ grid-column: 2; grid-row: 2;/);
  assert.match(css, /\.phone-class \{ grid-column: 3; grid-row: 2;/);
  assert.match(css, /\.phone-counter \{[^}]*grid-column: 4;[^}]*grid-row: 2;/);
  // The title keeps its single-line ellipsis, now with the whole row to use.
  assert.match(css, /\.phone-title-link \{ display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; \}/);
  // DOM order matches the visual row 2: repository, class chip, counter last.
  const phone = app.slice(app.indexOf("function phoneRow("), app.indexOf("function renderPhoneBoard("));
  assert.ok(phone.indexOf('"phone-repo"') < phone.indexOf('"phone-class"'));
  assert.ok(phone.indexOf('"phone-class"') < phone.indexOf('"phone-counter"'));
});

test("the narrow brief stays ordered, single-column, and legend-visible", async () => {
  const app = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
  const css = await readFile(new URL("../public/styles.css", import.meta.url), "utf8");
  assert.match(app, /element\("div", "brief-sections"\)/);
  assert.ok(
    app.indexOf('"Decisions waiting on you"') < app.indexOf('"Actions waiting on you"'),
  );
  assert.match(css, /@media \(max-width: 900px\) \{[\s\S]*\.legend \{[\s\S]*display: flex;/);
  assert.match(css, /@media \(max-width: 600px\) \{[\s\S]*\.brief-sections \{[\s\S]*grid-template-columns: minmax\(0, 1fr\);/);
  // Chips keep their natural width and share a line (#989).
  const narrow = css.slice(css.indexOf("@media (max-width: 600px)"));
  assert.match(narrow, /\.waiting-row \{\s+flex-wrap: wrap;/);
  assert.match(narrow, /\.waiting-title \{ flex: 1 0 100%;/);
  assert.match(narrow, /\.waiting-row \.chip \{\s+flex: 0 1 auto;/);
  assert.doesNotMatch(narrow, /\.waiting-row \{[^}]*display: grid;/);
});


test("the bar uses the producer's progress order, and the rows keep queue order", async () => {
  const source = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
  assert.match(source, /item\.pips/);
  // Still no client-side ordering: the producer sends both orders.
  assert.doesNotMatch(withoutRepoOptions(source), /\.(?:sort|filter|reverse)\s*\(/);
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

test("a blocked ticket always says what it waits on", async () => {
  const source = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
  assert.match(source, /blocked by \$\{names/);
  assert.match(source, /blocked until \$\{ticket\.blocked_until\}/);
  assert.match(source, /blocked, no reason recorded/);
});

test("a blocked project says so on its own row, wide and narrow", async () => {
  const source = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
  const project = source.slice(source.indexOf("function projectRow("));
  assert.match(project, /if \(item\.blocked\) title\.append\(blockedChip\(item\)\)/);
  const phone = source.slice(
    source.indexOf("function phoneDetails("), source.indexOf("function phoneRow("),
  );
  assert.equal((phone.match(/phoneField\("Blocked", blockedChip\(item\)\)/g) || []).length, 1);
  assert.match(phone, /phoneField\(label, blockedChip\(item\)\)/);
});

test("the phone detail view has no PR field and puts the short facts on one row (#990)", async () => {
  const source = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
  const css = await readFile(new URL("../public/styles.css", import.meta.url), "utf8");
  const phone = source.slice(
    source.indexOf("function phoneDetails("), source.indexOf("function phoneRow("),
  );
  assert.doesNotMatch(phone, /"PR"/);
  assert.equal((phone.match(/phoneMeta\(\[/g) || []).length, 2);
  assert.match(phone, /\["Tier", [\s\S]*\["Next step", [\s\S]*\["Updated", /);
  assert.match(css, /\.phone-meta \{\s+display: flex;\s+flex-wrap: wrap;/);
});

test("actions show how long they have waited, like decisions do", async () => {
  const source = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
  assert.match(source, /step\.waited/);
});

test("an action row names the repository without its ticket number", async () => {
  const source = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
  assert.match(source, /String\(step\.ref \|\| ""\)\.split\("#"\)\[0\]/);
});

test("the phone Class chip is appended as a node, never stringified (#988)", async () => {
  const source = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
  assert.doesNotMatch(source, /element\([^)]*phoneClass\(/);
  assert.match(source, /classCell\.append\(phoneClass\(className\)\)/);
  assert.match(source, /text instanceof Node\) node\.append\(text\)/);
});

test("the usage line shows rolling 7-day Muse spend against the cap", () => {
  assert.equal(
    museUsageText({ spent_dollars: 0.704, cap_dollars: 20.0, used_percent: 3.52 }),
    "Muse 7-day spend $0.70 of $20.00 (3.5%)",
  );
  assert.equal(museUsageText(null), null);
  assert.equal(museUsageText({}), null);
  assert.equal(
    museUsageText({ spent_dollars: "0.70", cap_dollars: 20.0, used_percent: 3.5 }),
    null,
  );
});

test("Muse account-panel usage shows its validated value, source and original sample age", () => {
  const sample = {
    used_percent: 15,
    sampled_at: "2026-09-29T20:55:00-07:00",
    source: "Muse account panel",
    source_url: "https://github.com/nateprich-projects/command-center/issues/1673#issuecomment-5903766309",
  };
  const now = Date.parse("2026-09-29T21:40:00-07:00");
  assert.deepEqual(musePanelUsage(sample, now), {
    state: "live",
    percent: 15,
    source: "Muse account panel",
    sourceUrl: sample.source_url,
    sampledAt: sample.sampled_at,
    ageText: "45m old",
  });

  const container = renderUsageFixture({ muse_panel: sample }, now);
  const row = usageRow(container, "usage-muse-panel");
  assert.equal(row.querySelector(".usage-percent").textContent, "15%");
  assert.equal(row.querySelector(".usage-sampled-at").textContent,
    "Sampled 2026-09-29T20:55:00-07:00");
  assert.equal(row.querySelector(".usage-source").textContent, "Muse account panel");
  assert.equal(row.querySelector(".usage-source").attributes.get("href"), sample.source_url);
  assert.equal(row.querySelector(".usage-age").textContent, "45m old");
  assert.ok(row.querySelector(".usage-fill"));
  assert.equal(row.querySelector(".usage-refresh").attributes.get("href"),
    "https://github.com/nateprich-projects/command-center/issues/2123");
});

test("Muse account-panel usage hides a stale value and keeps its source timestamp and age", () => {
  const sample = {
    used_percent: 15,
    sampled_at: "2026-09-29T20:55:00-07:00",
    source: "Muse account panel",
    source_url: "https://github.com/nateprich-projects/command-center/issues/1673#issuecomment-5903766309",
  };
  const now = Date.parse("2026-09-29T22:26:00-07:00");
  assert.equal(musePanelUsage(sample, now).state, "stale");

  const row = usageRow(renderUsageFixture({ muse_panel: sample }, now), "usage-muse-panel");
  assert.match(row.textContent, /Stale/);
  assert.equal(row.querySelector(".usage-percent"), null);
  assert.equal(row.querySelector(".usage-fill"), null);
  assert.equal(row.querySelector(".usage-source").textContent, "Muse account panel");
  assert.equal(row.querySelector(".usage-sampled-at").textContent,
    "Sampled 2026-09-29T20:55:00-07:00");
  assert.equal(row.querySelector(".usage-age").textContent, "1h old");
});

test("Muse panel usage is unavailable without a validated source reading", () => {
  assert.deepEqual(musePanelUsage(null), { state: "unavailable" });
  const row = usageRow(renderUsageFixture({}, Date.parse("2026-10-01T19:00:00Z")),
    "usage-muse-panel");
  assert.match(row.textContent, /Unavailable/);
  assert.equal(row.querySelector(".usage-percent"), null);
  assert.equal(row.querySelector(".usage-fill"), null);
  assert.equal(row.querySelector(".usage-sampled-at"), null);
  assert.equal(row.querySelector(".usage-refresh").attributes.get("href"),
    "https://github.com/nateprich-projects/command-center/issues/2123");
});

test("the Muse derived-spend estimate labels its journal source and sample age", () => {
  const now = Date.parse("2026-09-29T21:40:00-07:00");
  const estimate = {
    source: "Local Muse session journal estimate",
    captured_at: Date.parse("2026-09-29T20:55:00-07:00") / 1000,
    spent_dollars: 14.3,
    cap_dollars: 200,
    used_percent: 7.15,
    calls: 42,
  };
  assert.deepEqual(museEstimate(estimate, now), {
    state: "live",
    source: "Local Muse session journal estimate",
    sampledAt: new Date(estimate.captured_at * 1000).toISOString(),
    ageText: "45m old",
    spent: 14.3,
    cap: 200,
    percent: 7.15,
  });

  const row = usageRow(renderUsageFixture({ muse: estimate }, now), "usage-muse-estimate");
  assert.match(row.querySelector(".usage-label").textContent, /estimate/i);
  assert.equal(row.querySelector(".usage-source").textContent,
    "Local Muse session journal estimate");
  assert.equal(row.querySelector(".usage-sampled-at").textContent,
    "Sampled 2026-09-30T03:55:00.000Z");
  assert.equal(row.querySelector(".usage-age").textContent, "45m old");

  const stale = museEstimate(estimate, Date.parse("2026-09-29T22:26:00-07:00"));
  assert.equal(stale.state, "stale");
  const staleRow = usageRow(renderUsageFixture({ muse: estimate },
    Date.parse("2026-09-29T22:26:00-07:00")), "usage-muse-estimate");
  assert.match(staleRow.textContent, /Stale/);
  assert.equal(staleRow.querySelector(".usage-percent"), null);
  assert.equal(staleRow.querySelector(".usage-source").textContent,
    "Local Muse session journal estimate");
});

test("Claude weekly usage shows the exact percentage and recomputed sample age", () => {
  const now = Date.parse("2026-10-01T19:00:00Z");
  const sample = new Date(now - 45 * 60 * 1000).toISOString();
  assert.deepEqual(
    claudeUsage({ u: { sd: 42.375 }, t: sample }, now),
    { state: "live", percent: 42.375, ageText: "45m old" },
  );

  const container = new TestNode("div");
  const previousDocument = globalThis.document;
  globalThis.document = {
    createElement(tagName) { return new TestNode(tagName); },
    querySelector(selector) {
      assert.equal(selector, "#usage");
      return container;
    },
  };
  try {
    renderUsage({ claude: { u: { sd: 42.375 }, t: sample } }, now);
    const row = usageRow(container, "usage-claude");
    assert.equal(row.querySelector(".usage-percent").textContent, "42.375%");
    assert.equal(row.querySelector(".usage-age").textContent, "45m old");
    assert.match(row.textContent, /Claude weekly usage/);

    renderUsage({ claude: { u: { sd: 42.375 }, t: sample } }, now + 30 * 60 * 1000);
    assert.equal(usageRow(container, "usage-claude").querySelector(".usage-age").textContent,
      "1h old");

    renderUsage({ claude: { u: { sd: 42.375 }, t: now / 1000 - 91 * 60 } }, now);
    const staleRow = usageRow(container, "usage-claude");
    assert.match(staleRow.textContent, /Stale/);
    assert.equal(staleRow.querySelector(".usage-percent"), null);

    renderUsage({}, now);
    const unavailableRow = usageRow(container, "usage-claude");
    assert.match(unavailableRow.textContent, /Unavailable/);
    assert.equal(unavailableRow.querySelector(".usage-fill"), null);
  } finally {
    if (previousDocument === undefined) delete globalThis.document;
    else globalThis.document = previousDocument;
  }
});

test("Claude weekly usage is live through 90 minutes, then stale without a percentage", () => {
  const now = Date.parse("2026-10-01T19:00:00Z");
  const atLimit = (now - 90 * 60 * 1000) / 1000;
  assert.equal(claudeUsage({ u: { sd: 42.375 }, t: atLimit }, now).state, "live");
  assert.deepEqual(
    claudeUsage({ u: { sd: 42.375 }, t: atLimit - 1 }, now),
    { state: "stale", ageText: "1h old" },
  );
});

test("Claude weekly usage is unavailable when its provider fields are missing or malformed", () => {
  const now = Date.parse("2026-10-01T19:00:00Z");
  const validTime = new Date(now).toISOString();
  assert.deepEqual(claudeUsage(null, now), { state: "unavailable" });
  assert.deepEqual(claudeUsage({ u: {}, t: validTime }, now), { state: "unavailable" });
  assert.deepEqual(claudeUsage({ u: { sd: "42" }, t: validTime }, now), { state: "unavailable" });
  assert.deepEqual(claudeUsage({ u: { sd: 42 }, t: "bad timestamp" }, now), { state: "unavailable" });
});

test("same-timestamp snapshots re-render when the published Claude sample changes", () => {
  const previous = {
    generated_at: "2026-10-01T19:00:00Z",
    usage: { claude: { u: { sd: 42 }, t: "2026-10-01T18:30:00Z" } },
  };
  assert.equal(claudeUsageChanged(previous.usage.claude, {
    u: { sd: 43 }, t: previous.usage.claude.t,
  }), true);
  assert.equal(claudeUsageChanged(previous.usage.claude, {
    u: previous.usage.claude.u, t: "2026-10-01T18:31:00Z",
  }), true);
  assert.equal(snapshotNeedsRender(previous, {
    generated_at: previous.generated_at,
    usage: { claude: { u: { sd: 43 }, t: previous.usage.claude.t } },
  }, previous.generated_at), true);
  assert.equal(snapshotNeedsRender(previous, {
    generated_at: previous.generated_at,
    usage: { claude: { u: { sd: 42 }, t: previous.usage.claude.t } },
  }, previous.generated_at), false);
});

test("the usage line renders from the snapshot root, with no 24-hour companion", async () => {
  const source = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
  const html = await readFile(new URL("../public/index.html", import.meta.url), "utf8");
  const start = source.indexOf("function renderUsage(");
  const usage = source.slice(start, source.indexOf("\n}\n", start) + 2);
  assert.match(source, /renderUsage\(snapshot\.usage/);
  assert.match(html, /<div id="usage"><\/div>/);
  assert.doesNotMatch(usage, /five_hour/);
  assert.doesNotMatch(usage, /24-hour/);
});

test("the fixture's usage row renders as the spend line", async () => {
  const snapshot = JSON.parse(await readFile(
    new URL("../fixtures/snapshot.json", import.meta.url), "utf8",
  ));
  assert.equal(
    museUsageText(snapshot.usage.muse),
    "Muse 7-day spend $0.70 of $20.00 (3.5%)",
  );
});

test("the rendered phone board has no object text and every row has a title (#1004)", async () => {
  const snapshot = JSON.parse(await readFile(
    new URL("../fixtures/snapshot.json", import.meta.url), "utf8",
  ));
  const previousDocument = globalThis.document;
  globalThis.document = new TestDocument();
  try {
    const board = renderPhoneBoard(boardColumns(snapshot.board));
    const rows = board.querySelectorAll(".phone-row");

    assert.ok(rows.length > 0, "fixture should render at least one phone row");
    for (const node of board.walk()) {
      assert.doesNotMatch(node.textContent, /\[object/);
    }
    for (const row of rows) {
      assert.ok(row.querySelector(".phone-title-link")?.textContent.trim());
    }
  } finally {
    if (previousDocument === undefined) delete globalThis.document;
    else globalThis.document = previousDocument;
  }
});

test("only a ticket has a phone state; a project row has no number and no state", () => {
  assert.equal(phoneState({ number: 67, state: "OPEN" }), "open");
  assert.equal(phoneState({ number: 65, state: "CLOSED" }), "closed");
  assert.equal(phoneState({ number: 66, state: "OPEN", pr: "submitted" }), "submitted");
  // A project row carries neither, and pipState() would read its missing
  // state as closed and strike the whole project through.
  assert.equal(phoneState({ ref: "command-center#643", title: "Dashboard" }), null);
});

test("phone ticket rows carry a coloured pip and read as finished when closed", async () => {
  const css = await readFile(new URL("../public/styles.css", import.meta.url), "utf8");
  const narrow = css.slice(css.indexOf("@media (max-width: 600px)"));
  assert.match(narrow, /\.phone-title \{ display: flex;/);
  assert.match(narrow, /\.phone-row-closed > \.phone-summary \.phone-title \{ font-weight: 400; \}/);
  assert.match(narrow, /\.phone-row-closed > \.phone-summary \.phone-title-link \{[^}]*line-through/);
  assert.match(narrow, /\.phone-row-blocked > \.phone-summary \.phone-title-link \{ color: var\(--blocked-title\); \}/);

  const previousDocument = globalThis.document;
  globalThis.document = new TestDocument();
  try {
    const board = renderPhoneBoard([{
      stage: "Building",
      items: [{
        ref: "jeffy-finance-agent#62",
        title: "Retire the cutover ceremony",
        class: "Broken",
        tickets_closed: 1,
        tickets_total: 2,
        tickets: [
          { number: 65, title: "Retire cutover ceremony trees", state: "CLOSED" },
          { number: 67, title: "Implement the silence watchdog", state: "OPEN" },
        ],
      }],
    }]);
    const rows = board.querySelectorAll(".phone-row");
    assert.equal(rows.length, 3);
    const [project, closed, open] = rows;

    // The project row keeps its chevron and its progress bar; only tickets
    // carry a single state pip, exactly as on the wide board.
    assert.equal(project.querySelector(".phone-title").querySelector(".pip"), null);
    assert.ok(!project.className.includes("phone-row-closed"));

    assert.ok(closed.className.split(/\s+/).includes("phone-row-closed"));
    assert.ok(closed.querySelector(".phone-title").querySelector(".pip-closed"));
    assert.ok(open.className.split(/\s+/).includes("phone-row-open"));
    assert.ok(open.querySelector(".phone-title").querySelector(".pip-open"));
  } finally {
    if (previousDocument === undefined) delete globalThis.document;
    else globalThis.document = previousDocument;
  }
});

test("the Execution headline renders six R7/R28 tiles and keeps missing data as a gap", async () => {
  const fixture = JSON.parse(await readFile(
    new URL("../fixtures/execution_metrics.json", import.meta.url), "utf8",
  ));
  const previousDocument = globalThis.document;
  globalThis.document = new TestDocument();
  try {
    const grid = new TestNode("div");
    renderExecutionTiles(fixture, grid);
    const tiles = grid.querySelectorAll(".metric-tile");

    assert.equal(tiles.length, 6);
    assert.deepEqual(
      tiles.map((tile) => tile.attributes.get("data-metric")),
      ["A1", "A2", "C3", "C4", "D6", "E4"],
    );
    for (const tile of tiles) {
      assert.match(tile.textContent, /R7/);
      assert.match(tile.textContent, /R28/);
      assert.match(tile.textContent, /Delta/);
    }

    assert.match(tiles[2].textContent, /14\.3%/);
    assert.match(tiles[2].textContent, /-0\.7 pp/);
    assert.equal(tiles[2].querySelectorAll(".metric-gap").length, 3);
    assert.ok(tiles[2].querySelectorAll(".metric-series-label")
      .some((label) => label.textContent === "muse"));
    assert.match(tiles[3].textContent, /0\.1/);
    assert.match(tiles[4].textContent, /\+0\.03 h/);
  } finally {
    if (previousDocument === undefined) delete globalThis.document;
    else globalThis.document = previousDocument;
  }
});

test("the Budget panel renders D1-D6, marks Muse pace resets, and explains the D4 gap", async () => {
  const [fixtureText, html] = await Promise.all([
    readFile(new URL("../fixtures/execution_metrics.json", import.meta.url), "utf8"),
    readFile(new URL("../public/index.html", import.meta.url), "utf8"),
  ]);
  const fixture = JSON.parse(fixtureText);
  const previousDocument = globalThis.document;
  globalThis.document = new TestDocument();
  try {
    const grid = new TestNode("div");
    renderBudgetMetrics(fixture, grid);
    const cards = grid.querySelectorAll(".budget-tile");
    assert.deepEqual(cards.map((card) => card.attributes.get("data-metric")),
      ["D1", "D2", "D3", "D4", "D5", "D6"]);

    const muse = cards[0];
    assert.ok(muse.querySelectorAll(".chart-band").length > 0);
    assert.ok(muse.querySelectorAll(".chart-reset-marker").length > 0);
    assert.match(muse.textContent, /Next window reset/);

    assert.match(cards[1].textContent, /funnel share/i);
    assert.match(cards[1].textContent, /personal share/i);
    assert.match(cards[2].textContent, /Five-hour window/);
    assert.match(cards[2].textContent, /Seven-day window/);

    const cost = cards[3];
    assert.match(cost.textContent, /Notional API cost per merged PR by lane/i);
    assert.equal(cost.querySelectorAll(".metric-gap").length, 3);
    assert.match(cost.textContent, /complete priced cost/);
    assert.match(cards[4].textContent, /points per run/i);
    assert.match(cards[4].textContent, /resend ratio/i);
    assert.match(cards[5].textContent, /api reserve/i);

    assert.match(html, /Muse’s ChatGPT-side usage and Claude’s claude\.ai usage are invisible/);
    assert.match(html, /id="budget-grid"/);

    const estimateStatus = (value) => ({
      kind: "category",
      daily: Array(fixture.days.length).fill(null).map((item, index) => (
        index === fixture.days.length - 1 ? value : item
      )),
      r7: Array(fixture.days.length).fill(null),
      r28: Array(fixture.days.length).fill(null),
      delta: Array(fixture.days.length).fill(null),
    });
    for (const [value, label] of [
      [true, /Five-hour window \(estimated\)/],
      [false, /Five-hour window \(authoritative app reading\)/],
    ]) {
      fixture.metrics.D.D3.estimated = estimateStatus(value);
      renderBudgetMetrics(fixture, grid);
      const claude = grid.querySelectorAll(".budget-tile")[2];
      assert.match(claude.textContent, label);
      assert.match(claude.textContent, value
        ? /Seven-day window \(estimated\)/
        : /Seven-day window \(authoritative app reading\)/);
    }
  } finally {
    if (previousDocument === undefined) delete globalThis.document;
    else globalThis.document = previousDocument;
  }
});


test("the Runs panel renders C1-C6 by agent and job and preserves their gaps", async () => {
  const fixture = JSON.parse(await readFile(
    new URL("../fixtures/execution_metrics.json", import.meta.url), "utf8",
  ));
  const previousDocument = globalThis.document;
  globalThis.document = new TestDocument();
  try {
    const grid = new TestNode("div");
    renderRunMetrics(fixture, grid);
    const panels = grid.querySelectorAll(".run-metric-panel");
    assert.deepEqual(
      panels.map((panel) => panel.attributes.get("data-metric")),
      ["C1", "C2", "C3", "C4", "C5", "C6"],
    );

    for (const panel of panels) {
      const paths = panel.querySelectorAll(".run-series-row")
        .map((row) => row.attributes.get("data-series"));
      assert.ok(paths.length > 0, panel.attributes.get("data-metric") + " has series");
      assert.ok(paths.every((path) => !path.endsWith(".finishes")));
      for (const pair of ["codex.implement", "muse.review", "claude.shape", "claude.breakdown"]) {
        assert.ok(paths.some((path) => path.includes(pair)),
          panel.attributes.get("data-metric") + " renders " + pair);
      }
      assert.ok(panel.querySelectorAll(".metric-reading").some((reading) => (
        reading.textContent.includes("R7")
      )));
    }

    const fires = fixture.metrics.C.C1.by_agent_and_job.codex.implement;
    const errorRate = fixture.metrics.C.C3.error_rate_by_agent_and_job.codex.implement;
    const skippedDay = fires["skipped-over-pace"].daily.findIndex((value) => value > 0);
    assert.ok(skippedDay >= 0);
    assert.equal(
      errorRate.denominators[skippedDay],
      fires.done.daily[skippedDay] + fires.errored.daily[skippedDay],
    );
    assert.ok(errorRate.denominators[skippedDay] < fires.finishes.daily[skippedDay]);
    assert.match(panels[2].textContent, /skipped fires are excluded/);

    const c1Gap = panels[0].querySelectorAll(".run-series-row")
      .find((row) => row.attributes.get("data-series").endsWith("muse.review.done"));
    assert.ok(c1Gap);
    assert.ok(c1Gap.querySelectorAll(".chart-hit")
      .some((hit) => hit.textContent.includes("R7 Gap")));

    const c4Paths = panels[3].querySelectorAll(".run-series-row")
      .map((row) => row.attributes.get("data-series"));
    assert.ok(c4Paths.some((path) => path.endsWith(".floor")));
    assert.ok(c4Paths.some((path) => path.endsWith(".unclassified")));
    assert.match(panels[3].textContent, /unclassified errors stay separate/);
  } finally {
    if (previousDocument === undefined) delete globalThis.document;
    else globalThis.document = previousDocument;
  }
});

test("the Quality panel renders B1, B2 and B4, and B3 shows its gap until an hour measures it", async () => {
  const [seriesText, snapshotText, html] = await Promise.all([
    readFile(new URL("../fixtures/execution_metrics.json", import.meta.url), "utf8"),
    readFile(new URL("../../tests/fixtures/metrics_snapshot.json", import.meta.url), "utf8"),
    readFile(new URL("../public/index.html", import.meta.url), "utf8"),
  ]);
  const series = JSON.parse(seriesText);
  const snapshot = JSON.parse(snapshotText);
  const rework = snapshot.brief.outcome_signals.signals.rework_rate;
  const expectedRework = rework.rework_attempts / rework.merged_prs;
  const previousDocument = globalThis.document;
  globalThis.document = new TestDocument();
  try {
    const grid = new TestNode("div");
    renderQualityMetrics(series, grid);
    const panels = grid.querySelectorAll(".run-metric-panel");
    assert.deepEqual(
      panels.map((panel) => panel.attributes.get("data-metric")),
      ["B1", "B2", "B3", "B4"],
    );

    assert.match(panels[0].textContent, /First-pass approval/);
    assert.match(panels[0].textContent, /100\.0%/);
    assert.match(panels[1].textContent, /25\.0%/);
    const b2Series = series.metrics.B.B2;
    const last = series.days.length - 1;
    assert.equal(b2Series.r7[last], expectedRework);
    assert.equal(b2Series.r28[last], expectedRework);

    const b3 = panels[2];
    assert.match(b3.textContent, /mostly written by another Broken project's fix/);
    assert.match(b3.textContent, /No reading yet: no hour has measured fix recurrence\./);
    assert.equal(b3.querySelectorAll(".metric-reading").length, 0);
    assert.equal(b3.querySelectorAll(".metric-chart").length, 0);
    assert.equal(series.metrics.B.B3, undefined);

    const b4Rows = panels[3].querySelectorAll(".run-series-row");
    assert.deepEqual(
      b4Rows.map((row) => row.querySelector(".run-series-label").textContent),
      ["infra", "real"],
    );
    assert.equal(b4Rows.length, 2);
    assert.match(panels[3].textContent, /current state, not incident history/);

    assert.ok(html.indexOf('id="quality-grid"') < html.indexOf('id="runs-grid"'));
    assert.ok(html.indexOf('id="runs-grid"') < html.indexOf('id="budget-grid"'));
  } finally {
    if (previousDocument === undefined) delete globalThis.document;
    else globalThis.document = previousDocument;
  }
});

test("the Attention panel renders E1-E5 with gaps and no alert styling", async () => {
  const fixture = JSON.parse(await readFile(
    new URL("../fixtures/execution_metrics.json", import.meta.url), "utf8",
  ));

  const previousDocument = globalThis.document;
  globalThis.document = new TestDocument();
  try {
    const grid = new TestNode("div");
    renderAttentionMetrics(fixture, grid);
    const panels = grid.querySelectorAll(".run-metric-panel");
    assert.deepEqual(
      panels.map((panel) => panel.attributes.get("data-metric")),
      ["E1", "E2", "E3", "E4", "E5"],
    );

    for (const panel of panels) {
      const rows = panel.querySelectorAll(".run-series-row");
      assert.ok(rows.length > 0, panel.attributes.get("data-metric") + " has series");
      assert.ok(rows.every((row) => row.querySelectorAll(".metric-chart").length === 1));
      assert.ok(rows.some((row) => row.querySelectorAll(".chart-hit")
        .some((hit) => hit.textContent.includes("R7 Gap"))),
      panel.attributes.get("data-metric") + " preserves chart gaps");
      assert.ok(panel.querySelectorAll(".metric-reading")
        .some((reading) => reading.textContent.includes("R7")));
      assert.equal(panel.querySelectorAll(".alert").length, 0);
      assert.equal(panel.querySelectorAll(".threshold").length, 0);
      assert.equal(panel.querySelectorAll(".target").length, 0);
      assert.ok([...panel.walk()].every((node) => node.attributes.get("role") !== "alert"));
    }

    assert.match(panels[0].textContent, /Waiting on Nate/);
    assert.match(panels[0].textContent, /Gate dwell · Shaped/);
    assert.match(panels[0].textContent, /Gate dwell · Ready/);
    assert.match(panels[0].textContent, /\d+\.\d h/);
    assert.match(panels[1].textContent, /Opened per day/);
    assert.match(panels[1].textContent, /Outstanding now/);
    assert.match(panels[2].textContent, /Approvals per day/);
    assert.match(panels[2].textContent, /Merges per day/);
    assert.match(panels[3].textContent, /Actions per day/);
    assert.match(panels[4].textContent, /Stale locks taken over/);
    assert.match(panels[1].textContent, /R7Gap/);
  } finally {
    if (previousDocument === undefined) delete globalThis.document;
    else globalThis.document = previousDocument;
  }
});

test("the Churn panel separates repositories, keeps gaps, and pairs brief cost measures", async () => {
  const [fixtureText, html] = await Promise.all([
    readFile(new URL("../fixtures/execution_metrics.json", import.meta.url), "utf8"),
    readFile(new URL("../public/index.html", import.meta.url), "utf8"),
  ]);
  const fixture = JSON.parse(fixtureText);
  const previousDocument = globalThis.document;
  globalThis.document = new TestDocument();
  try {
    const grid = new TestNode("div");
    renderChurnMetrics(fixture, grid);
    const panels = grid.querySelectorAll(".run-metric-panel");
    assert.deepEqual(panels.map((panel) => panel.attributes.get("data-metric")),
      ["F1", "F2", "F3"]);

    const commits = panels[0].querySelectorAll(".run-series-row");
    assert.deepEqual(commits.map((row) => row.querySelector(".run-series-label").textContent),
      ["Command Center", "Member repositories"]);
    assert.ok(commits[0].querySelectorAll(".chart-hit")
      .some((hit) => hit.textContent.includes("R7 Gap")));
    assert.ok(commits[1].querySelectorAll(".chart-hit")
      .some((hit) => hit.textContent.includes("R7 Gap")));

    const lineCount = panels[1].querySelector(".run-series-row");
    assert.equal(lineCount.querySelector(".run-series-label").textContent, "Lines on main");
    assert.ok(lineCount.querySelectorAll(".chart-hit")
      .some((hit) => hit.textContent.includes("R7 Gap")));
    assert.ok(lineCount.querySelectorAll(".chart-line").length > 1,
      "the line count chart breaks at the fixture gap");

    const briefCost = panels[2].querySelectorAll(".run-series-row");
    assert.deepEqual(briefCost.map((row) => row.querySelector(".run-series-label").textContent),
      ["Project load (seconds)", "Degraded sections per run"]);
    for (const row of briefCost) {
      assert.ok(row.querySelectorAll(".chart-hit")
        .some((hit) => hit.textContent.includes("R7 Gap")));
      assert.ok(row.querySelectorAll(".metric-reading").some((reading) => (
        reading.textContent.includes("R28")
      )));
    }
    assert.match(html, /<div id="churn-grid" class="run-metric-grid"><\/div>/);
  } finally {
    if (previousDocument === undefined) delete globalThis.document;
    else globalThis.document = previousDocument;
  }
});

test("Output renders A1-A6 from metrics.py series paths", async () => {
  const fixture = JSON.parse(await readFile(
    new URL("../fixtures/execution_metrics.json", import.meta.url), "utf8",
  ));
  const previousDocument = globalThis.document;
  globalThis.document = new TestDocument();
  try {
    assert.equal(fixture.fixture_evidence.a_series_source, "metrics.series_from_rows");
    const grid = new TestNode("div");
    renderOutputPanel(fixture, grid);
    const tiles = grid.querySelectorAll(".output-metric");
    assert.deepEqual(
      tiles.map((tile) => tile.attributes.get("data-metric")),
      ["A1", "A2", "A3", "A4", "A5", "A6"],
    );
    for (const tile of tiles) {
      assert.ok(tile.querySelectorAll(".metric-chart").length > 0);
      assert.match(tile.textContent, /R7/);
      assert.match(tile.textContent, /R28/);
      assert.match(tile.textContent, /Delta/);
    }

    const a1 = fixture.metrics.A.A1;
    assert.equal(tiles[0].querySelectorAll(".metric-chart").length,
      1 + Object.keys(a1.by_repo).length);
    assert.ok(tiles[0].querySelectorAll(".metric-series-label")
      .some((label) => label.textContent === "command-center"));
    const repoDailyTotal = Object.values(a1.by_repo)
      .reduce((sum, repo) => sum + repo.daily[40], 0);
    assert.equal(repoDailyTotal, a1.total.daily[40]);

    const a4 = fixture.metrics.A.A4;
    assert.ok(a4.new_projects_started.daily.some(Number.isFinite));
    assert.ok(a4.days_since_last_new_started.daily.some(Number.isFinite));
    assert.match(tiles[3].textContent, /Days since last New project entered Building/);
    assert.match(tiles[3].textContent, /6 days/);
    assert.doesNotMatch(tiles[3].textContent, /Days since last New project entered Building\s+Gap/);

    assert.ok(tiles[2].querySelectorAll(".chart-hit")
      .some((hit) => /R7 Gap/.test(hit.textContent)));

    const a6 = fixture.metrics.A.A6;
    const revertSeries = Object.values(a6.reverts_by_repo);
    assert.ok(revertSeries.length > 0);
    assert.ok(revertSeries.some((repo) => repo.daily.some(Number.isFinite)));
    assert.ok(a6.reopened_tickets.daily.some(Number.isFinite));
    assert.equal(tiles[5].querySelectorAll(".metric-chart").length,
      revertSeries.length + 1);

    const evidence = fixture.fixture_evidence.a3_denominator;
    const upkeep = fixture.metrics.A.A3;
    assert.ok(evidence.closed_tickets > evidence.closed_projects);
    assert.equal(upkeep.numerators[evidence.day_index], evidence.upkeep_projects);
    assert.equal(upkeep.denominators[evidence.day_index], evidence.closed_projects);
    assert.equal(
      upkeep.daily[evidence.day_index],
      evidence.upkeep_projects / evidence.closed_projects,
    );
    assert.notEqual(
      upkeep.daily[evidence.day_index],
      evidence.upkeep_projects / evidence.closed_tickets,
    );
  } finally {
    if (previousDocument === undefined) delete globalThis.document;
    else globalThis.document = previousDocument;
  }
});

test("Execution uses a read-only request and the two views route on the same page", async () => {
  const [html, source, fixtureText] = await Promise.all([
    readFile(new URL("../public/index.html", import.meta.url), "utf8"),
    readFile(new URL("../public/app.js", import.meta.url), "utf8"),
    readFile(new URL("../fixtures/execution_metrics.json", import.meta.url), "utf8"),
  ]);
  const fixture = JSON.parse(fixtureText);
  const requests = [];
  const result = await requestMetrics(async (url, options) => {
    requests.push({ url, options });
    return { ok: true, async json() { return fixture; } };
  });

  assert.equal(result.schema_version, 1);
  assert.deepEqual(requests, [{
    url: "/api/metrics",
    options: { cache: "no-store" },
  }]);
  assert.match(html, /<nav id="view-nav"[^>]*aria-label="Dashboard views"/);
  assert.match(html, /href="\/\?tab=execution" data-tab="execution"/);
  assert.match(html, /<main id="funnel-view">/);
  assert.match(html, /<main id="execution-view"[^>]*hidden>/);
  assert.match(html, /<div id="runs-grid" class="run-metric-grid"><\/div>/);
  assert.match(html, /<div id="attention-grid" class="run-metric-grid"><\/div>/);
  assert.ok(html.indexOf('id="runs-grid"') < html.indexOf('id="budget-grid"'));
  assert.ok(html.indexOf('id="budget-grid"') < html.indexOf('id="attention-grid"'));
  assert.match(html, /<div id="output-grid" class="output-grid"><\/div>/);
  assert.match(source, /renderOutputPanel\(series, output\)/);
  assert.equal(tabFromUrl("https://funnel.nateprich.com/?tab=execution&repo=owner%2Frepo"),
    "execution");
  assert.equal(tabFromUrl("https://funnel.nateprich.com/?tab=unknown"), "funnel");
  assert.equal(
    tabUrl("execution", "https://funnel.nateprich.com/?repo=owner%2Frepo"),
    "/?repo=owner%2Frepo&tab=execution",
  );
  assert.equal(
    tabUrl("funnel", "https://funnel.nateprich.com/?repo=owner%2Frepo&tab=execution"),
    "/?repo=owner%2Frepo",
  );
  assert.match(source, /fetch\("\/api\/snapshot", \{ cache: "no-store" \}\)/);
});

test("the panel chart breaks the R7 line at a gap and draws the R28 as a rule", () => {
  const days = Array.from({ length: 70 }, (_, index) =>
    "2026-07-" + String(index + 1).padStart(2, "0"));
  const r7 = days.map((_, index) => 1 + (index % 5));
  r7[60] = null; // a gap in the window
  r7[65] = null;
  r7[67] = null; // day 66 stands alone between two gaps
  const r28 = days.map(() => 2.5);
  const previousDocument = globalThis.document;
  globalThis.document = new TestDocument();
  try {
    const svg = renderMetricChart({ r7, r28, daily: [], delta: [] }, days, {
      title: "Tickets landed / day", format: "count",
    });

    assert.equal(svg.tagName, "svg");
    assert.equal(svg.namespaceURI, "http://www.w3.org/2000/svg");
    assert.equal(svg.attributes.get("role"), "img");

    // Only the newest 56 days are drawn, though the series keeps more.
    const hits = svg.querySelectorAll(".chart-hit");
    assert.equal(hits.length, CHART_WINDOW_DAYS);
    assert.match(hits[0].textContent, /^2026-07-15 /);

    // The gaps split the line into separate runs; nothing bridges or zeroes them.
    const lines = svg.querySelectorAll(".chart-line");
    assert.equal(lines.length, 3);
    for (const line of lines) {
      assert.match(line.attributes.get("d"), /^M[\d. L]+$/);
      assert.doesNotMatch(line.attributes.get("d"), /NaN/);
    }
    const baseline = svg.querySelector(".chart-axis").attributes.get("y1");
    for (const line of lines) {
      const ys = line.attributes.get("d").slice(1).split(" L")
        .map((pair) => pair.split(" ")[1]);
      assert.ok(ys.every((value) => Number(value) < Number(baseline)));
    }
    assert.ok(hits.some((hit) => /R7 Gap/.test(hit.textContent)));

    // The lone day is a dot, and the newest reading is direct-labelled.
    const dots = svg.querySelectorAll(".chart-dot");
    assert.equal(dots.length, 2);
    assert.equal(svg.querySelector(".chart-end-label").textContent, "5.0");

    const rule = svg.querySelector(".chart-rule");
    assert.ok(rule);
    assert.equal(rule.attributes.get("y1"), rule.attributes.get("y2"));
    assert.equal(svg.querySelector(".chart-rule-label").textContent, "R28 2.5");

    // Everything is inline: no image, link or external reference.
    assert.equal(nodesByTag(svg, "image").length, 0);
    assert.ok([...svg.walk()].every((node) => !node.attributes.has("href")));
  } finally {
    if (previousDocument === undefined) delete globalThis.document;
    else globalThis.document = previousDocument;
  }
});

test("a panel chart with no readings renders an empty frame, never a zero line", () => {
  const days = ["2026-09-23", "2026-09-24"];
  const previousDocument = globalThis.document;
  globalThis.document = new TestDocument();
  try {
    const svg = renderMetricChart({ r7: [null, null], r28: [null, null] }, days);
    assert.equal(svg.querySelectorAll(".chart-line").length, 0);
    assert.equal(svg.querySelectorAll(".chart-dot").length, 0);
    assert.equal(svg.querySelector(".chart-rule"), null);
    assert.match(svg.attributes.get("aria-label"), /R7 Gap, R28 Gap/);
  } finally {
    if (previousDocument === undefined) delete globalThis.document;
    else globalThis.document = previousDocument;
  }
});

test("the chart has its own colour in both themes and makes no request", async () => {
  const [css, source] = await Promise.all([
    readFile(new URL("../public/styles.css", import.meta.url), "utf8"),
    readFile(new URL("../public/app.js", import.meta.url), "utf8"),
  ]);
  const light = css.slice(css.indexOf("@media (prefers-color-scheme: light)"));
  assert.match(css.slice(0, css.indexOf("@media")), /--chart-line: #3987e5;/);
  assert.match(light, /--chart-line: #2a78d6;/);
  const chart = source.slice(
    source.indexOf("const SVG_NS"), source.indexOf("function renderExecutionTiles("),
  );
  assert.doesNotMatch(chart, /fetch\(|XMLHttpRequest|<image|import\(/);
  assert.deepEqual(chart.match(/https?:\/\/[^"]+/g), ["http://www.w3.org/2000/svg"]);
});
