import assert from 'node:assert/strict'
import { mkdir, mkdtemp, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'

import { deviceName, parsePublicKey } from '../src/device.js'
import { main } from '../src/main.js'
import { system } from '../src/server.js'

const KEY = 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl'

function captureIO() {
  const io = { interactive: false, stdinIsTTY: false, outLines: [], errLines: [], out: (s) => io.outLines.push(s), err: (s) => io.errLines.push(s) }
  return io
}

// fakeDashboard отвечает как /api/cli/v1: ключ и имя устройства уникальны, как в дашборде.
async function fakeDashboard() {
  const state = {
    servers: [{ id: 'srv1', name: 'prod', last_seen_at: '2026-10-30T10:00:00Z' }, { id: 'srv2', name: 'stage', last_seen_at: null }],
    devices: [],
  }
  const send = (res, code, data) => {
    res.writeHead(code, { 'Content-Type': 'application/json' })
    res.end(data === undefined ? '' : JSON.stringify(data))
  }
  const server = createServer(async (req, res) => {
    const chunks = []
    for await (const c of req) chunks.push(c)
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks)) : null
    const p = new URL(req.url, 'http://x').pathname
    if (p === '/api/cli/v1/servers') return send(res, 200, { servers: state.servers })
    if (p === '/api/cli/v1/devices' && req.method === 'GET') return send(res, 200, { devices: state.devices, access: [] })
    if (p === '/api/cli/v1/devices' && req.method === 'POST') {
      if (state.devices.some((d) => d.public_key === body.public_key)) return send(res, 409, { error: { code: 'key_taken', message: 'key' } })
      if (state.devices.some((d) => d.name === body.name)) return send(res, 409, { error: { code: 'name_taken', message: 'name' } })
      const d = { id: `d${state.devices.length + 1}`, name: body.name, key_type: 'ssh-ed25519', fingerprint: 'SHA256:x', public_key: body.public_key, disabled: false, server_ids: body.server_ids }
      state.devices.push(d)
      return send(res, 201, d)
    }
    const servers = /^\/api\/cli\/v1\/devices\/([^/]+)\/servers$/.exec(p)
    if (servers && req.method === 'PUT') {
      state.devices.find((d) => d.id === servers[1]).server_ids = body.server_ids
      return send(res, 204)
    }
    send(res, 404, { error: { code: 'not_found', message: p } })
  })
  await new Promise((ok) => server.listen(0, '127.0.0.1', ok))
  return { state, url: `http://127.0.0.1:${server.address().port}`, close: () => new Promise((ok) => server.close(ok)) }
}

async function homeWithKey(file = 'id_ed25519.pub', text = `${KEY} dmitry@mac\n`) {
  const dir = await mkdtemp(join(tmpdir(), 'airlok-home-'))
  await mkdir(join(dir, '.ssh'))
  if (file) await writeFile(join(dir, '.ssh', file), text)
  return dir
}

test('parsePublicKey keeps only the type and the key', () => {
  assert.equal(parsePublicKey(`  ${KEY}   dmitry@mac  \n`), KEY)
  assert.throws(() => parsePublicKey('-----BEGIN OPENSSH PRIVATE KEY-----\nb3Bl\n-----END OPENSSH PRIVATE KEY-----'), /закрытый/)
  assert.throws(() => parsePublicKey(`${KEY}\n${KEY}`), /нет открытого/)
  assert.throws(() => parsePublicKey(`command="sh" ${KEY}`), /нет открытого/)
  assert.throws(() => parsePublicKey('ssh-dss AAAA'), /нет открытого/)
})

test('deviceName fits the dashboard name rules', () => {
  assert.equal(deviceName('Dmitrys-MacBook-Pro.local'), 'Dmitrys-MacBook-Pro')
  assert.equal(deviceName('--box/1'), 'box-1')
  assert.equal(deviceName('***'), 'Компьютер')
})

test('device add finds the key, names the device after the computer and opens servers', async (t) => {
  const dash = await fakeDashboard()
  t.after(dash.close)
  const home = await homeWithKey()
  const env = { AIRLOK_URL: dash.url, AIRLOK_TOKEN: 't', HOME: home }
  const io = captureIO()
  assert.equal(await main(['device', 'add', '--name', 'Рабочий макбук', '--server', 'prod', '--json'], io, env), 0, io.errLines.join('\n'))
  assert.deepEqual(dash.state.devices.map((d) => [d.name, d.public_key, d.server_ids]), [['Рабочий макбук', KEY, ['srv1']]])
  assert.deepEqual(JSON.parse(io.outLines[0]), {
    status: 'ok', device: 'Рабочий макбук', id: 'd1', created: true, key: join(home, '.ssh', 'id_ed25519.pub'), servers: ['prod'],
  })

  const again = captureIO()
  assert.equal(await main(['device', 'add', '--server', 'stage'], again, env), 0, again.errLines.join('\n'))
  assert.equal(dash.state.devices.length, 1, 'тот же ключ — то же устройство')
  assert.deepEqual(dash.state.devices[0].server_ids, ['srv1', 'srv2'])
  assert.match(again.errLines.join('\n'), /уже добавлен как «Рабочий макбук»/)

  const list = captureIO()
  assert.equal(await main(['device', 'list'], list, env), 0)
  assert.match(list.outLines.join('\n'), /Рабочий макбук\n {2}SHA256:x\n {2}серверы: prod, stage/)
})

test('device add refuses what is not a public key', async (t) => {
  const dash = await fakeDashboard()
  t.after(dash.close)
  const env = { AIRLOK_URL: dash.url, AIRLOK_TOKEN: 't', HOME: await homeWithKey(null) }

  const none = captureIO()
  assert.equal(await main(['device', 'add'], none, env), 1)
  assert.match(none.errLines.join('\n'), /ssh-keygen -t ed25519/)

  const reads = []
  const sys = { ...system, readFile: async (p) => (reads.push(p), 'secret') }
  const priv = captureIO()
  assert.equal(await main(['device', 'add', '--key', '/home/me/.ssh/id_ed25519'], priv, env, sys), 1)
  assert.match(priv.errLines.join('\n'), /файл \.pub/)
  assert.deepEqual(reads, [], 'закрытый ключ даже не читается')

  const unknown = captureIO()
  const home = await homeWithKey()
  assert.equal(await main(['device', 'add', '--server', 'prod,dev'], unknown, { ...env, HOME: home }), 1)
  assert.match(unknown.errLines.join('\n'), /Нет таких серверов: dev\. Есть: prod, stage/)
  assert.deepEqual(dash.state.devices, [])
})
