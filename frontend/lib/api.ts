import { z } from "zod"

import type {
  EnrichRequest,
  EnrichResponse,
  FitTier,
  HealthResponse,
  LeadListResponse,
  LeadResponse,
  LeadStatus,
} from "./types"
import { normalizeApiError } from "./utils"

/**
 * Typed client for the allowlisted Next.js proxy (`/api/*`). The browser never
 * talks to FastAPI directly, so every path here must exist in the proxy's
 * allowlist or it 404s.
 */

export const LEAD_PAGE_SIZE = 50
export const ACTIVITY_PAGE_SIZE = 100

export class ApiError extends Error {
  readonly status: number
  readonly body: unknown

  constructor(status: number, body: unknown) {
    super(normalizeApiError(status, body))
    this.name = "ApiError"
    this.status = status
    this.body = body
  }

  /** Proxy 502s and failed fetches both mean "no backend", not "bad request". */
  get isUnreachable(): boolean {
    return this.status === 0 || this.status >= 500
  }
}

interface ApiResult<T> {
  status: number
  data: T
}

async function request<T>(path: string, init?: RequestInit): Promise<ApiResult<T>> {
  let response: Response
  try {
    response = await fetch(`/api${path}`, { cache: "no-store", ...init })
  } catch (error) {
    // The Next server itself is unreachable (dev server stopped, offline).
    console.error("api_network_failure", { path, error })
    throw new ApiError(0, null)
  }

  // Proxy-generated errors and 204s may have no JSON body; that is not a fault.
  const body: unknown = await response.json().catch(() => null)
  if (!response.ok) throw new ApiError(response.status, body)
  return { status: response.status, data: body as T }
}

export type HealthState =
  | { kind: "healthy"; latencyMs: number }
  | { kind: "degraded" }
  | { kind: "unreachable" }

/** Never throws: a failing health check is a *state to display*, not an error. */
export async function fetchHealth(): Promise<HealthState> {
  const startedAt = performance.now()
  try {
    const { data } = await request<HealthResponse>("/health")
    return data.status === "healthy"
      ? { kind: "healthy", latencyMs: Math.round(performance.now() - startedAt) }
      : { kind: "degraded" }
  } catch (error) {
    // 502 comes from the proxy (backend down); 503 is the backend reporting
    // its own unhealthy database, so the API process is up but degraded.
    const proxyFailure = error instanceof ApiError && (error.status === 0 || error.status === 502)
    return proxyFailure || !(error instanceof ApiError) ? { kind: "unreachable" } : { kind: "degraded" }
  }
}

const openApiSchema = z.object({ info: z.object({ version: z.string() }) })

export async function fetchApiVersion(): Promise<string> {
  const { data } = await request<unknown>("/openapi.json")
  return openApiSchema.parse(data).info.version
}

export interface LeadListParams {
  page?: number
  pageSize?: number
  status?: LeadStatus | null
  tier?: FitTier | null
}

export async function fetchLeads(params: LeadListParams): Promise<LeadListResponse> {
  const query = new URLSearchParams()
  query.set("page", String(params.page ?? 1))
  query.set("page_size", String(params.pageSize ?? LEAD_PAGE_SIZE))
  if (params.status) query.set("status", params.status)
  if (params.tier) query.set("fit_tier", params.tier)
  const { data } = await request<LeadListResponse>(`/v1/leads?${query}`)
  return data
}

export async function fetchLead(id: number): Promise<LeadResponse> {
  const { data } = await request<LeadResponse>(`/v1/leads/${id}`)
  return data
}

export interface EnrichResult {
  /** 202 = queued, 200 = already processed (no work was scheduled). */
  outcome: "queued" | "already_processed"
  response: EnrichResponse
}

export async function submitEnrich(body: EnrichRequest): Promise<EnrichResult> {
  const { status, data } = await request<EnrichResponse>("/v1/enrich", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  })
  return { outcome: status === 200 ? "already_processed" : "queued", response: data }
}

export const queryKeys = {
  health: ["health"] as const,
  version: ["openapi-version"] as const,
  /** Prefix of every paginated list query, for bulk invalidation. */
  leadLists: ["leads", "list"] as const,
  leadList: (params: { page: number; status: LeadStatus | null; tier: FitTier | null }) =>
    ["leads", "list", params] as const,
  pending: ["leads", "pending"] as const,
  /** Prefix of every single-lead query, for bulk invalidation. */
  leadDetails: ["leads", "detail"] as const,
  lead: (id: number) => ["leads", "detail", id] as const,
}
