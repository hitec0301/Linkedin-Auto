// The one place that talks to the server.
//
// Same origin, so the session cookie travels on its own and there is no token
// in JavaScript to leak. Every call goes through `request`, which means every
// error is shaped the same and the 401 path is handled once.

async function request(path, options = {}) {
  const response = await fetch(path, {
    credentials: 'same-origin',
    headers: options.body ? { 'Content-Type': 'application/json' } : {},
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  })

  if (response.status === 401) {
    const error = new Error('signed out')
    error.status = 401
    throw error
  }
  if (response.status === 204) return null

  const text = await response.text()
  const data = text ? JSON.parse(text) : null
  if (!response.ok) {
    // FastAPI puts the message in `detail`. Those messages are written for the
    // person reading them, so show them rather than a generic failure.
    const error = new Error(detailOf(data) || `request failed (${response.status})`)
    error.status = response.status
    throw error
  }
  return data
}

function detailOf(data) {
  if (!data) return ''
  if (typeof data.detail === 'string') return data.detail
  if (Array.isArray(data.detail)) return data.detail.map((d) => d.msg).join('; ')
  return ''
}

export const api = {
  me: () => request('/api/me'),
  signOut: () => request('/auth/signout', { method: 'POST' }),

  rows: (status) => request(`/api/rows${status ? `?status_filter=${status}` : ''}`),
  curateNow: () => request('/api/rows/curate-now', { method: 'POST' }),
  editRow: (id, body) => request(`/api/rows/${id}`, { method: 'PATCH', body }),
  approve: (id) => request(`/api/rows/${id}/approve`, { method: 'POST' }),
  unapprove: (id) => request(`/api/rows/${id}/unapprove`, { method: 'POST' }),
  revise: (id, note) => request(`/api/rows/${id}/revise`, { method: 'POST', body: { note } }),
  skip: (id) => request(`/api/rows/${id}/skip`, { method: 'POST' }),
  redraftNow: (id) => request(`/api/rows/${id}/redraft-now`, { method: 'POST' }),

  setPaused: (paused) => request('/api/pause', { method: 'PUT', body: { paused } }),
  sources: () => request('/api/sources'),
  addSource: (body) => request('/api/sources', { method: 'POST', body }),
  updateSource: (id, body) => request(`/api/sources/${id}`, { method: 'PUT', body }),
  deleteSource: (id) => request(`/api/sources/${id}`, { method: 'DELETE' }),

  voiceCard: () => request('/api/voice-card'),
  saveVoiceCard: (content) => request('/api/voice-card', { method: 'PUT', body: { content } }),
  amendments: () => request('/api/amendments'),
  decideAmendment: (id, accepted) =>
    request(`/api/amendments/${id}`, { method: 'PUT', body: { accepted } }),

  usage: () => request('/api/usage'),
  health: () => request('/api/health-metric'),

  saveLinkedInApp: (body) => request('/auth/linkedin/app', { method: 'PUT', body }),
  disconnectLinkedIn: () => request('/auth/linkedin/connect', { method: 'DELETE' }),
}
