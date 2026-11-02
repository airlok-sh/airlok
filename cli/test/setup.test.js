import assert from 'node:assert/strict'
import { mkdir, mkdtemp, readFile, stat, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'

import { saveSession, session } from '../src/config.js'
import { main } from '../src/main.js'
import { agents, mergeServers, setupAgents, upsertTomlServer } from '../src/setup.js'

const URL_ = 'https://app.airlok.ru'
const TOKEN = 'airlok_pat_test'

test('mergeServers adds airlok and keeps everything else', () => {
  const first = mergeServers(null, { url: 'u' }, 'f.json')
  assert.deepEqual(JSON.parse(first), { mcpServers: { airlok: { url: 'u' } } })
  const other = JSON.stringify({ theme: 'dark', mcpServers: { github: { url: 'g' }, airlok: { url: 'old' } } })
  const merged = JSON.parse(mergeServers(other, { url: 'new' }, 'f.json'))
  assert.equal(merged.theme, 'dark')
  assert.deepEqual(merged.mcpServers, { github: { url: 'g' }, airlok: { url: 'new' } })
  assert.equal(mergeServers(mergeServers(other, { url: 'new' }, 'f'), { url: 'new' }, 'f'), mergeServers(other, { url: 'new' }, 'f'), 'повтор ничего не меняет')
  assert.throws(() => mergeServers('{ broken', { url: 'u' }, '/x/mcp.json'), /\/x\/mcp\.json — не JSON/)
})

test('upsertTomlServer replaces only the airlok section', () => {
  const block = '[mcp_servers.airlok]\nurl = "new"'
  const before = [
    'model = "gpt-5"',
    '',
    '[mcp_servers.airlok]',
    'url = "old"',
    '',
    '[mcp_servers.airlok.env]',
    'X = "1"',
    '',
    '[mcp_servers.github]',
    'url = "g"',
    '',
    '[mcp_servers."airlok-docs"]',
    'url = "d"',
  ].join('\n')
  const after = upsertTomlServer(before, block)
  assert.match(after, /model = "gpt-5"/)
  assert.match(after, /\[mcp_servers\.github\]\nurl = "g"/)
  assert.match(after, /\[mcp_servers\."airlok-docs"\]\nurl = "d"/, 'похожее имя не трогаем')
  assert.doesNotMatch(after, /old|X = "1"/)
  assert.equal(after.match(/\[mcp_servers\.airlok\]/g).length, 1)
  assert.equal(upsertTomlServer(after, block), after, 'повтор ничего не меняет')
  assert.equal(upsertTomlServer('', block), block + '\n')
})

async function fakeHome() {
  const home = await mkdtemp(join(tmpdir(), 'airlok-home-'))
  const skills = {}
  for (const name of ['airlok', 'airlok-yml']) {
    skills[name] = join(home, `${name}.md`)
    await writeFile(skills[name], `---\nname: ${name}\ndescription: test\n---\n`)
  }
  return { home, skills }
}

test('setupAgents configures every agent and copies the skill', async () => {
  const { home, skills } = await fakeHome()
  await mkdir(join(home, '.cursor'), { recursive: true })
  await writeFile(join(home, '.cursor', 'mcp.json'), JSON.stringify({ mcpServers: { other: { url: 'o' } } }))
  const calls = []
  const run = async (cmd, args) => calls.push([cmd, ...args])
  const chosen = agents(home).map((a) => ({ ...a, hasBin: a.id === 'claude' }))
  const report = await setupAgents({ chosen, url: URL_, token: TOKEN, run, skills })
  assert.ok(report.every((r) => r.ok), JSON.stringify(report))

  const add = calls.find((c) => c[2] === 'add-json')
  assert.deepEqual(add.slice(0, 4), ['claude', 'mcp', 'add-json', 'airlok'])
  assert.deepEqual(JSON.parse(add[4]), { type: 'http', url: `${URL_}/mcp`, headers: { Authorization: `Bearer ${TOKEN}` } })
  assert.deepEqual(add.slice(5), ['--scope', 'user'])

  const toml = await readFile(join(home, '.codex', 'config.toml'), 'utf8')
  assert.match(toml, /\[mcp_servers\.airlok\]\nurl = "https:\/\/app\.airlok\.ru\/mcp"\nhttp_headers = \{ Authorization = "Bearer airlok_pat_test" \}/)
  assert.equal((await stat(join(home, '.codex', 'config.toml'))).mode & 0o777, 0o600, 'в файле токен — читает только владелец')

  const ag = JSON.parse(await readFile(join(home, '.gemini', 'config', 'mcp_config.json'), 'utf8'))
  assert.deepEqual(ag.mcpServers.airlok, { serverUrl: `${URL_}/mcp`, headers: { Authorization: `Bearer ${TOKEN}` } })

  const cursor = JSON.parse(await readFile(join(home, '.cursor', 'mcp.json'), 'utf8'))
  assert.deepEqual(Object.keys(cursor.mcpServers), ['other', 'airlok'])
  assert.match(await readFile(join(home, '.cursor', 'mcp.json.airlok-backup'), 'utf8'), /"other"/, 'прежний файл сохранён')

  for (const dir of ['.claude/skills', '.agents/skills', '.gemini/config/skills']) {
    assert.match(await readFile(join(home, dir, 'airlok', 'SKILL.md'), 'utf8'), /name: airlok\n/, dir)
    assert.match(await readFile(join(home, dir, 'airlok-yml', 'SKILL.md'), 'utf8'), /name: airlok-yml/, dir)
  }
  await assert.rejects(stat(join(home, '.gemini', 'antigravity-cli')), 'CLI Antigravity не установлен — его каталог не заводим')

  const again = await setupAgents({ chosen, url: URL_, token: TOKEN, run, skills })
  assert.ok(again.every((r) => r.ok))
  assert.equal((await readFile(join(home, '.codex', 'config.toml'), 'utf8')).match(/\[mcp_servers\.airlok\]/g).length, 1)
})

test('setupAgents reports a broken config instead of overwriting it', async () => {
  const { home, skills } = await fakeHome()
  await mkdir(join(home, '.cursor'), { recursive: true })
  await writeFile(join(home, '.cursor', 'mcp.json'), '{ oops')
  const cursor = agents(home).filter((a) => a.id === 'cursor').map((a) => ({ ...a, hasBin: false }))
  const [r] = await setupAgents({ chosen: cursor, url: URL_, token: TOKEN, run: async () => {}, skills })
  assert.equal(r.ok, false)
  assert.match(r.error, /не JSON/)
  assert.equal(await readFile(join(home, '.cursor', 'mcp.json'), 'utf8'), '{ oops')
})

test('airlok setup with a token from the environment', async (t) => {
  const dash = await fakeDashboard({ me: 200 })
  t.after(dash.close)
  const { home } = await fakeHome()
  await mkdir(join(home, '.codex'), { recursive: true })
  const io = { interactive: false, outLines: [], errLines: [], out: (s) => io.outLines.push(s), err: (s) => io.errLines.push(s), ask: async () => '' }
  const env = { HOME: home, PATH: '', AIRLOK_TOKEN: TOKEN, AIRLOK_URL: dash.url }
  assert.equal(await main(['setup'], io, env), 0, io.errLines.join('\n'))
  assert.match(io.errLines.join('\n'), /✓ Codex/)
  assert.match(await readFile(join(home, '.codex', 'config.toml'), 'utf8'), /Bearer airlok_pat_test/)
  assert.match(await readFile(join(home, '.agents', 'skills', 'airlok', 'SKILL.md'), 'utf8'), /name: airlok/, 'настоящий навык из репозитория')
  assert.match(await readFile(join(home, '.agents', 'skills', 'airlok-yml', 'SKILL.md'), 'utf8'), /name: airlok-yml/)

  const none = { ...io, errLines: [] }
  none.err = (s) => none.errLines.push(s)
  assert.equal(await main(['setup'], none, { ...env, HOME: (await fakeHome()).home }), 1)
  assert.match(none.errLines.join('\n'), /--agents/)
  assert.equal(await main(['setup', '--agents', 'vim'], none, env), 1)
})

// fakeDashboard отвечает на проверку токена кодом me и выдаёт новый токен при входе.
async function fakeDashboard({ me }) {
  const server = createServer((req, res) => {
    const send = (code, data) => {
      res.writeHead(code, { 'Content-Type': 'application/json' })
      res.end(JSON.stringify(data))
    }
    req.resume()
    if (req.url === '/api/cli/v1/me') return me === 200 ? send(200, { login: 'anna' }) : send(me, { error: { code: 'x', message: 'x' } })
    if (req.url === '/api/cli/v1/login') return send(200, { device_code: 'd', user_code: 'ABCD', url: 'http://x/cli?code=ABCD', expires_in: 60, interval: 0 })
    if (req.url === '/api/cli/v1/login/token') return send(200, { token: 'airlok_pat_new', login: 'anna' })
    send(404, { error: { code: 'not_found', message: req.url } })
  })
  await new Promise((ok) => server.listen(0, '127.0.0.1', ok))
  return { url: `http://127.0.0.1:${server.address().port}`, close: () => new Promise((ok) => server.close(ok)) }
}

function setupIO() {
  const io = { interactive: false, outLines: [], errLines: [], out: (s) => io.outLines.push(s), err: (s) => io.errLines.push(s), ask: async () => '' }
  return io
}

async function homeWithCodex() {
  const { home } = await fakeHome()
  await mkdir(join(home, '.codex'), { recursive: true })
  return home
}

test('airlok setup logs in again instead of writing a revoked token', async (t) => {
  const dash = await fakeDashboard({ me: 401 })
  t.after(dash.close)
  const home = await homeWithCodex()
  const env = { HOME: home, PATH: '', AIRLOK_URL: dash.url }
  await saveSession({ url: dash.url, token: 'airlok_pat_revoked', login: 'anna' }, env)
  const io = setupIO()
  assert.equal(await main(['setup'], io, env), 0, io.errLines.join('\n'))
  assert.match(io.errLines.join('\n'), /больше не действует/)
  const config = await readFile(join(home, '.codex', 'config.toml'), 'utf8')
  assert.match(config, /Bearer airlok_pat_new/)
  assert.doesNotMatch(config, /airlok_pat_revoked/)
  assert.equal((await session(env)).token, 'airlok_pat_new')
})

test('airlok setup refuses a revoked AIRLOK_TOKEN: login would not replace it', async (t) => {
  const dash = await fakeDashboard({ me: 401 })
  t.after(dash.close)
  const home = await homeWithCodex()
  const io = setupIO()
  assert.equal(await main(['setup'], io, { HOME: home, PATH: '', AIRLOK_URL: dash.url, AIRLOK_TOKEN: 'airlok_pat_revoked' }), 1)
  assert.match(io.errLines.join('\n'), /AIRLOK_TOKEN отозван/)
  await assert.rejects(readFile(join(home, '.codex', 'config.toml'), 'utf8'))
})

test('airlok setup keeps the token when the server is failing', async (t) => {
  const dash = await fakeDashboard({ me: 500 })
  t.after(dash.close)
  const home = await homeWithCodex()
  const env = { HOME: home, PATH: '', AIRLOK_URL: dash.url }
  await saveSession({ url: dash.url, token: TOKEN, login: 'anna' }, env)
  const io = setupIO()
  assert.equal(await main(['setup'], io, env), 0, io.errLines.join('\n'))
  assert.match(await readFile(join(home, '.codex', 'config.toml'), 'utf8'), /Bearer airlok_pat_test/)
})
