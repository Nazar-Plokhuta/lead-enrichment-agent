"use client"

import { usePathname, useRouter, useSearchParams } from "next/navigation"
import { useCallback, useMemo } from "react"

import {
  parsePage,
  parseStatus,
  parseTierSlug,
  type TierSlug,
} from "@/lib/format"
import type { LeadStatus } from "@/lib/types"

export interface ConsoleParams {
  tier: TierSlug | null
  status: LeadStatus | null
  page: number
  lead: number | null
}

export type ParamPatch = Partial<{
  [K in keyof ConsoleParams]: ConsoleParams[K] | null
}>

function parseLeadId(value: string | null): number | null {
  return value !== null && /^\d+$/.test(value) ? Number(value) : null
}

/**
 * Console state lives in the URL (`?tier=&status=&page=&lead=`) so views are
 * linkable and survive reloads. Updates use `replace` to avoid flooding history
 * with every filter click. Changing a filter resets pagination.
 */
export function useConsoleParams() {
  const searchParams = useSearchParams()
  const router = useRouter()
  const pathname = usePathname()

  const params = useMemo<ConsoleParams>(
    () => ({
      tier: parseTierSlug(searchParams.get("tier")),
      status: parseStatus(searchParams.get("status")),
      page: parsePage(searchParams.get("page")),
      lead: parseLeadId(searchParams.get("lead")),
    }),
    [searchParams],
  )

  const update = useCallback(
    (patch: ParamPatch) => {
      const next = new URLSearchParams(searchParams.toString())
      for (const [key, value] of Object.entries(patch)) {
        if (value === null || value === undefined || (key === "page" && value === 1)) {
          next.delete(key)
        } else {
          next.set(key, String(value))
        }
      }
      if (("tier" in patch || "status" in patch) && !("page" in patch)) next.delete("page")
      const query = next.toString()
      router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false })
    },
    [pathname, router, searchParams],
  )

  const hasFilters = params.tier !== null || params.status !== null
  return { params, update, hasFilters }
}
