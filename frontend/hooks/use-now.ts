"use client"

import { useSyncExternalStore } from "react"

/**
 * One shared 1 Hz clock for every elapsed/relative-time label. A per-row
 * `setInterval` would spawn up to ~150 timers (table + activity popover);
 * this keeps a single timer that only runs while something is subscribed.
 */
let current = Date.now()
let timer: ReturnType<typeof setInterval> | null = null
const listeners = new Set<() => void>()

function subscribe(listener: () => void): () => void {
  if (listeners.size === 0) {
    // Resync: the module may have loaded long before the first subscriber.
    current = Date.now()
    timer = setInterval(() => {
      current = Date.now()
      listeners.forEach((notify) => notify())
    }, 1000)
  }
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
    if (listeners.size === 0 && timer !== null) {
      clearInterval(timer)
      timer = null
    }
  }
}

const getSnapshot = () => current
// Time-dependent UI only renders from query data, which is absent during SSR.
const getServerSnapshot = () => 0

export function useNow(): number {
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot)
}
