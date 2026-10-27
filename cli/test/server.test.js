import assert from 'node:assert/strict'
import { mkdir, mkdtemp, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'

import { main } from '../src/main.js'
import { defaultName, installCommand, parseTarget, sshFailure } from '../src/server.js'

test('parseTarget', () => {
  assert.deepEqual(parseTarget('1.2.3.4'), { user: 'root', host: '1.2.3.4', dest: 'root@1.2.3.4' })
  assert.deepEqual(parseTarget('deploy@my.server.ru'), { user: 'deploy', host: 'my.server.ru', dest: 'deploy@my.server.ru' })
  assert.equal(parseTarget('root@[2a01::1]').host, '2a01::1')
  assert.throws(() => parseTarget(''), /Укажите сервер/)
  assert.throws(() => parseTarget('-oProxyCommand=evil'), /адрес сервера/)
  assert.throws(() => parseTarget('root@-p'), /адрес сервера/)
  assert.throws(() => parseTarget('ro ot@host'), /имя пользователя/)
  assert.throws(() => parseTarget('root@host;rm'), /адрес сервера/)
})

test('defaultName fits the dashboard name rules', () => {
  assert.equal(defaultName('1.2.3.4'), '1.2.3.4')
  assert.equal(defaultName('2a01::1'), '2a01--1')
})

test('installCommand never carries the token and checks what goes into the shell', () => {
  assert.equal(installCommand('https://app.airlok.ru'), 'bash -c "$(curl -fsSL https://airlok.ru/install.sh)" install https://app.airlok.ru')
  assert.equal(
    installCommand('https://app.airlok.ru', { letsencrypt: true, email: 'a@b.ru', keysOnly: true, root: false }),
    'sudo -n env AIRLOK_SSH_CONFIRM=cli bash -c "$(curl -fsSL https://airlok.ru/install.sh)" install https://app.airlok.ru --letsencrypt a@b.ru --ssh-keys-only',
  )
  assert.match(installCommand('http://localhost:8080', { letsencrypt: true }), /install http:\/\/localhost:8080 --letsencrypt$/)
  assert.throws(() => installCommand('https://x.ru/$(id)'), /адрес дашборда/)
  assert.throws(() => installCommand('https://x.ru', { email: 'a@b.ru;id' }), /email/)
})

test('sshFailure', () => {
  assert.equal(sshFailure('root@1.2.3.4: Permission denied (publickey,password).'), 'auth')
  assert.equal(sshFailure('ssh: connect to host 1.2.3.4 port 22: Connection timed out'), 'network')
  assert.equal(sshFailure('WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!'), 'hostkey')
  assert.equal(sshFailure('kex_exchange_identification: banner exchange'), 'other')
})

function captureIO({ tty = false } = {}) {
  const io = { interactive: tty, stdinIsTTY: tty, outLines: [], errLines: [], out: (s) => io.outLines.push(s), err: (s) => io.errLines.push(s) }
  return io
}

async function fakeDashboard(servers = []) {
  const state = { servers, created: [], rotated: [] }
  const server = createServer(async (req, res) => {
    const chunks = []
    for await (const c of req) chunks.push(c)
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks)) : null
    const send = (code, data) => {
      res.writeHead(code, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify(data))
    }
    const p = new URL(req.url, 'http://x').pathname
    if (p === '/api/cli/v1/servers' && req.method === 'GET') return send(200, { servers: state.servers })
    if (p === '/api/cli/v1/servers' && req.method === 'POST') {
      const s = { id: `id-${body.name}`, name: body.name, last_seen_at: null }
      state.servers.push(s)
      state.created.push(body.name)
      return send(201, { server: s, agent_token: 'airlok_srv_secret' })
    }
    const rotate = /^\/api\/cli\/v1\/servers\/([^/]+)\/token$/.exec(p)
    if (rotate) {
      state.rotated.push(rotate[1])
      return send(200, { server: state.servers.find((s) => s.id === rotate[1]), agent_token: 'airlok_srv_rotated' })
    }
    send(404, { error: { code: 'not_found', message: p } })
  })
  await new Promise((ok) => server.listen(0, '127.0.0.1', ok))
  const url = `http://127.0.0.1:${server.address().port}`
  return { state, url, close: () => new Promise((ok) => server.close(ok)) }
}

