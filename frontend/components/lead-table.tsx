"use client"

import { ChevronLeft, ChevronRight, Loader2, WifiOff } from "lucide-react"
import { useEffect, useMemo, useRef, useState } from "react"

import { ScoreBar } from "@/components/score-bar"
import { StateGlyph } from "@/components/state-glyph"
import { TierBadge } from "@/components/tier-badge"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import { useConsoleParams } from "@/hooks/use-console-params"
import { useNow } from "@/hooks/use-now"
import { useLeadList } from "@/hooks/use-queries"
import { LEAD_PAGE_SIZE } from "@/lib/api"
import {
  PRESET_GROUPS,
  STATUS_FILTERS,
  TIER_FILTERS,
  formatElapsed,
  formatRelative,
  formatTimestampPair,
  hostnameOf,
  leadAgeMs,
  leadState,
  tierFromSlug,
} from "@/lib/format"
import { isNativelyActivatable, isShortcutBlocked } from "@/lib/keyboard"
import type { LeadResponse } from "@/lib/types"
import { cn } from "@/lib/utils"

interface SegmentedFilterProps<T extends string> {
  label: string
  options: readonly { value: T | null; label: string }[]
  value: T | null
  onChange: (value: T | null) => void
}

function SegmentedFilter<T extends string>({
  label,
  options,
  value,
  onChange,
}: SegmentedFilterProps<T>) {
  return (
    <div role="group" aria-label={label} className="flex items-center gap-1">
      <span className="mr-1 text-[11px] uppercase tracking-wide text-zinc-500">{label}</span>
      {options.map((option) => {
        const active = option.value === value
        return (
          <button
            key={option.label}
            type="button"
            aria-pressed={active}
            onClick={() => onChange(option.value)}
            className={cn(
              "h-6 rounded px-2 text-xs outline-none transition-colors focus-visible:ring-2 focus-visible:ring-indigo-400",
              active
                ? "bg-zinc-800 text-zinc-100"
                : "text-zinc-400 hover:bg-zinc-900/60 hover:text-zinc-200",
            )}
          >
            {option.label}
          </button>
        )
      })}
    </div>
  )
}

function UpdatedCell({ lead, nowMs }: { lead: LeadResponse; nowMs: number }) {
  const state = leadState(lead, nowMs)
  const age = leadAgeMs(lead, nowMs)
  const inFlight = state === "PENDING" || state === "STALLED"
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          className={cn(
            "font-mono tabular-nums",
            state === "STALLED" ? "text-amber-300" : "text-zinc-400",
          )}
        >
          {inFlight ? formatElapsed(age) : formatRelative(age)}
        </span>
      </TooltipTrigger>
      <TooltipContent className="font-mono text-xs">
        {formatTimestampPair(lead.updated_at)}
      </TooltipContent>
    </Tooltip>
  )
}

interface LeadRowProps {
  lead: LeadResponse
  nowMs: number
  isCursor: boolean
  isOpen: boolean
  onSelect: (id: number) => void
  onCursor: (id: number) => void
}

