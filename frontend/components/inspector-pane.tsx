"use client"

import { useCallback, useMemo } from "react"

import { LeadInspector } from "@/components/lead-inspector"
import { Sheet, SheetContent, SheetDescription, SheetTitle } from "@/components/ui/sheet"
import { useConsoleParams } from "@/hooks/use-console-params"
import { useMediaQuery } from "@/hooks/use-media-query"
import { useLeadList } from "@/hooks/use-queries"
import { tierFromSlug } from "@/lib/format"

/** Matches Tailwind's `xl`; below it a docked pane would starve the table. */
const DOCK_QUERY = "(min-width: 1280px)"

/**
 * Mounts the inspector whenever `?lead=<id>` is present: docked beside the
 * table on wide screens (so polling and `j`/`k` keep working), otherwise as a
 * modal Sheet. The URL is the single source of truth for open/closed.
 */
export function InspectorPane() {
  const { params, update } = useConsoleParams()
  const docked = useMediaQuery(DOCK_QUERY)

  // Same query key as the table, so this reads the shared cache (no extra fetch).
  const { data } = useLeadList({
    page: params.page,
    status: params.status,
    tier: params.tier ? tierFromSlug(params.tier) : null,
  })
  const ids = useMemo(() => data?.items.map((lead) => lead.id) ?? [], [data])

  const close = useCallback(() => update({ lead: null }), [update])
  const navigate = useCallback((id: number) => update({ lead: id }), [update])

  if (params.lead === null) return null
  const inspector = (
    <LeadInspector leadId={params.lead} ids={ids} onNavigate={navigate} onClose={close} />
  )

  if (docked) {
    return (
      <aside
        aria-label="Lead inspector"
        className="sticky top-12 h-[calc(100dvh-3rem)] w-[540px] shrink-0 self-start border-l border-zinc-800 bg-zinc-950"
      >
        {inspector}
      </aside>
    )
  }

  return (
    <Sheet open onOpenChange={(open) => (open ? undefined : close())}>
      <SheetContent
        side="right"
        showCloseButton={false}
        className="w-full gap-0 border-zinc-800 bg-zinc-950 p-0 text-zinc-100 data-[side=right]:w-full data-[side=right]:sm:max-w-[560px]"
      >
        <SheetTitle className="sr-only">Lead inspector</SheetTitle>
        <SheetDescription className="sr-only">
          Enrichment result, score breakdown and outreach for the selected lead.
        </SheetDescription>
        {inspector}
      </SheetContent>
    </Sheet>
  )
}
