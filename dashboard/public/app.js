// The dashboard renders exactly what the publisher sent, in the order it sent
// it: funnel.py owns ordering, and a second opinion here is how two views of
// the same board drift apart. Nate, 2026-09-15: the page carries the board and
// human steps, and no other brief section. The one thing the page drops is
// what the viewer's repository filter hides, and it keeps the order of what
// remains (Nate, 2026-09-24).


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

// The repository the viewer picked, or null for every repository. It lives in
// the URL, so a reload or a shared link keeps it.
let selectedRepo = null;
let activeTab = "funnel";

// The filter's key is the full owner/repo, read from the ref first: two
// owners can hold a repository of the same name, and the board row's own
// `repo` field is only the short name.
function repoOf(entry) {
  if (!entry || typeof entry !== "object") return null;
  if (typeof entry.ref === "string" && entry.ref.includes("#")) {
    return entry.ref.split("#")[0];
  }
  return entry.repo || entry.repository || null;
}

// The producer's rows the filter keeps, in the producer's order.
function visible(entries, repo = selectedRepo) {
  const kept = [];
  for (const entry of Array.isArray(entries) ? entries : []) {
    if (!repo || repoOf(entry) === repo) kept.push(entry);
  }
  return kept;
}

// The dropdown's own list, alphabetical: this orders repository names, never
// producer rows. The current choice stays listed even when nothing in the
// snapshot names it, so a quiet repository does not reset the filter.
function repoOptions(snapshot, current = selectedRepo) {
  const names = new Set();
  for (const column of boardColumns(snapshot && snapshot.board)) {
    for (const item of column.items || []) {
      const name = repoOf(item);
      if (name) names.add(name);
    }
  }
  const brief = (snapshot && snapshot.brief) || {};
  for (const entry of [...(brief.items || []), ...(brief.human_steps || [])]) {
    const name = repoOf(entry);
    if (name) names.add(name);
  }
  if (current) names.add(current);
  return [...names].sort((a, b) => (
    shortRepo(a).localeCompare(shortRepo(b)) || a.localeCompare(b)
  ));
}

