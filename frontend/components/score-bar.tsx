import { cn } from "@/lib/utils"

const TICKS = [25, 50, 75] as const

/** 40px bar with ticks at the tier boundaries (25 / 50 / 75). */
export function ScoreBar({ score }: { score: number | null }) {
  const value = score === null ? 0 : Math.min(100, Math.max(0, score))
  return (
    <div className="flex items-center gap-2">
      <span
        className={cn(
          "w-6 text-right font-mono tabular-nums",
          score === null ? "text-zinc-600" : "text-zinc-200",
        )}
      >
        {score ?? "—"}
      </span>
      <div
        aria-hidden
        className="relative h-1 w-10 shrink-0 overflow-hidden rounded-full bg-zinc-800"
      >
        <div className="h-full bg-indigo-400/70" style={{ width: `${value}%` }} />
        {TICKS.map((tick) => (
          <span
            key={tick}
            className="absolute inset-y-0 w-px bg-zinc-950"
            style={{ left: `${tick}%` }}
          />
        ))}
      </div>
    </div>
  )
}