// fakeSystem играет ssh: ответы по очереди на каждый вызов, а установщик «выводит агента на связь».
function fakeSystem(dash, replies) {
  const calls = []
  return {
    calls,
    async run(cmd, args, { input = '', out } = {}) {
      calls.push({ cmd, args, input })
      const reply = replies.shift() ?? { code: 0, output: '' }
      if (reply.online) for (const s of dash.state.servers) s.last_seen_at = '2026-10-28T10:00:00Z'
      if (out && reply.output) out(Buffer.from(reply.output))
      return { code: reply.code, output: reply.output ?? '' }
    },
    readFile: async () => 'ssh-ed25519 AAAA airlok@test\n',
    sleep: async () => {},
  }
}

async function home() {
  return mkdtemp(join(tmpdir(), 'airlok-home-'))
}

test('server connect with a working key installs without asking anything', async (t) => {
  const dash = await fakeDashboard()
  t.after(dash.close)
  const sys = fakeSystem(dash, [{ code: 0 }, { code: 0, output: 'Сервер готов.', online: true }])
  const io = captureIO()
  const env = { AIRLOK_URL: dash.url, AIRLOK_TOKEN: 't', HOME: await home() }
  const code = await main(['server', 'connect', 'root@1.2.3.4', '--letsencrypt', '--json'], io, env, sys)
  assert.equal(code, 0, io.errLines.join('\n'))
  assert.deepEqual(dash.state.created, ['1.2.3.4'])

  const [probe, install] = sys.calls
  assert.equal(probe.args.at(-1), 'true')
  assert.ok(probe.args.includes('BatchMode=yes'))
  assert.ok(!probe.args.includes('-i'), 'своего ключа airlok ещё нет — пускают ключи человека')
  assert.equal(install.input, 'airlok_srv_secret\n', 'токен идёт через stdin')
  assert.ok(!install.args.join(' ').includes('airlok_srv_'), 'и не попадает в аргументы')
  assert.equal(install.args.at(-1), `bash -c "$(curl -fsSL https://airlok.ru/install.sh)" install ${dash.url} --letsencrypt`)
  assert.deepEqual(install.args.slice(-3, -1), ['--', 'root@1.2.3.4'])

  const res = JSON.parse(io.outLines[0])
  assert.equal(res.status, 'ok')
  assert.equal(res.ssh, 'ssh root@1.2.3.4')
  assert.ok(!io.outLines.concat(io.errLines).join('\n').includes('airlok_srv_secret'))
})

test('server connect without a key refuses to ask for the password outside a terminal', async (t) => {
  const dash = await fakeDashboard()
  t.after(dash.close)
  const sys = fakeSystem(dash, [{ code: 255, output: 'root@1.2.3.4: Permission denied (publickey,password).' }])
  const io = captureIO()
  const code = await main(['server', 'connect', '1.2.3.4'], io, { AIRLOK_URL: dash.url, AIRLOK_TOKEN: 't', HOME: await home() }, sys)
  assert.equal(code, 1)
  assert.match(io.errLines.join('\n'), /пароль здесь ввести негде.*в своём терминале/s)
  assert.equal(sys.calls.length, 1)
  assert.deepEqual(dash.state.created, [], 'сервер в дашборде не заводится раньше времени')
})

