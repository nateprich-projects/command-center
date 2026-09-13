import { createSign, generateKeyPairSync } from "node:crypto";
import { readFile } from "node:fs/promises";
import { createServer } from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

import worker from "./worker.js";

const root = path.dirname(fileURLToPath(import.meta.url));
const publicRoot = path.join(root, "public");
const snapshot = JSON.parse(await readFile(path.join(root, "fixtures", "snapshot.json"), "utf8"));
const teamDomain = "https://local-access.invalid";
const audience = "local-dashboard";
const kid = "local-fixture-key";
const { privateKey, publicKey } = generateKeyPairSync("rsa", { modulusLength: 2048 });
const jwk = publicKey.export({ format: "jwk" });
Object.assign(jwk, { alg: "RS256", kid, use: "sig" });

function encodeJson(value) {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function assertion() {
  const now = Math.floor(Date.now() / 1000);
  const unsigned = `${encodeJson({ alg: "RS256", kid })}.${encodeJson({
    iss: teamDomain,
    aud: audience,
    exp: now + 3600,
  })}`;
  return `${unsigned}.${createSign("RSA-SHA256").update(unsigned).sign(privateKey).toString("base64url")}`;
}

function contentType(file) {
  if (file.endsWith(".html")) return "text/html; charset=utf-8";
  if (file.endsWith(".css")) return "text/css; charset=utf-8";
  if (file.endsWith(".js")) return "text/javascript; charset=utf-8";
  return "application/octet-stream";
}

const env = {
  ACCESS_TEAM_DOMAIN: teamDomain,
  ACCESS_AUD: audience,
  ACCESS_JWKS_FETCH: async () => Response.json({ keys: [jwk] }),
  FUNNEL_SNAPSHOT: {
    async get(key) {
      return key === "snapshot" ? snapshot : null;
    },
    async put() {},
  },
  ASSETS: {
    async fetch(request) {
      const pathname = new URL(request.url).pathname;
      const relative = pathname === "/" ? "index.html" : decodeURIComponent(pathname.slice(1));
      const file = path.resolve(publicRoot, relative);
      if (!file.startsWith(`${publicRoot}${path.sep}`)) return new Response("Not Found", { status: 404 });
      try {
        return new Response(await readFile(file), { headers: { "content-type": contentType(file) } });
      } catch {
        return new Response("Not Found", { status: 404 });
      }
    },
  },
};

createServer(async (request, response) => {
  try {
    const headers = new Headers();
    for (const [name, value] of Object.entries(request.headers)) {
      if (value) headers.set(name, Array.isArray(value) ? value.join(",") : value);
    }
    headers.set("Cf-Access-Jwt-Assertion", assertion());
    const result = await worker.fetch(new Request(`http://${request.headers.host}${request.url}`, {
      method: request.method,
      headers,
    }), env);
    response.writeHead(result.status, Object.fromEntries(result.headers));
    response.end(Buffer.from(await result.arrayBuffer()));
  } catch (error) {
    response.writeHead(500, { "content-type": "text/plain; charset=utf-8" });
    response.end(error.stack);
  }
}).listen(8787, "127.0.0.1", () => {
  process.stdout.write("Fixture dashboard: http://127.0.0.1:8787\n");
});
