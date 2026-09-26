import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  STAGES, age, boardColumns, failureState, museUsageText, nextOwner, ownerCell,
  phoneState, pipState, projectBlocked, projectHold, holdChip, renderPhoneBoard, ticketHold, unblocksChip,
  repoLabels, repoOf,
  repoOptions, rowTier, shortRepo, visible, renderExecutionTiles, requestMetrics,
  renderMetricChart, CHART_WINDOW_DAYS,
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

test("a pip carries the ticket's furthest state", () => {
  assert.equal(pipState({ state: "CLOSED", pr: "merged" }), "closed");
  assert.equal(pipState({ state: "OPEN", pr: "approved" }), "approved");
  assert.equal(pipState({ state: "OPEN", pr: "changes requested" }), "changes-requested");
  assert.equal(pipState({ state: "OPEN", pr: "submitted" }), "submitted");
  assert.equal(pipState({ state: "OPEN", blocked: true }), "blocked");
  assert.equal(
    pipState({ state: "OPEN", blocked: true, blocked_by_siblings: true }), "queued",
  );
  assert.equal(pipState({ state: "OPEN" }), "open");
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

test("the usage line renders from the snapshot root, with no 24-hour companion", async () => {
  const source = await readFile(new URL("../public/app.js", import.meta.url), "utf8");
  const html = await readFile(new URL("../public/index.html", import.meta.url), "utf8");
  assert.match(source, /renderUsage\(snapshot\.usage/);
  assert.match(html, /<div id="usage"><\/div>/);
  assert.doesNotMatch(source, /five_hour/);
  assert.doesNotMatch(source, /24-hour/);
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
