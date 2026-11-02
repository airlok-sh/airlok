import assert from 'node:assert/strict'
import { mkdir, mkdtemp, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'

import { main } from '../src/main.js'
import { defaultName, installCommand, parseTarget, osUpdatesText, sshFailure } from '../src/server.js'

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

async function fakeDashboard(servers = [], { devices = true } = {}) {
  const state = { servers, created: [], rotated: [], devices: [], upgrades: [], osUpdates: null, afterUpgrade: { total: 0, security: 0, reboot_required: false } }
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
    if (devices && p === '/api/cli/v1/devices' && req.method === 'POST') {
      const d = { id: `d-${body.name}`, name: body.name, public_key: body.public_key, server_ids: body.server_ids }
      state.devices.push(d)
      return send(201, d)
    }
    const upgrade = /^\/api\/cli\/v1\/servers\/([^/]+)\/os-upgrade$/.exec(p)
    if (upgrade) {
      if (state.upgradeError) return send(409, { error: { code: state.upgradeError, message: 'x' } })
      const srv = state.servers.find((s) => s.id === upgrade[1])
      const jobId = `job-${state.upgrades.length}`
      state.upgrades.push(body.action)
      srv.os_upgrade = { id: jobId, action: body.action, status: state.upgradeStatus ?? 'success', message: state.upgradeMessage ?? 'Готово: поставлено обновлений: 3' }
      srv.facts = { os_updates: body.action === 'reboot' ? { total: 0, security: 0, reboot_required: false } : state.afterUpgrade }
      return send(202, { id: jobId })
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
      if (reply.online) {
        for (const s of dash.state.servers) {
          s.last_seen_at = '2026-10-28T10:00:00Z'
          s.facts = { os_updates: dash.state.osUpdates ?? { total: 0, security: 0, reboot_required: false } }
        }
      }
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
  assert.ok(!probe.args.includes('-i'), 'своего ключа Airlok ещё нет — пускают ключи человека')
  assert.equal(install.input, 'airlok_srv_secret\n', 'токен идёт через stdin')
  assert.ok(!install.args.join(' ').includes('airlok_srv_'), 'и не попадает в аргументы')
  assert.equal(install.args.at(-1), `bash -c "$(curl -fsSL https://airlok.ru/install.sh)" install ${dash.url} --letsencrypt`)
  assert.deepEqual(install.args.slice(-3, -1), ['--', 'root@1.2.3.4'])

  const res = JSON.parse(io.outLines[0])
  assert.equal(res.status, 'ok')
  assert.equal(res.ssh, 'ssh root@1.2.3.4')
  assert.equal(res.device, dash.state.devices[0].name)
  assert.deepEqual(dash.state.devices.map((d) => [d.public_key, d.server_ids]), [['ssh-ed25519 AAAA', ['id-1.2.3.4']]],
    'ключ этого компьютера — устройство с доступом к новому серверу')
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
  assert.match(io.errLines.join('\n'), /устройство «.+»/)
})

test('server connect works with a dashboard that has no devices yet', async (t) => {
  const dash = await fakeDashboard([], { devices: false })
  t.after(dash.close)
  const sys = fakeSystem(dash, [{ code: 0 }, { code: 0, online: true }])
  const io = captureIO()
  assert.equal(await main(['server', 'connect', '1.2.3.4', '--json'], io, { AIRLOK_URL: dash.url, AIRLOK_TOKEN: 't', HOME: await home() }, sys), 0)
  assert.equal(JSON.parse(io.outLines[0]).device, '')
  assert.doesNotMatch(io.errLines.join('\n'), /не добавился/)
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
  assert.ok(sys.calls[0].args.includes(join(dir, '.ssh', 'airlok_ed25519')), 'свой ключ Airlok уже есть — входим с ним')

  dash.state.servers[0].last_seen_at = '2026-10-28T10:00:00Z'
  const again = captureIO()
  assert.equal(await main(['server', 'connect', '1.2.3.4'], again, { AIRLOK_URL: dash.url, AIRLOK_TOKEN: 't', HOME: dir }, fakeSystem(dash, [{ code: 0 }])), 1)
  assert.match(again.errLines.join('\n'), /уже подключён.*--name/s)
})

test('server connect finds a renamed server by its address', async (t) => {
  const dash = await fakeDashboard([{ id: 's1', name: 'Основной', public_ip: '1.2.3.4', last_seen_at: '2026-10-28T10:00:00Z' }])
  t.after(dash.close)
  const env = { AIRLOK_URL: dash.url, AIRLOK_TOKEN: 't', HOME: await home() }
  const io = captureIO()
  const sys = fakeSystem(dash, [{ code: 0 }])
  assert.equal(await main(['server', 'connect', 'root@1.2.3.4'], io, env, sys), 1)
  assert.match(io.errLines.join('\n'), /1\.2\.3\.4 под именем Основной уже подключён.*--name/s)
  assert.deepEqual(dash.state.created, [])
  assert.deepEqual(dash.state.rotated, [])
  assert.equal(sys.calls.length, 1, 'до установки дело не доходит')
})

test('server connect --name still adds a separate server at the same address', async (t) => {
  const dash = await fakeDashboard([{ id: 's1', name: 'Основной', public_ip: '1.2.3.4', last_seen_at: '2026-10-28T10:00:00Z' }])
  t.after(dash.close)
  const env = { AIRLOK_URL: dash.url, AIRLOK_TOKEN: 't', HOME: await home() }
  const io = captureIO()
  const code = await main(['server', 'connect', 'root@1.2.3.4', '--name', 'second', '--json'], io, env, fakeSystem(dash, [{ code: 0 }, { code: 0, online: true }]))
  assert.equal(code, 0, io.errLines.join('\n'))
  assert.deepEqual(dash.state.created, ['second'])
  assert.equal(JSON.parse(io.outLines[0]).server, 'second')
})

test('server connect --name retries reuse a server that never came online', async (t) => {
  const dash = await fakeDashboard([{ id: 'old', name: 'second', last_seen_at: null }])
  t.after(dash.close)
  const env = { AIRLOK_URL: dash.url, AIRLOK_TOKEN: 't', HOME: await home() }
  const io = captureIO()
  const code = await main(['server', 'connect', 'root@1.2.3.4', '--name', 'second'], io, env, fakeSystem(dash, [{ code: 0 }, { code: 0, online: true }]))
  assert.equal(code, 0, io.errLines.join('\n'))
  assert.deepEqual(dash.state.rotated, ['old'])
  assert.deepEqual(dash.state.created, [])
})

test('server connect explains a failed install and a changed host key', async (t) => {
  const dash = await fakeDashboard()
  t.after(dash.close)
  const env = { AIRLOK_URL: dash.url, AIRLOK_TOKEN: 't', HOME: await home() }
  const failed = captureIO()
  assert.equal(await main(['server', 'connect', '1.2.3.4'], failed, env, fakeSystem(dash, [{ code: 0 }, { code: 1, output: 'Ошибка: Airlok ставится на Ubuntu' }])), 1)
  assert.match(failed.errLines.join('\n'), /Ошибка: Airlok ставится на Ubuntu[\s\S]*Установка не закончилась/)

  const changed = captureIO()
  assert.equal(await main(['server', 'connect', '5.6.7.8'], changed, env, fakeSystem(dash, [{ code: 255, output: 'Host key verification failed.' }])), 1)
  assert.match(changed.errLines.join('\n'), /ssh-keygen -R 5\.6\.7\.8/)
})

async function connectWith(t, { osUpdates, answers = [], tty = false, args = [], ...state }) {
  const dash = await fakeDashboard()
  t.after(dash.close)
  Object.assign(dash.state, { osUpdates }, state)
  const io = captureIO({ tty })
  const asked = []
  io.ask = async (q) => {
    asked.push(q)
    return answers.shift() ?? ''
  }
  const sys = fakeSystem(dash, [{ code: 0 }, { code: 0, output: 'Сервер готов.', online: true }])
  const code = await main(['server', 'connect', 'root@1.2.3.4', ...args], io, { AIRLOK_URL: dash.url, AIRLOK_TOKEN: 't', HOME: await home() }, sys)
  return { dash, io, asked, code, err: io.errLines.join('\n') }
}

test('osUpdatesText counts in plain words', () => {
  assert.equal(osUpdatesText({ total: 0, security: 0 }), '')
  assert.match(osUpdatesText({ total: 1, security: 0 }), /не поставлено 1 обновление системы\.$/)
  assert.match(osUpdatesText({ total: 3, security: 2 }), /не поставлены 3 обновления системы, из них исправлений безопасности: 2\.$/)
  assert.match(osUpdatesText({ total: 12, security: 0 }), /не поставлено 12 обновлений/)
  assert.match(osUpdatesText({ total: 84, security: 0 }), /не поставлены 84 обновления/)
})

test('server connect offers updates in a terminal and installs only after yes', async (t) => {
  const { dash, asked, err, code } = await connectWith(t, { tty: true, answers: ['y'], osUpdates: { total: 3, security: 1, reboot_required: false } })
  assert.equal(code, 0, err)
  assert.deepEqual(dash.state.upgrades, ['upgrade'])
  assert.equal(asked.length, 1, 'перезагрузка не нужна — о ней не спрашиваем')
  assert.match(err, /не поставлены 3 обновления системы, из них исправлений безопасности: 1/)
  assert.match(err, /✓ Готово: поставлено обновлений: 3/)
})

test('server connect in a terminal leaves updates alone when the answer is not yes', async (t) => {
  for (const answer of ['', 'n', 'нет']) {
    const { dash, code } = await connectWith(t, { tty: true, answers: [answer], osUpdates: { total: 3, security: 1, reboot_required: false } })
    assert.equal(code, 0)
    assert.deepEqual(dash.state.upgrades, [], `ответ «${answer}»`)
  }
})

test('server connect asks about the reboot separately and never reboots by itself', async (t) => {
  const needsReboot = { total: 0, security: 0, reboot_required: true }
  const declined = await connectWith(t, { tty: true, answers: ['y', 'n'], osUpdates: { total: 3, security: 0, reboot_required: false }, afterUpgrade: needsReboot })
  assert.deepEqual(declined.dash.state.upgrades, ['upgrade'])
  assert.match(declined.err, /server reboot 1\.2\.3\.4/)

  const agreed = await connectWith(t, { tty: true, answers: ['y', 'да'], osUpdates: { total: 3, security: 0, reboot_required: false }, afterUpgrade: needsReboot })
  assert.deepEqual(agreed.dash.state.upgrades, ['upgrade', 'reboot'])

  const onlyReboot = await connectWith(t, { tty: true, answers: ['y'], osUpdates: needsReboot })
  assert.deepEqual(onlyReboot.dash.state.upgrades, ['reboot'])
})

test('server connect does not offer the reboot after a failed upgrade', async (t) => {
  const { dash, asked, err, code } = await connectWith(t, {
    tty: true, answers: ['y'], osUpdates: { total: 3, security: 0, reboot_required: false }, upgradeStatus: 'failed', upgradeMessage: 'на диске кончилось место',
    afterUpgrade: { total: 3, security: 0, reboot_required: true },
  })
  assert.equal(code, 0, 'сам сервер подключён')
  assert.equal(asked.length, 1)
  assert.deepEqual(dash.state.upgrades, ['upgrade'])
  assert.match(err, /✗ Не получилось: на диске кончилось место/)
})

test('server connect without a terminal only reports updates', async (t) => {
  const { dash, io, asked, err } = await connectWith(t, { args: ['--json'], osUpdates: { total: 3, security: 1, reboot_required: true } })
  assert.deepEqual(asked, [])
  assert.deepEqual(dash.state.upgrades, [])
  assert.match(err, /server upgrade 1\.2\.3\.4/)
  assert.deepEqual(JSON.parse(io.outLines[0]).os_updates, { total: 3, security: 1, reboot_required: true })
})

test('server connect with nothing to install says so and asks nothing', async (t) => {
  const { asked, err } = await connectWith(t, { tty: true, osUpdates: { total: 0, security: 0, reboot_required: false } })
  assert.deepEqual(asked, [])
  assert.match(err, /Обновления системы уже стоят/)
})

test('server upgrade reports the result and that a reboot is needed', async (t) => {
  const dash = await fakeDashboard([{ id: 's1', name: 'prod', last_seen_at: '2026-10-28T10:00:00Z' }])
  t.after(dash.close)
  dash.state.afterUpgrade = { total: 0, security: 0, reboot_required: true }
  const io = captureIO()
  const env = { AIRLOK_URL: dash.url, AIRLOK_TOKEN: 't', HOME: await home() }
  assert.equal(await main(['server', 'upgrade', 'prod', '--json'], io, env, fakeSystem(dash, [])), 0, io.errLines.join('\n'))
  assert.deepEqual(dash.state.upgrades, ['upgrade'])
  assert.equal(JSON.parse(io.outLines[0]).status, 'ok')
  assert.match(io.errLines.join('\n'), /server reboot prod/)

  dash.state.upgradeStatus = 'failed'
  assert.equal(await main(['server', 'reboot', 'prod'], captureIO(), env, fakeSystem(dash, [])), 1)
  assert.deepEqual(dash.state.upgrades, ['upgrade', 'reboot'])
})

test('server upgrade explains a busy or offline server and an unknown name', async (t) => {
  const dash = await fakeDashboard([{ id: 's1', name: 'prod', last_seen_at: null }])
  t.after(dash.close)
  const env = { AIRLOK_URL: dash.url, AIRLOK_TOKEN: 't', HOME: await home() }
  const run = async (name) => {
    const io = captureIO()
    assert.equal(await main(['server', 'upgrade', name], io, env, fakeSystem(dash, [])), 1)
    return io.errLines.join('\n')
  }
  dash.state.upgradeError = 'server_offline'
  assert.match(await run('prod'), /не на связи/)
  dash.state.upgradeError = 'os_upgrade_pending'
  assert.match(await run('prod'), /уже идёт/)
  assert.match(await run('nope'), /Сервера nope нет/)
})
