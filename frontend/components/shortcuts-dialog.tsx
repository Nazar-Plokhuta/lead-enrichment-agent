"use client"

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"

const SHORTCUTS: readonly { keys: readonly string[]; action: string }[] = [
  { keys: ["/"], action: "Focus the URL input" },
  { keys: ["j"], action: "Move selection down" },
  { keys: ["k"], action: "Move selection up" },
  { keys: ["Enter"], action: "Open the selected lead" },
  { keys: ["Esc"], action: "Blur input / close inspector" },
  { keys: ["?"], action: "Show this help" },
]

export function ShortcutsDialog({
  open,
  onOpenChange,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-sm border-zinc-800 bg-zinc-900">
        <DialogHeader>
          <DialogTitle>Keyboard shortcuts</DialogTitle>
          <DialogDescription>Single-key shortcuts are inactive while typing.</DialogDescription>
        </DialogHeader>
        <dl className="flex flex-col gap-2 text-sm">
          {SHORTCUTS.map(({ keys, action }) => (
            <div key={action} className="flex items-center justify-between gap-4">
              <dt className="text-zinc-300">{action}</dt>
              <dd className="flex gap-1">
                {keys.map((key) => (
                  <kbd
                    key={key}
                    className="min-w-6 rounded border border-zinc-700 bg-zinc-950 px-1.5 py-0.5 text-center font-mono text-xs text-zinc-300"
                  >
                    {key}
                  </kbd>
                ))}
              </dd>
            </div>
          ))}
        </dl>
      </DialogContent>
    </Dialog>
  )
}
