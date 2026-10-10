// Trial stage board (parent plan #2409, tickets #2410, #2411, #2412):
// today's stage board in the snapshot's own order. The published columns
// already embody projected_pull_order — the current page renders
// board.columns as received — so this page takes them as received too.
// There is deliberately no sorting or reversing anywhere in this file: any
// client-side ordering here would misrepresent the producer's rank. The one
// filter on this page narrows rows to a single trial domain (see
// domains.js); it keeps every kept row in the snapshot's order and drops
// nothing silently — repos with no table entry appear under an interim
// domain named for the repo itself. The top strip (see topstrip.js) reports
// the same snapshot's waiting total, weekly usage pace and urgent counts.

import { domainOf, listDomains } from "./domains.js";
import { renderTopStrip, topStrip } from "./topstrip.js";

function stagesBoard(snapshot) {
  const board = (snapshot && snapshot.board) || {};
  if (!Array.isArray(board.columns)) return [];
  const columns = [];
  for (const column of board.columns) {
    columns.push({
      stage: column.stage,
      items: Array.isArray(column.items) ? column.items.slice() : [],
    });
  }
  return columns;
}

function filterColumnsByDomain(columns, domain) {
  if (!domain) return columns;
  return columns.map((column) => ({
    stage: column.stage,
    items: column.items.filter((item) => domainOf(item) === domain),
  }));
}

function selectedDomainFromUrl(url) {
  try {
    const value = new URL(url, "https://trial.invalid").searchParams.get("domain");
    return value || null;
  } catch {
    return null;
  }
}

function stageItemLabel(item) {
  const title = item.title || item.ref || "Untitled";
  const repo = item.repo || "";
  const klass = item.class || "";
  const waited = item.waited || "";
  const parts = [];
  if (repo) parts.push(repo);
  if (klass) parts.push(klass);
  if (waited) parts.push(waited);
  return { title, meta: parts.join(" · ") };
}

function renderDomainFilters(container, domains, selected) {
  container.replaceChildren();
  const all = document.createElement("a");
  all.href = "?";
  all.textContent = "All domains";
  if (!selected) all.setAttribute("aria-current", "true");
  all.dataset.domain = "";
  container.append(all);
  for (const domain of domains) {
    const link = document.createElement("a");
    link.href = `?domain=${encodeURIComponent(domain)}`;
    link.textContent = domain;
    if (domain === selected) link.setAttribute("aria-current", "true");
    link.dataset.domain = domain;
    container.append(link);
  }
}

function renderStagesBoard(container, columns) {
  container.replaceChildren();
  let rendered = 0;
  for (const column of columns) {
    const section = document.createElement("section");
    section.className = "stage";
    const heading = document.createElement("h2");
    heading.textContent = `${column.stage} (${column.items.length})`;
    section.append(heading);
    if (column.items.length === 0) {
      const empty = document.createElement("p");
      empty.className = "empty";
      empty.textContent = "Nothing here.";
      section.append(empty);
    } else {
      const list = document.createElement("ul");
      list.className = "stage-list";
      for (const item of column.items) {
        const { title, meta } = stageItemLabel(item);
        const row = document.createElement("li");
        row.className = "stage-item";
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
          details.className = "stage-meta";
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

async function loadStagesBoard() {
  const status = document.querySelector("#stages-status");
  const container = document.querySelector("#stages-board");
  const filters = document.querySelector("#stages-domains");
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
  const columns = stagesBoard(snapshot);
  const domains = listDomains(columns);
  const selected = filters
    ? selectedDomainFromUrl(window.location.href)
    : null;
  const visible = selected && domains.includes(selected)
    ? filterColumnsByDomain(columns, selected)
    : columns;
  if (filters) {
    renderDomainFilters(filters, domains, selected && domains.includes(selected) ? selected : null);
    filters.addEventListener("click", (event) => {
      const link = event.target.closest("a[data-domain]");
      if (!link) return;
      event.preventDefault();
      const next = link.dataset.domain || null;
      const url = new URL(window.location.href);
      if (next) url.searchParams.set("domain", next);
      else url.searchParams.delete("domain");
      window.history.replaceState(null, "", url);
      const shown = next ? filterColumnsByDomain(columns, next) : columns;
      renderDomainFilters(filters, domains, next);
      const rendered = renderStagesBoard(container, shown);
      status.textContent = rendered === 1
        ? "One project on the board."
        : `${rendered} projects on the board.`;
    });
  }
  const rendered = renderStagesBoard(container, visible);
  status.textContent = rendered === 1
    ? "One project on the board."
    : `${rendered} projects on the board.`;
}

if (typeof document !== "undefined" && typeof window !== "undefined") {
  loadStagesBoard();
}

export { filterColumnsByDomain, loadStagesBoard, renderDomainFilters, renderStagesBoard, selectedDomainFromUrl, stagesBoard };
