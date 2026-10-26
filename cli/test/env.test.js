import assert from 'node:assert/strict'
import { webcrypto } from 'node:crypto'
import { mkdtemp, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'

import { findApp, parseDotEnv } from '../src/env.js'
import { keyId, seal } from '../src/envseal.js'
import { main } from '../src/main.js'

const { subtle } = webcrypto

async function agentKey() {
  const pair = await subtle.generateKey({ name: 'RSA-OAEP', modulusLength: 3072, publicExponent: new Uint8Array([1, 0, 1]), hash: 'SHA-256' }, true, ['encrypt', 'decrypt'])
  const der = Buffer.from(await subtle.exportKey('spki', pair.publicKey)).toString('base64')
  return { pub: der, priv: pair.privateKey }
}

// open — расшифровка так, как её делает агент: блоки по 384 байта с меткой.
async function open(priv, app, name, sealed) {
  assert.ok(sealed.startsWith('v1:'))
  const raw = Buffer.from(sealed.slice(3), 'base64')
  const n = raw.length / 384
  let out = Buffer.alloc(0)
  for (let i = 0; i < n; i++) {
    const label = new TextEncoder().encode(`airlok-env/v1/${app}/${name}/${i}/${n}`)
    out = Buffer.concat([out, Buffer.from(await subtle.decrypt({ name: 'RSA-OAEP', label }, priv, raw.subarray(i * 384, (i + 1) * 384)))])
  }
  return out.toString('utf8')
}

function captureIO({ stdin = '', tty = false, answers = [] } = {}) {
  const io = {
    interactive: tty, stdinIsTTY: tty, outLines: [], errLines: [],
    out: (s) => io.outLines.push(s),
    err: (s) => io.errLines.push(s),
    readStdin: async () => stdin,
    askSecret: async (q) => {
      io.errLines.push(q)
      return answers.shift() ?? ''
    },
  }
  return io
}

async function fakeDashboard(key) {
  const state = { put: null, hostedPut: null, deleted: [] }
  const apps = [
    { id: 'h1', name: 'shop', hosted: true, server_name: '@hosting' },
    { id: 's1', name: 'shop', hosted: false, server_name: 'prod' },
    { id: 's2', name: 'blog', hosted: false, server_name: 'prod' },
  ]
  const server = createServer(async (req, res) => {
    const chunks = []
    for await (const c of req) chunks.push(c)
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks)) : null
    const send = (code, data) => {
      res.writeHead(code, { 'Content-Type': 'application/json' })
      res.end(data === undefined ? '' : JSON.stringify(data))
    }
    const p = new URL(req.url, 'http://x').pathname
    if (p === '/api/cli/v1/apps') return send(200, { apps })
    if (p === '/api/cli/v1/apps/s1/server-env' || p === '/api/cli/v1/apps/s2/server-env') {
      if (req.method === 'GET') return send(200, { key: state.key, vars: [{ name: 'OLD', stale: true }] })
      state.put = { app: p.split('/')[5], ...body }
      return send(204)
    }
    if (p === '/api/cli/v1/apps/h1/env' && req.method === 'PUT') {
      state.hostedPut = body
      return send(204)
    }
    if (req.method === 'DELETE') {
      state.deleted.push(p)
      return p.endsWith('/NOPE') ? send(404, { error: { code: 'not_found', message: 'no' } }) : send(204)
    }
    send(404, { error: { code: 'not_found', message: p } })
  })
  state.key = { public_key: key.pub, key_id: await keyId(key.pub) }
  await new Promise((ok) => server.listen(0, '127.0.0.1', ok))
  const base = `http://127.0.0.1:${server.address().port}`
  return { state, env: { AIRLOK_URL: base, AIRLOK_TOKEN: 't', XDG_CONFIG_HOME: tmpdir() }, close: () => new Promise((ok) => server.close(ok)) }
}

test('parseDotEnv', () => {
  const { vars, bad } = parseDotEnv('# c\nexport A=1\nB="x\\ny \\"q\\""\nC=\'$raw\'\nD=v # note\nоops\n')
  assert.deepEqual(vars, { A: '1', B: 'x\ny "q"', C: '$raw', D: 'v' })
  assert.deepEqual(bad, [6])
})

test('findApp needs the server when names repeat', () => {
  const apps = [{ name: 'shop', hosted: true }, { name: 'shop', hosted: false, server_name: 'prod' }, { name: 'blog', hosted: false, server_name: 'prod' }]
  assert.equal(findApp(apps, 'blog').name, 'blog')
  assert.equal(findApp(apps, 'prod/shop').hosted, false)
  assert.equal(findApp(apps, 'hosting/shop').hosted, true)
  assert.throws(() => findApp(apps, 'shop'), /hosting\/shop, prod\/shop/)
  assert.throws(() => findApp(apps, 'nope'), /Нет приложения/)
})

