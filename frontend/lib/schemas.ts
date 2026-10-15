import { z } from "zod"

import type { EnrichedLeadPayload } from "./types"

const fitTierSchema = z.enum([
  "Tier 1 (High)",
  "Tier 2 (Medium)",
  "Tier 3 (Low)",
  "Disqualified",
])

/**
 * Runtime mirror of the backend's `EnrichedLeadPayload`. `enriched_data` is
 * typed as an open record over the wire, so it is validated here before any
 * component reads nested fields.
 */
export const enrichedLeadPayloadSchema: z.ZodType<EnrichedLeadPayload> =
  z.object({
    company_name: z.string(),
    analysis: z.object({
      industry: z.string(),
      target_audience: z.string(),
      value_proposition: z.string(),
      pain_points: z.array(z.string()),
    }),
    scoring: z.object({
      fit_tier: fitTierSchema,
      fit_score: z.number().int().min(0).max(100),
      scoring_rationale: z.string(),
      missing_information: z.array(z.string()),
    }),
    outreach: z.object({
      icebreaker: z.string(),
      suggested_angle: z.string(),
    }),
  })

/** Returns `null` instead of throwing so the UI can degrade to a raw view. */
export function parseEnrichedPayload(data: unknown): EnrichedLeadPayload | null {
  const result = enrichedLeadPayloadSchema.safeParse(data)
  return result.success ? result.data : null
}
