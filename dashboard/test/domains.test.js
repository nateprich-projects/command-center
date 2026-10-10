import assert from "node:assert/strict";
import test from "node:test";

import {
  REPO_TO_DOMAIN,
  domainOf,
  listDomains,
} from "../public/domains.js";
import { filterColumnsByDomain, stagesBoard } from "../public/stages.js";

test("the repo-to-domain table maps every trial repo", () => {
  assert.deepEqual(REPO_TO_DOMAIN, {
    "command-center": "Command center",
    "github-runners": "Command center",
    "The-League": "The League",
    AFL: "AFL",
    "Fantasy-GM": "Fantasy",
    workbench: "Personal OS",
    "career-toolset": "Career",
    "jeffy-finance-agent": "Jeffy",
  });
});

test("mapped repos resolve through refs and short repo fields", () => {
  assert.equal(domainOf({ ref: "nateprich-projects/command-center#12" }), "Command center");
  assert.equal(domainOf({ ref: "nateprich-projects/github-runners#3" }), "Command center");
  assert.equal(domainOf({ ref: "nateprich-projects/The-League#4" }), "The League");
  assert.equal(domainOf({ ref: "nateprich-projects/Fantasy-GM#5" }), "Fantasy");
  assert.equal(domainOf({ repo: "workbench" }), "Personal OS");
  assert.equal(domainOf({ repo: "nateprich-projects/career-toolset" }), "Career");
  assert.equal(domainOf({ repository: "jeffy-finance-agent" }), "Jeffy");
  // The ref's full owner/repo wins over a short `repo` row field, so two
  // owners' same-named repositories stay apart.
  assert.equal(
    domainOf({ ref: "owner/workbench#1", repo: "command-center" }),
    "Personal OS",
  );
});

// Unknown repos map to an interim domain per repo instead of dropping
// silently; the /stages filter then narrows to that interim domain while
// keeping the snapshot's projected order.
test("an unmapped repo appears under its interim domain and the filter narrows to it", () => {
  const columns = stagesBoard({
    board: {
      columns: [
        { stage: "Ideas", items: [{ ref: "o/mystery-repo#9", title: "zeta" }] },
        { stage: "Building", items: [
          { ref: "nateprich-projects/command-center#1", title: "zeta deploy" },
          { ref: "o/mystery-repo#2", title: "Alpha fix" },
          { ref: "nateprich-projects/github-runners#3", title: "mid refactor" },
        ] },
        { stage: "Done", items: [] },
      ],
    },
  });

  assert.equal(domainOf({ ref: "o/mystery-repo#9" }), "mystery-repo");
  assert.deepEqual(listDomains(columns), [
    "mystery-repo",
    "Command center",
  ]);

  const narrowed = filterColumnsByDomain(columns, "mystery-repo");
  assert.deepEqual(
    narrowed.map((column) => [column.stage, column.items.map((item) => item.title)]),
    [
      ["Ideas", ["zeta"]],
      ["Building", ["Alpha fix"]],
      ["Done", []],
    ],
  );

  const kept = filterColumnsByDomain(columns, "Command center");
  assert.deepEqual(
    kept.flatMap((column) => column.items.map((item) => item.title)),
    ["zeta deploy", "mid refactor"],
  );
});
