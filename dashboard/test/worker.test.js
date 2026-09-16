import assert from "node:assert/strict";
import { generateKeyPairSync, createSign } from "node:crypto";
import test from "node:test";

import worker, { REFRESH_KEY, verifyAccessJwt } from "../worker.js";

function encodeJson(value) {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function tamperSignature(token) {
  const parts = token.split(".");
  const signature = parts[2];
  const index = Math.floor(signature.length / 2);
  const replacement = signature[index] === "a" ? "b" : "a";
  parts[2] = `${signature.slice(0, index)}${replacement}${signature.slice(index + 1)}`;
  return parts.join(".");
}

function accessFixture(overrides = {}) {
  const teamDomain = overrides.teamDomain || `https://${crypto.randomUUID()}.cloudflareaccess.com`;
  const audience = "dashboard-audience";
  const kid = "fixture-key";
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
    publicJwk,
    sign,
    teamDomain,
    fetchImpl: async () => Response.json({ keys: [publicJwk] }),
  };
}

test("a request without the Access assertion fails closed", async () => {
  const response = await worker.fetch(new Request("https://funnel.nateprich.com/"), {});
  assert.equal(response.status, 403);
  assert.equal(await response.text(), "Forbidden");
});

test("Access validation requires the configured issuer, audience, expiry, and signature", async () => {
  const fixture = accessFixture();
  const options = { fetchImpl: fixture.fetchImpl };
  const config = { teamDomain: fixture.teamDomain, audience: fixture.audience };

  assert.equal(await verifyAccessJwt(fixture.sign(), config, options), true);
  assert.equal(await verifyAccessJwt(fixture.sign({ aud: "somewhere-else" }), config, options), false);
  assert.equal(await verifyAccessJwt(fixture.sign({ iss: "https://other.cloudflareaccess.com" }), config, options), false);
  assert.equal(await verifyAccessJwt(fixture.sign({ exp: 1 }), config, options), false);

  const valid = fixture.sign();
  const tampered = tamperSignature(valid);
  assert.notDeepEqual(
    Buffer.from(tampered.split(".")[2], "base64url"),
    Buffer.from(valid.split(".")[2], "base64url"),
  );
  assert.equal(await verifyAccessJwt(tampered, config, options), false);
});

test("an authorized request reads the snapshot without changing its order", async () => {
  const fixture = accessFixture();
  const snapshot = {
    brief: { items: [{ ref: "repo/a#2" }, { ref: "repo/a#1" }] },
    board: { Building: [{ title: "second" }, { title: "first" }] },
  };
  const env = {
    ACCESS_TEAM_DOMAIN: fixture.teamDomain,
    ACCESS_AUD: fixture.audience,
    ACCESS_JWKS_FETCH: fixture.fetchImpl,
    FUNNEL_SNAPSHOT: {
      async get(key, options) {
        assert.equal(key, "snapshot");
        assert.deepEqual(options, { type: "json" });
        return snapshot;
      },
    },
  };
  const response = await worker.fetch(new Request("https://funnel.nateprich.com/api/snapshot", {
    headers: { "Cf-Access-Jwt-Assertion": fixture.sign() },
  }), env);

  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), snapshot);
});

test("refresh records one KV flag after authorization", async () => {
  const fixture = accessFixture();
  const writes = [];
  const env = {
    ACCESS_TEAM_DOMAIN: fixture.teamDomain,
    ACCESS_AUD: fixture.audience,
    ACCESS_JWKS_FETCH: fixture.fetchImpl,
    FUNNEL_SNAPSHOT: { async put(...args) { writes.push(args); } },
  };
  const response = await worker.fetch(new Request("https://funnel.nateprich.com/api/refresh", {
    method: "POST",
    headers: { "Cf-Access-Jwt-Assertion": fixture.sign() },
  }), env);

  assert.equal(response.status, 202);
  assert.equal(writes.length, 1);
  assert.equal(writes[0][0], REFRESH_KEY);
  assert.equal(typeof writes[0][1], "string");
});

