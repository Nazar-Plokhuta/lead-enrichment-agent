"use client"

import {
  AlertCircle,
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  Hourglass,
  Loader2,
  RotateCw,
  X,
} from "lucide-react"
import { useEffect, useState, type ReactNode } from "react"
import { toast } from "sonner"

import {
  AnalysisSection,
  GapsSection,
  JsonView,
  OutreachSection,
  VerdictSection,
} from "@/components/inspector-sections"
import { StateGlyph } from "@/components/state-glyph"
import { TierBadge } from "@/components/tier-badge"
import { Button } from "@/components/ui/button"
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover"
import { ScrollArea } from "@/components/ui/scroll-area"
import { Separator } from "@/components/ui/separator"
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { useNow } from "@/hooks/use-now"
import { useEnrichMutation, useLeadInspector } from "@/hooks/use-queries"
import { ApiError } from "@/lib/api"
import { asFitTier, hostnameOf, leadAgeMs, leadState, type LeadState } from "@/lib/format"
import { safeHttpUrl } from "@/lib/inspector"
import { isTypingTarget } from "@/lib/keyboard"
import { parseEnrichedPayload } from "@/lib/schemas"
import type { EnrichedLeadPayload, LeadResponse } from "@/lib/types"
import { cn, getErrorHint } from "@/lib/utils"

// LLM-derived strings come from scraped pages: always plain text.
const TEXT = "whitespace-pre-wrap break-words"

function useRerun(lead: LeadResponse) {
  const mutation = useEnrichMutation()
  function rerun() {
    mutation.mutate(
      { url: lead.url, force: true },
      {
        onSuccess: ({ response }) => toast.success(`Re-run queued #${response.lead_id}`),
        onError: (error) => {
          console.error("rerun_failed", { leadId: lead.id, error })
          toast.error("Re-run failed", {
            description:
              error instanceof ApiError || error instanceof Error ? error.message : undefined,
          })
        },
      },
    )
  }
  return { rerun, isPending: mutation.isPending }
}

function RerunButton({ lead, state, primary }: { lead: LeadResponse; state: LeadState; primary: boolean }) {
  const { rerun, isPending } = useRerun(lead)
  const [confirmOpen, setConfirmOpen] = useState(false)
  // A live (non-stalled) PENDING lead is already being worked on.
  const running = state === "PENDING"

  const button = (
    <Button
      type="button"
      size="sm"
      variant={primary ? "default" : "outline"}
      disabled={running || isPending}
      onClick={state === "PROCESSED" ? undefined : rerun}
      className={cn(
        primary
          ? "bg-indigo-500 text-white hover:bg-indigo-400"
          : "border-zinc-800 bg-transparent text-zinc-300",
        "focus-visible:ring-2 focus-visible:ring-indigo-400",
      )}
    >
      {isPending || running ? <Loader2 className="animate-spin" /> : <RotateCw />}
      {running ? "Running…" : "Re-run"}
    </Button>
  )

  if (state !== "PROCESSED") return button
  return (
    <Popover open={confirmOpen} onOpenChange={setConfirmOpen}>
      <PopoverTrigger asChild>{button}</PopoverTrigger>
      <PopoverContent align="end" className="w-64 border-zinc-800 bg-zinc-900 text-zinc-100">
        <p className="text-xs text-zinc-300">Replaces current result · uses LLM credits</p>
        <div className="flex justify-end gap-1.5">
          <Button size="xs" variant="ghost" onClick={() => setConfirmOpen(false)}>
            Cancel
          </Button>
          <Button
            size="xs"
            autoFocus
            onClick={() => {
              setConfirmOpen(false)
              rerun()
            }}
            className="bg-indigo-500 text-white hover:bg-indigo-400"
          >
            Re-run
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  )
}

