const SNAPSHOT_KEY = "snapshot";
const REFRESH_KEY = "refresh-requested";
const JWKS_TTL_MS = 5 * 60 * 1000;
const CLOCK_SKEW_SECONDS = 30;

const textEncoder = new TextEncoder();
const jwksCache = new Map();

function base64UrlBytes(value) {
  if (typeof value !== "string" || !/^[A-Za-z0-9_-]*$/.test(value)) {
    throw new Error("invalid base64url value");
  }
  const padded = value.replace(/-/g, "+").replace(/_/g, "/").padEnd(
    Math.ceil(value.length / 4) * 4,
    "=",
  );
  const binary = atob(padded);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

function base64UrlJson(value) {
  return JSON.parse(new TextDecoder().decode(base64UrlBytes(value)));
}

function normalizedTeamDomain(value) {
  if (typeof value !== "string") {
    throw new Error("missing Access team domain");
  }
  const domain = value.replace(/\/$/, "");
  const url = new URL(domain);
  if (url.protocol !== "https:" || url.pathname !== "/" || url.search || url.hash) {
    throw new Error("invalid Access team domain");
  }
  return domain;
}

function hasAudience(claim, expected) {
  if (typeof claim === "string") {
    return claim === expected;
  }
  if (!Array.isArray(claim)) {
    return false;
  }
  return claim.some((audience) => audience === expected);
}

async function loadJwks(teamDomain, fetchImpl, nowMs) {
  const cached = jwksCache.get(teamDomain);
  if (cached && cached.expiresAt > nowMs) {
    return cached.keys;
  }

  const response = await fetchImpl(`${teamDomain}/cdn-cgi/access/certs`, {
    headers: { accept: "application/json" },
  });
  if (!response.ok) {
    throw new Error(`Access JWKS returned ${response.status}`);
  }
  const payload = await response.json();
  if (!payload || !Array.isArray(payload.keys)) {
    throw new Error("Access JWKS did not contain keys");
  }
  jwksCache.set(teamDomain, {
    expiresAt: nowMs + JWKS_TTL_MS,
    keys: payload.keys,
  });
  return payload.keys;
}

export async function verifyAccessJwt(
  token,
  { teamDomain, audience },
  { fetchImpl = fetch, nowMs = Date.now(), cryptoImpl = crypto } = {},
) {
  try {
    if (typeof audience !== "string" || !audience) {
      return false;
    }
    const issuer = normalizedTeamDomain(teamDomain);
    const parts = token.split(".");
    if (parts.length !== 3 || parts.some((part) => !part)) {
      return false;
    }

    const header = base64UrlJson(parts[0]);
    const claims = base64UrlJson(parts[1]);
    if (header.alg !== "RS256" || typeof header.kid !== "string") {
      return false;
    }

    const nowSeconds = Math.floor(nowMs / 1000);
    if (
      claims.iss !== issuer ||
      !hasAudience(claims.aud, audience) ||
      typeof claims.exp !== "number" ||
      claims.exp <= nowSeconds - CLOCK_SKEW_SECONDS ||
      (typeof claims.nbf === "number" && claims.nbf > nowSeconds + CLOCK_SKEW_SECONDS)
    ) {
      return false;
    }

    const keys = await loadJwks(issuer, fetchImpl, nowMs);
    const jwk = keys.find(
      (candidate) =>
        candidate &&
        candidate.kid === header.kid &&
        candidate.kty === "RSA" &&
        (!candidate.alg || candidate.alg === "RS256") &&
        (!candidate.use || candidate.use === "sig"),
    );
    if (!jwk) {
      return false;
    }

    const key = await cryptoImpl.subtle.importKey(
      "jwk",
      jwk,
      { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
      false,
      ["verify"],
    );
    return await cryptoImpl.subtle.verify(
      "RSASSA-PKCS1-v1_5",
      key,
      base64UrlBytes(parts[2]),
      textEncoder.encode(`${parts[0]}.${parts[1]}`),
    );
  } catch {
    return false;
  }
}

async function isAuthorized(request, env) {
  const assertion = request.headers.get("Cf-Access-Jwt-Assertion");
  if (!assertion) {
    return false;
  }
  return verifyAccessJwt(
    assertion,
    {
      teamDomain: env.ACCESS_TEAM_DOMAIN,
      audience: env.ACCESS_AUD,
    },
    { fetchImpl: env.ACCESS_JWKS_FETCH || fetch },
  );
}

function jsonResponse(value, status = 200) {
  return new Response(JSON.stringify(value), {
    status,
    headers: {
      "cache-control": "no-store",
      "content-type": "application/json; charset=utf-8",
      "x-content-type-options": "nosniff",
    },
  });
}

function withSecurityHeaders(response) {
  const secured = new Response(response.body, response);
  secured.headers.set(
    "content-security-policy",
    "default-src 'self'; connect-src 'self'; img-src 'self'; script-src 'self'; style-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
  );
  secured.headers.set("referrer-policy", "no-referrer");
  secured.headers.set("x-content-type-options", "nosniff");
  secured.headers.set("x-frame-options", "DENY");
  return secured;
}

async function handleRequest(request, env) {
  if (!(await isAuthorized(request, env))) {
    return new Response("Forbidden", {
      status: 403,
      headers: { "cache-control": "no-store" },
    });
  }

  const url = new URL(request.url);
  if (url.pathname === "/api/snapshot") {
    if (request.method !== "GET") {
      return jsonResponse({ error: "method not allowed" }, 405);
    }
    const snapshot = await env.FUNNEL_SNAPSHOT.get(SNAPSHOT_KEY, { type: "json" });
    if (!snapshot) {
      return jsonResponse({ error: "snapshot unavailable" }, 503);
    }
    return jsonResponse(snapshot);
  }

  if (url.pathname === "/api/refresh") {
    if (request.method !== "POST") {
      return jsonResponse({ error: "method not allowed" }, 405);
    }
    const requestedAt = new Date().toISOString();
    await env.FUNNEL_SNAPSHOT.put(REFRESH_KEY, requestedAt);
    return jsonResponse({ requested_at: requestedAt }, 202);
  }

  if (request.method !== "GET" && request.method !== "HEAD") {
    return new Response("Method Not Allowed", { status: 405 });
  }
  if (!env.ASSETS || typeof env.ASSETS.fetch !== "function") {
    return new Response("Assets unavailable", { status: 503 });
  }
  return withSecurityHeaders(await env.ASSETS.fetch(request));
}

export default { fetch: handleRequest };
export { REFRESH_KEY, SNAPSHOT_KEY };
