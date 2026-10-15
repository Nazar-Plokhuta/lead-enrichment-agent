import { TIER_BADGES, asFitTier } from "@/lib/format"
import { cn } from "@/lib/utils"

/** Ordinal single-hue scale: tier is rank, so only fill weight varies. */
export function TierBadge({ tier }: { tier: string | null }) {
  const known = asFitTier(tier)
  if (!known) return <span className="font-mono text-zinc-600">—</span>
  const { short, className } = TIER_BADGES[known]
  return (
    <span
      title={known}
      className={cn(
        "inline-flex h-5 min-w-7 items-center justify-center rounded px-1.5 font-mono text-[11px] font-medium",
        className,
      )}
    >
      {short}
    </span>
  )
}
