import assert from "node:assert/strict";
import { generateKeyPairSync, createSign } from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";

import worker from "../worker.js";
import {
  SLOT_LIMIT,
  domainHref,
  domainRows,
  renderDomainsHome,
} from "../public/domains-home.js";

function encodeJson(value) {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function accessFixture(overrides = {}) {
  const teamDomain = overrides.teamDomain || `https://${crypto.randomUUID()}.cloudflareaccess.com`;
  const audience = "dashboard-audience";
  const kid = "domains-home-fixture-key";
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

// Three in-flight non-urgent projects in one domain: snapshot order is the
// producer's oldest-first order, so the first two are the slots and the
// third is one over the two-slot limit. Urgent classes and Bugs hold no
// slot, Done and Parked are history rather than in flight, and Ideas items
// wait up next instead of taking slots.
function homeColumns() {
  const cc = (title, klass) => ({
    repo: "command-center",
    title,
    class: klass,
    url: `https://example.invalid/${encodeURIComponent(title)}`,
    waited: "1 day",
  });
  return [
    { stage: "Ideas", items: [cc("next idea", "New")] },
    { stage: "Shaped", items: [cc("oldest shaped", "Improve")] },
    { stage: "Ready", items: [cc("ready repair", "Improve")] },
    { stage: "Building", items: [
      cc("building fix", "Improve"),
      cc("broken outage", "Broken"),
      cc("latent defect", "Bug"),
      cc("probe question", "Investigate"),
      cc("degrading service", "Maintenance"),
    ] },
    { stage: "Parked", items: [cc("parked work", "Improve")] },
    { stage: "Done", items: [cc("finished work", "Broken")] },
  ];
}

test("the slot limit is two", () => {
  assert.equal(SLOT_LIMIT, 2);
});

test("a domain with three in-flight non-urgent projects shows two slots with one over", () => {
  const rows = domainRows(homeColumns());
  assert.equal(rows.length, 1);
  assert.equal(rows[0].domain, "Command center");
  assert.deepEqual(rows[0].slots.map((item) => item.title),
    ["oldest shaped", "ready repair"]);
  assert.equal(rows[0].overLimit, 1);
  assert.deepEqual(rows[0].waiting.map((item) => item.title), ["next idea"]);
});

test("urgent, Bug, Parked and Done items never take a slot", () => {
  const rows = domainRows(homeColumns());
  const slotTitles = rows[0].slots.map((item) => item.title);
  for (const title of [
    "broken outage", "latent defect", "probe question",
    "degrading service", "parked work", "finished work",
  ]) {
    assert.ok(!slotTitles.includes(title), `${title} must not take a slot`);
  }
  assert.equal(rows[0].slots.length + rows[0].overLimit, 3);
});

test("an interim domain gets its own row and slots", () => {
  const rows = domainRows([
    { stage: "Building", items: [
      { ref: "o/mystery-repo#9", title: "mystery build", class: "Improve" },
    ] },
    { stage: "Ideas", items: [
      { ref: "o/mystery-repo#10", title: "mystery idea", class: "New" },
    ] },
  ]);
  assert.deepEqual(rows.map((row) => row.domain), ["mystery-repo"]);
  assert.deepEqual(rows[0].slots.map((item) => item.title), ["mystery build"]);
  assert.equal(rows[0].overLimit, 0);
  assert.deepEqual(rows[0].waiting.map((item) => item.title), ["mystery idea"]);
});

test("rows link to the detail page", () => {
  assert.equal(domainHref("Command center"), "/domains/Command%20center");
  assert.equal(domainHref("Fantasy"), "/domains/Fantasy");
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

function allText(node) {
  const parts = [];
  if (node.textContent) parts.push(node.textContent);
  for (const child of node.children || []) {
    if (typeof child === "string") parts.push(child);
    else parts.push(allText(child));
  }
  return parts.join(" ");
}

function hrefs(node) {
  const found = [];
  const walk = (entry) => {
    if (!entry || typeof entry === "string") return;
    if (entry.href) found.push(entry.href);
    for (const child of entry.children || []) walk(child);
  };
  walk(node);
  return found;
}

test("the rendered rows show slots, the over count and links to detail", () => {
  const restore = stubDocument();
  try {
    const container = new StubNode("main");
    const rendered = renderDomainsHome(container, domainRows(homeColumns()));
    assert.equal(rendered, 2);
    const text = allText(container);
    assert.match(text, /oldest shaped/);
    assert.match(text, /ready repair/);
    assert.match(text, /1 more in flight/);
    assert.match(text, /over the two-slot limit/);
    assert.match(text, /Up next/);
    assert.match(text, /next idea/);
    assert.ok(hrefs(container).includes("/domains/Command%20center"));
  } finally {
    restore();
  }
});

test("the home module reads only the snapshot and never re-orders", async () => {
  const source = await readFile(new URL("../public/domains-home.js", import.meta.url), "utf8");
  assert.doesNotMatch(source, /\.(?:sort|reverse)\s*\(/);
  assert.match(source, /\/api\/snapshot/);
  assert.doesNotMatch(source, /\/api\/(?:metrics|refresh)/);
  assert.match(source, /domainOf/);
  assert.match(source, /topStrip/);
  assert.doesNotMatch(source, /localStorage|IndexedDB|KV|put\(/);
});

test("the home page reuses the shared table and top strip", async () => {
  const html = await readFile(new URL("../public/domains.html", import.meta.url), "utf8");
  const home = await readFile(new URL("../public/domains-home.js", import.meta.url), "utf8");
  assert.match(html, /\/domains-home\.js/);
  assert.match(html, /id="trial-strip"/);
  assert.match(html, /id="domains-rows"/);
  assert.match(home, /\.\/domains\.js/);
  assert.match(home, /\.\/topstrip\.js/);
});

test("/domains refuses an unsigned request, like every other page", async () => {
  const response = await worker.fetch(new Request("https://funnel.nateprich.com/domains"), {});
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

async function authorizedHomeEnv() {
  const access = accessFixture();
  const homeHtml = await readFile(new URL("../public/domains.html", import.meta.url), "utf8");
  const indexHtml = await readFile(new URL("../public/index.html", import.meta.url), "utf8");
  const seen = [];
  const env = {
    ACCESS_TEAM_DOMAIN: access.teamDomain,
    ACCESS_AUD: access.audience,
    ACCESS_JWKS_FETCH: access.fetchImpl,
    FUNNEL_SNAPSHOT: { async get() { return null; } },
    ...assetEnv({ "/domains.html": homeHtml, "/index.html": indexHtml }, seen),
  };
  return { access, env, seen, homeHtml, indexHtml };
}

test("/domains serves the new file behind the Access check", async () => {
  const { access, env, seen, homeHtml } = await authorizedHomeEnv();
  const response = await worker.fetch(new Request("https://funnel.nateprich.com/domains", {
    headers: { "Cf-Access-Jwt-Assertion": access.sign() },
  }), env);

  assert.equal(response.status, 200);
  assert.deepEqual(seen, ["/domains.html"]);
  assert.equal(await response.text(), homeHtml);
  assert.equal(response.headers.get("cache-control"), "no-cache, must-revalidate");
});

test("/ still serves the current page byte-identical", async () => {
  const { access, env, seen, indexHtml } = await authorizedHomeEnv();
  const response = await worker.fetch(new Request("https://funnel.nateprich.com/", {
    headers: { "Cf-Access-Jwt-Assertion": access.sign() },
  }), env);

  assert.equal(response.status, 200);
  assert.deepEqual(seen, ["/"]);
  assert.equal(await response.text(), indexHtml);
});

test("the /domains route serves only the new file", async () => {
  const source = await readFile(new URL("../worker.js", import.meta.url), "utf8");
  const start = source.indexOf('=== "/domains"');
  assert.ok(start !== -1, "the exact /domains route exists");
  const block = source.slice(start, source.indexOf('"/domains/"', start));
  assert.match(block, /\/domains\.html/);
  assert.doesNotMatch(block, /index\.html|app\.js|styles\.css/);
});
