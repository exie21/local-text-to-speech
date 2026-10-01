import { useEffect, useRef, useState } from 'react'
import { getHealth, getVoices, previewVoice, type Voice } from './api'

type Connection = 'checking' | 'connected' | 'unavailable'

const connectionText: Record<Connection, { title: string; description: string; color: string }> = {
  checking: {
    title: 'Connecting to your local service…',
    description: 'Just a moment while we check the connection.',
    color: 'bg-amber-500',
  },
  connected: {
    title: 'Connected to your local service',
    description: 'Choose a voice below and listen to a short sample.',
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
  const [voices, setVoices] = useState<Voice[]>([])
  const [voiceError, setVoiceError] = useState<string | null>(null)
  const [voicesLoading, setVoicesLoading] = useState(false)
  const [selectedVoice, setSelectedVoice] = useState('')
  const [previewUrl, setPreviewUrl] = useState<string | null>(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const previewController = useRef<AbortController | null>(null)
  const previewObjectUrl = useRef<string | null>(null)

  function clearPreview() {
    previewController.current?.abort()
    previewController.current = null
    if (previewObjectUrl.current) URL.revokeObjectURL(previewObjectUrl.current)
    previewObjectUrl.current = null
    setPreviewUrl(null)
    setPreviewLoading(false)
    setPreviewError(null)
  }

  useEffect(() => () => {
    previewController.current?.abort()
    if (previewObjectUrl.current) URL.revokeObjectURL(previewObjectUrl.current)
  }, [])

  useEffect(() => {
    clearPreview()
  }, [selectedVoice, connection])

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

  useEffect(() => {
    if (connection !== 'connected') return
    const controller = new AbortController()
    setVoicesLoading(true)
    setVoiceError(null)
    getVoices(controller.signal)
      .then((available) => {
        if (controller.signal.aborted) return
        setVoices(available)
        setSelectedVoice((current) => available.some((voice) => voice.id === current) ? current : (available[0]?.id ?? ''))
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return
        setVoices([])
        setSelectedVoice('')
        setVoiceError(error instanceof Error ? error.message : 'Could not load voices.')
      })
      .finally(() => {
        if (!controller.signal.aborted) setVoicesLoading(false)
      })
    return () => controller.abort()
  }, [connection, attempt])

  async function handlePreview() {
    if (!selectedVoice) return
    clearPreview()
    const controller = new AbortController()
    previewController.current = controller
    setPreviewLoading(true)
    try {
      const audio = await previewVoice(selectedVoice, controller.signal)
      if (controller.signal.aborted) return
      const url = URL.createObjectURL(audio)
      previewObjectUrl.current = url
      setPreviewUrl(url)
    } catch (error) {
      if (!controller.signal.aborted) {
        setPreviewError(error instanceof Error ? error.message : 'Could not generate the voice preview.')
      }
    } finally {
      if (!controller.signal.aborted) setPreviewLoading(false)
    }
  }

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
          LocalReader is taking shape. Explore the voices available on your own
          device and find one you enjoy hearing.
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

        <section aria-label="Voice preview" className="mt-6 w-full max-w-lg rounded-2xl border border-ink/15 bg-white/65 p-6">
          <h2 className="font-semibold">Listen to a voice</h2>
          <p className="mt-2 text-sm leading-relaxed text-ink/70">
            Previews speak the same short sentence so you can compare voices.
          </p>
          {connection !== 'connected' ? (
            <p className="mt-5 text-sm text-ink/70">Connect to the local service to load voices.</p>
          ) : voicesLoading ? (
            <p role="status" className="mt-5 text-sm text-ink/70">Loading voices…</p>
          ) : voiceError ? (
            <div className="mt-5" role="alert">
              <p className="text-sm text-rose-800">{voiceError}</p>
              <button type="button" onClick={() => setAttempt((previous) => previous + 1)} className="mt-3 text-sm font-semibold underline underline-offset-4">Try again</button>
            </div>
          ) : voices.length === 0 ? (
            <p className="mt-5 text-sm text-ink/70">No voices are available in the installed voice bank.</p>
          ) : (
            <div className="mt-5">
              <label htmlFor="voice" className="block text-sm font-medium">Voice</label>
              <select
                id="voice"
                value={selectedVoice}
                onChange={(event) => { clearPreview(); setSelectedVoice(event.target.value) }}
                className="mt-2 w-full rounded-lg border border-ink/25 bg-white px-3 py-2.5 text-sm focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink"
              >
                {voices.map((voice) => <option key={voice.id} value={voice.id}>{voice.display_name} · {voice.language}</option>)}
              </select>
              <button
                type="button"
                onClick={handlePreview}
                disabled={!selectedVoice || previewLoading}
                className="mt-4 rounded-lg bg-ink px-4 py-2.5 text-sm font-medium text-paper transition-colors hover:bg-ink/85 focus-visible:outline-2 focus-visible:outline-offset-4 focus-visible:outline-ink disabled:opacity-50"
              >
                {previewLoading ? 'Generating preview…' : 'Generate preview'}
              </button>
              {previewError && <p role="alert" className="mt-3 text-sm text-rose-800">{previewError}</p>}
              {previewUrl && <audio key={previewUrl} aria-label="Voice preview audio" className="mt-4 w-full" controls src={previewUrl} />}
            </div>
          )}
        </section>
      </main>

      <footer className="flex flex-wrap justify-between gap-3 border-t border-ink/15 py-6 text-xs text-ink/60">
        <span>LocalReader · Local development</span>
        <span>Voice preview · Document reading comes next</span>
      </footer>
    </div>
  )
}
