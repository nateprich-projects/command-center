// The dashboard renders exactly what the publisher sent, in the order it sent
// it: funnel.py owns ordering, and a second opinion here is how two views of
// the same board drift apart. Nate, 2026-09-15: the page carries the board and
// human steps, and no other brief section.


// Stages that open collapsed: finished and stopped work is reference, not
// the working board.
const COLLAPSED_STAGES = ["Parked", "Done"];

const STAGES = ["Ideas", "Shaped", "Ready", "Building", "Parked", "Done"];

const OWNER_CLASS = {
  Nate: "owner-nate",
  Claude: "owner-claude",
  Muse: "owner-muse",
  Codex: "owner-codex",
};

function present(value) {
  if (Array.isArray(value)) return value.length > 0;
  if (value && typeof value === "object") return Object.keys(value).length > 0;
  return value !== null && value !== undefined && value !== false && value !== "";
}

function age(timestamp, nowMs = Date.now()) {
  const then = Date.parse(timestamp);
  if (!Number.isFinite(then)) return "age unknown";
  const seconds = Math.max(0, Math.floor((nowMs - then) / 1000));
  if (seconds < 60) return `${seconds}s old`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m old`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h old`;
  return `${Math.floor(seconds / 86400)}d old`;
}

function boardColumns(board) {
  if (board && Array.isArray(board.columns)) return board.columns;

  const columns = STAGES.map((stage) => ({ stage, items: [] }));
  if (Array.isArray(board)) {
    for (const item of board) {
      const column = columns.find((candidate) => candidate.stage === item.status);
      if (column) column.items.push(item);
    }
    return columns;
  }
  if (board && typeof board === "object") {
    for (const column of columns) {
      if (Array.isArray(board[column.stage])) column.items = board[column.stage];
    }
  }
  return columns;
}

function shortRepo(value) {
  if (typeof value !== "string") return null;
  const pieces = value.split("/");
  return pieces[pieces.length - 1];
}

// A ticket's pip state, which is also its row flag: closed work is solid, a PR
// waiting on the reviewer is light blue, an approved PR light purple.
function pipState(ticket) {
  if (!ticket || typeof ticket !== "object") return "open";
  if (ticket.state !== "OPEN") return "closed";
  if (ticket.pr === "approved") return "approved";
  if (ticket.pr === "submitted" || ticket.pr === "merged") return "submitted";
  if (ticket.blocked) return "blocked";
  return "open";
}

// The row's PR flag is the furthest any of its tickets has travelled, so the
// board answers "is anything of this project in review" at a glance.
function rowPrState(tickets) {
  let found = null;
  for (const ticket of tickets || []) {
    if (ticket.pr === "approved") return "approved";
    if (ticket.pr === "submitted") found = "submitted";
  }
  return found;
}

function rowTier(tickets) {
  for (const ticket of tickets || []) {
    if (ticket.state === "OPEN" && ticket.tier === "escalated") return "escalated";
  }
  for (const ticket of tickets || []) {
    if (ticket.state === "OPEN") return "standard";
  }
  return null;
}

