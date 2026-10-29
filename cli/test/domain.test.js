import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { test } from 'node:test'

import { domainState } from '../src/domain.js'
import { main } from '../src/main.js'

function captureIO() {
  const io = { interactive: false, stdinIsTTY: false, outLines: [], errLines: [], out: (s) => io.outLines.push(s), err: (s) => io.errLines.push(s) }
  return io
}

// fakeDashboard отвечает как /api/cli/v1: домен из PUT подтверждается, только если в state.dns есть его A-запись.
async function fakeDashboard() {
  const state = { domain: null, dns: false, requests: [] }
  const apps = [
    { id: 'h1', name: 'landing', hosted: true, server_name: '@hosting', domain: 'landing.apps.airlok.ru' },
    { id: 's1', name: 'shop', hosted: false, server_id: 'srv1', server_name: 'prod', domain: 'shop.203-0-113-7.sslip.io' },
  ]
  const fail = (res, code, errCode) => send(res, code, { error: { code: errCode, message: errCode } })
  const send = (res, code, data) => {
    res.writeHead(code, { 'Content-Type': 'application/json' })
    res.end(data === undefined ? '' : JSON.stringify(data))
  }
  const server = createServer(async (req, res) => {
    const chunks = []
    for await (const c of req) chunks.push(c)
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks)) : null
    const p = new URL(req.url, 'http://x').pathname
    state.requests.push(`${req.method} ${p}`)
    if (p === '/api/cli/v1/apps') return send(res, 200, { apps })
    if (p === '/api/cli/v1/servers') return send(res, 200, { servers: [{ id: 'srv1', name: 'prod', public_ip: '203.0.113.7' }] })
    if (p === '/api/cli/v1/server-domains') return send(res, 200, { domains: state.domain ? [state.domain] : [] })
    if (p === '/api/cli/v1/apps/s1/server-domain' && req.method === 'PUT') {
      if (body.hostname === 'ru') return fail(res, 400, 'invalid_domain')
      const host = body.hostname.toLowerCase()
      state.domain = { app_id: 's1', hostname: host, display: host, verified: state.dns, checked_at: null, error: state.dns ? '' : 'у домена пока нет A-записи на 203.0.113.7' }
      return send(res, 200, state.domain)
    }
    if (p === '/api/cli/v1/apps/s1/server-domain/check') {
      if (!state.domain) return fail(res, 404, 'no_domain')
      if (state.dns) state.domain = { ...state.domain, verified: true, error: '' }
      return send(res, 200, state.domain)
    }
    if (p === '/api/cli/v1/apps/s1/server-domain' && req.method === 'DELETE') {
      if (!state.domain) return fail(res, 404, 'no_domain')
      state.domain = null
      return send(res, 204)
    }
    fail(res, 404, 'not_found')
  })
  await new Promise((ok) => server.listen(0, '127.0.0.1', ok))
  const base = `http://127.0.0.1:${server.address().port}`
  return { state, apps, env: { AIRLOK_URL: base, AIRLOK_TOKEN: 't', XDG_CONFIG_HOME: tmpdir() }, close: () => new Promise((ok) => server.close(ok)) }
}

test('domainState', () => {
  const app = { domain: 'shop.example.ru' }
  assert.equal(domainState(app, undefined), 'none')
  assert.equal(domainState(app, { hostname: 'shop.example.ru', verified: false }), 'waiting_dns')
  assert.equal(domainState({ domain: 'shop.1-2-3-4.sslip.io' }, { hostname: 'shop.example.ru', verified: true }), 'waiting_deploy')
  assert.equal(domainState(app, { hostname: 'shop.example.ru', verified: true }), 'live')
})

test('domain add, check and rm on own server', { timeout: 30_000 }, async (t) => {
  const dash = await fakeDashboard()
  t.after(dash.close)

  const none = captureIO()
  assert.equal(await main(['domain', 'show', '--app', 'shop', '--json'], none, dash.env), 0, none.errLines.join('\n'))
  assert.deepEqual(JSON.parse(none.outLines[0]), { status: 'ok', app: 'shop', address: 'shop.203-0-113-7.sslip.io', domain: null, state: 'none', record: null })

  const add = captureIO()
  assert.equal(await main(['domain', 'add', 'Shop.Example.ru', '--app', 'prod/shop'], add, dash.env), 0, add.errLines.join('\n'))
  const text = add.errLines.join('\n')
  assert.match(text, /ждём DNS — у домена пока нет A-записи/)
  assert.match(text, /A {2}shop\.example\.ru {2}→ {2}203\.0\.113\.7/)
  assert.match(text, /domain check --app prod\/shop/)

  dash.state.dns = true
  const check = captureIO()
  assert.equal(await main(['domain', 'check', '--app', 'shop', '--json'], check, dash.env), 0)
  const out = JSON.parse(check.outLines[0])
  assert.equal(out.state, 'waiting_deploy')
  assert.deepEqual(out.record, { type: 'A', name: 'shop.example.ru', value: '203.0.113.7' })
  assert.match(check.errLines.join('\n'), /включится следующей выкаткой/)

  dash.apps[1].domain = 'shop.example.ru'
  const live = captureIO()
  assert.equal(await main(['domain', 'show', '--app', 'shop'], live, dash.env), 0)
  assert.match(live.errLines.join('\n'), /Сайт работает на shop\.example\.ru/)

  const rm = captureIO()
  assert.equal(await main(['domain', 'rm', '--app', 'shop', '--json'], rm, dash.env), 0)
  assert.deepEqual(JSON.parse(rm.outLines[0]), { status: 'ok', app: 'shop', state: 'none' })
  const again = captureIO()
  assert.equal(await main(['domain', 'rm', '--app', 'shop'], again, dash.env), 1)
  assert.match(again.errLines.join('\n'), /нет своего домена/)
})

test('domain refuses bad input and hosted apps', { timeout: 30_000 }, async (t) => {
  const dash = await fakeDashboard()
  t.after(dash.close)

  const bad = captureIO()
  assert.equal(await main(['domain', 'add', 'ru', '--app', 'shop'], bad, dash.env), 1)
  assert.match(bad.errLines.join('\n'), /Домен не подходит/)

  const hosted = captureIO()
  assert.equal(await main(['domain', 'add', 'landing.example.ru', '--app', 'landing'], hosted, dash.env), 1)
  assert.match(hosted.errLines.join('\n'), /на хостинге.*add_domain/)
  assert.ok(!dash.state.requests.some((r) => r.includes('/apps/h1/')), 'для хостинга в API своих серверов не ходим')

  const noApp = captureIO()
  assert.equal(await main(['domain', 'check'], noApp, dash.env), 1)
  assert.match(noApp.errLines.join('\n'), /Укажите приложение/)
  assert.equal(await main(['domain', 'add', '--app', 'shop'], captureIO(), dash.env), 1)
  assert.equal(await main(['domain', 'list', '--app', 'shop'], captureIO(), dash.env), 1)
})
