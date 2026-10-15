"use client"

import { AlertCircle, CornerDownLeft, FlaskConical, Loader2, WifiOff } from "lucide-react"
import { useEffect, useState, type RefObject } from "react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"
import { Checkbox } from "@/components/ui/checkbox"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Input } from "@/components/ui/input"
import { useConsoleParams } from "@/hooks/use-console-params"
import { useEnrichMutation, useLead } from "@/hooks/use-queries"
import { ApiError } from "@/lib/api"
import { PRESET_GROUPS, hostnameOf, prepareSubmitUrl } from "@/lib/format"
import { isShortcutBlocked } from "@/lib/keyboard"
import type { EnrichResponse } from "@/lib/types"
import { cn } from "@/lib/utils"

type Feedback =
  | { kind: "queued"; lead: EnrichResponse }
  | { kind: "processed"; lead: EnrichResponse }
  | { kind: "invalid"; message: string }
  | { kind: "unreachable" }
  | { kind: "error"; message: string }

export function PresetMenu({ onPick }: { onPick: (url: string) => void }) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" className="border-zinc-800 bg-transparent text-zinc-300">
          <FlaskConical />
          Try
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start" className="w-72 border-zinc-800 bg-zinc-900">
        {PRESET_GROUPS.map((group, index) => (
          <DropdownMenuGroup key={group.label}>
            {index > 0 ? <DropdownMenuSeparator /> : null}
            <DropdownMenuLabel className="text-[11px] text-zinc-500">
              {group.label}
            </DropdownMenuLabel>
            {group.urls.map((url) => (
              <DropdownMenuItem
                key={url}
                onSelect={() => onPick(url)}
                className="font-mono text-xs"
              >
                {url}
              </DropdownMenuItem>
            ))}
          </DropdownMenuGroup>
        ))}
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

function ProcessedFeedback({
  lead,
  onRerun,
  onOpen,
}: {
  lead: EnrichResponse
  onRerun: () => void
  onOpen: () => void
}) {
  // The 200 body carries no score, so fetch the stored lead for display.
  const { data } = useLead(lead.lead_id)
  return (
    <span className="text-zinc-300">
      Already processed · <span className="font-mono text-zinc-400">#{lead.lead_id}</span>
      {data?.fit_score != null ? (
        <>
          {" "}
          · Score <span className="font-mono tabular-nums">{data.fit_score}</span>
        </>
      ) : null}{" "}
      <button
        type="button"
        onClick={onOpen}
        className="rounded text-indigo-300 underline-offset-2 outline-none hover:underline focus-visible:ring-2 focus-visible:ring-indigo-400"
      >
        [Open]
      </button>{" "}
      <button
        type="button"
        onClick={onRerun}
        className="rounded text-amber-300 underline-offset-2 outline-none hover:underline focus-visible:ring-2 focus-visible:ring-indigo-400"
      >
        Re-run with force
      </button>
    </span>
  )
}

function FeedbackLine({
  feedback,
  onRerun,
  onOpen,
}: {
  feedback: Feedback
  onRerun: (url: string) => void
  onOpen: (id: number) => void
}) {
  switch (feedback.kind) {
    case "queued":
      return (
        <span className="text-zinc-300">
          Queued <span className="font-mono text-zinc-400">#{feedback.lead.lead_id}</span> ·{" "}
          <span className="font-mono">{hostnameOf(feedback.lead.url)}</span>
        </span>
      )
    case "processed":
      return (
        <ProcessedFeedback
          lead={feedback.lead}
          onRerun={() => onRerun(feedback.lead.url)}
          onOpen={() => onOpen(feedback.lead.lead_id)}
        />
      )
    case "invalid":
      return (
        <span className="flex items-center gap-1.5 text-red-300">
          <AlertCircle aria-hidden className="size-3.5 shrink-0" />
          {feedback.message}
        </span>
      )
    case "unreachable":
      return (
        <span className="flex items-center gap-1.5 rounded bg-red-500/10 px-2 py-0.5 text-red-300 ring-1 ring-inset ring-red-400/30">
          <WifiOff aria-hidden className="size-3.5 shrink-0" />
          API unreachable. Check that the backend is running.
        </span>
      )
    case "error":
      return <span className="text-red-300">{feedback.message}</span>
  }
}

interface ActionBarProps {
  value: string
  onValueChange: (value: string) => void
  inputRef: RefObject<HTMLInputElement | null>
}