function Hairline() {
  return (
    <div aria-hidden className="relative h-px overflow-hidden bg-sky-400/20">
      <span className="absolute inset-y-0 left-0 w-2/5 bg-sky-400/70 motion-safe:animate-[hairline_1.4s_linear_infinite]" />
    </div>
  )
}

function Banner({
  tone,
  icon,
  children,
  role,
}: {
  tone: "sky" | "amber" | "red"
  icon: ReactNode
  children: ReactNode
  role?: "alert" | "status"
}) {
  const classes = {
    sky: "bg-sky-500/10 text-sky-300 ring-sky-400/30",
    amber: "bg-amber-500/10 text-amber-300 ring-amber-400/30",
    red: "bg-red-500/10 text-red-300 ring-red-400/30",
  }[tone]
  return (
    <div role={role} className={cn("flex items-start gap-2 rounded-md px-3 py-2 text-xs ring-1 ring-inset", classes)}>
      <span className="mt-px shrink-0">{icon}</span>
      <div className="flex min-w-0 flex-col gap-1">{children}</div>
    </div>
  )
}

function ErrorBox({ message }: { message: string }) {
  const hint = getErrorHint(message)
  return (
    <Banner tone="red" role="alert" icon={<AlertCircle aria-hidden className="size-3.5" />}>
      <p className={cn("font-medium", TEXT)}>{message}</p>
      {hint ? <p className="text-red-300/80">{hint}</p> : null}
    </Banner>
  )
}

interface HeaderProps {
  lead: LeadResponse
  payload: EnrichedLeadPayload | null
  state: LeadState
  position: { index: number; total: number } | null
  onPrev: (() => void) | null
  onNext: (() => void) | null
  onClose: () => void
}

function InspectorHeader({ lead, payload, state, position, onPrev, onNext, onClose }: HeaderProps) {
  const name = lead.company_name ?? payload?.company_name ?? hostnameOf(lead.url)
  const href = safeHttpUrl(lead.url)
  const score = lead.fit_score ?? payload?.scoring.fit_score ?? null
  const tier = lead.fit_tier ?? payload?.scoring.fit_tier ?? null
  const known = asFitTier(tier)

  return (
    <header className="flex flex-col gap-2 border-b border-zinc-800 px-4 py-3">
      <div className="flex items-start gap-2">
        <StateGlyph state={state} className="mt-1" />
        <h2 className={cn("min-w-0 flex-1 text-base font-medium leading-snug text-zinc-100", TEXT)}>
          {name}
        </h2>
        <div className="flex shrink-0 items-center gap-0.5">
          {position ? (
            <span className="mr-1 font-mono text-[11px] tabular-nums text-zinc-500">
              {position.index + 1}/{position.total}
            </span>
          ) : null}
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="Previous lead"
            title="Previous lead ([)"
            disabled={onPrev === null}
            onClick={onPrev ?? undefined}
          >
            <ChevronLeft />
          </Button>
          <Button
            variant="ghost"
            size="icon-sm"
            aria-label="Next lead"
            title="Next lead (])"
            disabled={onNext === null}
            onClick={onNext ?? undefined}
          >
            <ChevronRight />
          </Button>
          <Button variant="ghost" size="icon-sm" aria-label="Close inspector" title="Close (Esc)" onClick={onClose}>
            <X />
          </Button>
        </div>
      </div>
      <div className="flex min-w-0 items-center gap-2 font-mono text-xs text-zinc-400">
        <span className="tabular-nums">#{lead.id}</span>
        <span aria-hidden>·</span>
        {href ? (
          <a
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            className="flex min-w-0 items-center gap-1 rounded text-indigo-300 outline-none hover:underline focus-visible:ring-2 focus-visible:ring-indigo-400"
          >
            <span className="truncate">{hostnameOf(lead.url)}</span>
            <ExternalLink aria-hidden className="size-3 shrink-0" />
          </a>
        ) : (
          <span className="truncate" title={lead.url}>
            {hostnameOf(lead.url)}
          </span>
        )}
      </div>
      <div className="flex items-center gap-2">
        <TierBadge tier={tier} />
        {known ? <span className="text-xs text-zinc-400">{known}</span> : null}
        <span className="ml-1 font-mono text-sm tabular-nums text-zinc-100">
          {score ?? "—"}
          <span className="text-zinc-500">/100</span>
        </span>
        <div className="ml-auto">
          <RerunButton lead={lead} state={state} primary={state === "FAILED" && payload === null} />
        </div>
      </div>
    </header>
  )
}

