"use client"

import { useQueryClient } from "@tanstack/react-query"
import { Hourglass, Keyboard, Loader2 } from "lucide-react"
import { useEffect, useMemo, useState } from "react"
import { toast } from "sonner"

import { StateGlyph } from "@/components/state-glyph"
import { Button } from "@/components/ui/button"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { useConsoleParams } from "@/hooks/use-console-params"
import { useNow } from "@/hooks/use-now"
import { useApiVersion, useHealth, usePendingLeads } from "@/hooks/use-queries"
import { ACTIVITY_PAGE_SIZE, queryKeys, submitEnrich } from "@/lib/api"
import { formatElapsed, hostnameOf, isStalled, leadAgeMs } from "@/lib/format"
import type { LeadResponse } from "@/lib/types"
import { cn } from "@/lib/utils"

const BASE_TITLE = "Lead Enrichment · Console"

function HealthPill() {
  const { data, isPending } = useHealth()

  const { dot, text, textClass } = isPending || !data
    ? { dot: "bg-zinc-500 animate-pulse", text: "checking", textClass: "text-zinc-500" }
    : data.kind === "healthy"
      ? { dot: "bg-emerald-400", text: "healthy", textClass: "text-zinc-300" }
      : data.kind === "degraded"
        ? { dot: "bg-red-400", text: "degraded", textClass: "text-red-300" }
        : { dot: "bg-red-400", text: "unreachable", textClass: "text-red-300" }

  return (
    <div
      role="status"
      aria-label={`API ${text}`}
      className="flex items-center gap-2 rounded-md px-2 py-1 text-xs ring-1 ring-inset ring-zinc-800"
    >
      <span aria-hidden className={cn("size-1.5 rounded-full", dot)} />
      <span className={textClass}>{text}</span>
      {data?.kind === "healthy" ? (
        <span className="font-mono tabular-nums text-zinc-500">{data.latencyMs}ms</span>
      ) : null}
    </div>
  )
}

function VersionBadge() {
  const { data } = useApiVersion()
  if (!data) return null
  return (
    <span className="rounded px-1.5 py-0.5 font-mono text-[11px] text-zinc-500 ring-1 ring-inset ring-zinc-800">
      v{data}
    </span>
  )
}

function ActivityRow({ lead, nowMs, onOpen }: { lead: LeadResponse; nowMs: number; onOpen: () => void }) {
  const stalled = isStalled(lead, nowMs)
  return (
    <button
      type="button"
      onClick={onOpen}
      className="flex h-8 w-full items-center gap-2 rounded px-2 text-left text-[13px] outline-none hover:bg-zinc-900/60 focus-visible:ring-2 focus-visible:ring-indigo-400"
    >
      <StateGlyph state={stalled ? "STALLED" : "PENDING"} />
      <span className="w-10 font-mono tabular-nums text-zinc-400">#{lead.id}</span>
      <span className="min-w-0 flex-1 truncate text-zinc-200">{hostnameOf(lead.url)}</span>
      <span
        className={cn(
          "font-mono tabular-nums text-xs",
          stalled ? "text-amber-300" : "text-zinc-500",
        )}
      >
        {formatElapsed(leadAgeMs(lead, nowMs))}
      </span>
    </button>
  )
}

