import { describe, expect, it } from "vitest"

import { checkRubricIntegrity, parseScoreSummary } from "../parser"

// Tails of the golden rationales in tests/fixtures/eval (see
// tests/test_scoring_regression.py).
const FORGEBOARD =
  "Exact headcount inside that range is not stated. A=33, B=23, C=18, D=18 → total=92"
const HARBORLINE =
  "without named case studies or an integration marketplace. A=22, B=12, C=10, D=10 → total=54"
const WILLOW =
  "with no technical or customer-logo proof. A=0, B=4, C=4, D=3 → total=11"

describe("parseScoreSummary", () => {
  it.each([
    ["Forgeboard", FORGEBOARD, 92, { a: 33, b: 23, c: 18, d: 18, total: 92 }],
    ["Harborline", HARBORLINE, 54, { a: 22, b: 12, c: 10, d: 10, total: 54 }],
    ["Willow & Grain", WILLOW, 11, { a: 0, b: 4, c: 4, d: 3, total: 11 }],
  ])("accepts golden fixture %s", (_name, text, score, scores) => {
    const result = parseScoreSummary(text, score)
    expect(result.kind).toBe("ok")
    expect(result.scores).toEqual(scores)
    expect(result.caps).toEqual({ a: 35, b: 25, c: 20, d: 20 })
  })

  it.each(["->", "=>", "⇒", "→"])("tolerates the %s arrow", (arrow) => {
    const result = parseScoreSummary(
      `A=30, B=20, C=15, D=10 ${arrow} total=75`,
      75,
    )
    expect(result.kind).toBe("ok")
  })

  it("uses the last occurrence", () => {
    const text =
      "Example: A=28, B=18, C=12, D=14 → total=72. Actual: A=10, B=10, C=10, D=10 → total=40"
    const result = parseScoreSummary(text, 40)
    expect(result.kind).toBe("ok")
    expect(result.scores?.total).toBe(40)
  })

  it("flags a sub-score above its cap", () => {
    const result = parseScoreSummary("A=40, B=20, C=20, D=20 -> total=100", 100)
    expect(result.kind).toBe("inconsistent")
    expect(result.reason).toContain("A=40")
    expect(result.scores?.a).toBe(40)
  })

  it("flags an arithmetic mismatch", () => {
    const result = parseScoreSummary("A=30, B=20, C=20, D=20 -> total=85", 85)
    expect(result.kind).toBe("inconsistent")
    expect(result.reason).toContain("90")
  })

  it("flags a total that disagrees with fit_score", () => {
    const result = parseScoreSummary(FORGEBOARD, 80)
    expect(result.kind).toBe("inconsistent")
    expect(result.reason).toContain("80")
  })

  it("skips the fit_score check when none is given", () => {
    expect(parseScoreSummary(FORGEBOARD).kind).toBe("ok")
    expect(parseScoreSummary(FORGEBOARD, null).kind).toBe("ok")
  })

  it("returns absent without a formula", () => {
    const result = parseScoreSummary("No formula here, total=50", 50)
    expect(result.kind).toBe("absent")
    expect(result.scores).toBeUndefined()
    expect(result.caps).toEqual({ a: 35, b: 25, c: 20, d: 20 })
  })
})

describe("checkRubricIntegrity", () => {
  it.each([
    [0, "Disqualified", true],
    [24, "Disqualified", true],
    [25, "Disqualified", false],
    [24, "Tier 3 (Low)", false],
    [25, "Tier 3 (Low)", true],
    [49, "Tier 3 (Low)", true],
    [50, "Tier 3 (Low)", false],
    [50, "Tier 2 (Medium)", true],
    [74, "Tier 2 (Medium)", true],
    [75, "Tier 2 (Medium)", false],
    [75, "Tier 1 (High)", true],
    [100, "Tier 1 (High)", true],
    [74, "Tier 1 (High)", false],
  ] as const)("score %i vs %s -> %s", (score, tier, expected) => {
    expect(checkRubricIntegrity(score, tier)).toBe(expected)
  })

  it("rejects out-of-range and fractional scores", () => {
    expect(checkRubricIntegrity(101, "Tier 1 (High)")).toBe(false)
    expect(checkRubricIntegrity(-1, "Disqualified")).toBe(false)
    expect(checkRubricIntegrity(74.5, "Tier 2 (Medium)")).toBe(false)
  })
})
