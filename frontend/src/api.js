async function req(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || res.statusText)
  }
  return res.json()
}

export const api = {
  health: () => req('/api/health'),
  overview: () => req('/api/overview'),
  waybills: () => req('/api/waybills'),
  timeline: (id) => req(`/api/waybills/${encodeURIComponent(id)}/timeline`),
  quarantine: () => req('/api/quarantine'),
  sidecar: () => req('/api/sidecar'),
  ingest: () => req('/api/ingest/run', { method: 'POST' }),
  proofs: (waybillId) =>
    req(waybillId ? `/api/status-proofs?waybill_id=${encodeURIComponent(waybillId)}` : '/api/status-proofs'),
  routeSkipProofs: ({ waybillId, batchId } = {}) => {
    const qs = new URLSearchParams()
    if (waybillId) qs.set('waybill_id', waybillId)
    if (batchId) qs.set('batch_id', batchId)
    const s = qs.toString()
    return req(`/api/route-skip-proofs${s ? `?${s}` : ''}`)
  },
  skipTearVerifications: ({ waybillId, batchId } = {}) => {
    const qs = new URLSearchParams()
    if (waybillId) qs.set('waybill_id', waybillId)
    if (batchId) qs.set('batch_id', batchId)
    const s = qs.toString()
    return req(`/api/skip-tear-verifications${s ? `?${s}` : ''}`)
  },
  skipTearVerify: (waybillId) =>
    req(`/api/skip-tear-verifications/verify?waybill_id=${encodeURIComponent(waybillId)}`),
}
