import { useEffect, useState } from 'react'
import { getHealth } from './api'

type Connection = 'checking' | 'connected' | 'unavailable'

const connectionText: Record<Connection, { title: string; description: string; color: string }> = {
  checking: {
    title: 'Connecting to your local service…',
    description: 'Just a moment while we check the connection.',
    color: 'bg-amber-500',
  },
  connected: {
    title: 'Connected to your local service',
    description: 'The foundation is ready. Voices and speech generation come next.',
    color: 'bg-emerald-600',
  },
  unavailable: {
    title: 'Local service unavailable',
    description: 'Make sure the backend is running, then try connecting again.',
    color: 'bg-rose-600',
  },
}

export default function App() {
  const [connection, setConnection] = useState<Connection>('checking')
  const [attempt, setAttempt] = useState(0)

  useEffect(() => {
    const controller = new AbortController()
    setConnection('checking')

    getHealth(controller.signal)
      .then(() => {
        if (!controller.signal.aborted) setConnection('connected')
      })
      .catch(() => {
        if (!controller.signal.aborted) setConnection('unavailable')
      })

    return () => controller.abort()
  }, [attempt])

  const status = connectionText[connection]

  return (
    <div className="mx-auto flex min-h-dvh max-w-6xl flex-col px-6 sm:px-12">
      <header className="flex items-center justify-between gap-4 border-b border-ink/15 py-7">
        <a href="/" className="flex items-center gap-3 text-xl font-semibold tracking-tight">
          <span aria-hidden="true" className="flex size-10 items-center justify-center gap-1 rounded-full bg-ink text-paper">
            {[12, 21, 15, 9].map((height, index) => (
              <span key={index} className="w-0.5 rounded-full bg-current" style={{ height }} />
            ))}
          </span>
          LocalReader
        </a>
        <span className="text-xs font-medium uppercase tracking-[0.16em] text-ink/65">
          Made for your own space
        </span>
      </header>

      <main className="flex flex-1 flex-col items-start justify-center py-20 sm:py-28">
        <p className="mb-6 text-xs font-semibold uppercase tracking-[0.22em] text-ink/65">
          Your personal reading companion
        </p>
        <h1 className="max-w-3xl font-serif text-5xl leading-[1.08] tracking-tight sm:text-7xl">
          A private space<br />to listen.
        </h1>
        <p className="mt-7 max-w-lg text-lg leading-relaxed text-ink/70">
          LocalReader is taking shape. This first step connects your reading
          space to the local service that will bring your words to life.
        </p>

        <section aria-label="Connection status" className="mt-12 w-full max-w-lg rounded-2xl border border-ink/15 bg-white/65 p-6">
          <div role="status" aria-live="polite" aria-atomic="true">
            <h2 className="flex items-center gap-3 font-semibold">
              <span aria-hidden="true" className={`size-2 shrink-0 rounded-full ${status.color}`} />
              {status.title}
            </h2>
            <p className="mt-2 text-sm leading-relaxed text-ink/70">{status.description}</p>
          </div>
          <button
            type="button"
            disabled={connection === 'checking'}
            onClick={() => setAttempt((previous) => previous + 1)}
            className="mt-5 rounded-lg bg-ink px-4 py-2.5 text-sm font-medium text-paper transition-colors hover:bg-ink/85 focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-ink disabled:opacity-50"
          >
            {connection === 'checking' ? 'Connecting…' : connection === 'unavailable' ? 'Try again' : 'Check connection'}
          </button>
        </section>
      </main>

      <footer className="flex flex-wrap justify-between gap-3 border-t border-ink/15 py-6 text-xs text-ink/60">
        <span>LocalReader · Local development</span>
        <span>Foundation preview · Speech generation comes next</span>
      </footer>
    </div>
  )
}
