"use client"

import { useEffect, useRef, useState } from "react"

import { ActionBar } from "@/components/action-bar"
import { Header } from "@/components/header"
import { LeadTable } from "@/components/lead-table"
import { ShortcutsDialog } from "@/components/shortcuts-dialog"
import { useConsoleParams } from "@/hooks/use-console-params"
import { isShortcutBlocked } from "@/lib/keyboard"

/**
 * Page shell. Owns the URL draft so presets picked from the empty state can
 * prefill the action bar, and the global `?` / `Esc` shortcuts.
 */
export function Console() {
  const [draft, setDraft] = useState("")
  const [shortcutsOpen, setShortcutsOpen] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const { params, update } = useConsoleParams()

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (isShortcutBlocked(event)) return
      if (event.key === "?") {
        event.preventDefault()
        setShortcutsOpen(true)
      } else if (event.key === "Escape" && params.lead !== null) {
        update({ lead: null })
      }
    }
    window.addEventListener("keydown", onKeyDown)
    return () => window.removeEventListener("keydown", onKeyDown)
  }, [params.lead, update])

  return (
    <div className="flex min-h-screen flex-col">
      <Header onShowShortcuts={() => setShortcutsOpen(true)} />
      <ActionBar value={draft} onValueChange={setDraft} inputRef={inputRef} />
      <main className="flex flex-1 flex-col">
        <LeadTable
          onPickPreset={(url) => {
            setDraft(url)
            inputRef.current?.focus()
          }}
        />
      </main>
      <ShortcutsDialog open={shortcutsOpen} onOpenChange={setShortcutsOpen} />
    </div>
  )
}
