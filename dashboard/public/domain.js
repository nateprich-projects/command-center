// Trial domain detail page (parent plan #2409, ticket #2413): one domain's
// items laid out in the five analysis-phase columns. Phases are recorded
// nowhere yet, so an item's phase is its class once the reclass lands; until
// then Improve, New and Replace (and anything else unrecognised) sit in
// "Not yet phased". Reads only /api/snapshot; no store, no re-sorting —
// items keep the snapshot's order inside each column, and repos with no table
// entry appear under the interim domain named for the repo itself.

import { domainOf } from "./domains.js";

const PHASES = ["Curate", "Describe", "Hypothesize", "Test", "Implement"];
const UNPHASED = "Not yet phased";

// An explicit `phase` field wins when it names a phase (forward-compatible
// with the redesign recording phases); otherwise the class names the phase
// once the reclass lands. Anything else is not yet phased.
function phaseOf(item) {
  if (!item || typeof item !== "object") return null;
  if (PHASES.includes(item.phase)) return item.phase;
  if (PHASES.includes(item.class)) return item.class;
  return null;
}

// Every board item of one trial domain, in snapshot order.
function domainItems(columns, domain) {
  const items = [];
  for (const column of Array.isArray(columns) ? columns : []) {
    for (const item of (column && column.items) || []) {
      if (domainOf(item) === domain) items.push(item);
    }
  }
  return items;
}

// Items grouped into the five phase columns plus "Not yet phased" last.
// Grouping is stable: each column keeps the snapshot's order.
function groupByPhase(items) {
  const grouped = PHASES.map((phase) => ({ phase, items: [] }));
  const unphased = { phase: UNPHASED, items: [] };
  for (const item of Array.isArray(items) ? items : []) {
    const phase = phaseOf(item);
    const column = phase ? grouped.find((entry) => entry.phase === phase) : null;
    (column || unphased).items.push(item);
  }
  return [...grouped, unphased];
}

// The domain is the single segment after /domains/, e.g.
// /domains/Command%20center. Anything else names no domain.
function domainFromPath(pathname) {
  if (typeof pathname !== "string") return null;
  const prefix = "/domains/";
  if (!pathname.startsWith(prefix)) return null;
  const rest = pathname.slice(prefix.length).replace(/\/$/, "");
  if (!rest || rest.includes("/")) return null;
  try {
    const domain = decodeURIComponent(rest);
    return domain || null;
  } catch {
    return null;
  }
}

function phaseItemLabel(item) {
  const title = item.title || item.ref || "Untitled";
  const repo = item.repo || "";
  const klass = item.class || item.phase || "";
  const waited = item.waited || "";
  const parts = [];
  if (repo) parts.push(repo);
  if (klass) parts.push(klass);
  if (waited) parts.push(waited);
  return { title, meta: parts.join(" · ") };
}

function renderDomainBoard(container, grouped) {
  container.replaceChildren();
  let rendered = 0;
  for (const column of grouped) {
    const section = document.createElement("section");
    section.className = "phase";
    const heading = document.createElement("h2");
    heading.textContent = `${column.phase} (${column.items.length})`;
    section.append(heading);
    if (column.items.length === 0) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "Nothing here.";
      section.append(empty);
    } else {
      const list = document.createElement("ul");
      list.className = "phase-list";
      for (const item of column.items) {
        const { title, meta } = phaseItemLabel(item);
        const row = document.createElement("li");
        row.className = "phase-item";
        if (item.url) {
          const link = document.createElement("a");
          link.href = item.url;
          link.textContent = title;
          row.append(link);
        } else {
          row.append(document.createTextNode(title));
        }
        if (meta) {
          const details = document.createElement("div");
          details.className = "phase-meta";
          details.textContent = meta;
          row.append(details);
        }
        list.append(row);
        rendered += 1;
      }
      section.append(list);
    }
    container.append(section);
  }
  return rendered;
}

async function loadDomainBoard() {
  const name = document.querySelector("#domain-name");
  const status = document.querySelector("#domain-status");
  const container = document.querySelector("#domain-board");
  const domain = domainFromPath(window.location.pathname);
  if (!domain) {
    status.textContent = "No domain was named in the address.";
    return;
  }
  name.textContent = domain;
  document.title = `${domain} — Command Center trial`;
  let response;
  try {
    response = await fetch("/api/snapshot", { headers: { accept: "application/json" } });
  } catch {
    status.textContent = "The snapshot could not be read.";
    return;
  }
  if (!response.ok) {
    status.textContent = "The snapshot could not be read.";
    return;
  }
  const snapshot = await response.json();
  const board = (snapshot && snapshot.board) || {};
  const columns = Array.isArray(board.columns) ? board.columns : [];
  const items = domainItems(columns, domain);
  const rendered = renderDomainBoard(container, groupByPhase(items));
  status.textContent = rendered === 1
    ? `One project in ${domain}.`
    : `${rendered} projects in ${domain}.`;
}

if (typeof document !== "undefined" && typeof window !== "undefined") {
  loadDomainBoard();
}

export { UNPHASED, PHASES, domainFromPath, domainItems, groupByPhase, loadDomainBoard, phaseOf, renderDomainBoard };
