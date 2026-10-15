"use client"

import {
  keepPreviousData,
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query"

import {
  ACTIVITY_PAGE_SIZE,
  LEAD_PAGE_SIZE,
  fetchApiVersion,
  fetchHealth,
  fetchLead,
  fetchLeads,
  queryKeys,
  submitEnrich,
} from "@/lib/api"
import type { EnrichRequest, FitTier, LeadStatus } from "@/lib/types"

export const HEALTH_POLL_MS = 15_000
export const ACTIVITY_POLL_ACTIVE_MS = 3_000
export const ACTIVITY_POLL_IDLE_MS = 15_000

export function useHealth() {
  return useQuery({
    queryKey: queryKeys.health,
    queryFn: fetchHealth,
    refetchInterval: HEALTH_POLL_MS,
    // fetchHealth resolves to a state instead of throwing, so retrying only
    // delays the pill flipping to red.
    retry: false,
    staleTime: 0,
  })
}

export function useApiVersion() {
  return useQuery({
    queryKey: queryKeys.version,
    queryFn: fetchApiVersion,
    staleTime: Infinity,
    retry: 1,
  })
}

/** Global activity query: every in-flight lead, regardless of table filters. */
export function usePendingLeads() {
  return useQuery({
    queryKey: queryKeys.pending,
    queryFn: () => fetchLeads({ status: "PENDING", pageSize: ACTIVITY_PAGE_SIZE }),
    refetchInterval: (query) =>
      (query.state.data?.items.length ?? 0) > 0
        ? ACTIVITY_POLL_ACTIVE_MS
        : ACTIVITY_POLL_IDLE_MS,
    staleTime: 0,
  })
}

interface LeadListQuery {
  page: number
  status: LeadStatus | null
  tier: FitTier | null
}

/** Not polled: refetched only when the pending set changes or on mutation. */
export function useLeadList(params: LeadListQuery) {
  return useQuery({
    queryKey: queryKeys.leadList(params),
    queryFn: () =>
      fetchLeads({ ...params, pageSize: LEAD_PAGE_SIZE }),
    placeholderData: keepPreviousData,
  })
}

export function useLead(id: number | null) {
  return useQuery({
    queryKey: queryKeys.lead(id ?? -1),
    queryFn: () => fetchLead(id as number),
    enabled: id !== null,
  })
}

export function useEnrichMutation() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: (body: EnrichRequest) => submitEnrich(body),
    onSettled: () =>
      // Invalidate even on failure: a 5xx may still have created the row.
      Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.leadLists }),
        queryClient.invalidateQueries({ queryKey: queryKeys.pending }),
      ]),
  })
}
