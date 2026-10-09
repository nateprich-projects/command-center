// Trial stage board (parent plan #2409, ticket #2410): today's stage board in
// the snapshot's own order. The published columns already embody
// projected_pull_order — the current page renders board.columns as received
// — so this page takes them as received too. There is deliberately no
// sorting, filtering, or reversing anywhere in this file: any client-side
// ordering here would misrepresent the producer's rank.

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
  const columns = stagesBoard(snapshot);
  const rendered = renderStagesBoard(container, columns);
  status.textContent = rendered === 1
    ? "One project on the board."
    : `${rendered} projects on the board.`;
}

if (typeof document !== "undefined" && typeof window !== "undefined") {
  loadStagesBoard();
}

export { loadStagesBoard, renderStagesBoard, stagesBoard };
