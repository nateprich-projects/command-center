import assert from "node:assert/strict";
import { generateKeyPairSync, createSign } from "node:crypto";
import test from "node:test";

import worker, { REFRESH_KEY, verifyAccessJwt } from "../worker.js";

function encodeJson(value) {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
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

  const tampered = fixture.sign().replace(/.$/, (character) => character === "a" ? "b" : "a");
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
