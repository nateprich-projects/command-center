// Trial domain mapping (parent plan #2409, ticket #2411): one table mapping
// repository names to trial domains. Held here so the /stages filter and the
// later domains pages share it. Domains are display-only; the snapshot and
// the Project carry no domain field.

const REPO_TO_DOMAIN = {
  "command-center": "Command center",
  "github-runners": "Command center",
  "The-League": "The League",
  AFL: "AFL",
  "Fantasy-GM": "Fantasy",
  workbench: "Personal OS",
  "career-toolset": "Career",
  "jeffy-finance-agent": "Jeffy",
};

function shortRepoName(value) {
  if (typeof value !== "string" || !value) return null;
  const pieces = value.split("/");
  return pieces[pieces.length - 1] || null;
}

// The filter key is the full owner/repo, read from the ref first: two owners
// can hold a repository of the same name, and a board row's own `repo`
// field is only the short name. Mirrors repoOf in app.js.
function repoFullName(entry) {
  if (!entry || typeof entry !== "object") return null;
  if (typeof entry.ref === "string" && entry.ref.includes("#")) {
    return entry.ref.split("#")[0] || null;
  }
  if (typeof entry.repo === "string" && entry.repo) return entry.repo;
  if (typeof entry.repository === "string" && entry.repository) {
    return entry.repository;
  }
  return null;
}

// Unknown repos map to an interim domain per repo: the repository's own
// short name, so nothing ever drops silently.
function domainOf(entryOrRepo) {
  const full = typeof entryOrRepo === "string"
    ? entryOrRepo
    : repoFullName(entryOrRepo);
  const short = shortRepoName(full);
  if (!short) return null;
  return REPO_TO_DOMAIN[short] || short;
}

// Unique domains across board columns, in first-seen order. Ordering the
// filter labels is display order only; board rows keep producer order.
function listDomains(columns) {
  const seen = [];
  const known = new Set();
  for (const column of Array.isArray(columns) ? columns : []) {
    for (const item of (column && column.items) || []) {
      const domain = domainOf(item);
      if (domain && !known.has(domain)) {
        known.add(domain);
        seen.push(domain);
      }
    }
  }
  return seen;
}

export { REPO_TO_DOMAIN, domainOf, listDomains, repoFullName, shortRepoName };
