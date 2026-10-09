import assert from "node:assert/strict";
import { generateKeyPairSync, createSign } from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";

import worker from "../worker.js";
import { renderStagesBoard, stagesBoard } from "../public/stages.js";

function encodeJson(value) {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function accessFixture(overrides = {}) {
  const teamDomain = overrides.teamDomain || `https://${crypto.randomUUID()}.cloudflareaccess.com`;
  const audience = "dashboard-audience";
  const kid = "stages-fixture-key";
  const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
  const publicJwk = publicKey.export({ format: "jwk" });
  Object.assign(publicJwk, { alg: "RS256", kid, use: "sig" });

  function sign(claimOverrides = {}) {
    const now = Math.floor(Date.now() / 1000);
    const header = encodeJson({ alg: "RS256", kid });
    const claims = encodeJson({
      iss: teamDomain,
      aud: audience,
      exp: now + 300,
      ...claimOverrides,
    });
    const unsigned = `${header}.${claims}`;
    const signature = createSign("RSA-SHA256").update(unsigned).sign(privateKey).toString("base64url");
    return `${unsigned}.${signature}`;
  }

  return {
    audience,
    sign,
    teamDomain,
    fetchImpl: async () => Response.json({ keys: [publicJwk] }),
  };
}

// The snapshot's column order already embodies projected_pull_order, so the
// fixture keeps an order no display-side sort would produce: titles run
// zeta, Alpha, mid, and Building follows Ideas.
function projectedOrderSnapshot() {
  return {
    board: {
      columns: [
        { stage: "Ideas", items: [{ ref: "o/z#1", title: "zeta" }] },
        { stage: "Building", items: [
          { ref: "o/z#2", title: "zeta deploy" },
          { ref: "o/a#3", title: "Alpha fix" },
          { ref: "o/m#4", title: "mid refactor" },
        ] },
        { stage: "Done", items: [] },
      ],
    },
  };
}

const snapshotTitles = (columns) => columns.map(
  (column) => [column.stage, column.items.map((item) => item.title)],
);

test("the /stages board keeps the snapshot's projected order", () => {
  const snapshot = projectedOrderSnapshot();
  assert.deepEqual(snapshotTitles(stagesBoard(snapshot)), [
    ["Ideas", ["zeta"]],
    ["Building", ["zeta deploy", "Alpha fix", "mid refactor"]],
    ["Done", []],
  ]);
});

test("the /stages board tolerates a missing or empty board", () => {
  assert.deepEqual(stagesBoard({}), []);
  assert.deepEqual(stagesBoard({ board: {} }), []);
  assert.deepEqual(stagesBoard({ board: { columns: [{ stage: "Ideas" }] } }),
    [{ stage: "Ideas", items: [] }]);
});

class StubNode {
  constructor(tagName) {
    this.tagName = tagName;
    this.children = [];
    this.className = "";
    this.textContent = "";
    this.href = "";
  }

  append(...children) {
    this.children.push(...children);
  }

  replaceChildren() {
    this.children = [];
  }
}

function stubDocument() {
  const previous = globalThis.document;
  globalThis.document = {
    createElement: (tagName) => new StubNode(tagName),
    createTextNode: (value) => ({ nodeValue: value, textContent: value }),
  };
  return () => {
    if (previous === undefined) delete globalThis.document;
    else globalThis.document = previous;
  };
}

function renderedTitles(container) {
  const titles = [];
  for (const section of container.children) {
    const list = section.children.find((child) => child.tagName === "ul");
    if (!list) continue;
    for (const row of list.children) {
      const first = row.children[0];
      titles.push(first.textContent ?? first.nodeValue);
    }
  }
  return titles;
}

test("the rendered /stages rows follow projected order, not the alphabet", () => {
  const restore = stubDocument();
  try {
    const container = new StubNode("main");
    const rendered = renderStagesBoard(container, stagesBoard(projectedOrderSnapshot()));
    assert.equal(rendered, 4);
    assert.deepEqual(renderedTitles(container),
      ["zeta", "zeta deploy", "Alpha fix", "mid refactor"]);
  } finally {
    restore();
  }
});

test("the /stages page does no client-side ordering", async () => {
  const source = await readFile(new URL("../public/stages.js", import.meta.url), "utf8");
  assert.doesNotMatch(source, /\.(?:sort|filter|reverse)\s*\(/);
  assert.match(source, /\/api\/snapshot/);
});

test("/stages refuses an unsigned request, like every other page", async () => {
  const response = await worker.fetch(new Request("https://funnel.nateprich.com/stages"), {});
  assert.equal(response.status, 403);
  assert.equal(await response.text(), "Forbidden");
});

function assetEnv(files, seen) {
  return {
    ASSETS: {
      async fetch(request) {
        const pathname = new URL(request.url).pathname;
        seen.push(pathname);
        // Like the real asset layer, / resolves to the current page.
        const key = pathname === "/" ? "/index.html" : pathname;
        if (!(key in files)) return new Response("Not Found", { status: 404 });
        return new Response(files[key], {
          headers: { "content-type": "text/html; charset=utf-8" },
        });
      },
    },
  };
}

async function authorizedStagesEnv() {
  const access = accessFixture();
  const stagesHtml = await readFile(new URL("../public/stages.html", import.meta.url), "utf8");
  const indexHtml = await readFile(new URL("../public/index.html", import.meta.url), "utf8");
  const seen = [];
  const env = {
    ACCESS_TEAM_DOMAIN: access.teamDomain,
    ACCESS_AUD: access.audience,
    ACCESS_JWKS_FETCH: access.fetchImpl,
    FUNNEL_SNAPSHOT: { async get() { return null; } },
    ...assetEnv({ "/stages.html": stagesHtml, "/index.html": indexHtml }, seen),
  };
  return { access, env, seen, stagesHtml, indexHtml };
}

test("/stages serves the new file behind the Access check", async () => {
  const { access, env, seen, stagesHtml } = await authorizedStagesEnv();
  const response = await worker.fetch(new Request("https://funnel.nateprich.com/stages", {
    headers: { "Cf-Access-Jwt-Assertion": access.sign() },
  }), env);

  assert.equal(response.status, 200);
  assert.deepEqual(seen, ["/stages.html"]);
  assert.equal(await response.text(), stagesHtml);
  assert.equal(response.headers.get("cache-control"), "no-cache, must-revalidate");
});

test("/ still serves the current page byte-identical", async () => {
  const { access, env, seen, indexHtml } = await authorizedStagesEnv();
  const response = await worker.fetch(new Request("https://funnel.nateprich.com/", {
    headers: { "Cf-Access-Jwt-Assertion": access.sign() },
  }), env);

  assert.equal(response.status, 200);
  assert.deepEqual(seen, ["/"]);
  assert.equal(await response.text(), indexHtml);
});