function LeadRow({ lead, nowMs, isCursor, isOpen, onSelect, onCursor }: LeadRowProps) {
  const state = leadState(lead, nowMs)
  const secondLine =
    lead.status === "FAILED" && lead.error_message ? (
      <span title={lead.error_message} className="truncate text-red-300/80">
        {lead.error_message}
      </span>
    ) : (
      <span className="truncate text-zinc-500">{lead.url}</span>
    )

  return (
    <TableRow
      data-lead-id={lead.id}
      tabIndex={0}
      aria-current={isOpen ? "true" : undefined}
      onClick={() => {
        onCursor(lead.id)
        onSelect(lead.id)
      }}
      onFocus={() => onCursor(lead.id)}
      className={cn(
        "h-9 cursor-pointer border-zinc-800 text-[13px] tabular-nums outline-none hover:bg-zinc-900/60",
        (isCursor || isOpen) && "bg-zinc-900/60",
      )}
    >
      <TableCell className="relative w-8 py-0 pl-3 pr-1">
        {isCursor ? (
          <span aria-hidden className="absolute inset-y-0 left-0 w-0.5 bg-indigo-400" />
        ) : null}
        <StateGlyph state={state} />
      </TableCell>
      <TableCell className="w-14 py-0 pr-2 font-mono text-zinc-400">#{lead.id}</TableCell>
      <TableCell className="w-full max-w-0 py-0">
        <div className="flex min-w-0 flex-col leading-tight">
          {/* LLM-derived strings come from scraped pages: plain text only. */}
          <span className="truncate text-zinc-100">
            {lead.company_name ?? hostnameOf(lead.url)}
          </span>
          <span className="flex min-w-0 font-mono text-[11px] leading-tight">{secondLine}</span>
        </div>
      </TableCell>
      <TableCell className="w-28 py-0">
        <ScoreBar score={lead.fit_score} />
      </TableCell>
      <TableCell className="w-14 py-0">
        <TierBadge tier={lead.fit_tier} />
      </TableCell>
      <TableCell className="w-24 py-0 pr-4 text-right">
        <UpdatedCell lead={lead} nowMs={nowMs} />
      </TableCell>
    </TableRow>
  )
}

function EmptyState({
  filtered,
  onClear,
  onPickPreset,
}: {
  filtered: boolean
  onClear: () => void
  onPickPreset: (url: string) => void
}) {
  if (filtered) {
    return (
      <div className="flex flex-col items-center gap-2 py-16 text-center">
        <p className="text-sm text-zinc-300">No leads match</p>
        <Button variant="outline" size="sm" onClick={onClear} className="border-zinc-800">
          Clear filters
        </Button>
      </div>
    )
  }
  return (
    <div className="flex flex-col items-center gap-4 py-16 text-center">
      <div>
        <p className="text-sm text-zinc-300">No leads yet</p>
        <p className="mt-1 text-xs text-zinc-500">Submit a company URL above, or try a preset.</p>
      </div>
      {PRESET_GROUPS.map((group) => (
        <div key={group.label} className="flex flex-col items-center gap-1.5">
          <span className="text-[11px] text-zinc-500">{group.label}</span>
          <div className="flex flex-wrap justify-center gap-1.5">
            {group.urls.map((url) => (
              <button
                key={url}
                type="button"
                onClick={() => onPickPreset(url)}
                className="rounded px-2 py-1 font-mono text-xs text-zinc-300 ring-1 ring-inset ring-zinc-800 outline-none hover:bg-zinc-900/60 focus-visible:ring-2 focus-visible:ring-indigo-400"
              >
                {url}
              </button>
            ))}
          </div>
        </div>
      ))}
    </div>
  )
}

function SkeletonRows() {
  return (
    <>
      {Array.from({ length: 8 }, (_, index) => (
        <TableRow key={index} className="h-9 border-zinc-800 hover:bg-transparent">
          <TableCell colSpan={6} className="py-0 px-3">
            <Skeleton className="h-4 w-full bg-zinc-900" />
          </TableCell>
        </TableRow>
      ))}
    </>
  )
}