function EmptyResult({ children }: { children: ReactNode }) {
  return <p className="py-10 text-center text-xs text-zinc-500">{children}</p>
}

function Overview({ payload, raw }: { payload: EnrichedLeadPayload | null; raw: unknown }) {
  if (payload) {
    return (
      <div className="flex flex-col gap-5">
        <VerdictSection payload={payload} />
        <OutreachSection payload={payload} />
        <GapsSection gaps={payload.scoring.missing_information} />
        <AnalysisSection payload={payload} />
      </div>
    )
  }
  if (raw !== null) {
    return (
      <EmptyResult>
        Stored result doesn&apos;t match the expected schema. See the JSON tab.
      </EmptyResult>
    )
  }
  return <EmptyResult>No result yet</EmptyResult>
}

function InspectorBody({ lead, payload, state }: { lead: LeadResponse; payload: EnrichedLeadPayload | null; state: LeadState }) {
  const nowMs = useNow()
  const hasResult = lead.enriched_data !== null
  const failedNoPayload = state === "FAILED" && !hasResult
  const reRunning = state === "PENDING" && hasResult
  const dimmed = reRunning ? "opacity-50" : state === "FAILED" && hasResult ? "opacity-60" : state === "STALLED" && hasResult ? "opacity-60" : ""

  const banners: ReactNode[] = []
  if (state === "PENDING") {
    banners.push(
      <Banner key="pending" tone="sky" role="status" icon={<Loader2 aria-hidden className="size-3.5 animate-spin" />}>
        <p>{hasResult ? "Re-run in progress — showing previous result" : "Enrichment in progress"}</p>
        <Hairline />
      </Banner>,
    )
  } else if (state === "STALLED") {
    const minutes = Math.floor(leadAgeMs(lead, nowMs) / 60_000)
    banners.push(
      <Banner key="stalled" tone="amber" role="status" icon={<Hourglass aria-hidden className="size-3.5" />}>
        <p>
          No update for <span className="font-mono tabular-nums">{minutes}</span> min. Possibly
          stranded by a restart, or queued. Re-run is safe.
        </p>
      </Banner>,
    )
  } else if (state === "FAILED") {
    banners.push(<ErrorBox key="failed" message={lead.error_message ?? "Enrichment failed."} />)
  }

  return (
    <Tabs defaultValue="overview" className="min-h-0 flex-1 gap-0">
      <div className="flex flex-col gap-3 px-4 pt-3">
        {banners}
        <TabsList variant="line" className="h-8 w-full justify-start gap-3 border-b border-zinc-800 p-0">
          {(["overview", "json"] as const).map((value) => (
            <TabsTrigger
              key={value}
              value={value}
              className="h-8 flex-none px-1 text-xs capitalize text-zinc-400 after:bottom-0 after:bg-indigo-400 data-active:text-zinc-100"
            >
              {value === "json" ? "JSON" : "Overview"}
            </TabsTrigger>
          ))}
        </TabsList>
      </div>
      <ScrollArea className="min-h-0 flex-1">
        <div aria-busy={reRunning || undefined} className={cn("px-4 py-4", dimmed)}>
          <TabsContent value="overview">
            {failedNoPayload ? (
              <EmptyResult>No result was produced. Re-run to try again.</EmptyResult>
            ) : (
              <Overview payload={payload} raw={lead.enriched_data} />
            )}
          </TabsContent>
          <TabsContent value="json">
            <JsonView data={lead.enriched_data} />
          </TabsContent>
        </div>
      </ScrollArea>
    </Tabs>
  )
}

