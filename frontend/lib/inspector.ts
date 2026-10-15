import type { FitTier, ScoreBreakdown } from "./types"
import { checkRubricIntegrity, SCORE_CAPS } from "./parser"

// Mirrors the arrow tolerance of `parser.ts`; anchored to the end so a formula
// quoted mid-rationale is left intact and only the trailing summary is cut.
const TRAILING_FORMULA =
  /\s*A=\d+,\s*B=\d+,\s*C=\d+,\s*D=\d+\s*(?:→|->|=>|⇒)\s*total=\d+\.?\s*$/

/** Removes the machine-oriented `A=.. -> total=..` tail shown by the rubric UI instead. */
export function stripScoreFormula(rationale: string): string {
  return rationale.replace(TRAILING_FORMULA, "").trimEnd()
}

/**
 * Returns a normalized URL only for http(s). Scraped/LLM-adjacent data must
 * never reach an `href` with `javascript:` or `data:` schemes.
 */
export function safeHttpUrl(value: string): string | null {
  try {
    const url = new URL(value)
    return url.protocol === "http:" || url.protocol === "https:" ? url.href : null
  } catch {
    return null
  }
}

export interface RubricSegment {
  key: "a" | "b" | "c" | "d"
  label: string
  name: string
  cap: number
  /** Points awarded, clamped to `[0, cap]` so a bad sub-score cannot overflow its segment. */
  awarded: number
}

const SEGMENT_META = [
  { key: "a", label: "A", name: "Model" },
  { key: "b", label: "B", name: "ICP" },
  { key: "c", label: "C", name: "Comm" },
  { key: "d", label: "D", name: "Proof" },
] as const

export function rubricSegments(scores: NonNullable<ScoreBreakdown["scores"]>): RubricSegment[] {
  return SEGMENT_META.map(({ key, label, name }) => {
    const cap = SCORE_CAPS[key]
    return { key, label, name, cap, awarded: Math.min(cap, Math.max(0, scores[key])) }
  })
}

export interface IntegrityResult {
  ok: boolean
  /** Human-readable causes; empty when `ok`. */
  reasons: string[]
}

/**
 * Combines the formula cross-check (`parseScoreSummary`) with the tier-band
 * check. An absent formula is not a mismatch: older rows predate the rubric.
 */
export function evaluateIntegrity(
  breakdown: ScoreBreakdown,
  score: number,
  tier: FitTier,
): IntegrityResult {
  const reasons: string[] = []
  if (breakdown.kind === "inconsistent" && breakdown.reason) reasons.push(breakdown.reason)
  if (!checkRubricIntegrity(score, tier)) {
    reasons.push(`Score ${score} falls outside the band for ${tier}`)
  }
  return { ok: reasons.length === 0, reasons }
}