export function LeadTable({ onPickPreset }: { onPickPreset: (url: string) => void }) {
  const { params, update, hasFilters } = useConsoleParams()
  const nowMs = useNow()
  const tier = params.tier ? tierFromSlug(params.tier) : null
  const { data, isPending, isError, isFetching, isPlaceholderData, refetch } = useLeadList({
    page: params.page,
    status: params.status,
    tier,
  })
  const items = useMemo(() => data?.items ?? [], [data])
  const [cursorId, setCursorId] = useState<number | null>(null)
  const bodyRef = useRef<HTMLTableSectionElement>(null)

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (isShortcutBlocked(event)) return
      const rows = items
      if (rows.length === 0) return

      if (event.key === "j" || event.key === "k") {
        event.preventDefault()
        const index = rows.findIndex((lead) => lead.id === cursorId)
        const next =
          event.key === "j"
            ? Math.min(rows.length - 1, index + 1)
            : Math.max(0, index === -1 ? 0 : index - 1)
        const id = rows[next].id
        setCursorId(id)
        bodyRef.current
          ?.querySelector(`[data-lead-id="${id}"]`)
          ?.scrollIntoView({ block: "nearest" })
      } else if (event.key === "Enter" && !isNativelyActivatable(event)) {
        if (cursorId !== null) {
          event.preventDefault()
          update({ lead: cursorId })
        }
      }
    }
    window.addEventListener("keydown", onKeyDown)
    return () => window.removeEventListener("keydown", onKeyDown)
  }, [items, cursorId, update])

  // A shrinking result set (e.g. the last row of page 3 finishing) can leave
  // the URL pointing past the end; fall back to page 1 instead of "empty".
  const total = data?.total ?? 0
  useEffect(() => {
    if (!isPlaceholderData && data && items.length === 0 && total > 0 && params.page > 1) {
      update({ page: 1 })
    }
  }, [data, items.length, total, params.page, isPlaceholderData, update])

  const from = items.length === 0 ? 0 : (params.page - 1) * LEAD_PAGE_SIZE + 1
  const to = from === 0 ? 0 : from + items.length - 1

  return (
    <section className="flex flex-1 flex-col">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2 border-b border-zinc-800 px-4 py-2">
        <SegmentedFilter
          label="Tier"
          options={[
            { value: null, label: "All" },
            ...TIER_FILTERS.map((entry) => ({ value: entry.slug, label: entry.label })),
          ]}
          value={params.tier}
          onChange={(value) => update({ tier: value })}
        />
        <SegmentedFilter
          label="Status"
          options={[
            { value: null, label: "All" },
            ...STATUS_FILTERS.map((status) => ({ value: status, label: status })),
          ]}
          value={params.status}
          onChange={(value) => update({ status: value })}
        />
        {isFetching && !isPending ? (
          <Loader2 aria-label="Refreshing" className="ml-auto size-3.5 animate-spin text-zinc-500" />
        ) : null}
      </div>

      {isError && !data ? (
        <div className="flex flex-col items-center gap-3 py-16 text-center">
          <WifiOff aria-hidden className="size-5 text-red-300" />
          <p className="text-sm text-zinc-300">Couldn&apos;t load leads. API unreachable.</p>
          <Button variant="outline" size="sm" onClick={() => void refetch()} className="border-zinc-800">
            Retry
          </Button>
        </div>
      ) : (
        <Table>
          <TableHeader>
            <TableRow className="h-8 border-zinc-800 hover:bg-transparent">
              {["", "ID", "Company", "Score", "Tier", "Updated"].map((heading, index) => (
                <TableHead
                  key={heading || "state"}
                  className={cn(
                    "h-8 text-[11px] font-medium uppercase tracking-wide text-zinc-500",
                    index === 0 && "pl-3",
                    index === 5 && "pr-4 text-right",
                  )}
                >
                  {heading}
                </TableHead>
              ))}
            </TableRow>
          </TableHeader>
          <TableBody ref={bodyRef}>
            {isPending ? (
              <SkeletonRows />
            ) : (
              items.map((lead) => (
                <LeadRow
                  key={lead.id}
                  lead={lead}
                  nowMs={nowMs}
                  isCursor={cursorId === lead.id}
                  isOpen={params.lead === lead.id}
                  onCursor={setCursorId}
                  onSelect={(id) => update({ lead: id })}
                />
              ))
            )}
          </TableBody>
        </Table>
      )}

      {data && items.length === 0 && total === 0 ? (
        <EmptyState
          filtered={hasFilters}
          onClear={() => update({ tier: null, status: null })}
          onPickPreset={onPickPreset}
        />
      ) : null}

      <footer className="mt-auto flex items-center justify-between border-t border-zinc-800 px-4 py-2 text-xs text-zinc-400">
        <span className="font-mono tabular-nums">
          {from}–{to} of {total}
        </span>
        <div className="flex items-center gap-1">
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="Previous page"
            disabled={params.page <= 1}
            onClick={() => update({ page: params.page - 1 })}
          >
            <ChevronLeft />
          </Button>
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="Next page"
            disabled={to >= total}
            onClick={() => update({ page: params.page + 1 })}
          >
            <ChevronRight />
          </Button>
        </div>
      </footer>
    </section>
  )
}