function InspectorSkeleton({ onClose }: { onClose: () => void }) {
  return (
    <div className="flex flex-col gap-3 p-4" aria-busy="true">
      <div className="flex items-center justify-between">
        <Skeleton className="h-5 w-48 bg-zinc-900" />
        <Button variant="ghost" size="icon-sm" aria-label="Close inspector" onClick={onClose}>
          <X />
        </Button>
      </div>
      <Skeleton className="h-4 w-32 bg-zinc-900" />
      <Skeleton className="h-24 w-full bg-zinc-900" />
      <Skeleton className="h-32 w-full bg-zinc-900" />
    </div>
  )
}

function InspectorError({ error, onRetry, onClose }: { error: Error; onRetry: () => void; onClose: () => void }) {
  const notFound = error instanceof ApiError && error.status === 404
  return (
    <div className="flex flex-col gap-3 p-4">
      <div className="flex justify-end">
        <Button variant="ghost" size="icon-sm" aria-label="Close inspector" onClick={onClose}>
          <X />
        </Button>
      </div>
      <ErrorBox message={notFound ? "Lead not found." : error.message} />
      {notFound ? null : (
        <Button variant="outline" size="sm" onClick={onRetry} className="self-start border-zinc-800">
          Retry
        </Button>
      )}
    </div>
  )
}

interface LeadInspectorProps {
  leadId: number
  /** Ids of the visible table rows, in order; drives `[` / `]` stepping. */
  ids: readonly number[]
  onNavigate: (id: number) => void
  onClose: () => void
}

/**
 * Presentation core shared by the docked pane and the modal Sheet. It knows
 * nothing about its container; the pane decides how it is mounted.
 */
export function LeadInspector({ leadId, ids, onNavigate, onClose }: LeadInspectorProps) {
  const { data: lead, error, refetch } = useLeadInspector(leadId)
  const nowMs = useNow()

  const index = ids.indexOf(leadId)
  const prevId = index > 0 ? ids[index - 1] : null
  const nextId = index !== -1 && index < ids.length - 1 ? ids[index + 1] : null

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (isTypingTarget(event)) return
      if (event.key === "[" && prevId !== null) {
        event.preventDefault()
        onNavigate(prevId)
      } else if (event.key === "]" && nextId !== null) {
        event.preventDefault()
        onNavigate(nextId)
      }
    }
    window.addEventListener("keydown", onKeyDown)
    return () => window.removeEventListener("keydown", onKeyDown)
  }, [prevId, nextId, onNavigate])

  if (!lead) {
    return error ? (
      <InspectorError error={error} onRetry={() => void refetch()} onClose={onClose} />
    ) : (
      <InspectorSkeleton onClose={onClose} />
    )
  }

  const payload = parseEnrichedPayload(lead.enriched_data)
  const state = leadState(lead, nowMs)

  return (
    <div className="flex h-full min-h-0 flex-col">
      <InspectorHeader
        lead={lead}
        payload={payload}
        state={state}
        position={index === -1 ? null : { index, total: ids.length }}
        onPrev={prevId === null ? null : () => onNavigate(prevId)}
        onNext={nextId === null ? null : () => onNavigate(nextId)}
        onClose={onClose}
      />
      {/* Keyed so per-lead UI state (open tab, expanded integrity) resets on step. */}
      <InspectorBody key={lead.id} lead={lead} payload={payload} state={state} />
      <Separator className="bg-zinc-800" />
      <footer className="px-4 py-1.5 font-mono text-[10px] text-zinc-600">
        <kbd>[</kbd> <kbd>]</kbd> step · <kbd>Esc</kbd> close
      </footer>
    </div>
  )
}
