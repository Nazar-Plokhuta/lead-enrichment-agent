import { describe, expect, it } from "vitest"

import { parseEnrichedPayload } from "../schemas"
import {
  getErrorHint,
  normalizeApiError,
  normalizeTimestamp,
} from "../utils"

describe("normalizeApiError", () => {
  it("strips the Pydantic 'Value error, ' prefix", () => {
    const body = {
      detail: [
        {
          type: "value_error",
          loc: ["body", "url"],
          msg: "Value error, Address '127.0.0.1' is private or reserved",
        },
      ],
    }
    expect(normalizeApiError(422, body)).toBe(
      "Address '127.0.0.1' is private or reserved",
    )
  })

  it("joins multiple Pydantic issues", () => {
    const body = { detail: [{ msg: "first" }, { msg: "Value error, second" }] }
    expect(normalizeApiError(422, body)).toBe("first; second")
  })

  it("passes the DNS string shape through", () => {
    expect(
      normalizeApiError(422, { detail: "Host 'x' could not be resolved" }),
    ).toBe("Host 'x' could not be resolved")
  })

  it("falls back by status for 502, 503 and unknown bodies", () => {
    expect(normalizeApiError(502, null)).toMatch(/unreachable/i)
    expect(normalizeApiError(503, undefined)).toMatch(/unavailable/i)
    expect(normalizeApiError(500, { oops: true })).toBe(
      "Request failed (HTTP 500).",
    )
    expect(normalizeApiError(422, { detail: [] })).toBe(
      "Request failed (HTTP 422).",
    )
  })
})

describe("normalizeTimestamp", () => {
  const expected = Date.UTC(2026, 9, 14, 12, 0, 0)

  it("treats naive SQLite timestamps as UTC", () => {
    expect(normalizeTimestamp("2026-10-14T12:00:00").getTime()).toBe(expected)
  })

  it("handles naive timestamps with fractional seconds", () => {
    expect(normalizeTimestamp("2026-10-14T12:00:00.250").getTime()).toBe(
      expected + 250,
    )
  })

  it("honours Z and explicit offsets (Postgres)", () => {
    expect(normalizeTimestamp("2026-10-14T12:00:00Z").getTime()).toBe(expected)
    expect(normalizeTimestamp("2026-10-14T12:00:00+00:00").getTime()).toBe(
      expected,
    )
    expect(normalizeTimestamp("2026-10-14T14:00:00+02:00").getTime()).toBe(
      expected,
    )
    expect(normalizeTimestamp("2026-10-14T07:00:00-05:00").getTime()).toBe(
      expected,
    )
    expect(
      normalizeTimestamp("2026-10-14T12:00:00.123456+00:00").getTime(),
    ).toBe(expected + 123)
  })

  it("accepts a space separator", () => {
    expect(normalizeTimestamp("2026-10-14 12:00:00").getTime()).toBe(expected)
  })
})

describe("getErrorHint", () => {
  it.each([
    ["Navigation timed out after 30s", "Slow or blocking site. Retry may work."],
    [
      "URL blocked: private address",
      "Destination is not a public address. Retrying won't help.",
    ],
    ["LLM provider request failed", "Check API key and provider quota."],
    ["Page could not be loaded (HTTP 404)", "Retry may work."],
  ])("hints for %s", (message, hint) => {
    expect(getErrorHint(message)).toBe(hint)
  })

  it("returns null for unknown or missing messages", () => {
    expect(getErrorHint("Something else")).toBeNull()
    expect(getErrorHint(null)).toBeNull()
    expect(getErrorHint("")).toBeNull()
  })
})

describe("parseEnrichedPayload", () => {
  const valid = {
    company_name: "Forgeboard",
    analysis: {
      industry: "B2B SaaS",
      target_audience: "Mid-market engineering teams",
      value_proposition: "Workspace for engineers",
      pain_points: ["a", "b", "c"],
    },
    scoring: {
      fit_tier: "Tier 1 (High)",
      fit_score: 92,
      scoring_rationale: "A=33, B=23, C=18, D=18 → total=92",
      missing_information: [],
    },
    outreach: { icebreaker: "Hi", suggested_angle: "Angle" },
  }

  it("parses a valid payload", () => {
    expect(parseEnrichedPayload(valid)?.company_name).toBe("Forgeboard")
  })

  it("returns null for malformed input", () => {
    expect(parseEnrichedPayload(null)).toBeNull()
    expect(parseEnrichedPayload({ company_name: "x" })).toBeNull()
    expect(
      parseEnrichedPayload({
        ...valid,
        scoring: { ...valid.scoring, fit_tier: "Tier 9" },
      }),
    ).toBeNull()
  })
})
