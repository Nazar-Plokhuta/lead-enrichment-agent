"use client"

import { useQueryClient } from "@tanstack/react-query"
import { useEffect, useRef } from "react"

import { queryKeys } from "@/lib/api"

import { usePendingLeads } from "./use-queries"

/**
 * Keeps the (unpolled) table list fresh without polling it: whenever the set
 * of PENDING ids changes, a lead finished or a new one arrived, so the list
 * is invalidated once. Mount exactly once, or the invalidation fires per copy.
 */
export function usePendingSetSync(): void {
  const queryClient = useQueryClient()
  const { data } = usePendingLeads()
  const previous = useRef<string | null>(null)

  const signature = data
    ? data.items
        .map((lead) => lead.id)
        .sort((a, b) => a - b)
        .join(",")
    : null

  useEffect(() => {
    if (signature === null) return
    if (previous.current !== null && previous.current !== signature) {
      void queryClient.invalidateQueries({ queryKey: queryKeys.leadLists })
    }
    previous.current = signature
  }, [signature, queryClient])
}
