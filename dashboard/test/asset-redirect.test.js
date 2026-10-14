import assert from "node:assert/strict";
import { generateKeyPairSync, createSign } from "node:crypto";
import { readFile } from "node:fs/promises";
import test from "node:test";

import worker from "../worker.js";

// Ticket #2483 acceptance: the deployed worker looped because Workers Assets
// answers a .html path with a redirect to its extensionless path, and with
// run_worker_first = true the extensionless /stages and /domains paths
// re-enter the worker. The pre-existing ASSETS stubs never redirected, so
// they passed while production looped. This stub reproduces the redirect,
// so a .html fetch fails here the way it failed in production.
//
// Premise record, verified against origin/main before the fix (working-copy
// lines identical except worker.js:418,433,451, which the fix changes):
// - dashboard/worker.js:360 `handleRequest` serves the routes; :199
//   `isAuthorized` gates every route at :368 (only /api/github-webhook at
//   :365 is exempt), so the Access check fronts /stages, /domains and
//   /domains/<d> unchanged.
// - /stages (:411 `pathname === "/stages"`) and /domains (:426) are worker
//   routes, so an extensionless redirect to either re-enters the worker and
//   loops when the worker itself fetches a .html path.
// - /domain is not a worker route: no `pathname === "/domain"` condition
//   exists (grep empty); `/domain` appears only as the ASSETS fetch target
//   inside the "/domains/" block (:442 startsWith, :451 fetch), so the old
//   301 to /domain rendered while dropping the domain segment.
// - Base fetches were /stages.html (:418), /domains.html (:433) and
//   /domain.html (:451).
// - dashboard/wrangler.toml:13 sets run_worker_first = true with no
//   html_handling line, so default html_handling applies and is kept.
// - The pre-existing stub dashboard/test/stages.test.js:205 `assetEnv`
//   never redirects: no 301/302/redirect/location in the base test file; it
//   answers 200 directly, which is why it passed while production looped.
// - CI proof: .github/workflows/tests.yml:54-55 runs
//   `npm --prefix dashboard test`, and dashboard/package.json:8 defines
//   that as `node --test test/*.test.js`, so this file runs in CI.

