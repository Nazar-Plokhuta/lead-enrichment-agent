"use client"

import { useCallback, useSyncExternalStore } from "react"

/**
 * Subscribes to a CSS media query. The server snapshot is `false` so the first
 * paint is the narrow (Sheet) layout; the client corrects it right after
 * hydration, and the inspector only mounts from client-side URL state anyway.
 */
export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback(
    (notify: () => void) => {
      const list = window.matchMedia(query)
      list.addEventListener("change", notify)
      return () => list.removeEventListener("change", notify)
    },
    [query],
  )
  return useSyncExternalStore(
    subscribe,
    () => window.matchMedia(query).matches,
    () => false,
  )
}
