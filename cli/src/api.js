// Клиент API дашборда для CLI: /api/cli/v1 с личным токеном.

import { readFileSync } from 'node:fs'

const pkg = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'))

// По версии в User-Agent дашборд видит в журнале, какие CLI ещё в ходу, и может попросить старые обновиться.
export const USER_AGENT = `airlok-cli/${pkg.version} node/${process.versions.node} ${process.platform}`

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
    const headers = { 'User-Agent': USER_AGENT }
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

  // pollLogin — токен, когда вход подтверждён, или null, пока человек не подтвердил. previousToken —
  // прежний вход этого компьютера: дашборд отзывает его, когда выдаёт новый.
  async pollLogin(deviceCode, previousToken = '') {
    const body = { device_code: deviceCode, ...(previousToken && { previous_token: previousToken }) }
    const { status, data } = await this.request('POST', '/api/cli/v1/login/token', body, { okStatuses: [202] })
    return status === 202 ? null : data
  }

  // revokeToken отзывает токен, с которым пришёл запрос: им больше никто не войдёт.
  revokeToken() {
    return this.request('DELETE', '/api/cli/v1/token')
  }

  me() {
    return this.request('GET', '/api/cli/v1/me').then((r) => r.data)
  }

  // tokenScope — 'cli' у входа CLI, 'mcp' у токена из настроек; '' — дашборд старше областей токенов.
  async tokenScope() {
    try {
      const { data } = await this.request('GET', '/api/cli/v1/token')
      return data?.scope ?? ''
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) return ''
      throw err
    }
  }

  // createAgentToken — отдельный токен для конфигов ИИ-агентов: выпускает его только вход CLI.
  createAgentToken(client) {
    return this.request('POST', '/api/cli/v1/tokens', { client }).then((r) => r.data)
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

  // Свой домен приложения на своём сервере; на хостинге домены подключаются в дашборде.
  serverDomains() {
    return this.request('GET', '/api/cli/v1/server-domains').then((r) => r.data.domains)
  }

  setServerDomain(appId, hostname) {
    return this.request('PUT', `/api/cli/v1/apps/${encodeURIComponent(appId)}/server-domain`, { hostname }).then((r) => r.data)
  }

  checkServerDomain(appId) {
    return this.request('POST', `/api/cli/v1/apps/${encodeURIComponent(appId)}/server-domain/check`).then((r) => r.data)
  }

  removeServerDomain(appId) {
    return this.request('DELETE', `/api/cli/v1/apps/${encodeURIComponent(appId)}/server-domain`)
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

  // Обновления системы на сервере: action — upgrade или reboot. Выполняет агент сервера.
  startOSUpgrade(id, action) {
    return this.request('POST', `/api/cli/v1/servers/${encodeURIComponent(id)}/os-upgrade`, { action }).then((r) => r.data)
  }

  // Устройства — открытые SSH-ключи компьютеров и серверы, на которые агент их кладёт.
  devices() {
    return this.request('GET', '/api/cli/v1/devices').then((r) => r.data)
  }

  createDevice(name, publicKey, serverIds) {
    return this.request('POST', '/api/cli/v1/devices', { name, public_key: publicKey, server_ids: serverIds }).then((r) => r.data)
  }

  setDeviceServers(id, serverIds) {
    return this.request('PUT', `/api/cli/v1/devices/${encodeURIComponent(id)}/servers`, { server_ids: serverIds })
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

  // createTemporary публикует без входа: Api без токена, в ответе — токен сайта и ссылка «забрать себе».
  createTemporary(archive, title) {
    const q = new URLSearchParams({ title })
    return this.request('POST', `/api/cli/v1/temporary?${q}`, archive, { raw: true }).then((r) => r.data)
  }

  // updateTemporary обновляет сайт без входа: токен — токен этого сайта, а не вход CLI.
  updateTemporary(archive, title) {
    const q = new URLSearchParams({ title })
    return this.request('PUT', `/api/cli/v1/temporary?${q}`, archive, { raw: true }).then((r) => r.data)
  }

  temporaryCommand(id) {
    return this.request('GET', `/api/cli/v1/temporary/commands/${encodeURIComponent(id)}`).then((r) => r.data)
  }
}
