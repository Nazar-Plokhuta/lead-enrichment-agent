import { Suspense } from "react"

import { Console } from "@/components/console"

// `useSearchParams` (filters, `?lead=`) needs a Suspense boundary or the whole
// route bails out of static prerendering and `next build` fails.
export default function Home() {
  return (
    <Suspense fallback={<div className="min-h-screen bg-zinc-950" />}>
      <Console />
    </Suspense>
  )
}
