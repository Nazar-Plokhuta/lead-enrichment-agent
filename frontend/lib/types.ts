/**
 * API contracts mirroring `src/api/schemas.py` and `src/llm/schemas.py`.
 * The backend is the source of truth; change these only when it changes.
 */

export type LeadStatus = "PENDING" | "PROCESSED" | "FAILED"

export type FitTier =
  | "Tier 1 (High)"
  | "Tier 2 (Medium)"
  | "Tier 3 (Low)"
  | "Disqualified"

export interface EnrichRequest {
  url: string
  force?: boolean
}

export interface EnrichResponse {
  lead_id: number
  url: string
  status: LeadStatus
  message: string
}

export interface LeadResponse {
  id: number
  url: string
  status: LeadStatus
  company_name: string | null
  fit_score: number | null
  fit_tier: FitTier | null
  enriched_data: Record<string, unknown> | null
  error_message: string | null
  /** SQLite emits naive UTC (no `Z`); use `normalizeTimestamp` before parsing. */
  created_at: string
  updated_at: string
}

export interface LeadListResponse {
  items: LeadResponse[]
  total: number
  page: number
  page_size: number
}

export interface HealthResponse {
  status: "healthy" | "unhealthy"
  database: "ok" | "unreachable"
}

// --- LLM extraction payload (`enriched_data`) -------------------------------

export interface CompanyAnalysis {
  industry: string
  target_audience: string
  value_proposition: string
  pain_points: string[]
}

export interface LeadScoring {
  fit_tier: FitTier
  fit_score: number
  scoring_rationale: string
  missing_information: string[]
}

export interface OutreachStrategy {
  icebreaker: string
  suggested_angle: string
}

export interface EnrichedLeadPayload {
  company_name: string
  analysis: CompanyAnalysis
  scoring: LeadScoring
  outreach: OutreachStrategy
}

// --- Errors & score breakdown -----------------------------------------------

/** One entry of FastAPI's Pydantic 422 `detail` array. */
export interface ValidationIssue {
  type: string
  loc: (string | number)[]
  msg: string
  input?: unknown
  ctx?: Record<string, unknown>
}

/** 422s come in two shapes: Pydantic array (syntax/SSRF) or plain string (DNS). */
export type ApiErrorBody = { detail: string } | { detail: ValidationIssue[] }

export interface ScoreBreakdown {
  kind: "ok" | "inconsistent" | "absent"
  scores?: { a: number; b: number; c: number; d: number; total: number }
  caps: { a: 35; b: 25; c: 20; d: 20 }
  reason?: string
}
