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
  createRow: (body) => request('/api/rows', { method: 'POST', body }),
  curateNow: () => request('/api/rows/curate-now', { method: 'POST' }),
  editRow: (id, body) => request(`/api/rows/${id}`, { method: 'PATCH', body }),
  setStatus: (id, status) => request(`/api/rows/${id}/status`, { method: 'PUT', body: { status } }),
  redraftNow: (id, take) => request(`/api/rows/${id}/redraft-now`, { method: 'POST', body: { take } }),
  bulkSkip: (ids) => request('/api/rows/bulk-skip', { method: 'POST', body: { ids } }),
  reschedule: (id, scheduledFor) =>
    request(`/api/rows/${id}/schedule`, { method: 'PUT', body: { scheduled_for: scheduledFor } }),
  publishNow: (id) => request(`/api/rows/${id}/publish-now`, { method: 'POST' }),
  generateImage: (id) => request(`/api/rows/${id}/generate-image`, { method: 'POST' }),
  removeImage: (id) => request(`/api/rows/${id}/image`, { method: 'DELETE' }),

  setPaused: (paused) => request('/api/pause', { method: 'PUT', body: { paused } }),
  sources: () => request('/api/sources'),
  addSource: (body) => request('/api/sources', { method: 'POST', body }),
  updateSource: (id, body) => request(`/api/sources/${id}`, { method: 'PUT', body }),
  deleteSource: (id) => request(`/api/sources/${id}`, { method: 'DELETE' }),

  setAudience: (description) => request('/api/audience', { method: 'PUT', body: { description } }),
  voiceCard: () => request('/api/voice-card'),
  saveVoiceCard: (content) => request('/api/voice-card', { method: 'PUT', body: { content } }),
  amendments: () => request('/api/amendments'),
  decideAmendment: (id, accepted) =>
    request(`/api/amendments/${id}`, { method: 'PUT', body: { accepted } }),

  usage: () => request('/api/usage'),
  health: () => request('/api/health-metric'),

  discussions: () => request('/api/discussions'),
  discussion: (id) => request(`/api/discussions/${id}`),
  startDiscussion: (body) => request('/api/discussions', { method: 'POST', body }),
  postDiscussionMessage: (id, content) =>
    request(`/api/discussions/${id}/messages`, { method: 'POST', body: { content } }),
  turnIntoPost: (id) => request(`/api/discussions/${id}/turn-into-post`, { method: 'POST' }),
  deleteDiscussion: (id) => request(`/api/discussions/${id}`, { method: 'DELETE' }),

  saveLinkedInApp: (body) => request('/auth/linkedin/app', { method: 'PUT', body }),
  disconnectLinkedIn: () => request('/auth/linkedin/connect', { method: 'DELETE' }),
}
