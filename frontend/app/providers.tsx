"use client"

import { QueryClient, QueryClientProvider } from "@tanstack/react-query"
import { useState } from "react"

import { TooltipProvider } from "@/components/ui/tooltip"
import { usePendingSetSync } from "@/hooks/use-pending-sync"

function PendingSetSync() {
  usePendingSetSync()
  return null
}

export function Providers({ children }: { children: React.ReactNode }) {
  // Created in state so each browser session gets one client, never shared
  // across requests during SSR.
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: { staleTime: 5_000, refetchOnWindowFocus: false, retry: 1 },
        },
      }),
  )

  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider delayDuration={200}>
        <PendingSetSync />
        {children}
      </TooltipProvider>
    </QueryClientProvider>
  )
}
