// The dashboard renders exactly what the publisher sent, in the order it sent
// it: funnel.py owns ordering, and a second opinion here is how two views of
// the same board drift apart. Nate, 2026-09-15: the page carries the board and
// human steps, and no other brief section.

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
function rowOwners(tickets) {
  const owners = [];
  for (const ticket of tickets || []) {
    if (ticket.state !== "OPEN" || !ticket.owner) continue;
    if (!owners.includes(ticket.owner)) owners.push(ticket.owner);
  }
  return owners;
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

function pips(tickets, closed, total) {
  const wrap = element("div", "pips");
  const rows = Array.isArray(tickets) ? tickets : [];
  if (rows.length) {
    for (const ticket of rows) {
      const state = pipState(ticket);
      const pip = element("i", `pip pip-${state}`);
      pip.title = `#${ticket.number} ${ticket.title || ""} — ${state}`;
      wrap.append(pip);
    }
  } else if (Number.isFinite(total)) {
    for (let index = 0; index < total; index += 1) {
      wrap.append(element("i", `pip pip-${index < closed ? "closed" : "open"}`));
    }
  }
  if (Number.isFinite(closed) && Number.isFinite(total) && total > 0) {
    wrap.append(element("span", "pip-count", `${closed}/${total}`));
  }
  return wrap;
}

function ticketRow(ticket) {
  const row = element("li", `ticket ticket-${pipState(ticket)}`);
  row.append(element("i", `pip pip-${pipState(ticket)}`));
  row.append(link(`#${ticket.number} ${ticket.title || ""}`, ticket.url, "ticket-title"));
  const flags = element("span", "ticket-flags");
  if (ticket.pr) {
    flags.append(chip(ticket.pr, `chip-pr chip-pr-${ticket.pr}`,
      ticket.pr_number ? `PR #${ticket.pr_number}` : null));
  }
  if (ticket.state === "OPEN" && ticket.tier === "escalated") {
    flags.append(chip("escalated", "chip-tier"));
  }
  if (ticket.blocked) flags.append(chip("blocked", "chip-blocked"));
  if (ticket.owner) {
    flags.append(chip(ticket.owner, `chip-owner ${OWNER_CLASS[ticket.owner] || ""}`));
  }
  row.append(flags);
  return row;
}

function projectRow(item) {
  const row = element("details", "row");
  const head = element("summary", "row-head");

  const title = element("span", "cell cell-title");
  title.append(link(item.title || item.ref || "Untitled", item.url, "row-title"));
  if (item.pinned) title.append(chip("pinned", "chip-pin"));
  head.append(title);

  head.append(element("span", "cell cell-repo", shortRepo(item.repo || item.repository) || ""));

  const flags = element("span", "cell cell-flags");
  const pr = rowPrState(item.tickets);
  if (pr) flags.append(chip(pr, `chip-pr chip-pr-${pr}`));
  const tier = rowTier(item.tickets);
  if (tier === "escalated") flags.append(chip("escalated", "chip-tier"));
  for (const owner of rowOwners(item.tickets)) {
    flags.append(chip(owner, `chip-owner ${OWNER_CLASS[owner] || ""}`));
  }
  head.append(flags);

  head.append(pips(item.tickets, item.tickets_closed, item.tickets_total));
  if (item.class) head.append(chip(item.class, `chip-class chip-class-${item.class.toLowerCase()}`));
  head.append(element("span", "cell cell-age", item.waited || ""));
  row.append(head);

  const tickets = Array.isArray(item.tickets) ? item.tickets : [];
  if (tickets.length) {
    const list = element("ul", "tickets");
    for (const ticket of tickets) list.append(ticketRow(ticket));
    row.append(list);
  } else {
    row.append(element("p", "tickets-empty", "No tickets yet."));
  }
  return row;
}

function renderBoard(board) {
  const container = document.querySelector("#board");
  container.replaceChildren();
  for (const column of boardColumns(board)) {
    const items = Array.isArray(column.items) ? column.items : [];
    if (!items.length) continue;
    const group = element("section", "group");
    group.dataset.stage = column.stage;
    const head = element("div", "group-head");
    head.append(element("span", `dot dot-${column.stage.toLowerCase()}`));
    head.append(element("h3", null, column.stage));
    head.append(element("span", "count", items.length));
    group.append(head);
    for (const item of items) group.append(projectRow(item));
    container.append(group);
  }
  if (!container.childElementCount) container.append(element("p", "empty", "The board is empty."));
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
    section.append(element("h3", null, "Human steps"));
    const list = element("ul", null);
    for (const step of brief.human_steps) list.append(humanStepRow(step));
    section.append(list);
    container.append(section);
  }
}

function failureState(snapshot) {
  return snapshot.last_brief_failed ?? snapshot.status?.last_brief_failed ?? false;
}

async function loadSnapshot() {
  const response = await fetch("/api/snapshot", { cache: "no-store" });
  if (!response.ok) throw new Error(`Snapshot returned ${response.status}`);
  const snapshot = await response.json();
  const generatedAt = snapshot.generated_at || snapshot.brief?.generated_at;
  const status = document.querySelector("#snapshot-status");
  const failure = failureState(snapshot);
  status.textContent = generatedAt ? `Snapshot ${age(generatedAt)}` : "Snapshot age unknown";
  if (failure) status.textContent += " · last brief failed";
  status.classList.toggle("failed", Boolean(failure));
  renderWaiting(snapshot.brief || {});
  renderBoard(snapshot.board || {});
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
  loadSnapshot().catch((error) => {
    const status = document.querySelector("#snapshot-status");
    status.textContent = error.message;
    status.classList.add("failed");
  });
}

export { STAGES, age, boardColumns, failureState, pipState, rowOwners, rowPrState, rowTier, shortRepo };
