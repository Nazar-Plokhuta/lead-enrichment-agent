"use client"

import { useState, type ReactNode } from "react"

import { CopyButton } from "@/components/copy-button"
import { RubricBar, RubricGrid } from "@/components/rubric-bar"
import { evaluateIntegrity, stripScoreFormula } from "@/lib/inspector"
import { parseScoreSummary } from "@/lib/parser"
import type { EnrichedLeadPayload } from "@/lib/types"
import { cn } from "@/lib/utils"

// Every string rendered here (rationale, icebreaker, pain points, ...) is
// derived from scraped web content. It is always a React text child with
// `whitespace-pre-wrap`; never inject it as HTML.
const TEXT = "whitespace-pre-wrap break-words"

function Section({
  title,
  action,
  children,
}: {
  title: string
  action?: ReactNode
  children: ReactNode
}) {
  return (
    <section className="flex flex-col gap-2">
      <div className="flex h-6 items-center justify-between">
        <h3 className="text-[11px] font-medium uppercase tracking-wide text-zinc-500">{title}</h3>
        {action}
      </div>
      {children}
    </section>
  )
}

function Card({ className, ...props }: React.ComponentProps<"div">) {
  return (
    <div
      className={cn("rounded-md border border-zinc-800 bg-zinc-900/40 p-3", className)}
      {...props}
    />
  )
}

function IntegrityLine({ ok, reasons }: { ok: boolean; reasons: string[] }) {
  const [expanded, setExpanded] = useState(false)
  if (ok) {
    // Plain green, not emerald: emerald is reserved for the system health dot.
    return <p className="text-xs text-green-400/80">✓ Rubric consistent</p>
  }
  return (
    <div className="text-xs">
      <button
        type="button"
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
        className="rounded text-amber-300 outline-none hover:underline focus-visible:ring-2 focus-visible:ring-indigo-400"
      >
        ⚠ Integrity mismatch
      </button>
      {expanded ? (
        <ul className="mt-1 list-disc pl-5 text-zinc-400">
          {reasons.map((reason) => (
            <li key={reason} className="font-mono">
              {reason}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}

export function VerdictSection({ payload }: { payload: EnrichedLeadPayload }) {
  const { fit_score, fit_tier, scoring_rationale } = payload.scoring
  const breakdown = parseScoreSummary(scoring_rationale, fit_score)
  const integrity = evaluateIntegrity(breakdown, fit_score, fit_tier)
  const rationale = stripScoreFormula(scoring_rationale)

  return (
    <Section title="Verdict">
      <Card className="flex flex-col gap-3">
        {rationale ? <p className={cn("text-[13px] leading-relaxed text-zinc-200", TEXT)}>{rationale}</p> : null}
        <div className="flex flex-col gap-2">
          <RubricBar breakdown={breakdown} fitScore={fit_score} />
          <RubricGrid breakdown={breakdown} />
        </div>
        <IntegrityLine ok={integrity.ok} reasons={integrity.reasons} />
      </Card>
    </Section>
  )
}

function OutreachCard({ title, text }: { title: string; text: string }) {
  return (
    <Card className="flex flex-col gap-1.5">
      <div className="flex h-5 items-center justify-between">
        <h4 className="text-xs font-medium text-zinc-300">{title}</h4>
        <CopyButton text={text} label={`Copy ${title.toLowerCase()}`} />
      </div>
      <p className={cn("text-[13px] leading-relaxed text-zinc-200", TEXT)}>{text}</p>
    </Card>
  )
}

export function OutreachSection({ payload }: { payload: EnrichedLeadPayload }) {
  const { icebreaker, suggested_angle } = payload.outreach
  const both = `Icebreaker:\n${icebreaker}\n\nSuggested angle:\n${suggested_angle}`
  return (
    <Section title="Outreach strategy" action={<CopyButton text={both} label="Copy both" showLabel />}>
      <OutreachCard title="Icebreaker" text={icebreaker} />
      <OutreachCard title="Suggested angle" text={suggested_angle} />
    </Section>
  )
}

export function GapsSection({ gaps }: { gaps: string[] }) {
  return (
    <Section title="Gaps">
      {gaps.length === 0 ? (
        <p className="text-xs text-zinc-500">No gaps reported (sufficient information)</p>
      ) : (
        <ul className="flex flex-wrap gap-1.5">
          {gaps.map((gap, index) => (
            <li
              key={`${index}-${gap}`}
              className={cn(
                "rounded border border-dashed border-zinc-700 px-2 py-0.5 text-xs text-zinc-300",
                TEXT,
              )}
            >
              {gap}
            </li>
          ))}
        </ul>
      )}
    </Section>
  )
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col gap-0.5">
      <dt className="text-[11px] text-zinc-500">{label}</dt>
      <dd className={cn("text-[13px] text-zinc-200", TEXT)}>{value || "—"}</dd>
    </div>
  )
}

export function AnalysisSection({ payload }: { payload: EnrichedLeadPayload }) {
  const { industry, target_audience, value_proposition, pain_points } = payload.analysis
  return (
    <Section title="Company analysis">
      <Card className="flex flex-col gap-3">
        <dl className="grid grid-cols-2 gap-3">
          <Field label="Industry" value={industry} />
          <Field label="Target audience" value={target_audience} />
        </dl>
        <dl>
          <Field label="Value proposition" value={value_proposition} />
        </dl>
        <div className="flex flex-col gap-1">
          <h4 className="text-[11px] text-zinc-500">Pain points</h4>
          {pain_points.length === 0 ? (
            <p className="text-xs text-zinc-500">None reported</p>
          ) : (
            <ul className="list-disc pl-5 text-[13px] text-zinc-200 marker:text-zinc-600">
              {pain_points.map((point, index) => (
                <li key={`${index}-${point}`} className={TEXT}>
                  {point}
                </li>
              ))}
            </ul>
          )}
        </div>
      </Card>
    </Section>
  )
}

export function JsonView({ data }: { data: Record<string, unknown> | null }) {
  if (data === null) {
    return <p className="text-xs text-zinc-500">No payload stored for this lead.</p>
  }
  // Serializing for display only; nothing here parses LLM output.
  const text = JSON.stringify(data, null, 2)
  return (
    <div className="relative">
      <div className="absolute right-1 top-1">
        <CopyButton text={text} label="Copy JSON" />
      </div>
      <pre
        className={cn(
          "overflow-x-auto rounded-md border border-zinc-800 bg-zinc-900/40 p-3 pr-9 font-mono text-[11px] leading-relaxed text-zinc-300",
          TEXT,
        )}
      >
        {text}
      </pre>
    </div>
  )
}