function ActivityIndicator() {
  const { data } = usePendingLeads()
  const nowMs = useNow()
  const queryClient = useQueryClient()
  const { update } = useConsoleParams()
  const [open, setOpen] = useState(false)
  const [requeueing, setRequeueing] = useState(false)

  const items = useMemo(() => data?.items ?? [], [data])
  const stalled = items.filter((lead) => isStalled(lead, nowMs))
  const running = items.length - stalled.length
  const truncated = (data?.total ?? 0) > items.length

  // Dynamic tab title so in-flight work is visible from a background tab.
  useEffect(() => {
    document.title = items.length > 0 ? `(${items.length}) ${BASE_TITLE}` : BASE_TITLE
    return () => {
      document.title = BASE_TITLE
    }
  }, [items.length])

  async function requeueStalled() {
    setRequeueing(true)
    // allSettled: one dead URL must not abort re-queueing the rest.
    const results = await Promise.allSettled(
      stalled.map((lead) => submitEnrich({ url: lead.url, force: true })),
    )
    const failed = results.filter((result) => result.status === "rejected").length
    const succeeded = results.length - failed
    if (failed > 0) {
      toast.error(`Re-queued ${succeeded}, failed ${failed}`, {
        description: "Some requests were rejected. Check API health.",
      })
    } else {
      toast.success(`Re-queued ${succeeded} stalled lead${succeeded === 1 ? "" : "s"}`)
    }
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: queryKeys.pending }),
      queryClient.invalidateQueries({ queryKey: queryKeys.leadLists }),
    ])
    setRequeueing(false)
  }

  const idle = items.length === 0

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label="Pipeline activity"
          className="flex items-center gap-2 rounded-md px-2 py-1 text-xs outline-none ring-1 ring-inset ring-zinc-800 hover:bg-zinc-900/60 focus-visible:ring-2 focus-visible:ring-indigo-400"
        >
          {idle ? (
            <span className="text-zinc-500">idle</span>
          ) : (
            <>
              <span className="flex items-center gap-1.5 text-sky-300">
                <span aria-hidden className="size-1.5 animate-pulse rounded-full bg-sky-400" />
                <span className="font-mono tabular-nums">{running}</span> running
              </span>
              {stalled.length > 0 ? (
                <span className="flex items-center gap-1 text-amber-300">
                  <Hourglass aria-hidden className="size-3" />
                  <span className="font-mono tabular-nums">{stalled.length}</span> stalled
                </span>
              ) : null}
            </>
          )}
        </button>
      </PopoverTrigger>
      <PopoverContent
        align="end"
        className="w-96 border-zinc-800 bg-zinc-900 p-1 text-zinc-100"
      >
        <div className="flex items-center justify-between px-2 py-1.5">
          <span className="text-xs font-medium text-zinc-300">In flight</span>
          {stalled.length > 0 ? (
            <Button
              size="xs"
              variant="outline"
              disabled={requeueing}
              onClick={() => void requeueStalled()}
              className="border-amber-400/30 text-amber-300 hover:bg-amber-500/10 hover:text-amber-200"
            >
              {requeueing ? <Loader2 className="animate-spin" /> : null}
              Re-queue stalled ({stalled.length})
            </Button>
          ) : null}
        </div>
        {idle ? (
          <p className="px-2 py-4 text-center text-xs text-zinc-500">No jobs in flight.</p>
        ) : (
          <div className="max-h-72 overflow-y-auto">
            {items.map((lead) => (
              <ActivityRow
                key={lead.id}
                lead={lead}
                nowMs={nowMs}
                onOpen={() => {
                  update({ lead: lead.id })
                  setOpen(false)
                }}
              />
            ))}
          </div>
        )}
        {truncated ? (
          <p className="px-2 py-1.5 text-[11px] text-zinc-500">
            Showing {ACTIVITY_PAGE_SIZE} of <span className="font-mono">{data?.total}</span>
          </p>
        ) : null}
      </PopoverContent>
    </Popover>
  )
}

export function Header({ onShowShortcuts }: { onShowShortcuts: () => void }) {
  return (
    <header className="sticky top-0 z-30 flex h-12 items-center justify-between border-b border-zinc-800 bg-zinc-950 px-4">
      <div className="flex items-center gap-3">
        <h1 className="text-sm font-medium text-zinc-100">
          Lead Enrichment <span className="text-zinc-500">Console</span>
        </h1>
        <VersionBadge />
      </div>
      <div className="flex items-center gap-2">
        <ActivityIndicator />
        <HealthPill />
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label="Keyboard shortcuts"
          onClick={onShowShortcuts}
          className="text-zinc-500 hover:text-zinc-200"
        >
          <Keyboard />
        </Button>
      </div>
    </header>
  )
}
