import { rubricSegments } from "@/lib/inspector"
import type { ScoreBreakdown } from "@/lib/types"

const TICKS = [25, 50, 75] as const

function Ticks() {
  return (
    <>
      {TICKS.map((tick) => (
        <span
          key={tick}
          aria-hidden
          className="absolute inset-y-0 w-px bg-zinc-950"
          style={{ left: `${tick}%` }}
        />
      ))}
    </>
  )
}

interface RubricBarProps {
  breakdown: ScoreBreakdown
  fitScore: number
}

/**
 * One 100-unit bar split by rubric weight (A35 / B25 / C20 / D20). Each
 * segment fills to the points awarded; tick marks at 25/50/75 are the tier
 * boundaries. Without a parsed formula it degrades to a single `fit_score` bar.
 */
export function RubricBar({ breakdown, fitScore }: RubricBarProps) {
  const { scores } = breakdown
  if (!scores) {
    const value = Math.min(100, Math.max(0, fitScore))
    return (
      <div
        role="img"
        aria-label={`Fit score ${fitScore} of 100`}
        className="relative h-2 overflow-hidden rounded-full bg-zinc-800"
      >
        <div className="h-full bg-indigo-400/70" style={{ width: `${value}%` }} />
        <Ticks />
      </div>
    )
  }

  const segments = rubricSegments(scores)
  const summary = segments.map((s) => `${s.label} ${s.awarded} of ${s.cap}`).join(", ")
  return (
    <div
      role="img"
      aria-label={`Score ${scores.total} of 100: ${summary}`}
      className="relative flex h-2 gap-px overflow-hidden rounded-full"
    >
      {segments.map((segment) => (
        <div
          key={segment.key}
          className="h-full overflow-hidden bg-zinc-800"
          style={{ width: `${segment.cap}%` }}
        >
          <div
            className="h-full bg-indigo-400/70"
            style={{ width: `${(segment.awarded / segment.cap) * 100}%` }}
          />
        </div>
      ))}
      <Ticks />
    </div>
  )
}

/** `A: Model (30/35)` grid shown under the bar; mono + tabular for alignment. */
export function RubricGrid({ breakdown }: { breakdown: ScoreBreakdown }) {
  if (!breakdown.scores) return null
  return (
    <dl className="grid grid-cols-4 gap-2 font-mono text-[11px] tabular-nums">
      {rubricSegments(breakdown.scores).map((segment) => (
        <div key={segment.key} className="flex flex-col">
          <dt className="text-zinc-500">
            {segment.label}: {segment.name}
          </dt>
          <dd className="text-zinc-200">
            {breakdown.scores?.[segment.key]}/{segment.cap}
          </dd>
        </div>
      ))}
    </dl>
  )
}
