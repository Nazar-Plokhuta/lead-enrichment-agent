export { cn } from "cn"

const VALUE_ERROR_PREFIX = "Value error, "

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null
}

/**
 * Turns a failed API response into one human-readable line. FastAPI 422s have
 * two shapes (Pydantic array for syntax/SSRF checks, plain string for DNS
 * failures); proxy errors carry no useful body, so status drives the fallback.
 */
export function normalizeApiError(status: number, data: unknown): string {
  if (isRecord(data)) {
    const { detail } = data
    if (typeof detail === "string" && detail.trim()) return detail
    if (Array.isArray(detail)) {
      const messages = detail
        .map((issue) =>
          isRecord(issue) && typeof issue.msg === "string"
            ? issue.msg.startsWith(VALUE_ERROR_PREFIX)
              ? issue.msg.slice(VALUE_ERROR_PREFIX.length)
              : issue.msg
            : null,
        )
        .filter((msg): msg is string => msg !== null && msg !== "")
      if (messages.length > 0) return messages.join("; ")
    }
  }
  if (status === 502) return "Backend is unreachable. Is the API server running?"
  if (status === 503) return "Service unavailable. Check backend health."
  return `Request failed (HTTP ${status}).`
}

// Matches a trailing `Z` or `±hh[:mm]` offset after the time component.
const HAS_TIMEZONE = /T.*(?:Z|[+-]\d{2}(?::?\d{2})?)$/i

/**
 * SQLite returns naive UTC timestamps (no `Z`), which `Date` would read as
 * local time and skew relative ages and the STALLED check. Postgres returns
 * explicit offsets and passes through untouched.
 */
export function normalizeTimestamp(isoString: string): Date {
  const iso = isoString.trim().replace(" ", "T")
  const hasTime = iso.includes("T")
  return new Date(hasTime && !HAS_TIMEZONE.test(iso) ? `${iso}Z` : iso)
}

const ERROR_HINTS: readonly (readonly [prefix: string, hint: string])[] = [
  ["Navigation timed out", "Slow or blocking site. Retry may work."],
  [
    "URL blocked",
    "Destination is not a public address. Retrying won't help.",
  ],
  ["LLM provider request failed", "Check API key and provider quota."],
  ["Page could not be loaded", "Retry may work."],
]

/** Maps sanitized backend `public_reason` prefixes to an operator action. */
export function getErrorHint(errorMessage: string | null): string | null {
  if (!errorMessage) return null
  const match = ERROR_HINTS.find(([prefix]) => errorMessage.startsWith(prefix))
  return match ? match[1] : null
}
