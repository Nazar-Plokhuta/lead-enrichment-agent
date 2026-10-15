import type { FitTier, LeadResponse, LeadStatus } from "./types"
import { normalizeTimestamp } from "./utils"

/** A PENDING lead older than this is presumed orphaned (e.g. server restart). */
export const STALL_THRESHOLD_MS = 5 * 60 * 1000

const HAS_SCHEME = /^[a-z][a-z0-9+.-]*:\/\//i

export interface SubmitUrl {
  /** The value to POST. `null` when the input is blank. */
  url: string | null
  /** True when `https://` was added, so the UI can show what will be sent. */
  prefixed: boolean
}

/**
 * Prefixes bare domains with `https://`. Anything that already carries a
 * scheme is passed through verbatim, including SSRF probes such as
 * `http://127.0.0.1@example.com`, because the backend is what must reject them.
 */
export function prepareSubmitUrl(input: string): SubmitUrl {
  const trimmed = input.trim()
  if (trimmed === "") return { url: null, prefixed: false }
  if (HAS_SCHEME.test(trimmed)) return { url: trimmed, prefixed: false }
  return { url: `https://${trimmed}`, prefixed: true }
}

/** Display hostname; falls back to the raw string for unparsable URLs. */
export function hostnameOf(url: string): string {
  try {
    return new URL(url).hostname || url
  } catch {
    // Stored URLs passed backend validation, but a display helper must never throw.
    return url
  }
}

export type LeadState = LeadStatus | "STALLED"

export function leadAgeMs(lead: Pick<LeadResponse, "updated_at">, nowMs: number): number {
  return Math.max(0, nowMs - normalizeTimestamp(lead.updated_at).getTime())
}

/**
 * Age is measured from `updated_at`, not `created_at`: a forced re-run resets
 * the existing row to PENDING, so `created_at` would flag it stalled at once.
 */
export function isStalled(
  lead: Pick<LeadResponse, "status" | "updated_at">,
  nowMs: number,
): boolean {
  return lead.status === "PENDING" && leadAgeMs(lead, nowMs) > STALL_THRESHOLD_MS
}

export function leadState(
  lead: Pick<LeadResponse, "status" | "updated_at">,
  nowMs: number,
): LeadState {
  return isStalled(lead, nowMs) ? "STALLED" : lead.status
}

/** Compact relative age: `just now`, `42s ago`, `2m ago`, `3h ago`, `5d ago`. */
export function formatRelative(ageMs: number): string {
  const seconds = Math.floor(Math.max(0, ageMs) / 1000)
  if (seconds < 5) return "just now"
  if (seconds < 60) return `${seconds}s ago`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m ago`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h ago`
  return `${Math.floor(hours / 24)}d ago`
}

/** Running timer for in-flight jobs: `0:42`, `12:05`, `1:02:03`. */
export function formatElapsed(ageMs: number): string {
  const total = Math.floor(Math.max(0, ageMs) / 1000)
  const seconds = String(total % 60).padStart(2, "0")
  const minutes = Math.floor(total / 60) % 60
  const hours = Math.floor(total / 3600)
  return hours > 0
    ? `${hours}:${String(minutes).padStart(2, "0")}:${seconds}`
    : `${minutes}:${seconds}`
}

/** `2026-10-15 12:34:56 UTC · 15:34:56 local` for tooltips. */
export function formatTimestampPair(isoString: string): string {
  const date = normalizeTimestamp(isoString)
  if (Number.isNaN(date.getTime())) return isoString
  const utc = date.toISOString().slice(0, 19).replace("T", " ")
  const pad = (n: number) => String(n).padStart(2, "0")
  const local = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
  return `${utc} UTC · ${local} local`
}

// --- Filters -----------------------------------------------------------------

/** URL-safe slugs keep `?tier=t1` readable; the API wants the full label. */
export const TIER_FILTERS = [
  { slug: "t1", label: "Tier 1", tier: "Tier 1 (High)" },
  { slug: "t2", label: "Tier 2", tier: "Tier 2 (Medium)" },
  { slug: "t3", label: "Tier 3", tier: "Tier 3 (Low)" },
  { slug: "dq", label: "Disqualified", tier: "Disqualified" },
] as const satisfies readonly { slug: string; label: string; tier: FitTier }[]

export type TierSlug = (typeof TIER_FILTERS)[number]["slug"]

export const STATUS_FILTERS = ["PENDING", "PROCESSED", "FAILED"] as const satisfies readonly LeadStatus[]

export function parseTierSlug(value: string | null): TierSlug | null {
  return TIER_FILTERS.find((entry) => entry.slug === value)?.slug ?? null
}

export function parseStatus(value: string | null): LeadStatus | null {
  return STATUS_FILTERS.find((status) => status === value) ?? null
}

export function tierFromSlug(slug: TierSlug): FitTier {
  const entry = TIER_FILTERS.find((candidate) => candidate.slug === slug)
  // `slug` is a closed union, so a miss is a programming error, not bad input.
  if (!entry) throw new Error(`Unknown tier slug: ${slug}`)
  return entry.tier
}

export function parsePage(value: string | null): number {
  const page = Number.parseInt(value ?? "", 10)
  return Number.isInteger(page) && page >= 1 ? page : 1
}

export const TIER_BADGES: Record<FitTier, { short: string; className: string }> = {
  "Tier 1 (High)": {
    short: "T1",
    className: "bg-indigo-500/20 text-indigo-200 ring-1 ring-inset ring-indigo-400/50",
  },
  "Tier 2 (Medium)": {
    short: "T2",
    className: "bg-indigo-500/10 text-indigo-300 ring-1 ring-inset ring-indigo-400/30",
  },
  "Tier 3 (Low)": {
    short: "T3",
    className: "bg-transparent text-zinc-300 ring-1 ring-inset ring-zinc-700",
  },
  Disqualified: {
    short: "DQ",
    className: "bg-transparent text-zinc-400 ring-1 ring-inset ring-zinc-800",
  },
}

/** Narrows the backend's untyped `fit_tier: string` to a known tier. */
export function asFitTier(value: string | null): FitTier | null {
  return value !== null && value in TIER_BADGES ? (value as FitTier) : null
}

export const PRESET_GROUPS = [
  {
    label: "Live targets",
    urls: ["https://linear.app", "https://vercel.com"],
  },
  {
    label: "SSRF & Security probes",
    urls: [
      "http://169.254.169.254",
      "http://localhost:8000",
      "http://127.0.0.1@example.com",
      "http://non-existent-domain-test.xyz",
    ],
  },
] as const
