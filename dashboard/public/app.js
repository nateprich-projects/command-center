const STAGES = ["Ideas", "Shaped", "Ready", "Building", "Parked", "Done"];

const DIAGNOSTICS = [
  ["working_tree_touched", "Checkout changes"],
  ["human_steps", "Human steps"],
  ["machine_local_steps", "Machine-local steps"],
  ["closed_itself", "Projects closed by the funnel"],
  ["cleared_blocks", "Blocks cleared by the funnel"],
  ["unattended_approvals", "Unattended approvals"],
  ["recorded_cause_regressions", "Broken projects with recorded cause"],
  ["command_center_ticket_pr_share", "Command Center ticket PR share"],
  ["blocked", "Blocked"],
  ["parked", "Parked"],
  ["prose_dependencies", "Prose dependencies"],
  ["suspected_human_steps", "Suspected human steps"],
  ["unclassed_captures", "Unclassed captures"],
  ["needs_class", "Needs Class"],
  ["stale_locks_taken_over", "Stale locks taken over"],
  ["stranded", "Stranded work"],
  ["in_motion", "In motion"],
  ["awaiting_breakdown", "Awaiting breakdown"],
  ["unattended_merges", "Unattended merges"],
  ["agent_health", "Agent health"],
  ["resend_ratio", "Resend ratio"],
  ["rejected_merges", "Rejected merges"],
  ["degraded", "Degraded sections"],
  ["closed_with_access_vocabulary", "Closed with access vocabulary"],
];

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
  if (board && Array.isArray(board.columns)) {
    return board.columns;
  }

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
      if (Array.isArray(board[column.stage])) {
        column.items = board[column.stage];
      }
    }
  }
  return columns;
}

function textElement(tag, className, value) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  element.textContent = String(value);
  return element;
}

function linkedTitle(item) {
  const title = item.title || item.ref || "Untitled";
  if (typeof item.url !== "string" || !item.url.startsWith("https://github.com/")) {
    return textElement("span", "item-title", title);
  }
  const link = textElement("a", "item-title", title);
  link.href = item.url;
  link.rel = "noreferrer";
  return link;
}

function itemCard(item) {
  const card = document.createElement("article");
  card.className = "item-card";
  card.append(linkedTitle(item));
  const metadata = [];
  for (const detail of [item.class, item.pinned ? "Pinned" : null, item.waiting_on, item.waited]) {
    if (detail) metadata.push(detail);
  }
  if (metadata.length) card.append(textElement("p", "item-meta", metadata.join(" · ")));
  if (item.ref) card.append(textElement("p", "item-ref", item.ref));
  return card;
}

function diagnosticRow(value) {
  const row = document.createElement("li");
  if (typeof value === "string" || typeof value === "number") {
    row.textContent = String(value);
    return row;
  }
  if (value && typeof value === "object") {
    const lead = value.title || value.ref || value.section || value.reason;
    row.textContent = lead ? `${lead} — ${JSON.stringify(value)}` : JSON.stringify(value);
    return row;
  }
  row.textContent = String(value);
  return row;
}

function renderDiagnostic(container, key, label, value) {
  if (!present(value)) return;
  const section = document.createElement("section");
  section.className = "diagnostic";
  section.dataset.section = key;
  section.append(textElement("h3", null, label));
  const list = document.createElement("ul");
  const values = Array.isArray(value) ? value : [value];
  for (const row of values) list.append(diagnosticRow(row));
  section.append(list);
  container.append(section);
}

function renderBrief(brief) {
  const container = document.querySelector("#brief");
  container.replaceChildren();

  if (present(brief.missing)) {
    const warning = textElement("p", "warning", "This brief is partial. Some sections could not be read.");
    container.append(warning);
    renderDiagnostic(container, "missing", "Missing sections", brief.missing);
  }

  if (brief.total_needing_nate === 0) {
    container.append(document.querySelector("#empty-state").content.cloneNode(true));
  } else {
    container.append(textElement("p", "brief-count", `${brief.total_needing_nate ?? "?"} decisions waiting`));
    const items = document.createElement("div");
    items.className = "brief-items";
    for (const item of brief.items || []) items.append(itemCard(item));
    container.append(items);
  }

  for (const [key, label] of DIAGNOSTICS.slice(0, 6)) {
    renderDiagnostic(container, key, label, brief[key]);
  }

  if (present(brief.counts_by_gate)) {
    const counts = Object.entries(brief.counts_by_gate)
      .map(([gate, count]) => `${gate}: ${count}`)
      .join(" · ");
    container.append(textElement("p", "gate-counts", counts));
  }

  if (present(brief.maintenance_load) || present(brief.disposal)) {
    const metrics = document.createElement("section");
    metrics.className = "diagnostic";
    metrics.append(textElement("h3", null, "Portfolio signals"));
    metrics.append(textElement("pre", null, JSON.stringify({
      maintenance_load: brief.maintenance_load,
      disposal: brief.disposal,
    }, null, 2)));
    container.append(metrics);
  }

  for (const [key, label] of DIAGNOSTICS.slice(6)) {
    renderDiagnostic(container, key, label, brief[key]);
  }
}

function ticketProgress(item) {
  const closed = item.tickets_closed ?? item.tickets?.closed;
  const total = item.tickets_total ?? item.tickets?.total;
  return Number.isFinite(closed) && Number.isFinite(total) ? `${closed}/${total} tickets` : null;
}

function shortRepo(value) {
  if (typeof value !== "string") return null;
  const pieces = value.split("/");
  return pieces[pieces.length - 1];
}

function renderBoard(board) {
  const container = document.querySelector("#board");
  container.replaceChildren();
  for (const column of boardColumns(board)) {
    const section = document.createElement("section");
    section.className = "board-column";
    section.dataset.stage = column.stage;
    section.append(textElement("h3", null, column.stage));
    const items = Array.isArray(column.items) ? column.items : [];
    if (items.length === 0) {
      section.append(textElement("p", "column-empty", "None"));
    } else {
      for (const item of items) {
        const card = itemCard(item);
        const details = [
          shortRepo(item.repo || item.repository),
          ticketProgress(item),
        ];
        const metadata = [];
        for (const detail of details) if (detail) metadata.push(detail);
        if (metadata.length) card.append(textElement("p", "item-meta", metadata.join(" · ")));
        section.append(card);
      }
    }
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
  status.textContent = `${generatedAt ? `Snapshot ${age(generatedAt)}` : "Snapshot age unknown"} · ${failure ? "Last brief failed" : "Last brief succeeded"}`;
  status.classList.toggle("failed", Boolean(failure));
  renderBrief(snapshot.brief || {});
  renderBoard(snapshot.board || {});
}

async function requestRefresh() {
  const button = document.querySelector("#refresh");
  button.disabled = true;
  try {
    const response = await fetch("/api/refresh", { method: "POST" });
    if (!response.ok) throw new Error(`Refresh returned ${response.status}`);
    button.textContent = "Refresh requested";
  } catch (error) {
    button.textContent = error.message;
  } finally {
    window.setTimeout(() => {
      button.disabled = false;
      button.textContent = "Request refresh";
    }, 3000);
  }
}

if (typeof document !== "undefined") {
  document.querySelector("#refresh").addEventListener("click", requestRefresh);
  loadSnapshot().catch((error) => {
    document.querySelector("#snapshot-status").textContent = error.message;
    document.querySelector("#snapshot-status").classList.add("failed");
  });
}

export { DIAGNOSTICS, STAGES, age, boardColumns, failureState, shortRepo };
