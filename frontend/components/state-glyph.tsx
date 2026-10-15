import { AlertCircle, Check, Hourglass, Loader2 } from "lucide-react"

import type { LeadState } from "@/lib/format"
import { cn } from "@/lib/utils"

const GLYPHS: Record<LeadState, { label: string; className: string }> = {
  PROCESSED: { label: "Processed", className: "text-zinc-400" },
  PENDING: { label: "Pending", className: "text-sky-300" },
  FAILED: { label: "Failed", className: "text-red-300" },
  STALLED: { label: "Stalled: pending for over 5 minutes", className: "text-amber-300" },
}

export function StateGlyph({ state, className }: { state: LeadState; className?: string }) {
  const { label, className: color } = GLYPHS[state]
  const props = {
    role: "img",
    "aria-label": label,
    className: cn("size-3.5 shrink-0", color, className),
  } as const

  switch (state) {
    case "PROCESSED":
      return <Check {...props} />
    case "PENDING":
      return <Loader2 {...props} className={cn(props.className, "animate-spin")} />
    case "FAILED":
      return <AlertCircle {...props} />
    case "STALLED":
      return <Hourglass {...props} />
  }
}
