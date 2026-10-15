import { describe, expect, it } from "vitest"

import {
  STALL_THRESHOLD_MS,
  asFitTier,
  formatElapsed,
  formatRelative,
  hostnameOf,
  isStalled,
  leadState,
  parsePage,
  parseStatus,
  parseTierSlug,
  prepareSubmitUrl,
  tierFromSlug,
} from "../format"

describe("prepareSubmitUrl", () => {
  it("returns null for blank input", () => {
    expect(prepareSubmitUrl("   ")).toEqual({ url: null, prefixed: false })
  })

  it("prefixes bare domains with https://", () => {
    expect(prepareSubmitUrl(" linear.app ")).toEqual({
      url: "https://linear.app",
      prefixed: true,
    })
  })

  it("leaves URLs with a scheme untouched, including SSRF probes", () => {
    for (const probe of [
      "http://169.254.169.254",
      "http://127.0.0.1@example.com",
      "HTTPS://Vercel.com",
    ]) {
      expect(prepareSubmitUrl(probe)).toEqual({ url: probe, prefixed: false })
    }
  })

  it("prefixes host:port inputs that merely contain a colon", () => {
    expect(prepareSubmitUrl("localhost:8000").url).toBe("https://localhost:8000")
  })
})

describe("hostnameOf", () => {
  it("extracts the hostname and tolerates garbage", () => {
    expect(hostnameOf("https://linear.app/about")).toBe("linear.app")
    expect(hostnameOf("not a url")).toBe("not a url")
  })
})

describe("stalled detection", () => {
  const now = Date.parse("2026-01-01T12:00:00Z")
  const ago = (ms: number) => new Date(now - ms).toISOString()

  it("flags PENDING leads older than five minutes", () => {
    const lead = { status: "PENDING" as const, updated_at: ago(STALL_THRESHOLD_MS + 1000) }
    expect(isStalled(lead, now)).toBe(true)
    expect(leadState(lead, now)).toBe("STALLED")
  })

  it("does not flag fresh PENDING or non-PENDING leads", () => {
    expect(isStalled({ status: "PENDING", updated_at: ago(60_000) }, now)).toBe(false)
    expect(isStalled({ status: "FAILED", updated_at: ago(10 * STALL_THRESHOLD_MS) }, now)).toBe(
      false,
    )
  })

  it("treats naive SQLite timestamps as UTC", () => {
    const naive = "2026-01-01 11:50:00"
    expect(isStalled({ status: "PENDING", updated_at: naive }, now)).toBe(true)
  })
})

describe("time formatting", () => {
  it("formats relative ages", () => {
    expect(formatRelative(2_000)).toBe("just now")
    expect(formatRelative(42_000)).toBe("42s ago")
    expect(formatRelative(120_000)).toBe("2m ago")
    expect(formatRelative(3 * 3_600_000)).toBe("3h ago")
    expect(formatRelative(5 * 86_400_000)).toBe("5d ago")
  })

  it("formats elapsed timers", () => {
    expect(formatElapsed(42_000)).toBe("0:42")
    expect(formatElapsed(725_000)).toBe("12:05")
    expect(formatElapsed(3_723_000)).toBe("1:02:03")
    expect(formatElapsed(-5)).toBe("0:00")
  })
})

describe("filter params", () => {
  it("round-trips tier slugs and rejects unknown ones", () => {
    expect(parseTierSlug("t2")).toBe("t2")
    expect(parseTierSlug("bogus")).toBeNull()
    expect(tierFromSlug("dq")).toBe("Disqualified")
  })

  it("validates status and page", () => {
    expect(parseStatus("FAILED")).toBe("FAILED")
    expect(parseStatus("failed")).toBeNull()
    expect(parsePage("3")).toBe(3)
    expect(parsePage("0")).toBe(1)
    expect(parsePage("abc")).toBe(1)
    expect(parsePage(null)).toBe(1)
  })

  it("narrows fit_tier strings", () => {
    expect(asFitTier("Tier 1 (High)")).toBe("Tier 1 (High)")
    expect(asFitTier("Tier 9")).toBeNull()
    expect(asFitTier(null)).toBeNull()
  })
})
