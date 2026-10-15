import type { FitTier, ScoreBreakdown } from "./types"

export const SCORE_CAPS = { a: 35, b: 25, c: 20, d: 20 } as const

const TIER_BANDS: Record<FitTier, readonly [number, number]> = {
  "Tier 1 (High)": [75, 100],
  "Tier 2 (Medium)": [50, 74],
  "Tier 3 (Low)": [25, 49],
  Disqualified: [0, 24],
}

// The v2.0 prompt mandates this chain-of-thought line before fit_score. The
// arrow is tolerated in several spellings because models drift from `→`.
const SCORE_SUMMARY =
  /A=(\d+),\s*B=(\d+),\s*C=(\d+),\s*D=(\d+)\s*(?:→|->|=>|⇒)\s*total=(\d+)/g

/**
 * Extracts the A/B/C/D sub-scores from a scoring rationale and cross-checks
 * them against the rubric. The LAST match wins: a rationale may quote an
 * example formula earlier, and the prompt places the real one at the end.
 */
export function parseScoreSummary(
  rationale: string,
  fitScore?: number | null,
): ScoreBreakdown {
  const caps = { ...SCORE_CAPS }
  const matches = [...rationale.matchAll(SCORE_SUMMARY)]
  const last = matches.at(-1)
  if (!last) return { kind: "absent", caps }

  const [a, b, c, d, total] = last.slice(1).map(Number)
  const scores = { a, b, c, d, total }

  const exceeded = (["a", "b", "c", "d"] as const).filter(
    (key) => scores[key] > SCORE_CAPS[key],
  )
  let reason: string | undefined
  if (exceeded.length > 0) {
    reason = `Sub-score exceeds cap: ${exceeded
      .map((key) => `${key.toUpperCase()}=${scores[key]} (max ${SCORE_CAPS[key]})`)
      .join(", ")}`
  } else if (a + b + c + d !== total) {
    reason = `Sub-scores sum to ${a + b + c + d}, but total states ${total}`
  } else if (fitScore != null && total !== fitScore) {
    reason = `Rationale total ${total} differs from fit_score ${fitScore}`
  }

  return reason
    ? { kind: "inconsistent", scores, caps, reason }
    : { kind: "ok", scores, caps }
}

/** True when `score` falls inside the published band of `tier`. */
export function checkRubricIntegrity(score: number, tier: FitTier): boolean {
  const [lower, upper] = TIER_BANDS[tier]
  return Number.isInteger(score) && score >= lower && score <= upper
}
