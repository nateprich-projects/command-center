// Shared top strip for the trial pages (parent plan #2409, ticket #2412).
// Mounted on /stages now; /domains reuses this module later. It reads only
// what /api/snapshot already holds: brief.total_needing_nate, the board
// columns' classes, and usage. No new store, no new fetch, no snapshot change.
//
// Weekly usage is shown against days elapsed in the provider's own weekly
// window: Muse weeks open Monday 00:00 UTC (mirrors usage.muse_window_start),
// Claude weeks open Saturday 12:00 local (mirrors usage.last_weekly_reset).
// The pace figure is the even-burn line: elapsed days / 7 * 100. A positive
// delta means ahead of that line (burning faster than even).

const DAY_MS = 24 * 3600 * 1000;
const WEEK_MS = 7 * DAY_MS;

// Urgent classes for the strip headline (parent plan #2409). Bugs are counted
// separately, beside the urgent count, never inside it.
const URGENT_CLASSES = ["Broken", "Investigate", "Maintenance"];
const BUG_CLASS = "Bug";
// Done is history, never urgent: counting a finished Broken project as
// "urgent work" would keep cleared work on the headline forever.
const HISTORY_STAGES = ["Done"];

// Monday 00:00 UTC at or before nowMs.
function museWeekStartMs(nowMs) {
  const at = new Date(nowMs);
  const midnight = Date.UTC(at.getUTCFullYear(), at.getUTCMonth(), at.getUTCDate());
  const back = (at.getUTCDay() + 6) % 7;
  return midnight - back * DAY_MS;
}

// Saturday 12:00 local at or before nowMs.
function claudeWeekStartMs(nowMs) {
  const at = new Date(nowMs);
  const candidate = new Date(
    at.getFullYear(), at.getMonth(), at.getDate(), 12, 0, 0, 0,
  );
  const pythonWeekday = (candidate.getDay() + 6) % 7;
  candidate.setDate(candidate.getDate() - ((pythonWeekday - 5 + 7) % 7));
  if (candidate.getTime() > nowMs) candidate.setDate(candidate.getDate() - 7);
  return candidate.getTime();
}

function daysElapsed(startMs, nowMs) {
  if (!Number.isFinite(startMs) || !Number.isFinite(nowMs)) return null;
  return Math.max(0, (nowMs - startMs) / DAY_MS);
}

// Even-burn pace for elapsedDays into a 7-day window, as a percent.
function expectedPacePercent(elapsedDays) {
  if (typeof elapsedDays !== "number" || !Number.isFinite(elapsedDays)) return null;
  const clamped = Math.min(7, Math.max(0, elapsedDays));
  return (clamped / 7) * 100;
}

// Weekly usage against days elapsed. Returns {state: "unavailable"} unless
// usedPercent is a finite number; otherwise {state: "known", used, expected,
// delta, elapsedDays}.
function paceFigures(usedPercent, elapsedDays) {
  if (typeof usedPercent !== "number" || !Number.isFinite(usedPercent)) {
    return { state: "unavailable" };
  }
  const expected = expectedPacePercent(elapsedDays);
  if (expected === null) return { state: "unavailable" };
  return {
    state: "known",
    used: usedPercent,
    expected,
    delta: usedPercent - expected,
    elapsedDays,
  };
}

// Muse 7-day spend from usage.muse, against its Monday-UTC week.
function museWeekly(usage, nowMs) {
  const muse = usage && usage.muse;
  const spent = muse && muse.spent_dollars;
  const cap = muse && muse.cap_dollars;
  const percent = muse && muse.used_percent;
  for (const value of [spent, cap, percent]) {
    if (typeof value !== "number" || !Number.isFinite(value)) {
      return { state: "unavailable" };
    }
  }
  if (cap <= 0 || spent < 0) return { state: "unavailable" };
  return {
    ...paceFigures(percent, daysElapsed(museWeekStartMs(nowMs), nowMs)),
    spent,
    cap,
  };
}

// Claude weekly percent from usage.claude ({u: {sd}, t}), against its
// Saturday-noon-local week. The sample timestamp is carried for display only;
// pace is measured from nowMs, not from when the sample landed.
function claudeWeekly(usage, nowMs) {
  const claude = usage && usage.claude;
  const percent = claude && claude.u && claude.u.sd;
  if (typeof percent !== "number" || !Number.isFinite(percent)) {
    return { state: "unavailable" };
  }
  if (percent < 0 || percent > 100) return { state: "unavailable" };
  return paceFigures(percent, daysElapsed(claudeWeekStartMs(nowMs), nowMs));
}

// Urgent and Bug counts across board columns, excluding history stages.
function countUrgent(columns) {
  let urgent = 0;
  let bugs = 0;
  for (const column of Array.isArray(columns) ? columns : []) {
    if (!column || HISTORY_STAGES.includes(column.stage)) continue;
    for (const item of column.items || []) {
      if (!item || typeof item.class !== "string") continue;
      if (URGENT_CLASSES.includes(item.class)) urgent += 1;
      else if (item.class === BUG_CLASS) bugs += 1;
    }
  }
  return { urgent, bugs };
}

// The whole strip as data, for render and for tests. needingNate is null when
// the brief carries no readable total, never a guessed zero.
function topStrip(snapshot, nowMs = Date.now()) {
  const brief = (snapshot && snapshot.brief) || {};
  const total = brief.total_needing_nate;
  return {
    needingNate: Number.isSafeInteger(total) && total >= 0 ? total : null,
    ...countUrgent(snapshot && snapshot.board && snapshot.board.columns),
    muse: museWeekly(snapshot && snapshot.usage, nowMs),
    claude: claudeWeekly(snapshot && snapshot.usage, nowMs),
  };
}

function formatPercent(value) {
  return `${value.toFixed(1)}%`;
}

function usageLine(label, figure) {
  if (!figure || figure.state !== "known") return `${label}: unavailable`;
  const pace = figure.delta >= 0 ? "ahead of pace" : "behind pace";
  return `${label} ${formatPercent(figure.used)} · ` +
    `day ${figure.elapsedDays.toFixed(1)} of 7 ` +
    `(pace ${formatPercent(figure.expected)}, ${pace})`;
}

function renderTopStrip(container, summary) {
  container.replaceChildren();
  const waiting = document.createElement("p");
  waiting.className = "trial-strip-waiting";
  waiting.textContent = summary.needingNate === null
    ? "Waiting on you: could not be read."
    : `${summary.needingNate} waiting on you.`;
  container.append(waiting);

  const usage = document.createElement("p");
  usage.className = "trial-strip-usage";
  usage.textContent = [usageLine("Muse", summary.muse), usageLine("Claude", summary.claude)].join(" · ");
  container.append(usage);

  const urgent = document.createElement("p");
  urgent.className = "trial-strip-urgent";
  urgent.textContent = `${summary.urgent} urgent · ${summary.bugs} bugs.`;
  container.append(urgent);
}

export {
  BUG_CLASS,
  URGENT_CLASSES,
  claudeWeekStartMs,
  claudeWeekly,
  countUrgent,
  daysElapsed,
  expectedPacePercent,
  museWeekStartMs,
  museWeekly,
  paceFigures,
  renderTopStrip,
  topStrip,
  usageLine,
};
