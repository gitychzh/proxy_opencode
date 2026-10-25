// forward.ts — upstream transport via bun fetch (boringssl TLS + HTTP/2).
//
// Why this exists (2026-10-24 incident): the anonymous free tier upstream
// began judging the CLIENT TRANSPORT STACK — an httpx replay (python
// OpenSSL, HTTP/1.1) of a byte-exact genuine 1.19.4 client capture got
// HTTP 429 `FreeUsageLimitError` from a FRESH residential egress IP,
// while bun (boringssl, HTTP/2) replays of the same bytes got 200 from
// the same IP within minutes. The gateway therefore hands the one TLS +
// HTTP hop to bun, the exact runtime the official client embeds. The
// egress-selection semantics (which bucket serves, which agent proxy)
// stay with the python-side EgressPool: every attempt runs this script
// with HTTPS_PROXY pointed at the leased bucket's agent.
//
// Protocol (raw bytes everywhere; nothing sensitive is ever logged):
//   argv:   <path>             request path suffix, e.g. /chat/completions
//   stdin:  request body bytes (already exactly what the gateway sends)
//   stdout: response body bytes, streamed verbatim as they arrive
//   stderr: ONE line  META {"status": N, "headers": {...}, "error": "..."}
//           written as soon as the upstream responds (before any body
//           byte), so the caller can classify status/cordons first.
//   exit:   0 — a status was observed (200 or any 4xx/5xx relayed);
//           1 — transport failure (status 0).
//
// Env:
//   ZEN_UPSTREAM_BASE    upstream base, e.g. https://opencode.ai/inference/openai/v1
//   ZEN_BUN_HEADERS      JSON object of request headers (Host excluded)
//   ZEN_BUN_TIMEOUT_MS   hard deadline for the whole fetch (default 300000)
//   ZEN_BUN_TTFB_MS      deadline for the response HEADERS only (default 60000,
//                        0 disables). A blackholed egress hop otherwise burns
//                        the whole 300s before the caller can fail over.
//   HTTPS_PROXY          optional bucket agent (HTTP CONNECT) — set by caller

const UPSTREAM = (process.env.ZEN_UPSTREAM_BASE ?? "").replace(/\/+$/, "");
const PATH = process.argv[2] ?? "/chat/completions";
const TIMEOUT_MS = Number(process.env.ZEN_BUN_TIMEOUT_MS ?? "300000") || 300000;
const TTFB_MS = Number(process.env.ZEN_BUN_TTFB_MS ?? "60000") || 0;

// hop-by-hop names the gateway must not see relayed (bun sets its own
// Host/Content-Length; the proxy connection layer is not the caller's).
const HOP_BY_HOP = new Set([
  "host",
  "content-length",
  "content-encoding",
  "connection",
  "keep-alive",
  "transfer-encoding",
  "proxy-connection",
  "proxy-authorization",
  "te",
  "trailer",
  "upgrade",
]);

// Response headers worth surfacing to the caller (quota/logic input only).
const ECHO_RESPONSE_HEADERS = ["content-type", "retry-after", "x-request-id"];

async function readStdinBody(): Promise<Uint8Array> {
  const buf = await Bun.stdin.arrayBuffer();
  return new Uint8Array(buf).slice(0);
}

async function main(): Promise<number> {
  if (!UPSTREAM) {
    process.stderr.write('META {"status":0,"error":"ZEN_UPSTREAM_BASE missing"}\n');
    return 1;
  }
  const url = UPSTREAM + (PATH.startsWith("/") ? PATH : "/" + PATH);
  const headers: Record<string, string> = {};
  const rawHeaders = JSON.parse(process.env.ZEN_BUN_HEADERS ?? "{}") as Record<
    string,
    string
  >;
  for (const [name, value] of Object.entries(rawHeaders)) {
    if (!name || value === null || value === undefined) continue;
    if (HOP_BY_HOP.has(name.toLowerCase())) continue;
    headers[name] = String(value);
  }

  const ac = new AbortController();
  const timer = setTimeout(() => ac.abort(), TIMEOUT_MS);
  // Header deadline. Cleared as soon as fetch() resolves: from that point on
  // the silence budget belongs to the caller's stream watchdog, which is
  // measured (not guessed) on the SSE body.
  let ttfbFired = false;
  let ttfbTimer: ReturnType<typeof setTimeout> | undefined;
  if (TTFB_MS > 0 && TTFB_MS < TIMEOUT_MS) {
    ttfbTimer = setTimeout(() => {
      ttfbFired = true;
      ac.abort();
    }, TTFB_MS);
  }
  try {
    const body = await readStdinBody();
    const res = await fetch(url, {
      method: "POST",
      headers,
      body: body.byteLength > 0 ? body : undefined,
      redirect: "manual",
      signal: ac.signal,
    });
    if (ttfbTimer !== undefined) {
      clearTimeout(ttfbTimer);
      ttfbTimer = undefined;
    }
    const echo: Record<string, string> = {};
    for (const name of ECHO_RESPONSE_HEADERS) {
      const value = res.headers.get(name);
      if (value !== null) echo[name] = value;
    }
    process.stderr.write(
      `META ${JSON.stringify({ status: res.status, headers: echo })}\n`,
    );
    if (res.body !== null) {
      const reader = res.body.getReader();
      try {
        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          process.stdout.write(value);
        }
      } finally {
        reader.releaseLock();
      }
    }
    return 0;
  } catch (exc: unknown) {
    const name = ttfbFired
      ? "TtfbTimeout"
      : exc instanceof Error
        ? exc.name
        : "unknown";
    const message = exc instanceof Error ? String(exc.message).slice(0, 200) : "unknown";
    process.stderr.write(
      `META ${JSON.stringify({ status: 0, error: `${name}: ${message}` })}\n`,
    );
    return 1;
  } finally {
    clearTimeout(timer);
    if (ttfbTimer !== undefined) clearTimeout(ttfbTimer);
  }
}

process.exitCode = await main();