test('server connect in a terminal makes a key, puts it on the server and confirms keys-only login', async (t) => {
  const dash = await fakeDashboard()
  t.after(dash.close)
  const dir = await home()
  const sys = fakeSystem(dash, [
    { code: 255, output: 'Permission denied (publickey,password).' },
    { code: 0 }, // ssh-keygen
    { code: 0 }, // ключ на сервер, пароль спросил ssh
    { code: 0 }, // проверка входа по ключу
    { code: 0, output: 'ВАЖНО: вход по паролю выключен.' },
    { code: 0, online: true }, // confirm-ssh
  ])
  const io = captureIO({ tty: true })
  const code = await main(['server', 'connect', 'root@1.2.3.4', '--name', 'prod', '--ssh-keys-only'], io, { AIRLOK_URL: dash.url, AIRLOK_TOKEN: 't', HOME: dir }, sys)
  assert.equal(code, 0, io.errLines.join('\n'))
  const key = join(dir, '.ssh', 'airlok_ed25519')
  assert.deepEqual(sys.calls.map((c) => c.cmd), ['ssh', 'ssh-keygen', 'ssh', 'ssh', 'ssh', 'ssh'])
  assert.equal(sys.calls[1].args.at(-1), key)
  assert.equal(sys.calls[2].input, 'ssh-ed25519 AAAA airlok@test\n')
  assert.ok(!sys.calls[2].args.includes('BatchMode=yes'), 'здесь ssh спрашивает пароль')
  assert.ok(sys.calls[4].args.includes(key))
  assert.match(sys.calls[4].args.at(-1), /^env AIRLOK_SSH_CONFIRM=cli bash .* --ssh-keys-only$/)
  assert.match(sys.calls[5].args.at(-1), /confirm-ssh/)
  assert.deepEqual(dash.state.created, ['prod'])
  assert.match(io.errLines.join('\n'), /Вход по паролю закрыт/)
})

test('server connect retries reuse a server that never came online', async (t) => {
  const dash = await fakeDashboard([{ id: 'old', name: '1.2.3.4', last_seen_at: null }])
  t.after(dash.close)
  const dir = await home()
  await mkdir(join(dir, '.ssh'))
  await writeFile(join(dir, '.ssh', 'airlok_ed25519'), 'key')
  const sys = fakeSystem(dash, [{ code: 0 }, { code: 0, online: true }])
  const io = captureIO()
  assert.equal(await main(['server', 'connect', '1.2.3.4'], io, { AIRLOK_URL: dash.url, AIRLOK_TOKEN: 't', HOME: dir }, sys), 0, io.errLines.join('\n'))
  assert.deepEqual(dash.state.rotated, ['old'])
  assert.deepEqual(dash.state.created, [])
  assert.equal(sys.calls[1].input, 'airlok_srv_rotated\n')
  assert.ok(sys.calls[0].args.includes(join(dir, '.ssh', 'airlok_ed25519')), 'свой ключ airlok уже есть — входим с ним')

  dash.state.servers[0].last_seen_at = '2026-10-28T10:00:00Z'
  const again = captureIO()
  assert.equal(await main(['server', 'connect', '1.2.3.4'], again, { AIRLOK_URL: dash.url, AIRLOK_TOKEN: 't', HOME: dir }, fakeSystem(dash, [{ code: 0 }])), 1)
  assert.match(again.errLines.join('\n'), /уже подключён.*--name/s)
})

test('server connect explains a failed install and a changed host key', async (t) => {
  const dash = await fakeDashboard()
  t.after(dash.close)
  const env = { AIRLOK_URL: dash.url, AIRLOK_TOKEN: 't', HOME: await home() }
  const failed = captureIO()
  assert.equal(await main(['server', 'connect', '1.2.3.4'], failed, env, fakeSystem(dash, [{ code: 0 }, { code: 1, output: 'Ошибка: airlok ставится на Ubuntu' }])), 1)
  assert.match(failed.errLines.join('\n'), /Ошибка: airlok ставится на Ubuntu[\s\S]*Установка не закончилась/)

  const changed = captureIO()
  assert.equal(await main(['server', 'connect', '5.6.7.8'], changed, env, fakeSystem(dash, [{ code: 255, output: 'Host key verification failed.' }])), 1)
  assert.match(changed.errLines.join('\n'), /ssh-keygen -R 5\.6\.7\.8/)
})
