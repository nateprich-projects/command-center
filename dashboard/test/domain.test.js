import assert from "node:assert/strict";
import { generateKeyPairSync, createSign } from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";

import worker from "../worker.js";
import {
  PHASES,
  UNPHASED,
  domainFromPath,
  domainItems,
  groupByPhase,
  phaseOf,
  renderDomainBoard,
} from "../public/domain.js";

function encodeJson(value) {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function accessFixture(overrides = {}) {
  const teamDomain = overrides.teamDomain || `https://${crypto.randomUUID()}.cloudflareaccess.com`;
  const audience = "dashboard-audience";
  const kid = "domain-fixture-key";
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

test("the detail page has five phase columns plus Not yet phased", () => {
  assert.deepEqual(PHASES, ["Curate", "Describe", "Hypothesize", "Test", "Implement"]);
  assert.equal(UNPHASED, "Not yet phased");
});

// Until the reclass lands, Improve, New and Replace stay out of the phase
// columns; a phased item lands in its own phase.
test("phased items land in their phase; Improve, New and Replace do not", () => {
  assert.equal(phaseOf({ class: "Test" }), "Test");
  assert.equal(phaseOf({ class: "Curate" }), "Curate");
  assert.equal(phaseOf({ phase: "Describe" }), "Describe");
  assert.equal(phaseOf({ class: "Improve" }), null);
  assert.equal(phaseOf({ class: "New" }), null);
  assert.equal(phaseOf({ class: "Replace" }), null);
  assert.equal(phaseOf({ class: "Bug" }), null);
  assert.equal(phaseOf({}), null);
  assert.equal(phaseOf(null), null);
});

function detailSnapshot() {
  return {
    board: {
      columns: [
        { stage: "Ideas", items: [
          { ref: "nateprich-projects/command-center#1", title: "zeta", class: "Test" },
          { ref: "o/mystery-repo#9", title: "other", class: "Curate" },
        ] },
        { stage: "Building", items: [
          { ref: "nateprich-projects/github-runners#2", title: "zeta deploy", class: "Improve" },
          { ref: "nateprich-projects/command-center#3", title: "Alpha fix", class: "Curate" },
          { ref: "nateprich-projects/command-center#4", title: "mid refactor", class: "New" },
        ] },
        { stage: "Done", items: [] },
      ],
    },
  };
}

test("the detail page selects one domain through the shared table and groups stably", () => {
  const columns = detailSnapshot().board.columns;
  const items = domainItems(columns, "Command center");
  assert.deepEqual(items.map((item) => item.title),
    ["zeta", "zeta deploy", "Alpha fix", "mid refactor"]);

  const grouped = groupByPhase(items);
  assert.deepEqual(grouped.map((column) => column.phase),
    ["Curate", "Describe", "Hypothesize", "Test", "Implement", "Not yet phased"]);
  const byPhase = Object.fromEntries(
    grouped.map((column) => [column.phase, column.items.map((item) => item.title)]),
  );
  assert.deepEqual(byPhase.Curate, ["Alpha fix"]);
  assert.deepEqual(byPhase.Test, ["zeta"]);
  assert.deepEqual(byPhase["Not yet phased"], ["zeta deploy", "mid refactor"]);

  // An interim domain works the same way: nothing drops silently.
  const interim = domainItems(columns, "mystery-repo");
  assert.deepEqual(interim.map((item) => item.title), ["other"]);
  assert.deepEqual(
    groupByPhase(interim).find((column) => column.phase === "Curate").items.map((item) => item.title),
    ["other"],
  );

  assert.deepEqual(domainItems(columns, "Nobody here"), []);
});

test("the domain comes from the URL path", () => {
  assert.equal(domainFromPath("/domains/Command%20center"), "Command center");
  assert.equal(domainFromPath("/domains/Fantasy"), "Fantasy");
  assert.equal(domainFromPath("/domains/"), null);
  assert.equal(domainFromPath("/domains"), null);
  assert.equal(domainFromPath("/domains/a/b"), null);
  assert.equal(domainFromPath("/stages"), null);
  assert.equal(domainFromPath(null), null);
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

test("the rendered detail columns keep snapshot order without re-sorting", () => {
  const restore = stubDocument();
  try {
    const columns = detailSnapshot().board.columns;
    const container = new StubNode("main");
    const rendered = renderDomainBoard(
      container, groupByPhase(domainItems(columns, "Command center")),
    );
    assert.equal(rendered, 4);
    const headings = container.children.map((section) => section.children[0].textContent);
    assert.deepEqual(headings, [
      "Curate (1)", "Describe (0)", "Hypothesize (0)",
      "Test (1)", "Implement (0)", "Not yet phased (2)",
    ]);
  } finally {
    restore();
  }
});

test("the detail module reads only the snapshot and never re-orders", async () => {
  const source = await readFile(new URL("../public/domain.js", import.meta.url), "utf8");
  assert.doesNotMatch(source, /\.(?:sort|reverse)\s*\(/);
  assert.match(source, /\/api\/snapshot/);
  assert.doesNotMatch(source, /\/api\/(?:metrics|refresh)/);
  assert.match(source, /domainOf/);
  assert.doesNotMatch(source, /localStorage|IndexedDB|KV|put\(/);
});

test("the KPI tree and findings show Not built yet", async () => {
  const html = await readFile(new URL("../public/domain.html", import.meta.url), "utf8");
  const kpi = html.slice(html.indexOf('id="domain-kpi"'), html.indexOf('id="domain-findings"'));
  assert.match(kpi, /KPI tree/);
  assert.match(kpi, /Not built yet\./);
  const findings = html.slice(html.indexOf('id="domain-findings"'));
  assert.match(findings, /Findings/);
  assert.match(findings, /Not built yet\./);
});

test("/domains/<domain> refuses an unsigned request, like every other page", async () => {
  const response = await worker.fetch(
    new Request("https://funnel.nateprich.com/domains/Command%20center"), {},
  );
  assert.equal(response.status, 403);
  assert.equal(await response.text(), "Forbidden");
});

function assetEnv(files, seen) {
  return {
    ASSETS: {
      async fetch(request) {
        const pathname = new URL(request.url).pathname;
        seen.push(pathname);
        const key = pathname === "/" ? "/index.html" : pathname;
        if (!(key in files)) return new Response("Not Found", { status: 404 });
        return new Response(files[key], {
          headers: { "content-type": "text/html; charset=utf-8" },
        });
      },
    },
  };
}

async function authorizedDomainEnv() {
  const access = accessFixture();
  const domainHtml = await readFile(new URL("../public/domain.html", import.meta.url), "utf8");
  const indexHtml = await readFile(new URL("../public/index.html", import.meta.url), "utf8");
  const seen = [];
  const env = {
    ACCESS_TEAM_DOMAIN: access.teamDomain,
    ACCESS_AUD: access.audience,
    ACCESS_JWKS_FETCH: access.fetchImpl,
    FUNNEL_SNAPSHOT: { async get() { return null; } },
    ...assetEnv({ "/domain.html": domainHtml, "/index.html": indexHtml }, seen),
  };
  return { access, env, seen, domainHtml, indexHtml };
}

test("/domains/<domain> serves the new file behind the Access check", async () => {
  const { access, env, seen, domainHtml } = await authorizedDomainEnv();
  const response = await worker.fetch(new Request("https://funnel.nateprich.com/domains/Command%20center", {
    headers: { "Cf-Access-Jwt-Assertion": access.sign() },
  }), env);

  assert.equal(response.status, 200);
  assert.deepEqual(seen, ["/domain.html"]);
  assert.equal(await response.text(), domainHtml);
  assert.equal(response.headers.get("cache-control"), "no-cache, must-revalidate");
});

test("/ still serves the current page byte-identical", async () => {
  const { access, env, seen, indexHtml } = await authorizedDomainEnv();
  const response = await worker.fetch(new Request("https://funnel.nateprich.com/", {
    headers: { "Cf-Access-Jwt-Assertion": access.sign() },
  }), env);

  assert.equal(response.status, 200);
  assert.deepEqual(seen, ["/"]);
  assert.equal(await response.text(), indexHtml);
});

test("the detail route serves only the new file", async () => {
  const source = await readFile(new URL("../worker.js", import.meta.url), "utf8");
  const block = source.slice(source.indexOf('"/domains/"'));
  assert.match(block, /\/domain\.html/);
  assert.doesNotMatch(block, /index\.html|app\.js|styles\.css/);
});
