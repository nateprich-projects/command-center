// Domains trial home (parent plan #2409, ticket #2414): one row per
// domain with up to two slot cards for the domain's oldest in-flight
// non-urgent projects, an over-the-limit count, the next waiting items,
// and a link to /domains/<domain>. Reads only /api/snapshot; no store,
// no new fetch, no snapshot change. The current page is untouched.
//
// Display-only reading of the funnel's two-slot rule: in-flight means the
// Shaped, Ready and Building columns; non-urgent excludes Broken,
// Investigate, Maintenance and Bug (defects stay out of slots, as in
// funnel.DOMAIN_SLOTS); waiting means the Ideas column. Snapshot order is
// the producer's oldest-first order, so the first two in-flight items are
// the slots and the rest are over the limit. There is deliberately no
// sorting or reversing anywhere in this file. Domains come from the shared
// table (see domains.js); repos with no table entry appear under an
// interim domain named for the repo itself, so nothing drops silently.

import { domainOf, listDomains } from "./domains.js";
import { renderTopStrip, topStrip } from "./topstrip.js";

const SLOT_LIMIT = 2;
const IN_FLIGHT_STAGES = ["Shaped", "Ready", "Building"];
const WAITING_STAGES = ["Ideas"];
// Urgent classes plus Bug: none of them holds a display slot.
const SLOT_EXCLUDED_CLASSES = ["Broken", "Investigate", "Maintenance", "Bug"];

function domainHref(domain) {
  return `/domains/${encodeURIComponent(domain)}`;
}

// One row per domain, in the shared table's first-seen order. Each row
// carries the domain's in-flight non-urgent items first (snapshot order),
// split into at most two slot cards plus an over-the-limit count, and the
// domain's waiting items in snapshot order.
function domainRows(columns) {
  const rows = [];
  const safe = Array.isArray(columns) ? columns : [];
  const domains = listDomains(safe);
  for (const domain of domains) {
    const inflight = [];
    const waiting = [];
    for (const column of safe) {
      if (!column) continue;
      const isFlight = IN_FLIGHT_STAGES.includes(column.stage);
      const isWaiting = WAITING_STAGES.includes(column.stage);
      if (!isFlight && !isWaiting) continue;
      const items = Array.isArray(column.items) ? column.items : [];
      for (const item of items) {
        if (domainOf(item) !== domain) continue;
        if (isFlight) {
          const klass = item && item.class;
          if (!SLOT_EXCLUDED_CLASSES.includes(klass)) inflight.push(item);
        }
        if (isWaiting) waiting.push(item);
      }
    }
    rows.push({
      domain,
      slots: inflight.slice(0, SLOT_LIMIT),
      overLimit: Math.max(0, inflight.length - SLOT_LIMIT),
      waiting,
    });
  }
  return rows;
}

function rowItemLabel(item) {
  const title = (item && (item.title || item.ref)) || "Untitled";
  const repo = (item && item.repo) || "";
  const klass = (item && item.class) || "";
  const waited = (item && item.waited) || "";
  const parts = [];
  if (repo) parts.push(repo);
  if (klass) parts.push(klass);
  if (waited) parts.push(waited);
  return { title, meta: parts.join(" · ") };
}

function appendItem(list, item, rowClass, metaClass) {
  const { title, meta } = rowItemLabel(item);
  const row = document.createElement("li");
  row.className = rowClass;
  if (item && item.url) {
    const link = document.createElement("a");
    link.href = item.url;
    link.textContent = title;
    row.append(link);
  } else {
    row.append(document.createTextNode(title));
  }
  if (meta) {
    const details = document.createElement("div");
    details.className = metaClass;
    details.textContent = meta;
    row.append(details);
  }
  list.append(row);
}

function renderDomainsHome(container, rows) {
  container.replaceChildren();
  let rendered = 0;
  for (const row of rows) {
    const section = document.createElement("section");
    section.className = "domain-row";
    const heading = document.createElement("h2");
    const link = document.createElement("a");
    link.href = domainHref(row.domain);
    link.textContent = row.domain;
    heading.append(link);
    section.append(heading);

    if (row.slots.length === 0) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "No in-flight projects.";
      section.append(empty);
    } else {
      const list = document.createElement("ul");
      list.className = "slot-list";
      for (const item of row.slots) {
        appendItem(list, item, "slot-card", "slot-meta");
        rendered += 1;
      }
      section.append(list);
    }

    if (row.overLimit > 0) {
      const over = document.createElement("p");
      over.className = "over-limit";
      over.textContent = row.overLimit === 1
        ? "1 more in flight — over the two-slot limit."
        : `${row.overLimit} more in flight — over the two-slot limit.`;
      section.append(over);
    }

    if (row.waiting.length > 0) {
      const sub = document.createElement("h3");
      sub.textContent = "Up next";
      section.append(sub);
      const list = document.createElement("ul");
      list.className = "waiting-list";
      for (const item of row.waiting) {
        appendItem(list, item, "waiting-item", "waiting-meta");
      }
      section.append(list);
    }
    container.append(section);
  }
  return rendered;
}

async function loadDomainsHome() {
  const status = document.querySelector("#domains-status");
  const container = document.querySelector("#domains-rows");
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
  const strip = document.querySelector("#trial-strip");
  if (strip) renderTopStrip(strip, topStrip(snapshot));
  const board = (snapshot && snapshot.board) || {};
  const columns = Array.isArray(board.columns) ? board.columns : [];
  const rows = domainRows(columns);
  const rendered = renderDomainsHome(container, rows);
  status.textContent = rendered === 1
    ? "One in-flight project across 1 domain."
    : `${rendered} in-flight projects across ${rows.length} domains.`;
}

if (typeof document !== "undefined" && typeof window !== "undefined") {
  loadDomainsHome();
}

export {
  IN_FLIGHT_STAGES,
  SLOT_EXCLUDED_CLASSES,
  SLOT_LIMIT,
  WAITING_STAGES,
  domainHref,
  domainRows,
  loadDomainsHome,
  renderDomainsHome,
};
