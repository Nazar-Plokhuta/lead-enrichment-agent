"use client"

import { Check, Copy } from "lucide-react"
import { useEffect, useState } from "react"
import { toast } from "sonner"

import { Button } from "@/components/ui/button"

/** Clipboard writes can be refused (permissions, insecure origin); surface that. */
export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text)
    toast.success("Copied to clipboard")
    return true
  } catch (error) {
    console.error("clipboard_write_failed", { error })
    toast.error("Couldn't copy", { description: "Clipboard access was denied." })
    return false
  }
}

interface CopyButtonProps {
  text: string
  label: string
  /** Render the label as visible text next to the icon. */
  showLabel?: boolean
}

export function CopyButton({ text, label, showLabel = false }: CopyButtonProps) {
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    if (!copied) return
    const timer = setTimeout(() => setCopied(false), 1500)
    return () => clearTimeout(timer)
  }, [copied])

  const Icon = copied ? Check : Copy
  return (
    <Button
      type="button"
      variant="ghost"
      size={showLabel ? "xs" : "icon-xs"}
      aria-label={label}
      title={label}
      onClick={() => void copyToClipboard(text).then((ok) => ok && setCopied(true))}
      className="text-zinc-400 hover:text-zinc-100 focus-visible:ring-2 focus-visible:ring-indigo-400"
    >
      <Icon aria-hidden />
      {showLabel ? label : null}
    </Button>
  )
}
