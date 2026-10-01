export type HealthResponse = { status: 'ok' }

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
