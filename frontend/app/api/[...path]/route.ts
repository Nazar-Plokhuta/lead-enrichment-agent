import type { NextRequest } from "next/server";

/**
 * Allowlisted reverse proxy to the FastAPI backend.
 *
 * The browser only ever talks to this origin, so the backend needs no CORS
 * configuration and is never exposed directly. Anything not explicitly listed
 * in `resolveRoute` is rejected with 404 before any upstream request is made.
 */

const DEFAULT_API_ORIGIN = "http://127.0.0.1:8000";
const UPSTREAM_TIMEOUT_MS = 30_000;
const LEAD_ID_PATTERN = /^\d+$/;
const LEAD_LIST_QUERY_KEYS = ["page", "page_size", "fit_tier", "status"] as const;

interface ResolvedRoute {
  upstreamPath: string;
  forwardedQueryKeys: readonly string[];
  hasBody: boolean;
}

function resolveRoute(method: string, segments: readonly string[]): ResolvedRoute | null {
  const key = segments.join("/");

  if (method === "GET") {
    switch (key) {
      case "health":
        return { upstreamPath: "/health", forwardedQueryKeys: [], hasBody: false };
      case "openapi.json":
        return { upstreamPath: "/openapi.json", forwardedQueryKeys: [], hasBody: false };
      case "v1/leads":
        return {
          upstreamPath: "/api/v1/leads",
          forwardedQueryKeys: LEAD_LIST_QUERY_KEYS,
          hasBody: false,
        };
    }
    // Segment-wise match: an encoded slash inside an id arrives as one decoded
    // segment containing "/", which can never satisfy the digits-only pattern.
    if (
      segments.length === 3 &&
      segments[0] === "v1" &&
      segments[1] === "leads" &&
      LEAD_ID_PATTERN.test(segments[2])
    ) {
      return {
        upstreamPath: `/api/v1/leads/${segments[2]}`,
        forwardedQueryKeys: [],
        hasBody: false,
      };
    }
  }

  if (method === "POST" && key === "v1/enrich") {
    return { upstreamPath: "/api/v1/enrich", forwardedQueryKeys: [], hasBody: true };
  }

  return null;
}

function jsonError(status: number, detail: string): Response {
  return Response.json({ detail }, { status, headers: { "Cache-Control": "no-store" } });
}

// Typed by hand rather than via the generated `RouteContext` helper so that
// `tsc --noEmit` works on a fresh checkout before Next has generated types.
interface ProxyContext {
  params: Promise<{ path: string[] }>;
}

async function proxy(request: NextRequest, ctx: ProxyContext): Promise<Response> {
  const { path } = await ctx.params;
  const route = resolveRoute(request.method, path);
  if (route === null) {
    return jsonError(404, "Not Found");
  }

  // Read per request so the origin can change without a rebuild.
  const apiOrigin = (process.env.API_ORIGIN || DEFAULT_API_ORIGIN).replace(/\/+$/, "");

  const query = new URLSearchParams();
  for (const name of route.forwardedQueryKeys) {
    for (const value of request.nextUrl.searchParams.getAll(name)) {
      query.append(name, value);
    }
  }
  const queryString = query.toString();
  const target = `${apiOrigin}${route.upstreamPath}${queryString ? `?${queryString}` : ""}`;

  const headers = new Headers();
  const contentType = request.headers.get("content-type");
  if (contentType !== null) {
    headers.set("content-type", contentType);
  }

  try {
    const upstream = await fetch(target, {
      method: request.method,
      headers,
      body: route.hasBody ? await request.arrayBuffer() : undefined,
      cache: "no-store",
      redirect: "manual",
      signal: AbortSignal.timeout(UPSTREAM_TIMEOUT_MS),
    });
    // Buffered so the timeout also covers a stalled body, and a mid-body
    // failure surfaces as a clean 502 instead of a truncated response.
    const payload = await upstream.arrayBuffer();

    // Status and body pass through verbatim (including the backend's 422/503
    // shapes); only the content type is relayed, since fetch has already
    // decoded the body and encoding/length headers would no longer match.
    const responseHeaders = new Headers({ "Cache-Control": "no-store" });
    const upstreamType = upstream.headers.get("content-type");
    if (upstreamType !== null) {
      responseHeaders.set("content-type", upstreamType);
    }
    return new Response(payload, { status: upstream.status, headers: responseHeaders });
  } catch (error) {
    console.error(
      JSON.stringify({
        event: "proxy_upstream_failure",
        method: request.method,
        upstreamPath: route.upstreamPath,
        reason: error instanceof Error ? `${error.name}: ${error.message}` : String(error),
      }),
    );
    return jsonError(502, "API unreachable");
  }
}

// Every method funnels through the allowlist so that disallowed methods get
// the same 404 as unknown paths rather than Next's default 405.
export {
  proxy as GET,
  proxy as POST,
  proxy as PUT,
  proxy as PATCH,
  proxy as DELETE,
  proxy as HEAD,
  proxy as OPTIONS,
};