// Options as [value, label]: the short name, unless two owners share it.
function repoLabels(names) {
  const counts = new Map();
  for (const name of names) {
    counts.set(shortRepo(name), (counts.get(shortRepo(name)) || 0) + 1);
  }
  return names.map((name) => [
    name, counts.get(shortRepo(name)) > 1 ? name : shortRepo(name),
  ]);
}

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
// waiting on the reviewer is light blue, a rejected current head is danger
// red, and an approved PR is light purple. A block made only of sibling
// tickets is the plan's sequencing: that ticket is queued, drawn as open.
function pipState(ticket) {
  if (!ticket || typeof ticket !== "object") return "open";
  if (ticket.state !== "OPEN") return "closed";
  if (ticket.pr === "approved") return "approved";
  if (ticket.pr === "changes requested") return "changes-requested";
  if (ticket.pr === "submitted" || ticket.pr === "merged") return "submitted";
  if (ticket.blocked) return ticket.blocked_by_siblings ? "queued" : "blocked";
  return "open";
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
  // A node passed as text is appended, never stringified: that is how the
  // phone board came to print "[object HTMLSpanElement]" (#988).
  if (typeof Node !== "undefined" && text instanceof Node) node.append(text);
  else if (text !== undefined && text !== null) node.textContent = String(text);
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

function pips(item, tickets, closed, total) {
  const wrap = element("div", "pips");
  const bar = element("div", "pip-bar");
  // The producer sends bar segments already in progress order, so finished
  // work fills from the left while the rows below stay in queue order.
  const states = Array.isArray(item && item.pips) ? item.pips : null;
  const rows = Array.isArray(tickets) ? tickets : [];
  if (states) {
    for (const state of states) bar.append(element("i", `pip pip-${state}`));
  } else if (rows.length) {
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

// Escalated is the exception worth a chip; standard stays quiet text so a
// board of ordinary work does not read as a wall of badges.
function tierCell(tier) {
  if (!tier) return element("span", "muted", "—");
  if (tier === "standard") return element("span", "tier-standard", "standard");
  return chip(tier, `chip-tier chip-tier-${tier}`);
}

// A row nobody can act on until a block lifts says so where the owner would
// be, rather than a blank: Queued when it waits only on its siblings, Blocked
// otherwise (Nate, 2026-09-24). ``hold`` is "queued", "blocked", or a
// boolean, where true means blocked.
function ownerCell(owner, hold = false, note = null) {
  if (!owner && hold === "queued") return chip("Queued", "chip-owner owner-queued");
  if (!owner && hold === "paused") return chip("Paused", "chip-owner owner-paused", note);
  if (!owner && hold) return chip("Blocked", "chip-owner owner-blocked");
  if (!owner) return element("span", "muted", "—");
  return chip(owner, `chip-owner ${OWNER_CLASS[owner] || ""}`);
}

// What holds an open ticket: "queued" behind its siblings, "blocked",
// "paused" after repeated failed runs, or null.
function ticketHold(ticket) {
  if (!ticket || ticket.state !== "OPEN") return null;
  if (ticket.blocked) return ticket.blocked_by_siblings ? "queued" : "blocked";
  return ticket.paused_until ? "paused" : null;
}

// A project's hold for its Next step cell: blocked, or waiting out a pause.
function projectHold(item) {
  if (projectBlocked(item)) return "blocked";
  return item && item.next_step_paused_until ? "paused" : null;
}

// A time in the viewer's own zone, never UTC (Nate's standing rule).
function localTime(iso) {
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return String(iso);
  return when.toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" });
}

// The engine holds the Project fields do not show (Nate, 2026-09-25): a
// ticket paused after repeated failed runs, or one its comments record as
// finished and waiting for Nate to close.
function holdChip(ticket) {
  if (!ticket || ticket.state !== "OPEN") return null;
  if (ticket.finished_by_comments) {
    return chip("finished — close it", "chip-finished",
      "Its comments record it done; it waits for Nate to close it.");
  }
  if (ticket.paused_until) {
    const runs = Number.isFinite(ticket.paused_failures)
      ? `${ticket.paused_failures} failed runs in a row` : "repeated failed runs";
    return chip(`paused until ${localTime(ticket.paused_until)}`, "chip-paused",
      `${runs}; offered again then, or as soon as a run finishes it.`);
  }
  return null;
}

// The producer's flag; an older snapshot without it falls back to the
// project's own block.
function projectBlocked(item) {
  if (!item) return false;
  if (typeof item.next_step_blocked === "boolean") return item.next_step_blocked;
  return Boolean(item.blocked);
}

function headerRow() {
  const row = gridRow("div", "thead");
  row.append(element("div", "cell cell-twisty"));
  for (const [className, label] of [
    ["cell-title", "Title"],
    ["cell-repo", "Repository"],
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

// What a block is waiting on, in one chip: the blocking tickets, the date it
// lifts, or the recorded reason. A bare "blocked" says nothing, which is what
// #711 looked like on the board.
function blockedChip(ticket) {
  const refs = Array.isArray(ticket.blockers) ? ticket.blockers : [];
  const names = refs.map((ref) => `#${String(ref).split("#").pop()}`);
  if (names.length && ticket.blocked_by_siblings) {
    return chip(`queued behind ${names.join(", ")}`, "chip-queued", refs.join(", "));
  }
  if (names.length) {
    return chip(`blocked by ${names.join(", ")}`, "chip-blocked", refs.join(", "));
  }
  if (ticket.blocked_until) {
    return chip(`blocked until ${ticket.blocked_until}`, "chip-blocked",
      ticket.block_reason || null);
  }
  if (ticket.block_reason) {
    const short = ticket.block_reason.length > 48
      ? `${ticket.block_reason.slice(0, 45)}…` : ticket.block_reason;
    return chip(`blocked: ${short}`, "chip-blocked", ticket.block_reason);
  }
  return chip("blocked, no reason recorded", "chip-blocked");
}

function refList(refs, limit = Infinity) {
  const names = refs.map((ref) => `#${String(ref).split("#").pop()}`);
  if (names.length > limit) {
    return `${names.slice(0, limit).join(", ")}, & ${names.length - limit} more`;
  }
  if (names.length < 3) return names.join(" & ");
  return `${names.slice(0, -1).join(", ")}, & ${names[names.length - 1]}`;
}

// The open tickets waiting on this one, then the rest of the chain behind
// them: why a ticket can rank above its project's class (Nate, 2026-09-24,
// on #1424: "unblocks #1465, then #1466 & #1467").
function unblocksChip(ticket) {
  const direct = Array.isArray(ticket && ticket.unblocks) ? ticket.unblocks : [];
  if (!direct.length) return null;
  const later = Array.isArray(ticket.unblocks_later) ? ticket.unblocks_later : [];
  let text = `unblocks ${refList(direct)}`;
  if (later.length) text += `, then ${refList(later, 3)}`;
  return chip(text, "chip-unblocks", direct.concat(later).join(", "));
}

function classChip(value) {
  if (!value) return element("span", "muted", "");
  return chip(value, `chip-class chip-class-${String(value).toLowerCase()}`);
}

function ticketRow(ticket) {
  const row = gridRow("div", `ticket ticket-${pipState(ticket)}`);
  row.append(cell("cell-twisty", element("i", `pip pip-${pipState(ticket)}`)));

  const title = element("div", "cell cell-title cell-child");
  title.append(element("span", "child-rule"));
  title.append(link(`#${ticket.number} ${ticket.title || ""}`, ticket.url, "ticket-title"));
  if (ticket.blocked) title.append(blockedChip(ticket));
  const hold = holdChip(ticket);
  if (hold) title.append(hold);
  const unblocks = ticket.state === "OPEN" ? unblocksChip(ticket) : null;
  if (unblocks) title.append(unblocks);
  row.append(title);

  row.append(cell("cell-repo", element("span", "muted", "")));
  row.append(cell("cell-tier", tierCell(ticket.state === "OPEN" ? ticket.tier : null)));
  row.append(cell("cell-owner", ownerCell(ticket.owner, ticketHold(ticket))));
  row.append(cell("cell-pips", element("span", "muted", "")));
  // The class the ticket ranks as, which can be higher than its project's.
  row.append(cell("cell-class", classChip(ticket.state === "OPEN" ? ticket.class : null)));
  row.append(cell("cell-age", element("span", "muted", "")));
  return row;
}

function phoneChildren(item) {
  return item && Array.isArray(item.tickets) ? item.tickets : [];
}

function phoneRepo(item) {
  return shortRepo(repoOf(item)) || "";
}

function phoneCounterParts(item, children) {
  let closed = Number.isFinite(item && item.tickets_closed)
    ? item.tickets_closed : 0;
  const total = Number.isFinite(item && item.tickets_total)
    ? item.tickets_total : children.length;
  if (!Number.isFinite(item && item.tickets_closed)) {
    closed = 0;
    for (const child of children) {
      if (child && child.state !== "OPEN") closed += 1;
    }
  }
  return { closed, total };
}

function phoneClass(value) {
  if (!value) return element("span", "muted", "—");
  return chip(value, `chip-class chip-class-${String(value).toLowerCase()}`);
}

// A phone row's ticket state, or null for a project row. Only tickets carry
// a number and a state; a project row has neither, and pipState() would read
// its missing state as closed and strike the whole project through.
function phoneState(item) {
  return Number.isFinite(item && item.number) ? pipState(item) : null;
}

function phoneTitle(item) {
  if (Number.isFinite(item && item.number)) {
    return `#${item.number} ${item.title || ""}`;
  }
  return item && (item.title || item.ref) || "Untitled";
}

function phoneField(label, value) {
  const field = element("div", "phone-field");
  field.append(element("dt", "phone-label", label));
  const content = element("dd", "phone-value");
  content.append(value || element("span", "muted", "—"));
  field.append(content);
  return field;
}

// Several short facts on one row, each with its label above its value.
function phoneMeta(pairs) {
  const row = element("div", "phone-meta");
  for (const [label, value] of pairs) {
    const field = element("div", "phone-meta-field");
    field.append(element("dt", "phone-label", label));
    const content = element("dd", "phone-value");
    content.append(value || element("span", "muted", "—"));
    field.append(content);
    row.append(field);
  }
  return row;
}

function phoneProgress(item, children) {
  const { closed, total } = phoneCounterParts(item, children);
  const value = pips(item, children, closed, total);
  if (!value.childElementCount) return element("span", "muted", "—");
  value.classList.add("phone-progress");
  value.setAttribute("role", "img");
  value.setAttribute("aria-label", `Sub-issue progress: ${closed}/${total}`);
  // pips() already shows closed/total; a second copy printed it twice (#994).
  return value;
}

function phoneDetails(item, inheritedClass, children) {
  const fields = element("dl", "phone-fields");
  const project = !Number.isFinite(item && item.number);
  const className = item && item.class || inheritedClass;

  // No PR field, and the short facts share one row (Nate, 2026-09-17, #990).
  if (project) {
    fields.append(phoneMeta([
      ["Tier", tierCell(rowTier(children))],
      ["Next step", ownerCell(nextOwner(item), projectHold(item),
        item.next_step_paused_until ? `until ${localTime(item.next_step_paused_until)}` : null)],
      ["Updated", element("span", "age", item.waited || "—")],
    ]));
    if (item.blocked) fields.append(phoneField("Blocked", blockedChip(item)));
    fields.append(phoneField("Progress", phoneProgress(item, children)));
  } else {
    fields.append(phoneMeta([
      ["Tier", tierCell(item.state === "OPEN" ? item.tier : null)],
      ["Next step", ownerCell(item.owner, ticketHold(item))],
    ]));
    if (item.blocked) {
      const label = ticketHold(item) === "queued" ? "Queued" : "Blocked";
      fields.append(phoneField(label, blockedChip(item)));
    }
    const hold = holdChip(item);
    if (hold) fields.append(phoneField(item.finished_by_comments ? "Finished" : "Paused", hold));
    const unblocks = item.state === "OPEN" ? unblocksChip(item) : null;
    if (unblocks) fields.append(phoneField("Unblocks", unblocks));
    if (children.length || Number.isFinite(item.tickets_total)) {
      fields.append(phoneField("Progress", phoneProgress(item, children)));
    }
  }

  const detail = element("div", "phone-details");
  detail.append(fields);
  if (children.length) {
    const childList = element("div", "phone-children");
    childList.append(element("div", "phone-children-label", "Sub-issues"));
    for (const child of children) {
      childList.append(phoneRow(child, className));
    }
    detail.append(childList);
  }
  return detail;
}

function phoneRow(item, inheritedClass) {
  const children = phoneChildren(item);
  const className = item && item.class || inheritedClass;
  const { closed, total } = phoneCounterParts(item, children);
  const state = phoneState(item);
  const row = element("details", "phone-row");
  // The state is both a coloured pip beside the title and a class on the row,
  // exactly as the wide ticket rows carry it: on the phone a finished ticket
  // read as one more queued one (Nate, 2026-09-18).
  if (state) row.classList.add(`phone-row-${state}`);
  const summary = element("summary", "phone-summary");
  summary.append(element("span", "phone-arrow", "\u25B8"));

  const title = element("span", "phone-title");
  if (state) title.append(element("i", `pip pip-${state}`));
  title.append(link(phoneTitle(item), item && item.url, "phone-title-link"));
  summary.append(title);
  summary.append(element("span", "phone-repo", phoneRepo(item) || "—"));
  const classCell = element("span", "phone-class");
  classCell.append(phoneClass(className));
  summary.append(classCell);
  summary.append(element("span", "phone-counter", `${closed}/${total}`));
  row.append(summary);
  row.append(phoneDetails(item, className, children));

  const key = item && item.ref;
  if (expandAll() || (key && expanded.has(key))) row.open = true;
  if (key) {
    row.addEventListener("toggle", () => {
      if (row.open) expanded.add(key);
      else expanded.delete(key);
    });
  }
  return row;
}

function renderPhoneBoard(columns) {
  const board = element("div", "phone-board");
  for (const column of columns) {
    const items = visible(column.items);
    if (!items.length) continue;
    const group = element("details", "phone-group");
    group.open = !COLLAPSED_STAGES.includes(column.stage);
    const head = element("summary", "phone-group-head");
    head.append(element("span", "group-twisty", "\u25B8"));
    head.append(element("span", `dot dot-${column.stage.toLowerCase()}`));
    head.append(element("span", "group-name", column.stage));
    head.append(element("span", "count", items.length));
    group.append(head);
    const body = element("div", "phone-group-body");
    for (const item of items) body.append(phoneRow(item));
    group.append(body);
    board.append(group);
  }
  return board;
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
  if (item.blocked) title.append(blockedChip(item));
  row.append(title);

  row.append(cell("cell-repo", element("span", "repo", shortRepo(item.repo || item.repository) || "")));
  row.append(cell("cell-tier", tierCell(rowTier(tickets))));
  row.append(cell("cell-owner", ownerCell(nextOwner(item), projectHold(item),
    item.next_step_paused_until ? `until ${localTime(item.next_step_paused_until)}` : null)));
  row.append(cell("cell-pips", pips(item, tickets, item.tickets_closed, item.tickets_total)));
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
  const columns = boardColumns(board);
  const table = element("div", "table");
  table.append(headerRow());
  let rendered = 0;
  for (const column of columns) {
    const items = visible(column.items);
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
    container.append(element("p", "empty", selectedRepo
      ? `Nothing on the board for ${shortRepo(selectedRepo)}.` : "The board is empty."));
    return;
  }
  container.append(table);
  container.append(renderPhoneBoard(columns));
}

function decisionRow(item) {
  const row = element("li", "waiting-row");
  row.append(link(item.title || item.ref || "Untitled", item.url, "waiting-title"));
  if (item.waiting_on) row.append(chip(item.waiting_on, "chip-gate"));
  if (item.waiting_reason) row.append(chip(item.waiting_reason, "chip-reason"));
  if (item.class) {
    row.append(chip(item.class, `chip-class chip-class-${String(item.class).toLowerCase()}`));
  }
  if (item.pinned) row.append(chip("pinned", "chip-pin"));
  row.append(element("span", "waiting-meta",
    `${shortRepo(item.repo) || item.ref || ""} · ${item.waited || ""}`));
  return row;
}

function humanStepRow(step) {
  const row = element("li", "waiting-row");
  row.append(link(step.title || step.ref || "Untitled", step.url, "waiting-title"));
  if (step.reason) row.append(chip(step.reason, "chip-reason"));
  // The ref carries the ticket number; the row wants the repository and the
  // wait, as the decision rows have (Nate, 2026-09-16).
  const repo = shortRepo(String(step.ref || "").split("#")[0]) || "";
  row.append(element("span", "waiting-meta",
    `${repo}${step.waited ? ` · ${step.waited}` : ""}`));
  return row;
}

function waitingSection(title, count, rows) {
  const section = element("div", "waiting-section");
  const head = element("div", "waiting-head");
  head.append(element("h3", null, title));
  if (Number.isFinite(count)) head.append(element("span", "count", count));
  section.append(head);
  const list = element("ul", "waiting-list");
  for (const row of rows) list.append(row);
  section.append(list);
  return section;
}

function renderWaiting(brief) {
  const container = document.querySelector("#waiting");
  container.replaceChildren();
  const total = brief.total_needing_nate;
  const decisions = visible(brief.items);
  const steps = visible(brief.human_steps);

  if (selectedRepo && !decisions.length && !steps.length) {
    container.append(element("p", "empty",
      `Nothing in ${shortRepo(selectedRepo)} is waiting on you.`));
    return;
  }
  if (total === 0 && !present(steps)) {
    container.append(document.querySelector("#empty-state").content.cloneNode(true));
    return;
  }

  // Keep the two rendered brief sections in the same order as /funnel: Nate's
  // decisions first, then work he owes. CSS changes their narrow presentation
  // without changing the producer's payload or its ordering.
  const sections = element("div", "brief-sections");
  if (decisions.length) {
    sections.append(waitingSection(
      "Decisions waiting on you", decisions.length, decisions.map(decisionRow),
    ));
  }
  if (present(steps)) {
    sections.append(waitingSection(
      "Actions waiting on you", steps.length, steps.map(humanStepRow),
    ));
  }
  container.append(sections);
}

function failureState(snapshot) {
  return snapshot.last_brief_failed ?? snapshot.status?.last_brief_failed ?? false;
}

// Rolling seven-day Muse spend against the decided cap, read from the
// snapshot root rather than the brief. The one usage line Nate chose for the
// test week, with no companion line for a shorter window (2026-09-18). An
// unreadable row renders nothing, so a missing reader never blocks the board.
function museUsage(muse) {
  if (!muse || typeof muse !== "object") return null;
  const spent = muse.spent_dollars;
  const cap = muse.cap_dollars;
  const percent = muse.used_percent;
  for (const value of [spent, cap, percent]) {
    if (typeof value !== "number" || !Number.isFinite(value)) return null;
  }
  if (cap <= 0 || spent < 0) return null;
  return { spent, cap, percent };
}

function museUsageText(muse) {
  const parsed = museUsage(muse);
  if (!parsed) return null;
  return `Muse 7-day spend $${parsed.spent.toFixed(2)} of $${parsed.cap.toFixed(2)} (${parsed.percent.toFixed(1)}%)`;
}

function renderUsage(usage) {
  const container = document.querySelector("#usage");
  container.replaceChildren();
  const parsed = museUsage(usage && usage.muse);
  if (!parsed) return;
  const wrap = element("div", "usage");
  wrap.append(element("span", "usage-label",
    `Muse 7-day spend $${parsed.spent.toFixed(2)} of $${parsed.cap.toFixed(2)}`));
  const bar = element("div", "usage-bar");
  const fill = element("i", `usage-fill${parsed.percent >= 100 ? " over" : ""}`);
  fill.setAttribute("style", `width: ${Math.min(100, parsed.percent).toFixed(1)}%`);
  bar.append(fill);
  wrap.append(bar);
  wrap.append(element("span", "usage-percent", `${parsed.percent.toFixed(1)}%`));
  container.append(wrap);
}

let lastGeneratedAt = null;
let lastSnapshot = null;
let loading = false;

function readRepoFromUrl() {
  if (typeof window === "undefined" || !window.location) return null;
  return new URLSearchParams(window.location.search).get("repo") || null;
}

function writeRepoToUrl(repo) {
  if (typeof window === "undefined" || !window.history) return;
  const url = new URL(window.location.href);
  if (repo) url.searchParams.set("repo", repo);
  else url.searchParams.delete("repo");
  window.history.replaceState(null, "", url);
}

function tabFromUrl(href) {
  const url = new URL(href, "https://funnel.nateprich.com");
  return url.searchParams.get("tab") === "execution" ? "execution" : "funnel";
}

function tabUrl(tab, href) {
  const url = new URL(href, "https://funnel.nateprich.com");
  if (tab === "execution") url.searchParams.set("tab", "execution");
  else url.searchParams.delete("tab");
  return url.pathname + url.search + url.hash;
}

// Rebuilt only when the list changes: replacing the options on every poll
// would close the dropdown under a viewer who is choosing.
let renderedOptions = null;

function renderRepoFilter(snapshot) {
  const select = document.querySelector("#repo-filter");
  if (!select) return;
  const labels = repoLabels(repoOptions(snapshot));
  const signature = JSON.stringify(labels);
  if (signature !== renderedOptions) {
    const options = [element("option", null, "All repositories")];
    options[0].value = "";
    for (const [value, label] of labels) {
      const option = element("option", null, label);
      option.value = value;
      options.push(option);
    }
    select.replaceChildren(...options);
    renderedOptions = signature;
  }
  select.value = selectedRepo || "";
}

function renderAll(snapshot) {
  renderRepoFilter(snapshot);
  renderWaiting(snapshot.brief || {});
  renderUsage(snapshot.usage || {});
  renderBoard(snapshot.board || {});
}

function isMetricNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

function isMetricSeries(value) {
  return value && typeof value === "object" &&
    ["daily", "r7", "r28", "delta"].every((key) => Array.isArray(value[key]));
}

function metricAtPath(root, path) {
  let value = root;
  for (const part of path) {
    if (!value || typeof value !== "object") return null;
    value = value[part];
  }
  return isMetricSeries(value) ? value : null;
}

function metricLeaves(value, path = [], found = []) {
  if (isMetricSeries(value)) {
    found.push({ path, series: value });
  } else if (value && typeof value === "object" && !Array.isArray(value)) {
    for (const [key, child] of Object.entries(value)) {
      metricLeaves(child, [...path, key], found);
    }
  }
  return found;
}

function metricValues(series, index) {
  const read = (key) => {
    const value = series && series[key] && series[key][index];
    return isMetricNumber(value) ? value : null;
  };
  return { r7: read("r7"), r28: read("r28"), delta: read("delta") };
}

// A missing component makes that aggregate a gap; never turn it into zero.
function sumMetricValues(series, index) {
  const sum = (key) => {
    if (!series.length) return null;
    let total = 0;
    for (const item of series) {
      const value = metricValues(item, index)[key];
      if (!isMetricNumber(value)) return null;
      total += value;
    }
    return total;
  };
  return { r7: sum("r7"), r28: sum("r28"), delta: sum("delta") };
}

function formatMetricNumber(value, digits, signed = false) {
  const number = new Intl.NumberFormat("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(value);
  return signed && value >= 0 ? "+" + number : number;
}

function formatMetricValue(value, format, isDelta = false) {
  if (!isMetricNumber(value)) return "Gap";
  if (format === "percent") {
    const points = formatMetricNumber(value * 100, 1, isDelta);
    return isDelta ? points + " pp" : points + "%";
  }
  if (format === "hours") {
    return formatMetricNumber(value, 2, isDelta) + " h";
  }
  return formatMetricNumber(value, 1, isDelta);
}

function appendMetricRow(tile, label, values, format) {
  const row = element("div", "metric-series-row");
  if (label) row.append(element("p", "metric-series-label", label));
  const readings = element("dl", "metric-values");
  for (const [name, value, isDelta] of [
    ["R7", values && values.r7, false],
    ["R28", values && values.r28, false],
    ["Delta", values && values.delta, true],
  ]) {
    const reading = element(
      "div",
      "metric-reading" + (isDelta ? " metric-delta" : ""),
    );
    reading.append(element("dt", null, name));
    const output = element(
      "dd",
      isMetricNumber(value) ? null : "metric-gap",
      formatMetricValue(value, format, isDelta),
    );
    if (!isMetricNumber(value)) output.setAttribute("aria-label", "Gap");
    reading.append(output);
    readings.append(reading);
  }
  row.append(readings);
  tile.append(row);
}

function appendMetricTile(container, definition, rows) {
  const tile = element("article", "metric-tile");
  tile.setAttribute("data-metric", definition.code);
  tile.append(element("p", "metric-code", definition.code));
  tile.append(element("h3", "metric-title", definition.title));
  for (const row of rows) {
    appendMetricRow(tile, row.label, row.values, definition.format);
  }
  container.append(tile);
}

// The chart every Execution panel draws: an inline SVG line of the R7 over the
// last CHART_WINDOW_DAYS days, with the newest R28 as a horizontal rule. The
// series keeps 90 days, so the window can widen without a schema change. No
// library and no request: the worker's CSP is script-src 'self'. A missing
// day is a break in the line, never a zero and never interpolated.
const SVG_NS = "http://www.w3.org/2000/svg";
const CHART_WINDOW_DAYS = 56;
const CHART_WIDTH = 320;
const CHART_HEIGHT = 120;
const CHART_PAD = { top: 12, right: 44, bottom: 18, left: 4 };

function svgNode(tag, attributes = {}, text) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [name, value] of Object.entries(attributes)) {
    node.setAttribute(name, String(value));
  }
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

// One path per unbroken run of numbers; a lone day between gaps is a dot,
// because a one-point path draws nothing.
function chartRuns(values) {
  const runs = [];
  let run = [];
  values.forEach((value, index) => {
    if (isMetricNumber(value)) {
      run.push(index);
    } else if (run.length) {
      runs.push(run);
      run = [];
    }
  });
  if (run.length) runs.push(run);
  return runs;
}

function chartTop(values, format) {
  let top = 0;
  for (const value of values) if (isMetricNumber(value) && value > top) top = value;
  if (format === "percent") return Math.min(1, top * 1.1) || 1;
  return top * 1.1 || 1;
}

function renderMetricChart(series, days, { title = "", format = "count" } = {}) {
  const start = Math.max(0, (days || []).length - CHART_WINDOW_DAYS);
  const windowDays = (days || []).slice(start);
  const read = (key) => windowDays.map((_, offset) => {
    const value = series && Array.isArray(series[key]) ? series[key][start + offset] : null;
    return isMetricNumber(value) ? value : null;
  });
  const r7 = read("r7");
  const r28 = read("r28");
  const rule = r28.length ? r28[r28.length - 1] : null;
  const top = chartTop([...r7, rule], format);

  const plotWidth = CHART_WIDTH - CHART_PAD.left - CHART_PAD.right;
  const plotHeight = CHART_HEIGHT - CHART_PAD.top - CHART_PAD.bottom;
  const x = (index) => CHART_PAD.left +
    (windowDays.length > 1 ? (index / (windowDays.length - 1)) * plotWidth : plotWidth);
  const y = (value) => CHART_PAD.top + plotHeight - (value / top) * plotHeight;
  const point = (index) => x(index).toFixed(1) + " " + y(r7[index]).toFixed(1);

  const latest = r7.length ? r7[r7.length - 1] : null;
  const summary = (title ? title + ": " : "") + "R7 " +
    formatMetricValue(latest, format) + ", R28 " + formatMetricValue(rule, format) +
    ", " + windowDays.length + " days";
  const svg = svgNode("svg", {
    class: "metric-chart",
    viewBox: "0 0 " + CHART_WIDTH + " " + CHART_HEIGHT,
    role: "img",
    "aria-label": summary,
  });
  svg.append(svgNode("title", {}, summary));
  // Drawn first so a hovered day shades beneath the line, not over it.
  const hits = svgNode("g", { class: "chart-hits" });
  svg.append(hits);

  const baseline = CHART_PAD.top + plotHeight;
  svg.append(svgNode("line", {
    class: "chart-axis",
    x1: CHART_PAD.left, x2: CHART_PAD.left + plotWidth, y1: baseline, y2: baseline,
  }));

  if (isMetricNumber(rule)) {
    svg.append(svgNode("line", {
      class: "chart-rule",
      x1: CHART_PAD.left, x2: CHART_PAD.left + plotWidth, y1: y(rule), y2: y(rule),
    }));
    // Labelled at the left end, clear of the R7's end label on the right.
    svg.append(svgNode("text", {
      class: "chart-rule-label", x: CHART_PAD.left + 2, y: y(rule) - 4,
    }, "R28 " + formatMetricValue(rule, format)));
  }

  for (const run of chartRuns(r7)) {
    if (run.length === 1) {
      svg.append(svgNode("circle", {
        class: "chart-dot", cx: x(run[0]).toFixed(1), cy: y(r7[run[0]]).toFixed(1), r: 4,
      }));
    } else {
      svg.append(svgNode("path", {
        class: "chart-line",
        d: "M" + run.map(point).join(" L"),
      }));
    }
  }

  if (isMetricNumber(latest)) {
    const last = r7.length - 1;
    svg.append(svgNode("circle", {
      class: "chart-dot", cx: x(last).toFixed(1), cy: y(latest).toFixed(1), r: 4,
    }));
    svg.append(svgNode("text", {
      class: "chart-end-label", x: x(last) + 7, y: y(latest) + 3,
    }, formatMetricValue(latest, format)));
  }

  // Hover: one full-height band per day carrying that day's reading, so a gap
  // says "Gap" on hover rather than showing nothing.
  const band = windowDays.length > 1 ? plotWidth / (windowDays.length - 1) : plotWidth;
  windowDays.forEach((day, index) => {
    const hit = svgNode("rect", {
      class: "chart-hit",
      x: (x(index) - band / 2).toFixed(1), y: CHART_PAD.top,
      width: band.toFixed(1), height: plotHeight,
    });
    hit.append(svgNode("title", {}, day + " · R7 " + formatMetricValue(r7[index], format) +
      " · R28 " + formatMetricValue(r28[index], format)));
    hits.append(hit);
  });

  if (windowDays.length) {
    svg.append(svgNode("text", {
      class: "chart-date", x: CHART_PAD.left, y: CHART_HEIGHT - 4,
    }, windowDays[0]));
    svg.append(svgNode("text", {
      class: "chart-date", x: CHART_PAD.left + plotWidth, y: CHART_HEIGHT - 4,
      "text-anchor": "end",
    }, windowDays[windowDays.length - 1]));
  }
  return svg;
}

function renderExecutionTiles(series, container) {
  if (!container) return;
  container.replaceChildren();
  const root = series && series.metrics && typeof series.metrics === "object"
    ? series.metrics : {};
  const days = series && Array.isArray(series.days) ? series.days : [];
  const index = days.length - 1;
  const single = (path) => metricValues(metricAtPath(root, path), index);

  appendMetricTile(container, {
    code: "A1", title: "Tickets landed / day", format: "count",
  }, [{ label: "", values: single(["A", "A1", "total"]) }]);

  appendMetricTile(container, {
    code: "A2", title: "Self-directed share", format: "percent",
  }, [{ label: "", values: single(["A", "A2"]) }]);

  const implementByAgent = root.C && root.C.C3 &&
    root.C.C3.error_rate_by_agent_and_job;
  const implementRows = [];
  if (implementByAgent && typeof implementByAgent === "object") {
    for (const [agent, jobs] of Object.entries(implementByAgent)) {
      if (jobs && isMetricSeries(jobs.implement)) {
        implementRows.push({
          label: agent,
          values: metricValues(jobs.implement, index),
        });
      }
    }
  }
  if (!implementRows.length) implementRows.push({
    label: "", values: { r7: null, r28: null, delta: null },
  });
  appendMetricTile(container, {
    code: "C3", title: "Implement error rate", format: "percent",
  }, implementRows);

  const regressionLeaves = [];
  for (const item of metricLeaves(
    root.C && root.C.C4 && root.C.C4.by_agent_and_job,
  )) {
    if (item.path[item.path.length - 1] === "regression") regressionLeaves.push(item);
  }
  appendMetricTile(container, {
    code: "C4", title: "Regression errors / day", format: "count",
  }, [{
    label: "",
    values: sumMetricValues(regressionLeaves.map((item) => item.series), index),
  }]);

  const heldLeaves = metricLeaves(
    root.D && root.D.D6 && root.D.D6.held_hours_by_agent_and_reason,
  );
  appendMetricTile(container, {
    code: "D6", title: "Held hours", format: "hours",
  }, [{
    label: "",
    values: sumMetricValues(heldLeaves.map((item) => item.series), index),
  }]);

  appendMetricTile(container, {
    code: "E4", title: "Watch interventions / day", format: "count",
  }, [{ label: "", values: single(["E", "E4"]) }]);
}

async function requestMetrics(fetchImpl = fetch) {
  const response = await fetchImpl("/api/metrics", { cache: "no-store" });
  if (!response.ok) throw new Error("Metrics returned " + response.status);
  return response.json();
}

let metricsLoading = false;

async function loadMetrics() {
  if (metricsLoading) return;
  metricsLoading = true;
  const status = document.querySelector("#metrics-status");
  const container = document.querySelector("#metrics-grid");
  try {
    const series = await requestMetrics();
    status.textContent = series.as_of
      ? "Metrics through " + series.as_of
      : "Metrics date unknown";
    status.classList.remove("failed");
    renderExecutionTiles(series, container);
  } catch (error) {
    status.textContent = error.message || "Metrics unavailable";
    status.classList.add("failed");
    renderExecutionTiles(null, container);
    throw error;
  } finally {
    metricsLoading = false;
  }
}

function bindRepoFilter() {
  const select = document.querySelector("#repo-filter");
  if (!select) return;
  select.addEventListener("change", () => {
    selectedRepo = select.value || null;
    writeRepoToUrl(selectedRepo);
    if (lastSnapshot) renderAll(lastSnapshot);
  });
}

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
    lastSnapshot = snapshot;
    renderAll(snapshot);
  } finally {
    loading = false;
  }
}

function activateTab(tab) {
  activeTab = tab === "execution" ? "execution" : "funnel";
  const execution = activeTab === "execution";
  document.querySelector("#funnel-view").hidden = execution;
  document.querySelector("#execution-view").hidden = !execution;
  document.querySelector("#page-heading").textContent = execution ? "Execution" : "Funnel";
  document.querySelector("#snapshot-status").hidden = execution;
  document.querySelector("#metrics-status").hidden = !execution;
  document.querySelector(".repo-filter").hidden = execution;
  document.title = (execution ? "Execution" : "Funnel") + " · Command Center";
  for (const link of document.querySelectorAll("#view-nav [data-tab]")) {
    if (link.dataset.tab === activeTab) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  if (execution) {
    loadMetrics().catch(() => {});
  } else {
    loadSnapshot({ force: true }).catch((error) => {
      const status = document.querySelector("#snapshot-status");
      status.textContent = error.message;
      status.classList.add("failed");
    });
  }
}

function bindViewNavigation() {
  const nav = document.querySelector("#view-nav");
  if (!nav) return;
  nav.addEventListener("click", (event) => {
    if (
      event.defaultPrevented ||
      event.button !== 0 ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey ||
      event.altKey
    ) return;
    const anchor = event.target.closest && event.target.closest("a[data-tab]");
    if (!anchor || !anchor.dataset.tab) return;
    event.preventDefault();
    const destination = tabUrl(anchor.dataset.tab, window.location.href);
    const current = window.location.pathname + window.location.search + window.location.hash;
    if (destination !== current) window.history.pushState(null, "", destination);
    activateTab(anchor.dataset.tab);
  });
  window.addEventListener("popstate", () => activateTab(tabFromUrl(window.location.href)));
}

function startPolling() {
  window.setInterval(() => {
    // A hidden tab is not being read; skip the request rather than poll a
    // background window every thirty seconds.
    if (activeTab !== "funnel" || document.visibilityState === "hidden") return;
    loadSnapshot().catch(() => {});
  }, POLL_MS);
  document.addEventListener("visibilitychange", () => {
    if (activeTab === "funnel" && document.visibilityState === "visible") {
      loadSnapshot().catch(() => {});
    }
  });
}

if (typeof document !== "undefined") {
  selectedRepo = readRepoFromUrl();
  bindRepoFilter();
  bindViewNavigation();
  activateTab(tabFromUrl(window.location.href));
  startPolling();
}

export {
  STAGES, age, boardColumns, failureState, museUsageText, nextOwner, ownerCell,
  phoneState, pipState, projectBlocked, projectHold, holdChip, localTime,
  renderPhoneBoard, ticketHold, unblocksChip,
  repoLabels, repoOf, repoOptions, rowTier, shortRepo, visible,
  renderExecutionTiles, renderMetricChart, requestMetrics, CHART_WINDOW_DAYS,
  tabFromUrl, tabUrl,
};
