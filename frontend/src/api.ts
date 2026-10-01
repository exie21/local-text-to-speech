export type HealthResponse = { status: 'ok' }
export type Voice = { id: string; display_name: string; language: string; engine: string }
export type JobState = 'queued' | 'processing' | 'assembling' | 'completed' | 'failed' | 'expired'
export type JobStatus = {
  id: string
  status: JobState
  voice: string
  generation_speed: number
  created_at: string
  updated_at: string
  completed_at: string | null
  expires_at: string | null
  progress: number
  current_chunk: number
  total_chunks: number
  audio_duration: number | null
  error: string | null
}
export type DocumentResult = { text: string; character_count: number; file_type: 'txt' | 'pdf' }

export async function getHealth(signal: AbortSignal): Promise<HealthResponse> {
  const response = await fetch('/api/health', {
    headers: { Accept: 'application/json' },
    cache: 'no-store',
    signal: AbortSignal.any([signal, AbortSignal.timeout(5_000)]),
  })

  if (!response.ok) {
    throw new Error('The local service is unavailable.')
  }

  const data: unknown = await response.json()
  if (typeof data !== 'object' || data === null || !('status' in data) || data.status !== 'ok') {
    throw new Error('The local service returned an unexpected health response.')
  }

  return { status: 'ok' }
}

async function responseError(response: Response, fallback: string): Promise<Error> {
  try {
    const data: unknown = await response.json()
    if (typeof data === 'object' && data !== null && 'detail' in data && typeof data.detail === 'string') {
      return new Error(data.detail)
    }
  } catch {
    // A proxy or stopped backend may return a non-JSON error page.
  }
  return new Error(fallback)
}

export async function getVoices(signal: AbortSignal): Promise<Voice[]> {
  const response = await fetch('/api/voices', {
    headers: { Accept: 'application/json' },
    cache: 'no-store',
    signal: AbortSignal.any([signal, AbortSignal.timeout(30_000)]),
  })
  if (!response.ok) throw await responseError(response, 'Could not load voices.')

  const data: unknown = await response.json()
  if (typeof data !== 'object' || data === null || !('voices' in data) || !Array.isArray(data.voices)) {
    throw new Error('The local service returned an unexpected voice list.')
  }
  const voices: unknown[] = data.voices
  if (!voices.every((voice): voice is Voice =>
    typeof voice === 'object' && voice !== null &&
    'id' in voice && typeof voice.id === 'string' &&
    'display_name' in voice && typeof voice.display_name === 'string' &&
    'language' in voice && typeof voice.language === 'string' &&
    'engine' in voice && typeof voice.engine === 'string'
  )) {
    throw new Error('The local service returned an unexpected voice list.')
  }
  return voices
}

export async function previewVoice(voiceId: string, signal: AbortSignal): Promise<Blob> {
  const response = await fetch(`/api/voices/${encodeURIComponent(voiceId)}/preview`, {
    method: 'POST',
    headers: { Accept: 'audio/wav' },
    cache: 'no-store',
    signal: AbortSignal.any([signal, AbortSignal.timeout(60_000)]),
  })
  if (!response.ok) throw await responseError(response, 'Could not generate the voice preview.')
  if (!response.headers.get('content-type')?.startsWith('audio/wav')) {
    throw new Error('The local service returned an unexpected preview format.')
  }
  return response.blob()
}

export async function uploadDocument(file: File, signal: AbortSignal): Promise<DocumentResult> {
  const body = new FormData()
  body.append('file', file)
  const response = await fetch('/api/documents', { method: 'POST', body, cache: 'no-store', signal })
  if (!response.ok) throw await responseError(response, 'Could not read that document.')
  const data: unknown = await response.json()
  if (typeof data !== 'object' || data === null ||
      !('text' in data) || typeof data.text !== 'string' ||
      !('character_count' in data) || typeof data.character_count !== 'number' ||
      !('file_type' in data) || (data.file_type !== 'txt' && data.file_type !== 'pdf')) {
    throw new Error('The local service returned an unexpected document response.')
  }
  return { text: data.text, character_count: data.character_count, file_type: data.file_type }
}

export async function createJob(
  text: string, voice: string, generationSpeed: number, signal: AbortSignal,
): Promise<{ id: string; status: 'queued' }> {
  const response = await fetch('/api/jobs', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify({ text, voice, generation_speed: generationSpeed }),
    cache: 'no-store',
    signal,
  })
  if (!response.ok) throw await responseError(response, 'Could not start speech generation.')
  const data: unknown = await response.json()
  if (typeof data !== 'object' || data === null ||
      !('id' in data) || typeof data.id !== 'string' ||
      !('status' in data) || data.status !== 'queued') {
    throw new Error('The local service returned an unexpected job response.')
  }
  return { id: data.id, status: 'queued' }
}

const jobStates: JobState[] = ['queued', 'processing', 'assembling', 'completed', 'failed', 'expired']

export async function getJob(jobId: string, signal: AbortSignal): Promise<JobStatus> {
  const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, {
    headers: { Accept: 'application/json' }, cache: 'no-store', signal,
  })
  if (!response.ok) throw await responseError(response, 'Could not check speech progress.')
  const data: unknown = await response.json()
  if (typeof data !== 'object' || data === null ||
      !('id' in data) || data.id !== jobId ||
      !('status' in data) || !jobStates.includes(data.status as JobState) ||
      !('progress' in data) || typeof data.progress !== 'number' ||
      !('current_chunk' in data) || typeof data.current_chunk !== 'number' ||
      !('total_chunks' in data) || typeof data.total_chunks !== 'number' ||
      !('expires_at' in data) || (data.expires_at !== null && typeof data.expires_at !== 'string') ||
      !('error' in data) || (data.error !== null && typeof data.error !== 'string')) {
    throw new Error('The local service returned an unexpected job status.')
  }
  return data as JobStatus
}

export async function deleteJob(jobId: string, signal: AbortSignal): Promise<void> {
  const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`, {
    method: 'DELETE', cache: 'no-store', signal,
  })
  if (!response.ok && response.status !== 404) {
    throw await responseError(response, 'Could not remove the audio job.')
  }
}

export function jobAudioUrl(jobId: string): string {
  return `/api/jobs/${encodeURIComponent(jobId)}/audio`
}

export function jobDownloadUrl(jobId: string): string {
  return `/api/jobs/${encodeURIComponent(jobId)}/download`
}
