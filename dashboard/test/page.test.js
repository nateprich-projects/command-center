import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  STAGES, age, boardColumns, failureState, nextOwner, pipState, renderPhoneBoard,
  rowPrState, rowTier, shortRepo,
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

test("the phone progress shows its count once (#994)", async () => {
  const source = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
  const progress = source.slice(
    source.indexOf("function phoneProgress("), source.indexOf("function phoneDetails("),
  );
  assert.match(progress, /pips\(item, children, closed, total\)/);
  assert.doesNotMatch(progress, /`\$\{closed\}\/\$\{total\}`\)\);/);
  assert.match(source, /element\("span", "pip-count", `\$\{closed\}\/\$\{total\}`\)/);
});

test("the page polls its own snapshot and re-renders only on a new timestamp", async () => {
  const source = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
  assert.match(source, /setInterval/);
  assert.match(source, /generatedAt === lastGeneratedAt/);
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
  assert.match(css, /grid-template-columns: 24px minmax\(0, 1fr\) minmax\(0, 76px\) auto auto;/);
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
  assert.equal((phone.match(/phoneField\("Blocked", blockedChip\(item\)\)/g) || []).length, 2);
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