function encodeJson(value) {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function accessFixture() {
  const teamDomain = `https://${crypto.randomUUID()}.cloudflareaccess.com`;
  const audience = "dashboard-audience";
  const kid = "redirect-fixture-key";
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

// Faithful to Workers Assets default html_handling: a .html asset path
// answers 301 to its extensionless path, while an extensionless path serves
// its file directly. run_worker_first sends that extensionless path back
// through the worker, so a worker that fetches .html paths loops.
function redirectFaithfulAssets(files, seen) {
  return {
    ASSETS: {
      async fetch(request) {
        const pathname = new URL(request.url).pathname;
        seen.push(pathname);
        if (pathname.endsWith(".html")) {
          const target = pathname.slice(0, -".html".length) || "/";
          return new Response(null, {
            status: 301,
            headers: { location: target },
          });
        }
        const key = pathname === "/" ? "/index.html" : pathname;
        if (!(key in files)) return new Response("Not Found", { status: 404 });
        return new Response(files[key], {
          headers: { "content-type": "text/html; charset=utf-8" },
        });
      },
    },
  };
}

async function authorizedRedirectEnv() {
  const access = accessFixture();
  const stagesHtml = await readFile(new URL("../public/stages.html", import.meta.url), "utf8");
  const domainsHtml = await readFile(new URL("../public/domains.html", import.meta.url), "utf8");
  const domainHtml = await readFile(new URL("../public/domain.html", import.meta.url), "utf8");
  const seen = [];
  const env = {
    ACCESS_TEAM_DOMAIN: access.teamDomain,
    ACCESS_AUD: access.audience,
    ACCESS_JWKS_FETCH: access.fetchImpl,
    FUNNEL_SNAPSHOT: { async get() { return null; } },
    ...redirectFaithfulAssets(
      { "/stages": stagesHtml, "/domains": domainsHtml, "/domain": domainHtml },
      seen,
    ),
  };
  return { access, env, seen, stagesHtml, domainsHtml, domainHtml };
}

// Follows redirects back through the worker the way a browser follows the
// deployed loop: an extensionless Location re-enters the worker routes.
async function followThroughWorker(path, env, access, limit = 10) {
  let current = path;
  for (let hop = 0; hop < limit; hop += 1) {
    const response = await worker.fetch(new Request(`https://funnel.nateprich.com${current}`, {
      headers: { "Cf-Access-Jwt-Assertion": access.sign() },
    }), env);
    if (![301, 302, 303, 307, 308].includes(response.status)) {
      return { response, hops: hop, path: current };
    }
    current = new URL(
      response.headers.get("location"),
      "https://funnel.nateprich.com",
    ).pathname;
  }
  throw new Error(`redirect loop at ${path}: still redirecting after ${limit} hops`);
}

test("/stages loads in one worker pass under a redirect-faithful stub", async () => {
  const { access, env, seen, stagesHtml } = await authorizedRedirectEnv();
  const { response, hops, path } = await followThroughWorker("/stages", env, access);

  assert.equal(hops, 0);
  assert.equal(path, "/stages");
  assert.equal(response.status, 200);
  assert.deepEqual(seen, ["/stages"]);
  assert.equal(await response.text(), stagesHtml);
});

test("/domains loads in one worker pass under a redirect-faithful stub", async () => {
  const { access, env, seen, domainsHtml } = await authorizedRedirectEnv();
  const { response, hops, path } = await followThroughWorker("/domains", env, access);

  assert.equal(hops, 0);
  assert.equal(path, "/domains");
  assert.equal(response.status, 200);
  assert.deepEqual(seen, ["/domains"]);
  assert.equal(await response.text(), domainsHtml);
});

test("/domains/<d> serves the detail file at the named URL without redirecting", async () => {
  const { access, env, seen, domainHtml } = await authorizedRedirectEnv();
  const { response, hops, path } = await followThroughWorker(
    "/domains/Fantasy", env, access,
  );

  // Any redirect off the named URL drops the domain segment: the old .html
  // fetch 301d to /domain, which renders without a named domain.
  assert.equal(hops, 0);
  assert.equal(path, "/domains/Fantasy");
  assert.equal(response.status, 200);
  assert.deepEqual(seen, ["/domain"]);
  assert.equal(await response.text(), domainHtml);
});

test("the pre-fix .html fetch 301s to a worker route: the loop shape", async () => {
  const { access, env } = await authorizedRedirectEnv();
  // Old worker.js:418 fetched "/stages.html" from ASSETS. Under faithful
  // html_handling that answers 301 to /stages, which is a worker route, so
  // the browser re-entered the worker and the pre-fix worker 301d again.
  const first = await env.ASSETS.fetch(
    new Request("https://funnel.nateprich.com/stages.html"),
  );
  assert.equal(first.status, 301);
  assert.equal(
    new URL(first.headers.get("location"), "https://funnel.nateprich.com").pathname,
    "/stages",
  );
  // Post-fix the same URL loads in one worker pass (no redirect to follow).
  const { response, hops } = await followThroughWorker("/stages", env, access);
  assert.equal(hops, 0);
  assert.equal(response.status, 200);
});

test("the pre-existing assetEnv stub never redirected", async () => {
  const source = await readFile(new URL("./stages.test.js", import.meta.url), "utf8");
  const start = source.indexOf("function assetEnv");
  assert.ok(start !== -1, "the pre-existing assetEnv stub exists");
  const block = source.slice(start, source.indexOf("\n}\n", start));
  assert.doesNotMatch(block, /30[12378]/);
  assert.doesNotMatch(block, /location/i);
  assert.doesNotMatch(block, /redirect/i);
});

test("/domain is not a worker route: only an ASSETS target", async () => {
  const source = await readFile(new URL("../worker.js", import.meta.url), "utf8");
  assert.doesNotMatch(source, /pathname === "\/domain"/);
  const blockStart = source.indexOf('startsWith("/domains/")');
  assert.ok(blockStart !== -1, "the /domains/<d> block exists");
  const block = source.slice(blockStart, source.indexOf("}", source.indexOf('"/domain"', blockStart)) + 1);
  assert.match(block, /"\/domain"/);
});

test("the Access check still fronts all three routes", async () => {
  const { env, seen } = await authorizedRedirectEnv();
  for (const path of ["/stages", "/domains", "/domains/Fantasy"]) {
    const response = await worker.fetch(
      new Request(`https://funnel.nateprich.com${path}`), env,
    );
    assert.equal(response.status, 403);
    assert.equal(await response.text(), "Forbidden");
  }
  assert.deepEqual(seen, []);
});

test("wrangler keeps default html_handling: no global asset change", async () => {
  const wrangler = await readFile(new URL("../wrangler.toml", import.meta.url), "utf8");
  assert.doesNotMatch(wrangler, /html_handling/);
});