test('seal is readable only with the agent key and only for this variable', async () => {
  const key = await agentKey()
  const value = 'ключ ' + 'x'.repeat(700)
  const sealed = await seal(key.pub, 'shop', 'API_KEY', value)
  assert.equal(Buffer.from(sealed.slice(3), 'base64').length, 3 * 384)
  assert.equal(await open(key.priv, 'shop', 'API_KEY', sealed), value)
  await assert.rejects(open(key.priv, 'shop', 'OTHER', sealed))
  await assert.rejects(open(key.priv, 'blog', 'API_KEY', sealed))
  assert.equal(await open(key.priv, 'shop', 'E', await seal(key.pub, 'shop', 'E', '')), '')
})

test('env set encrypts for the server and never sends the value in clear', { timeout: 30_000 }, async (t) => {
  const key = await agentKey()
  const dash = await fakeDashboard(key)
  t.after(dash.close)

  const piped = captureIO({ stdin: 'sk-secret\n' })
  assert.equal(await main(['env', 'set', 'API_KEY', '--app', 'prod/shop'], piped, dash.env), 0, piped.errLines.join('\n'))
  assert.equal(dash.state.put.app, 's1')
  assert.equal(dash.state.put.key_id, await keyId(key.pub))
  assert.ok(!JSON.stringify(dash.state.put).includes('sk-secret'))
  assert.equal(await open(key.priv, 'shop', 'API_KEY', dash.state.put.vars.API_KEY), 'sk-secret', 'перевод строки от echo отрезан')
  assert.match(piped.errLines.join('\n'), /Шифрую под ключ сервера prod: SHA256:/)

  const dir = await mkdtemp(join(tmpdir(), 'airlok-env-'))
  await writeFile(join(dir, '.env'), 'S3_BUCKET=files\nS3_KEY="a b"\n')
  const fromFile = captureIO()
  assert.equal(await main(['env', 'set', '--app', 'blog', '--from', join(dir, '.env'), '--json'], fromFile, dash.env), 0, fromFile.errLines.join('\n'))
  assert.deepEqual(JSON.parse(fromFile.outLines[0]), { status: 'ok', app: 'blog', set: ['S3_BUCKET', 'S3_KEY'] })
  assert.equal(await open(key.priv, 'blog', 'S3_KEY', dash.state.put.vars.S3_KEY), 'a b')

  const asked = captureIO({ tty: true, answers: ['v1', 'v2'] })
  assert.equal(await main(['env', 'set', 'A', 'B', '--app', 'blog'], asked, dash.env), 0)
  assert.deepEqual(Object.keys(dash.state.put.vars), ['A', 'B'])

  const hosted = captureIO({ stdin: 'plain' })
  assert.equal(await main(['env', 'set', 'X', '--app', 'hosting/shop'], hosted, dash.env), 0)
  assert.deepEqual(dash.state.hostedPut, { vars: { X: 'plain' } }, 'на хостинге значения шифрует дашборд')

  const list = captureIO()
  assert.equal(await main(['env', 'list', '--app', 'blog'], list, dash.env), 0)
  assert.match(list.outLines[0], /OLD {2}\(под прежним ключом/)

  const two = captureIO({ stdin: 'x' })
  assert.equal(await main(['env', 'set', 'A', 'B', '--app', 'blog'], two, dash.env), 1)
  assert.match(two.errLines.join('\n'), /одно значение/)

  assert.equal(await main(['env', 'rm', 'NOPE', '--app', 'blog'], captureIO(), dash.env), 1)
  assert.equal(await main(['env', 'rm', 'A', '--app', 'blog'], captureIO(), dash.env), 0)
  assert.ok(dash.state.deleted.includes('/api/cli/v1/apps/s2/server-env/A'))
})

test('env set refuses a key that does not match its fingerprint', { timeout: 30_000 }, async (t) => {
  const key = await agentKey()
  const dash = await fakeDashboard(key)
  t.after(dash.close)
  dash.state.key = { ...dash.state.key, key_id: 'SHA256:other' }
  const io = captureIO({ stdin: 'v' })
  assert.equal(await main(['env', 'set', 'A', '--app', 'blog'], io, dash.env), 1)
  assert.match(io.errLines.join('\n'), /чужим отпечатком/)
  assert.equal(dash.state.put, null)

  dash.state.key = null
  const nokey = captureIO({ stdin: 'v' })
  assert.equal(await main(['env', 'set', 'A', '--app', 'blog'], nokey, dash.env), 1)
  assert.match(nokey.errLines.join('\n'), /ещё не прислал ключ/)
})
