import { useEffect, useRef, useState } from 'react'
import {
  createJob, deleteJob, getHealth, getJob, getVoices, jobAudioUrl,
  jobDownloadUrl, previewVoice, uploadDocument, type JobStatus, type Voice,
} from './api'

type Connection = 'checking' | 'connected' | 'unavailable'
const playbackSpeeds = [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2, 2.5, 3]
const generationSpeeds = [0.5, 0.75, 1, 1.25, 1.5, 2]
const button = 'rounded-lg bg-ink px-4 py-2.5 text-sm font-semibold text-paper hover:bg-ink/85 disabled:opacity-50'
const outline = 'rounded-lg border border-ink/25 px-3 py-2 text-sm font-medium hover:bg-ink/5 disabled:opacity-50'
const field = 'mt-2 w-full rounded-lg border border-ink/25 bg-white px-3 py-2.5 text-sm focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ink'

function message(error: unknown, fallback: string) {
  return error instanceof Error ? error.message : fallback
}

function secondsLeft(value: string | null) {
  const time = value ? Date.parse(value) : NaN
  return Number.isFinite(time) ? Math.max(0, Math.ceil((time - Date.now()) / 1000)) : 0
}

function clock(seconds: number) {
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`
}

export default function App() {
  const [connection, setConnection] = useState<Connection>('checking')
  const [attempt, setAttempt] = useState(0)
  const [voices, setVoices] = useState<Voice[]>([])
  const [voiceError, setVoiceError] = useState<string | null>(null)
  const [selectedVoice, setSelectedVoice] = useState('')
  const [previewUrl, setPreviewUrl] = useState<string | null>(null)
  const [previewError, setPreviewError] = useState<string | null>(null)
  const [previewLoading, setPreviewLoading] = useState(false)
  const [text, setText] = useState('')
  const [documentName, setDocumentName] = useState<string | null>(null)
  const [dragging, setDragging] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [generationSpeed, setGenerationSpeed] = useState(1)
  const [generating, setGenerating] = useState(false)
  const [job, setJob] = useState<JobStatus | null>(null)
  const [readerError, setReaderError] = useState<string | null>(null)
  const [playbackSpeed, setPlaybackSpeed] = useState(1)
  const [playing, setPlaying] = useState(false)
  const [remaining, setRemaining] = useState(0)

  const previewController = useRef<AbortController | null>(null)
  const previewObjectUrl = useRef<string | null>(null)
  const uploadController = useRef<AbortController | null>(null)
  const generationController = useRef<AbortController | null>(null)
  const pollController = useRef<AbortController | null>(null)
  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const activeJobId = useRef<string | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const fileInputRef = useRef<HTMLInputElement | null>(null)

  function clearPreview() {
    previewController.current?.abort()
    if (previewObjectUrl.current) URL.revokeObjectURL(previewObjectUrl.current)
    previewObjectUrl.current = null
    setPreviewUrl(null)
    setPreviewError(null)
    setPreviewLoading(false)
  }

  function stopPolling() {
    pollController.current?.abort()
    if (pollTimer.current) clearTimeout(pollTimer.current)
    pollTimer.current = null
  }

  useEffect(() => () => {
    previewController.current?.abort()
    if (previewObjectUrl.current) URL.revokeObjectURL(previewObjectUrl.current)
    uploadController.current?.abort()
    generationController.current?.abort()
    stopPolling()
  }, [])

  useEffect(() => { clearPreview() }, [selectedVoice, connection])

  useEffect(() => {
    const controller = new AbortController()
    setConnection('checking')
    getHealth(controller.signal)
      .then(() => { if (!controller.signal.aborted) setConnection('connected') })
      .catch(() => { if (!controller.signal.aborted) setConnection('unavailable') })
    return () => controller.abort()
  }, [attempt])

  useEffect(() => {
    if (connection !== 'connected') return
    const controller = new AbortController()
    getVoices(controller.signal)
      .then((items) => {
        if (controller.signal.aborted) return
        setVoices(items)
        setVoiceError(null)
        setSelectedVoice((current) => items.some((voice) => voice.id === current) ? current : (items[0]?.id ?? ''))
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return
        setVoices([])
        setSelectedVoice('')
        setVoiceError(message(error, 'Could not load voices.'))
      })
    return () => controller.abort()
  }, [connection, attempt])

  useEffect(() => {
    if (job?.status !== 'completed' || !job.expires_at) return
    let checked = false
    const update = () => {
      const left = secondsLeft(job.expires_at)
      setRemaining(left)
      if (left === 0 && !checked) {
        checked = true
        audioRef.current?.pause()
        getJob(job.id, new AbortController().signal)
          .then((latest) => { if (activeJobId.current === job.id) setJob(latest) })
          .catch(() => { if (activeJobId.current === job.id) setJob({ ...job, status: 'expired' }) })
      }
    }
    update()
    const timer = setInterval(update, 1000)
    return () => clearInterval(timer)
  }, [job?.id, job?.status, job?.expires_at])

  function poll(jobId: string) {
    stopPolling()
    const controller = new AbortController()
    pollController.current = controller
    const tick = async () => {
      try {
        const current = await getJob(jobId, controller.signal)
        if (controller.signal.aborted || activeJobId.current !== jobId) return
        setJob(current)
        setReaderError(current.status === 'failed' ? current.error || 'Speech generation failed.' : null)
        if (['queued', 'processing', 'assembling'].includes(current.status)) {
          pollTimer.current = setTimeout(tick, 1000)
        }
      } catch (error) {
        if (controller.signal.aborted || activeJobId.current !== jobId) return
        setReaderError(message(error, 'Could not check speech progress.'))
        pollTimer.current = setTimeout(tick, 3000)
      }
    }
    void tick()
  }

  async function handlePreview() {
    if (!selectedVoice) return
    clearPreview()
    const controller = new AbortController()
    previewController.current = controller
    setPreviewLoading(true)
    try {
      const blob = await previewVoice(selectedVoice, controller.signal)
      if (controller.signal.aborted) return
      const url = URL.createObjectURL(blob)
      previewObjectUrl.current = url
      setPreviewUrl(url)
    } catch (error) {
      if (!controller.signal.aborted) setPreviewError(message(error, 'Could not generate the preview.'))
    } finally {
      if (!controller.signal.aborted) setPreviewLoading(false)
    }
  }

  async function handleFile(file: File) {
    uploadController.current?.abort()
    const controller = new AbortController()
    uploadController.current = controller
    setUploading(true)
    setReaderError(null)
    try {
      const document = await uploadDocument(file, controller.signal)
      if (controller.signal.aborted) return
      setText(document.text)
      setDocumentName(file.name)
    } catch (error) {
      if (!controller.signal.aborted) setReaderError(message(error, 'Could not read that document.'))
    } finally {
      if (!controller.signal.aborted) setUploading(false)
      if (fileInputRef.current) fileInputRef.current.value = ''
    }
  }

  async function generate() {
    if (!text.trim() || !selectedVoice || connection !== 'connected') return
    const controller = new AbortController()
    generationController.current = controller
    stopPolling()
    activeJobId.current = null
    setJob(null)
    setReaderError(null)
    setGenerating(true)
    try {
      const created = await createJob(text, selectedVoice, generationSpeed, controller.signal)
      if (controller.signal.aborted) return
      activeJobId.current = created.id
      poll(created.id)
    } catch (error) {
      if (!controller.signal.aborted) setReaderError(message(error, 'Could not start generation.'))
    } finally {
      if (!controller.signal.aborted) setGenerating(false)
    }
  }

  async function cancel() {
    if (!job) return
    try {
      await deleteJob(job.id, new AbortController().signal)
      stopPolling()
      activeJobId.current = null
      setJob(null)
      setReaderError(null)
    } catch (error) {
      setReaderError(message(error, 'Could not cancel the job.'))
    }
  }

  function skip(seconds: number) {
    const audio = audioRef.current
    if (audio) audio.currentTime = Math.max(0, Math.min(audio.duration || Infinity, audio.currentTime + seconds))
  }

  function setSpeed(speed: number) {
    setPlaybackSpeed(speed)
    if (audioRef.current) audioRef.current.playbackRate = speed
  }

  const active = job?.status === 'queued' || job?.status === 'processing' || job?.status === 'assembling'
  const expired = job?.status === 'expired' || (job?.status === 'completed' && remaining === 0)
  const playable = job?.status === 'completed' && !expired

  return <div className="mx-auto flex min-h-dvh max-w-6xl flex-col px-5 sm:px-10">
    <header className="flex items-center justify-between border-b border-ink/15 py-6">
      <a href="/" className="flex items-center gap-3 text-xl font-semibold"><span aria-hidden="true" className="grid size-10 place-items-center rounded-full bg-ink text-paper">▶</span>LocalReader</a>
      <span className="hidden text-xs font-medium uppercase tracking-[0.16em] text-ink/65 sm:block">Private listening</span>
    </header>
    <main className="flex-1 py-10 sm:py-14">
      <div className="grid gap-10 lg:grid-cols-[minmax(0,0.8fr)_minmax(0,1.2fr)] lg:gap-14">
        <div>
          <p className="text-xs font-semibold uppercase tracking-[0.22em] text-ink/65">Your reading companion</p>
          <h1 className="mt-5 font-serif text-5xl leading-tight sm:text-6xl">A private space<br />to listen.</h1>
          <p className="mt-6 max-w-md text-lg leading-relaxed text-ink/70">Paste a passage or bring a document. LocalReader turns it into speech on your own device.</p>
          <section aria-label="Connection status" className="mt-9 rounded-2xl border border-ink/15 bg-white/65 p-5">
            <h2 role="status" aria-live="polite" className="font-semibold">{connection === 'connected' ? '● Connected to your local service' : connection === 'checking' ? '● Connecting…' : '● Local service unavailable'}</h2>
            <p className="mt-2 text-sm text-ink/70">{connection === 'connected' ? 'Your text and speech stay with this local service.' : 'Make sure the backend is running, then try again.'}</p>
            <button type="button" disabled={connection === 'checking'} onClick={() => setAttempt((value) => value + 1)} className="mt-4 text-sm font-semibold underline underline-offset-4 disabled:opacity-50">{connection === 'unavailable' ? 'Try again' : 'Check connection'}</button>
          </section>
        </div>
        <div className="space-y-6">
          <section aria-label="Create audio" className="rounded-2xl border border-ink/15 bg-white/75 p-5 shadow-sm sm:p-7">
            <h2 className="font-serif text-2xl">Your text</h2>
            <p className="mt-1 text-sm text-ink/65">Paste text, choose a TXT or PDF, or drop one below.</p>
            <label htmlFor="reader-text" className="sr-only">Text to read aloud</label>
            <textarea id="reader-text" value={text} onChange={(event) => { setText(event.target.value); setDocumentName(null) }} placeholder="Paste the words you want to hear…" rows={10} className="mt-5 w-full resize-y rounded-xl border border-ink/25 bg-white p-4 leading-relaxed focus-visible:outline-2 focus-visible:outline-ink" />
            <div onDragOver={(event) => { event.preventDefault(); setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={(event) => { event.preventDefault(); setDragging(false); const file = event.dataTransfer.files[0]; if (file) void handleFile(file) }} className={`mt-3 rounded-xl border border-dashed p-4 text-sm focus-within:ring-2 focus-within:ring-ink ${dragging ? 'border-ink bg-ink/10' : 'border-ink/25 bg-paper/65'}`}>
              <label htmlFor="document-file" className="cursor-pointer font-semibold underline underline-offset-4">Upload TXT or PDF</label>
              <input ref={fileInputRef} id="document-file" type="file" accept=".txt,.pdf,text/plain,application/pdf" disabled={uploading} onChange={(event) => { const file = event.target.files?.[0]; if (file) void handleFile(file) }} className="sr-only" />
              <span className="ml-2 text-ink/65">or drop a file here</span>
              {uploading && <p role="status" className="mt-2">Reading document…</p>}
              {documentName && <p className="mt-2 break-all text-ink/65">Loaded: {documentName}</p>}
            </div>
            <p className="mt-2 text-xs text-ink/60">{text.length.toLocaleString()} characters</p>
            <div className="mt-6 grid gap-4 sm:grid-cols-2">
              <div><label htmlFor="voice" className="text-sm font-medium">Voice</label><select id="voice" value={selectedVoice} disabled={connection !== 'connected' || voices.length === 0} onChange={(event) => setSelectedVoice(event.target.value)} className={field}>{voices.length === 0 && <option value="">No voices loaded</option>}{voices.map((voice) => <option key={voice.id} value={voice.id}>{voice.display_name} · {voice.language}</option>)}</select></div>
              <div><label htmlFor="generation-speed" className="text-sm font-medium">Generation speed</label><select id="generation-speed" value={generationSpeed} onChange={(event) => setGenerationSpeed(Number(event.target.value))} className={field}>{generationSpeeds.map((speed) => <option key={speed} value={speed}>{speed}×</option>)}</select></div>
            </div>
            {voiceError && <p role="alert" className="mt-2 text-sm text-rose-800">{voiceError}</p>}
            <div className="mt-4 flex flex-wrap gap-3">
              <button type="button" onClick={handlePreview} disabled={!selectedVoice || previewLoading || connection !== 'connected'} className={outline}>{previewLoading ? 'Generating preview…' : 'Preview voice'}</button>
              <button type="button" onClick={generate} disabled={!text.trim() || !selectedVoice || connection !== 'connected' || uploading || generating || active} className={button}>{generating ? 'Starting…' : 'Generate audio'}</button>
            </div>
            {previewError && <p role="alert" className="mt-3 text-sm text-rose-800">{previewError}</p>}
            {previewUrl && <audio key={previewUrl} aria-label="Voice preview audio" controls src={previewUrl} className="mt-4 w-full" />}
            {readerError && <p role="alert" className="mt-4 rounded-lg bg-rose-50 p-3 text-sm text-rose-800">{readerError}</p>}
          </section>

          {job && <section aria-label="Speech job" className="rounded-2xl border border-ink/15 bg-white/75 p-5 shadow-sm sm:p-7">
            <div className="flex flex-wrap items-start justify-between gap-3">
              <div><h2 className="font-serif text-2xl">{playable ? 'Ready to listen' : expired ? 'Audio expired' : job.status === 'failed' ? 'Generation failed' : 'Making your audio'}</h2>
                <p role="status" aria-live="polite" className="mt-1 text-sm text-ink/65">{job.status === 'queued' ? 'Waiting for the current job.' : job.status === 'processing' ? `Reading chunk ${job.current_chunk} of ${job.total_chunks}.` : job.status === 'assembling' ? 'Putting the audio together…' : playable ? 'Your MP3 is ready.' : expired ? 'The MP3 has been deleted.' : job.error}</p></div>
              {active && <button type="button" onClick={cancel} className="text-sm font-semibold underline underline-offset-4">Cancel job</button>}
            </div>
            {active && <div className="mt-5"><div className="flex justify-between text-xs font-medium"><span>Progress</span><span>{job.progress}%</span></div><progress value={job.progress} max={100} aria-label="Generation progress" className="mt-2 h-3 w-full accent-ink" /><p className="mt-1 text-xs text-ink/60">{job.current_chunk} of {job.total_chunks} chunks complete</p></div>}
            {playable && <div className="mt-5">
              <audio ref={audioRef} key={job.id} controls preload="metadata" aria-label="Generated speech audio" src={jobAudioUrl(job.id)} onLoadedMetadata={(event) => { event.currentTarget.playbackRate = playbackSpeed }} onPlay={() => setPlaying(true)} onPause={() => setPlaying(false)} onEnded={() => setPlaying(false)} onError={() => { setReaderError('Audio could not load. Check whether it has expired.'); void getJob(job.id, new AbortController().signal).then((latest) => { if (activeJobId.current === job.id) setJob(latest) }).catch(() => {}) }} className="w-full" />
              <div className="mt-4 flex flex-wrap gap-2"><button type="button" onClick={() => skip(-10)} className={outline}>−10 sec</button><button type="button" onClick={() => { const audio = audioRef.current; if (!audio) return; if (audio.paused) void audio.play().catch(() => setReaderError('Audio could not start.')); else audio.pause() }} className={outline}>{playing ? 'Pause' : 'Play'}</button><button type="button" onClick={() => skip(10)} className={outline}>+10 sec</button></div>
              <fieldset className="mt-5"><legend className="text-sm font-medium">Playback speed</legend><div className="mt-2 flex flex-wrap gap-2">{playbackSpeeds.map((speed) => <button key={speed} type="button" aria-pressed={playbackSpeed === speed} onClick={() => setSpeed(speed)} className={`rounded-full border px-3 py-1.5 text-xs font-semibold ${playbackSpeed === speed ? 'border-ink bg-ink text-paper' : 'border-ink/25 bg-white'}`}>{speed}×</button>)}</div></fieldset>
              <div className="mt-5 flex flex-wrap items-center justify-between gap-3 border-t border-ink/10 pt-4"><a href={jobDownloadUrl(job.id)} download={`localreader-${job.id}.mp3`} className={button}>Download MP3</a><p className="text-sm text-ink/65">Automatically deletes in <strong>{clock(remaining)}</strong></p></div>
            </div>}
          </section>}
        </div>
      </div>
    </main>
    <footer className="flex flex-wrap justify-between gap-3 border-t border-ink/15 py-6 text-xs text-ink/60"><span>LocalReader · Local development</span><span>Private text-to-speech on your device</span></footer>
  </div>
}
