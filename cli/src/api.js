// Клиент API дашборда для CLI: /api/cli/v1 с личным токеном.

export class ApiError extends Error {
  constructor(status, code, message) {
    super(message)
    this.status = status
    this.code = code
  }
}

export class Api {
  constructor(url, token = '', fetchImpl = globalThis.fetch) {
    this.url = url
    this.token = token
    this.fetch = fetchImpl
  }

  async request(method, path, body, { raw = false, okStatuses = [] } = {}) {
    const headers = { 'User-Agent': 'airlok-cli' }
    if (this.token) headers.Authorization = `Bearer ${this.token}`
    if (body !== undefined && !raw) headers['Content-Type'] = 'application/json'
    if (raw) headers['Content-Type'] = 'application/gzip'
    let res
    try {
      res = await this.fetch(this.url + path, { method, headers, body: body === undefined ? undefined : raw ? body : JSON.stringify(body) })
    } catch (err) {
      throw new ApiError(0, 'network', `Нет связи с ${this.url}: ${err.cause?.code ?? err.message}`)
    }
    const data = res.status === 204 ? null : await res.json().catch(() => null)
    if (!res.ok && !okStatuses.includes(res.status)) {
      throw new ApiError(res.status, data?.error?.code ?? 'unknown', data?.error?.message ?? `сервер ответил ${res.status}`)
    }
    return { status: res.status, data }
  }

  startLogin(client) {
    return this.request('POST', '/api/cli/v1/login', { client }).then((r) => r.data)
  }

  // pollLogin — токен, когда вход подтверждён, или null, пока человек не подтвердил.
  async pollLogin(deviceCode) {
    const { status, data } = await this.request('POST', '/api/cli/v1/login/token', { device_code: deviceCode }, { okStatuses: [202] })
    return status === 202 ? null : data
  }

  me() {
    return this.request('GET', '/api/cli/v1/me').then((r) => r.data)
  }

  // apps — приложения на хостинге: архивом выкатывают только туда.
  async apps() {
    return (await this.allApps()).filter((a) => a.hosted)
  }

  allApps() {
    return this.request('GET', '/api/cli/v1/apps').then((r) => r.data.apps)
  }

  // Переменные приложения на хостинге: значения шифрует дашборд.
  env(appId) {
    return this.request('GET', `/api/cli/v1/apps/${encodeURIComponent(appId)}/env`).then((r) => r.data.vars)
  }

  putEnv(appId, vars) {
    return this.request('PUT', `/api/cli/v1/apps/${encodeURIComponent(appId)}/env`, { vars })
  }

  deleteEnv(appId, name) {
    return this.request('DELETE', `/api/cli/v1/apps/${encodeURIComponent(appId)}/env/${encodeURIComponent(name)}`)
  }

  // Переменные приложения на своём сервере: ключ агента и шифротексты, которые сделал CLI.
  serverEnv(appId) {
    return this.request('GET', `/api/cli/v1/apps/${encodeURIComponent(appId)}/server-env`).then((r) => r.data)
  }

  putServerEnv(appId, keyId, vars) {
    return this.request('PUT', `/api/cli/v1/apps/${encodeURIComponent(appId)}/server-env`, { key_id: keyId, vars })
  }

  deleteServerEnv(appId, name) {
    return this.request('DELETE', `/api/cli/v1/apps/${encodeURIComponent(appId)}/server-env/${encodeURIComponent(name)}`)
  }

  servers() {
    return this.request('GET', '/api/cli/v1/servers').then((r) => r.data.servers)
  }

  // createServer и rotateServerToken отдают токен агента: он нужен только установщику на сервере.
  createServer(name) {
    return this.request('POST', '/api/cli/v1/servers', { name }).then((r) => r.data)
  }

  rotateServerToken(id) {
    return this.request('POST', `/api/cli/v1/servers/${encodeURIComponent(id)}/token`).then((r) => r.data)
  }

  createApp(name) {
    return this.request('POST', '/api/cli/v1/apps', { target: 'hosted', name }).then((r) => r.data.id)
  }

  // check — что airlok сделает с проектом, без сборки: архив проверяется и сразу удаляется.
  check(archive) {
    return this.request('POST', '/api/cli/v1/check', archive, { raw: true }).then((r) => r.data)
  }

  deployArchive(appId, archive, title) {
    const q = new URLSearchParams({ title })
    return this.request('POST', `/api/cli/v1/apps/${encodeURIComponent(appId)}/archive?${q}`, archive, { raw: true }).then((r) => r.data)
  }

  command(id) {
    return this.request('GET', `/api/cli/v1/commands/${encodeURIComponent(id)}`).then((r) => r.data)
  }
}