export function ActionBar({ value, onValueChange, inputRef }: ActionBarProps) {
  const [force, setForce] = useState(false)
  const [feedback, setFeedback] = useState<Feedback | null>(null)
  const mutation = useEnrichMutation()
  const { update } = useConsoleParams()

  const { url: submitUrl, prefixed } = prepareSubmitUrl(value)

  // `/` focuses the input unless the user is already typing somewhere.
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== "/" || isShortcutBlocked(event)) return
      event.preventDefault()
      inputRef.current?.focus()
      inputRef.current?.select()
    }
    window.addEventListener("keydown", onKeyDown)
    return () => window.removeEventListener("keydown", onKeyDown)
  }, [inputRef])

  function submit(url: string, forced: boolean, onAccepted?: () => void) {
    setFeedback(null)
    mutation.mutate(
      { url, force: forced },
      {
        onSuccess: ({ outcome, response }) => {
          onAccepted?.()
          if (outcome === "queued") {
            setFeedback({ kind: "queued", lead: response })
            toast.success(`Queued #${response.lead_id}`, {
              description: hostnameOf(response.url),
            })
          } else {
            setFeedback({ kind: "processed", lead: response })
          }
        },
        onError: (error) => {
          if (error instanceof ApiError && error.status === 422) {
            setFeedback({ kind: "invalid", message: error.message })
          } else if (error instanceof ApiError && error.isUnreachable) {
            setFeedback({ kind: "unreachable" })
          } else {
            setFeedback({ kind: "error", message: error.message })
          }
        },
        // The checkbox is deliberately non-sticky: one force per submit, so a
        // forgotten tick can't silently burn LLM budget on later URLs.
        onSettled: () => setForce(false),
      },
    )
  }

  function onSubmit(event: React.FormEvent) {
    event.preventDefault()
    if (submitUrl === null || mutation.isPending) return
    submit(submitUrl, force, () => onValueChange(""))
  }

  return (
    <div className="sticky top-12 z-20 border-b border-zinc-800 bg-zinc-950/95 px-4 py-2 backdrop-blur">
      <form onSubmit={onSubmit} className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-64 max-w-xl flex-1">
          <Input
            ref={inputRef}
            value={value}
            onChange={(event) => {
              onValueChange(event.target.value)
              setFeedback(null)
            }}
            onKeyDown={(event) => {
              if (event.key === "Escape") event.currentTarget.blur()
            }}
            placeholder="company.com or https://company.com"
            aria-label="Company URL"
            autoFocus
            autoComplete="off"
            spellCheck={false}
            className="h-8 border-zinc-800 bg-zinc-900/40 pr-8 font-mono text-[13px] focus-visible:ring-2 focus-visible:ring-indigo-400"
          />
          <kbd className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 rounded border border-zinc-700 px-1 font-mono text-[10px] text-zinc-500">
            /
          </kbd>
        </div>
        <PresetMenu onPick={(url) => {
          onValueChange(url)
          setFeedback(null)
          inputRef.current?.focus()
        }} />
        <label
          className={cn(
            "flex cursor-pointer select-none items-center gap-1.5 rounded-md px-2 py-1 text-xs transition-colors",
            force ? "bg-amber-500/10 text-amber-300 ring-1 ring-inset ring-amber-400/30" : "text-zinc-400",
          )}
        >
          <Checkbox
            checked={force}
            onCheckedChange={(checked) => setForce(checked === true)}
            className="data-checked:border-amber-400 data-checked:bg-amber-400 data-checked:text-zinc-950 dark:data-checked:bg-amber-400"
          />
          force
        </label>
        <Button
          type="submit"
          disabled={submitUrl === null || mutation.isPending}
          className="bg-indigo-500 text-white hover:bg-indigo-400 focus-visible:ring-2 focus-visible:ring-indigo-400"
        >
          {mutation.isPending ? <Loader2 className="animate-spin" /> : null}
          Enrich
          {mutation.isPending ? null : <CornerDownLeft aria-hidden />}
        </Button>
      </form>
      <div aria-live="polite" className="mt-1.5 min-h-5 text-xs">
        {feedback ? (
          <FeedbackLine
            feedback={feedback}
            onRerun={(url) => submit(url, true)}
            onOpen={(id) => update({ lead: id })}
          />
        ) : prefixed && submitUrl ? (
          <span className="text-zinc-500">
            Will submit: <span className="font-mono">{submitUrl}</span>
          </span>
        ) : null}
      </div>
    </div>
  )
}