// Owners of open tickets, in the order the producer sent them, without
// duplicates: this is a summary of the rows below, never a re-ranking.
// The producer names the owner of the next step in the chain; the page shows
// that and nothing else (Nate, 2026-09-15).
function nextOwner(item) {
  if (item && item.next_owner) return item.next_owner;
  for (const ticket of (item && item.tickets) || []) {
    if (ticket.state === "OPEN" && ticket.owner) return ticket.owner;
  }
  return null;
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function link(text, url, className) {
  if (typeof url !== "string" || !url.startsWith("https://github.com/")) {
    return element("span", className, text);
  }
  const anchor = element("a", className, text);
  anchor.href = url;
  anchor.rel = "noreferrer";
  return anchor;
}

function chip(text, className, title) {
  const node = element("span", `chip ${className}`, text);
  if (title) node.title = title;
  return node;
}

function cell(className, node) {
  const wrap = element("div", `cell ${className}`);
  if (node) wrap.append(node);
  return wrap;
}

// One grid row, so every column lines up across groups and nesting depths.
function gridRow(tag, className) {
  return element(tag, `grid-row ${className}`);
}

function pips(tickets, closed, total) {
  const wrap = element("div", "pips");
  const bar = element("div", "pip-bar");
  const rows = Array.isArray(tickets) ? tickets : [];
  if (rows.length) {
    for (const ticket of rows) {
      const state = pipState(ticket);
      const pip = element("i", `pip pip-${state}`);
      pip.title = `#${ticket.number} ${ticket.title || ""} — ${state}`;
      bar.append(pip);
    }
  } else if (Number.isFinite(total)) {
    for (let index = 0; index < total; index += 1) {
      bar.append(element("i", `pip pip-${index < closed ? "closed" : "open"}`));
    }
  }
  if (bar.childElementCount) wrap.append(bar);
  if (Number.isFinite(closed) && Number.isFinite(total) && total > 0) {
    wrap.append(element("span", "pip-count", `${closed}/${total}`));
  }
  return wrap;
}

function prCell(state, number) {
  if (!state) return element("span", "muted", "—");
  return chip(state, `chip-pr chip-pr-${state}`, number ? `PR #${number}` : null);
}

// Escalated is the exception worth a chip; standard stays quiet text so a
// board of ordinary work does not read as a wall of badges.
function tierCell(tier) {
  if (!tier) return element("span", "muted", "—");
  if (tier === "standard") return element("span", "tier-standard", "standard");
  return chip(tier, `chip-tier chip-tier-${tier}`);
}

function ownerCell(owner) {
  if (!owner) return element("span", "muted", "—");
  return chip(owner, `chip-owner ${OWNER_CLASS[owner] || ""}`);
}

function headerRow() {
  const row = gridRow("div", "thead");
  row.append(element("div", "cell cell-twisty"));
  for (const [className, label] of [
    ["cell-title", "Title"],
    ["cell-repo", "Repository"],
    ["cell-pr", "PR"],
    ["cell-tier", "Tier"],
    ["cell-owner", "Next step"],
    ["cell-pips", "Sub-issues"],
    ["cell-class", "Class"],
    ["cell-age", "Updated"],
  ]) {
    row.append(element("div", `cell ${className}`, label));
  }
  return row;
}

function ticketRow(ticket) {
  const row = gridRow("div", `ticket ticket-${pipState(ticket)}`);
  row.append(cell("cell-twisty", element("i", `pip pip-${pipState(ticket)}`)));

  const title = element("div", "cell cell-title cell-child");
  title.append(element("span", "child-rule"));
  title.append(link(`#${ticket.number} ${ticket.title || ""}`, ticket.url, "ticket-title"));
  if (ticket.blocked) {
    const refs = Array.isArray(ticket.blockers) ? ticket.blockers : [];
    const names = refs.map((ref) => `#${String(ref).split("#").pop()}`);
    title.append(chip(
      names.length ? `blocked by ${names.join(", ")}` : "blocked",
      "chip-blocked",
      refs.join(", ") || null,
    ));
  }
  row.append(title);

  row.append(cell("cell-repo", element("span", "muted", "")));
  row.append(cell("cell-pr", prCell(ticket.pr, ticket.pr_number)));
  row.append(cell("cell-tier", tierCell(ticket.state === "OPEN" ? ticket.tier : null)));
  row.append(cell("cell-owner", ownerCell(ticket.owner)));
  row.append(cell("cell-pips", element("span", "muted", "")));
  row.append(cell("cell-class", element("span", "muted", "")));
  row.append(cell("cell-age", element("span", "muted", "")));
  return row;
}

// `?expand` opens every project's tickets on load: useful for a wide screen,
// and it is how this view is checked in a headless render.
const expanded = new Set();

function expandAll() {
  if (typeof window === "undefined" || !window.location) return false;
  return new URLSearchParams(window.location.search).has("expand");
}

function projectRow(item) {
  const tickets = Array.isArray(item.tickets) ? item.tickets : [];
  const wrap = element("div", "row-wrap");
  const row = gridRow("div", "row");

  const twisty = element("button", "twisty");
  twisty.type = "button";
  twisty.setAttribute("aria-expanded", "false");
  twisty.setAttribute("aria-label", `Show tickets for ${item.title || item.ref || "project"}`);
  twisty.textContent = tickets.length ? "\u25B8" : "";
  twisty.disabled = tickets.length === 0;
  row.append(cell("cell-twisty", twisty));

  const title = element("div", "cell cell-title");
  title.append(link(item.title || item.ref || "Untitled", item.url, "row-title"));
  if (item.pinned) title.append(chip("pinned", "chip-pin"));
  row.append(title);

  row.append(cell("cell-repo", element("span", "repo", shortRepo(item.repo || item.repository) || "")));
  row.append(cell("cell-pr", prCell(rowPrState(tickets))));
  row.append(cell("cell-tier", tierCell(rowTier(tickets))));
  row.append(cell("cell-owner", ownerCell(nextOwner(item))));
  row.append(cell("cell-pips", pips(tickets, item.tickets_closed, item.tickets_total)));
  row.append(cell("cell-class", item.class
    ? chip(item.class, `chip-class chip-class-${String(item.class).toLowerCase()}`)
    : element("span", "muted", "—")));
  row.append(cell("cell-age", element("span", "age", item.waited || "")));
  wrap.append(row);

  if (tickets.length) {
    const children = element("div", "children");
    const openAtRender = expandAll() || (item.ref && expanded.has(item.ref));
    children.hidden = !openAtRender;
    for (const ticket of tickets) children.append(ticketRow(ticket));
    wrap.append(children);
    const toggle = () => {
      const open = children.hidden;
      children.hidden = !open;
      twisty.setAttribute("aria-expanded", String(open));
      twisty.textContent = open ? "\u25BE" : "\u25B8";
      row.classList.toggle("expanded", open);
      if (item.ref) {
        if (open) expanded.add(item.ref);
        else expanded.delete(item.ref);
      }
    };
    // One handler on the row: the chevron is inside it, and a second handler
    // there would toggle twice and leave the row looking dead.
    row.addEventListener("click", (event) => {
      if (event.target.closest("a")) return;
      toggle();
    });
  }
  return wrap;
}

function renderBoard(board) {
  const container = document.querySelector("#board");
  container.replaceChildren();
  const table = element("div", "table");
  table.append(headerRow());
  let rendered = 0;
  for (const column of boardColumns(board)) {
    const items = Array.isArray(column.items) ? column.items : [];
    if (!items.length) continue;
    const collapsed = COLLAPSED_STAGES.includes(column.stage);
    const head = gridRow("button", "group-head");
    head.type = "button";
    head.setAttribute("aria-expanded", String(!collapsed));
    const label = element("div", "cell cell-group");
    label.append(element("span", "group-twisty", collapsed ? "\u25B8" : "\u25BE"));
    label.append(element("span", `dot dot-${column.stage.toLowerCase()}`));
    label.append(element("span", "group-name", column.stage));
    label.append(element("span", "count", items.length));
    head.append(label);
    table.append(head);

    const body = element("div", "group-body");
    body.hidden = collapsed;
    for (const item of items) {
      body.append(projectRow(item));
      rendered += 1;
    }
    table.append(body);
    head.addEventListener("click", () => {
      const open = body.hidden;
      body.hidden = !open;
      head.setAttribute("aria-expanded", String(open));
      label.firstChild.textContent = open ? "\u25BE" : "\u25B8";
    });
  }
  if (!rendered) {
    container.append(element("p", "empty", "The board is empty."));
    return;
  }
  container.append(table);
}

function decisionCard(item) {
  const card = element("article", "decision");
  card.append(link(item.title || item.ref || "Untitled", item.url, "decision-title"));
  const meta = element("p", "decision-meta");
  if (item.waiting_on) meta.append(chip(item.waiting_on, "chip-gate"));
  if (item.class) meta.append(chip(item.class, `chip-class chip-class-${String(item.class).toLowerCase()}`));
  if (item.pinned) meta.append(chip("pinned", "chip-pin"));
  meta.append(element("span", "decision-age", `${shortRepo(item.repo) || item.ref || ""} · ${item.waited || ""}`));
  card.append(meta);
  return card;
}

function humanStepRow(step) {
  const row = element("li", "human-step");
  row.append(link(step.title || step.ref || "Untitled", step.url, "human-step-title"));
  if (step.reason) row.append(chip(step.reason, "chip-reason"));
  return row;
}

function renderWaiting(brief) {
  const container = document.querySelector("#waiting");
  container.replaceChildren();
  const count = document.querySelector("#waiting-count");
  const total = brief.total_needing_nate;
  count.textContent = Number.isFinite(total) ? String(total) : "";

  if (total === 0) {
    container.append(document.querySelector("#empty-state").content.cloneNode(true));
  } else {
    const list = element("div", "decisions");
    for (const item of brief.items || []) list.append(decisionCard(item));
    container.append(list);
  }

  if (present(brief.human_steps)) {
    const section = element("div", "human-steps");
    section.append(element("h3", null, "Actions waiting on you"));
    const list = element("ul", null);
    for (const step of brief.human_steps) list.append(humanStepRow(step));
    section.append(list);
    container.append(section);
  }
}

function failureState(snapshot) {
  return snapshot.last_brief_failed ?? snapshot.status?.last_brief_failed ?? false;
}

let lastGeneratedAt = null;
let loading = false;

// The page polls its own snapshot: a published snapshot is a KV read through
// the Worker, so this costs no GitHub budget, and it re-renders only when the
// producer's timestamp actually moved. Without it an open tab showed whatever
// was current when it loaded (Nate, 2026-09-15).
const POLL_MS = 30000;

async function loadSnapshot({ force = false } = {}) {
  if (loading) return;
  loading = true;
  try {
    const response = await fetch("/api/snapshot", { cache: "no-store" });
    if (!response.ok) throw new Error(`Snapshot returned ${response.status}`);
    const snapshot = await response.json();
    const generatedAt = snapshot.generated_at || snapshot.brief?.generated_at;
    const status = document.querySelector("#snapshot-status");
    const failure = failureState(snapshot);
    status.textContent = generatedAt ? `Snapshot ${age(generatedAt)}` : "Snapshot age unknown";
    if (failure) status.textContent += " · last brief failed";
    status.classList.toggle("failed", Boolean(failure));
    if (!force && generatedAt && generatedAt === lastGeneratedAt) return;
    lastGeneratedAt = generatedAt || null;
    renderWaiting(snapshot.brief || {});
    renderBoard(snapshot.board || {});
  } finally {
    loading = false;
  }
}

function startPolling() {
  window.setInterval(() => {
    // A hidden tab is not being read; skip the request rather than poll a
    // background window every thirty seconds.
    if (document.visibilityState === "hidden") return;
    loadSnapshot().catch(() => {});
  }, POLL_MS);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") loadSnapshot().catch(() => {});
  });
}

async function requestRefresh() {
  const button = document.querySelector("#refresh");
  button.disabled = true;
  try {
    const response = await fetch("/api/refresh", { method: "POST" });
    if (!response.ok) throw new Error(`Refresh returned ${response.status}`);
    button.textContent = "Requested";
  } catch (error) {
    button.textContent = error.message;
  } finally {
    window.setTimeout(() => {
      button.disabled = false;
      button.textContent = "Refresh";
    }, 3000);
  }
}

if (typeof document !== "undefined") {
  document.querySelector("#refresh").addEventListener("click", requestRefresh);
  loadSnapshot({ force: true })
    .then(startPolling)
    .catch((error) => {
      const status = document.querySelector("#snapshot-status");
      status.textContent = error.message;
      status.classList.add("failed");
      startPolling();
    });
}

export { STAGES, age, boardColumns, failureState, nextOwner, pipState, rowPrState, rowTier, shortRepo };
