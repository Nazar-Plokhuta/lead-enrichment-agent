import { describe, expect, it } from "vitest"

import {
  evaluateIntegrity,
  rubricSegments,
  safeHttpUrl,
  stripScoreFormula,
} from "../inspector"
import { parseScoreSummary } from "../parser"

describe("stripScoreFormula", () => {
  it("removes the trailing formula", () => {
    expect(stripScoreFormula("Strong SaaS fit. A=30, B=20, C=15, D=10 -> total=75")).toBe(
      "Strong SaaS fit.",
    )
  })

  it("accepts the unicode arrow and trailing period", () => {
    expect(stripScoreFormula("Ok.\nA=1, B=2, C=3, D=4 → total=10.")).toBe("Ok.")
  })

  it("keeps a formula quoted mid-rationale", () => {
    const text = "Example A=1, B=1, C=1, D=1 -> total=4 is the shape. Real fit is weak."
    expect(stripScoreFormula(text)).toBe(text)
  })

  it("leaves text without a formula untouched", () => {
    expect(stripScoreFormula("No formula here")).toBe("No formula here")
  })
})

describe("safeHttpUrl", () => {
  it("accepts http and https", () => {
    expect(safeHttpUrl("https://linear.app")).toBe("https://linear.app/")
    expect(safeHttpUrl("http://example.com/x")).toBe("http://example.com/x")
  })

  it("rejects other schemes and garbage", () => {
    expect(safeHttpUrl("javascript:alert(1)")).toBeNull()
    expect(safeHttpUrl("data:text/html,<b>x</b>")).toBeNull()
    expect(safeHttpUrl("ftp://example.com")).toBeNull()
    expect(safeHttpUrl("not a url")).toBeNull()
  })
})

describe("rubricSegments", () => {
  it("clamps awarded points to each cap", () => {
    const segments = rubricSegments({ a: 40, b: 10, c: -2, d: 20, total: 68 })
    expect(segments.map((s) => s.awarded)).toEqual([35, 10, 0, 20])
    expect(segments.map((s) => s.cap)).toEqual([35, 25, 20, 20])
  })
})

describe("evaluateIntegrity", () => {
  it("passes a consistent rubric inside its tier band", () => {
    const breakdown = parseScoreSummary("x A=30, B=20, C=15, D=10 -> total=75", 75)
    expect(evaluateIntegrity(breakdown, 75, "Tier 1 (High)")).toEqual({ ok: true, reasons: [] })
  })

  it("treats an absent formula as consistent when the tier matches", () => {
    const breakdown = parseScoreSummary("no formula", 60)
    expect(evaluateIntegrity(breakdown, 60, "Tier 2 (Medium)").ok).toBe(true)
  })

  it("reports both a bad sum and an out-of-band tier", () => {
    const breakdown = parseScoreSummary("A=30, B=20, C=15, D=10 -> total=80", 80)
    const result = evaluateIntegrity(breakdown, 80, "Tier 2 (Medium)")
    expect(result.ok).toBe(false)
    expect(result.reasons).toHaveLength(2)
  })
})