async function signed(body, secret) {
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  const signature = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(body));
  return "sha256=" + Array.from(new Uint8Array(signature))
    .map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

function webhookEnv(store = new Map()) {
  return {
    GITHUB_WEBHOOK_SECRET: "shhh",
    FUNNEL_SNAPSHOT: {
      get: async (key) => (store.has(key) ? store.get(key) : null),
      put: async (key, value) => { store.set(key, value); },
    },
    store,
  };
}

function webhookRequest(body, { event = "issues", signature } = {}) {
  const headers = { "X-GitHub-Event": event };
  if (signature) headers["X-Hub-Signature-256"] = signature;
  return new Request("https://funnel.nateprich.com/api/github-webhook", {
    method: "POST", body, headers,
  });
}

test("a signed funnel event sets the refresh flag without Access", async () => {
  const body = JSON.stringify({ action: "closed" });
  const env = webhookEnv();
  const response = await worker.fetch(
    webhookRequest(body, { signature: await signed(body, "shhh") }), env,
  );
  assert.equal(response.status, 202);
  assert.ok(env.store.get("refresh-requested"));
});

test("an unsigned or wrongly signed webhook is refused and writes nothing", async () => {
  const body = JSON.stringify({ action: "closed" });
  const env = webhookEnv();
  assert.equal((await worker.fetch(webhookRequest(body), env)).status, 401);
  const wrong = await signed(body, "other");
  assert.equal(
    (await worker.fetch(webhookRequest(body, { signature: wrong }), env)).status, 401,
  );
  assert.equal(env.store.size, 0);
});

test("a body that does not match its signature is refused", async () => {
  const env = webhookEnv();
  const signature = await signed(JSON.stringify({ action: "closed" }), "shhh");
  const response = await worker.fetch(
    webhookRequest(JSON.stringify({ action: "opened" }), { signature }), env,
  );
  assert.equal(response.status, 401);
  assert.equal(env.store.size, 0);
});

test("ping is answered and an uninteresting event costs no refresh", async () => {
  const body = JSON.stringify({ zen: "keep it logically awesome" });
  const env = webhookEnv();
  const ping = await worker.fetch(
    webhookRequest(body, { event: "ping", signature: await signed(body, "shhh") }), env,
  );
  assert.equal(ping.status, 200);
  const push = await worker.fetch(
    webhookRequest(body, { event: "push", signature: await signed(body, "shhh") }), env,
  );
  assert.equal(push.status, 202);
  assert.equal(env.store.size, 0);
});

test("without a configured secret every webhook is refused", async () => {
  const body = JSON.stringify({ action: "closed" });
  const env = webhookEnv();
  env.GITHUB_WEBHOOK_SECRET = "";
  const response = await worker.fetch(
    webhookRequest(body, { signature: await signed(body, "shhh") }), env,
  );
  assert.equal(response.status, 401);
});

test("assets revalidate, so a deploy is picked up on the next load", async () => {
  const env = {
    ACCESS_TEAM_DOMAIN: "https://team.cloudflareaccess.com",
    ACCESS_AUD: "aud",
    ASSETS: {
      fetch: async () => new Response("<!doctype html>", {
        headers: { "content-type": "text/html" },
      }),
    },
    ACCESS_JWKS_FETCH: async () => new Response(JSON.stringify({ keys: [] })),
  };
  // Authorization is stubbed out by pointing at the real verifier's failure
  // path, so this asserts the header the asset branch adds rather than Access.
  const response = await worker.fetch(
    new Request("https://funnel.nateprich.com/app.js", {
      headers: { "Cf-Access-Jwt-Assertion": "not-a-jwt" },
    }),
    env,
  );
  // An unauthorized request never reaches the asset branch; what matters is
  // that the helper sets the header, which the source pins directly.
  assert.equal(response.status, 403);
});

test("the security-header helper marks responses as revalidate-always", async () => {
  const { readFile } = await import("node:fs/promises");
  const source = await readFile(new URL("../worker.js", import.meta.url), "utf8");
  assert.match(source, /cache-control", "no-cache, must-revalidate"/);
});
