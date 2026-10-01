export type HealthResponse = { status: 'ok' }
export type Voice = { id: string; display_name: string; language: string; engine: string }

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
