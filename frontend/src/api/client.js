// One small fetch wrapper for both apps. Cookies carry the session (HttpOnly; never visible here);
// every change sends the CSRF token. If the token is stale (403 csrf_rejected) we fetch a new one
// and retry once. Errors keep the server's code, message and per-field details.

export class ApiError extends Error {
  constructor(status, body) {
    const err = body?.error || {}
    super(err.message || `Request failed (${status})`)
    this.status = status
    this.code = err.code || 'error'
    this.fields = err.fields || {}
  }
}

export function createApi(prefix, csrfPath) {
  let csrf = null

  async function refreshCsrf() {
    const r = await fetch(csrfPath, { credentials: 'same-origin', headers: { Accept: 'application/json' } })
    if (!r.ok) throw new ApiError(r.status, await r.json().catch(() => null))
    csrf = (await r.json()).csrf_token
    return csrf
  }

  async function request(method, path, { body, query, headers = {}, retry = true, raw = false } = {}) {
    const qs = query
      ? '?' + new URLSearchParams(Object.entries(query).filter(([, v]) => v !== '' && v !== null && v !== undefined)).toString()
      : ''
    const url = (path.startsWith('/api/') ? path : prefix + path) + (qs === '?' ? '' : qs)
    const h = { Accept: 'application/json', ...headers }
    if (method !== 'GET') {
      h['X-CSRF-Token'] = csrf || (await refreshCsrf())
      if (body !== undefined) h['Content-Type'] = 'application/json'
    }
    const r = await fetch(url, {
      method,
      headers: h,
      credentials: 'same-origin',
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(45000),
    })
    if (raw && r.ok) return r
    if (r.status === 204) return null
    const data = await r.json().catch(() => null)
    if (!r.ok) {
      if (r.status === 403 && data?.error?.code === 'csrf_rejected' && retry && method !== 'GET') {
        csrf = null
        return request(method, path, { body, query, headers, retry: false, raw })
      }
      throw new ApiError(r.status, data)
    }
    return data
  }

  return {
    get: (p, query) => request('GET', p, { query }),
    post: (p, body, headers) => request('POST', p, { body: body ?? {}, headers }),
    put: (p, body) => request('PUT', p, { body }),
    patch: (p, body) => request('PATCH', p, { body }),
    del: (p) => request('DELETE', p),
    raw: (p) => request('GET', p, { raw: true }),
    setCsrf: (token) => { csrf = token },
    refreshCsrf,
  }
}

export const shopApi = createApi('/api/v1/storefront', '/api/v1/storefront/auth/csrf')
export const adminApi = createApi('/api/v1', '/api/v1/auth/csrf')
