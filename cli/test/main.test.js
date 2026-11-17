import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { mkdir, mkdtemp, readFile, stat, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import { gunzipSync } from 'node:zlib'

import { ApiError, USER_AGENT } from '../src/api.js'
import { saveSession } from '../src/config.js'
import { agentToken, appLimitText, duration, errorText, logUpdate, main, projectFiles, stageUpdates, suggestName } from '../src/main.js'
import { untar } from './untar.js'

test('errorText explains what a token from the dashboard cannot do', () => {
  const text = errorText(new ApiError(403, 'cli_login_required', 'this action needs a CLI login'))
  assert.match(text, /npx @airlok\/cli login/)
  assert.match(text, /AIRLOK_TOKEN/)
})

test('agentToken gives AI agents their own token, not the CLI login', async () => {
  const io = captureIO()
  const fake = (result) => ({
    token: 'airlok_pat_login',
    createAgentToken: async (client) => {
      if (result instanceof Error) throw result
      return { name: `ИИ-агенты ${client}`, token: result }
    },
  })
  assert.equal(await agentToken(fake('airlok_pat_agents'), 'mac', io), 'airlok_pat_agents')
  assert.equal(await agentToken(fake(new ApiError(403, 'cli_login_required', 'нет')), 'mac', io), 'airlok_pat_login',
    'AIRLOK_TOKEN уже токен для агентов — его и пишем')
  assert.deepEqual(io.errLines, [])
  assert.equal(await agentToken(fake(new ApiError(404, 'not_found', 'нет')), 'mac', io), 'airlok_pat_login', 'старый дашборд')
  assert.match(io.errLines.join('\n'), /записываю им вход CLI/)
  await assert.rejects(agentToken(fake(new ApiError(0, 'network', 'нет связи')), 'mac', io), /нет связи/)
})

test('duration reads like the dashboard', () => {
  assert.equal(duration(0.3), '<1 с')
  assert.equal(duration(28), '28 с')
  assert.equal(duration(72), '1 мин 12 с')
  assert.equal(duration(120), '2 мин')
})

test('suggestName makes an app name from a folder', () => {
  assert.equal(suggestName('My Shop'), 'my-shop')
  assert.equal(suggestName('2024-site'), 'site')
  assert.equal(suggestName('Проект'), 'app')
})

test('stageUpdates prints each stage state once', () => {
  const seen = new Map()
  const t = (s) => new Date(Date.UTC(2026, 9, 22, 10, 0, s)).toISOString()
  assert.deepEqual(stageUpdates(seen, [{ key: 'source', status: 'running', note: 'архив · 3f9a2c1', started_at: t(0) }, { key: 'detect', status: 'pending' }]), [
    '  → Код           архив · 3f9a2c1',
  ])
  assert.deepEqual(stageUpdates(seen, [{ key: 'source', status: 'running', started_at: t(0) }]), [])
  assert.deepEqual(stageUpdates(seen, [
    { key: 'source', status: 'done', note: 'архив · 3f9a2c1', started_at: t(0), finished_at: t(2) },
    { key: 'install', status: 'skipped', note: 'не нужны' },
  ]), ['  ✓ Код           архив · 3f9a2c1  · 2 с', '  – Зависимости   не нужны'])
})

test('logUpdate prints only new whole lines while the log window slides', () => {
  let u = logUpdate('', 'a\nb\nпол')
  assert.deepEqual(u.lines, ['a', 'b'], 'недописанная строка ждёт')
  u = logUpdate(u.shown, 'a\nb\nполная\nc\n')
  assert.deepEqual(u.lines, ['полная', 'c'])
  assert.deepEqual(logUpdate(u.shown, 'a\nb\nполная\nc\n').lines, [], 'без новых строк — ничего')
  u = logUpdate(u.shown, 'b\nполная\nc\nd\n')
  assert.deepEqual(u.lines, ['d'], 'начало лога отрезано — продолжаем с того же места')
  assert.deepEqual(logUpdate(u.shown, 'x\ny\n').lines, ['…', 'x', 'y'], 'за опрос лог ушёл дальше окна — помечаем пропуск')
})

// fakeDashboard — сервер с API для CLI: вход подтверждается со второго опроса, сборка
// проходит этапы и завершается успехом.
async function fakeDashboard() {
  const state = { polls: 0, created: [], archive: null, title: '', commandPolls: 0, temporary: [], tempUntil: new Date(Date.now() + 3600_000).toISOString() }
  const app = { id: 'a1', name: 'shop', domain: 'shop.airlok.site', hosted: true }
  const server = createServer(async (req, res) => {
    const chunks = []
    for await (const c of req) chunks.push(c)
    const body = Buffer.concat(chunks)
    const url = new URL(req.url, 'http://x')
    const send = (code, data) => {
      res.writeHead(code, { 'Content-Type': 'application/json' })
      res.end(data === undefined ? '' : JSON.stringify(data))
    }
    const authed = req.headers.authorization === 'Bearer airlok_pat_test'
    // Сайт без входа: создать можно без токена, обновить и следить — только токеном самого сайта.
    const site = { app: 'tmp-k3f9qz', url: 'https://tmp-k3f9qz.airlok.site', expires_at: state.tempUntil }
    if (url.pathname === '/api/cli/v1/temporary') {
      state.temporary.push({ method: req.method, auth: req.headers.authorization ?? '', title: url.searchParams.get('title') })
      if (req.method === 'POST') return send(201, { ...site, token: 'tmp_token', claim_path: '/claim/tmp_token', command: { id: 't1', status: 'queued' } })
      if (req.headers.authorization !== 'Bearer tmp_token') return send(404, { error: { code: 'temporary_not_found', message: 'нет' } })
      return send(202, { ...site, command: { id: 't2', status: 'queued' } })
    }
    if (url.pathname.startsWith('/api/cli/v1/temporary/commands/')) {
      if (req.headers.authorization !== 'Bearer tmp_token') return send(404, { error: { code: 'temporary_not_found', message: 'нет' } })
      const at = '2026-11-17T10:00:00Z'
      return send(200, { id: url.pathname.split('/').pop(), status: 'success', started_at: at, finished_at: '2026-11-17T10:00:09Z',
        stages: [{ key: 'source', status: 'done', started_at: at, finished_at: at }] })
    }
    if (req.method === 'POST' && url.pathname === '/api/cli/v1/login') {
      return send(201, { device_code: 'dev', user_code: 'BCDF-GHJK', url: `${state.base}/cli?code=BCDF-GHJK`, expires_in: 60, interval: 0 })
    }
    if (req.method === 'POST' && url.pathname === '/api/cli/v1/login/token') {
      return ++state.polls < 2 ? send(202, { status: 'pending' }) : send(200, { token: 'airlok_pat_test', login: 'dmitry', name: 'Дмитрий' })
    }
    if (!authed) return send(401, { error: { code: 'unauthorized', message: 'no' } })
    if (url.pathname === '/api/cli/v1/me') return send(200, { login: 'dmitry', name: 'Дмитрий' })
    if (req.method === 'GET' && url.pathname === '/api/cli/v1/apps') return send(200, { apps: [app, { id: 's1', name: 'vps-app', hosted: false }, ...state.created] })
    if (req.method === 'POST' && url.pathname === '/api/cli/v1/apps') {
      const { name } = JSON.parse(body)
      state.created.push({ id: 'a2', name, domain: `${name}.airlok.site`, hosted: true })
      return send(201, { id: 'a2' })
    }
    if (req.method === 'POST' && url.pathname.endsWith('/archive')) {
      state.archive = body
      state.title = url.searchParams.get('title')
      state.archiveApp = url.pathname.split('/')[5]
      return send(202, { id: 'c1', status: 'queued' })
    }
    if (url.pathname === '/api/cli/v1/commands/c1') {
      const n = ++state.commandPolls
      const at = '2026-10-22T10:00:00Z'
      if (n === 1) return send(200, { id: 'c1', status: 'queued', stages: [] })
      if (state.noStart) {
        const built = [{ key: 'source', status: 'done', started_at: at, finished_at: '2026-10-22T10:00:02Z' }]
        if (n === 2) return send(200, { id: 'c1', status: 'running', stages: [...built, { key: 'release', status: 'running', note: 'запускаем новую версию', started_at: at }] })
        return send(200, {
          id: 'c1', status: 'failed', message: 'Новая версия не запустилась. Сайт работает на прежней версии.', log: 'npm ci\nadded 3 packages\n',
          stages: [...built, { key: 'release', status: 'failed', started_at: at, finished_at: '2026-10-22T10:01:00Z' }],
        })
      }
      if (n === 2) return send(200, { id: 'c1', status: 'running', stages: [{ key: 'source', status: 'running', started_at: at }], log: '→ Код\nnpm ci\n' })
      return send(200, {
        id: 'c1', status: 'success', started_at: at, finished_at: '2026-10-22T10:00:48Z', log: '→ Код\nnpm ci\nadded 3 packages\n',
        stages: [{ key: 'source', status: 'done', started_at: at, finished_at: '2026-10-22T10:00:02Z' }, { key: 'release', status: 'done', started_at: at, finished_at: at }],
      })
    }
    send(404, { error: { code: 'not_found', message: url.pathname } })
  })
  await new Promise((ok) => server.listen(0, '127.0.0.1', ok))
  state.base = `http://127.0.0.1:${server.address().port}`
  return { state, close: () => server.close() }
}

function captureIO(answers = []) {
  const io = { interactive: answers.length > 0, outLines: [], errLines: [] }
  io.out = (s) => io.outLines.push(s)
  io.err = (s) => io.errLines.push(s)
  io.ask = async () => answers.shift() ?? ''
  return io
}

test('login, then deploy a folder into a new app and follow the build', { timeout: 30_000 }, async (t) => {
  const dash = await fakeDashboard()
  t.after(dash.close)
  const home = await mkdtemp(join(tmpdir(), 'airlok-home-'))
  const env = { XDG_CONFIG_HOME: home, AIRLOK_URL: dash.state.base }

  const io = captureIO()
  assert.equal(await main(['login'], io, env), 0)
  const cfgPath = join(home, 'airlok', 'config.json')
  const saved = JSON.parse(await readFile(cfgPath, 'utf8'))
  assert.equal(saved.token, 'airlok_pat_test')
  assert.equal((await stat(cfgPath)).mode & 0o777, 0o600, 'токен читает только владелец')
  assert.match(io.errLines.join('\n'), /BCDF-GHJK/)

  const who = captureIO()
  assert.equal(await main(['whoami'], who, env), 0)
  assert.match(who.outLines[0], /^dmitry \(Дмитрий\)/)

  const dir = await mkdtemp(join(tmpdir(), 'My Shop '))
  await writeFile(join(dir, 'index.html'), '<h1>hi</h1>')
  await writeFile(join(dir, '.env'), 'SECRET=1')

  const notLinked = captureIO()
  assert.equal(await main(['deploy', dir], notLinked, env), 1)
  assert.match(notLinked.errLines.join('\n'), /--app <имя> или --new <имя>/, 'без терминала CLI не спрашивает, а объясняет флаги')

  const dep = captureIO(['2', 'promo'])
  assert.equal(await main(['deploy', dir, '--title', 'Первая'], dep, env), 0)
  assert.equal(dash.state.archiveApp, 'a2', 'публикация ушла в созданное приложение')
  assert.equal(dash.state.title, 'Первая')
  assert.deepEqual(untar(gunzipSync(dash.state.archive)).map((e) => e.path), ['src/index.html'], '.env в архив не попал')
  const log = dep.errLines.join('\n')
  assert.match(log, /Ждём сборщика/)
  assert.match(log, /✓ Код {11}· 2 с/)
  assert.deepEqual(dep.errLines.filter((l) => l.startsWith('    ')), ['    → Код', '    npm ci', '    added 3 packages'], 'строки лога — по мере появления, без повторов')
  assert.match(log, /✓ Опубликовано за 48 с: https:\/\/promo\.airlok\.site/)
  assert.deepEqual(dep.outLines, ['https://promo.airlok.site'])

  const link = JSON.parse(await readFile(join(dir, '.airlok', 'project.json'), 'utf8'))
  assert.deepEqual(link, { app_id: 'a2', app: 'promo' })

  // Повторная выкатка идёт в привязанное приложение без вопросов, итог — одной строкой JSON.
  dash.state.commandPolls = 2
  const again = captureIO()
  assert.equal(await main(['deploy', dir, '--json'], again, env), 0)
  assert.equal(dash.state.archiveApp, 'a2')
  assert.deepEqual(JSON.parse(again.outLines.at(-1)), { status: 'success', command_id: 'c1', app: 'promo', url: 'https://promo.airlok.site' })
  assert.equal(again.errLines.filter((l) => l.startsWith('    ')).length, 0, 'с --json лог не печатается: его читает ИИ-агент')

  // Собралась, но не запустилась: выкатка ждёт итога запуска, а причину надо искать в логах приложения.
  dash.state.commandPolls = 0
  dash.state.noStart = true
  const broken = captureIO()
  assert.equal(await main(['deploy', dir, '--json'], broken, env), 1)
  assert.equal(JSON.parse(broken.outLines.at(-1)).failed_stage, 'release')
  const brokenLog = broken.errLines.join('\n')
  assert.match(brokenLog, /→ Публикация {4}запускаем новую версию/)
  assert.match(brokenLog, /✗ Новая версия не запустилась/)
  assert.match(brokenLog, /Логи приложения: .*\/apps\/a2\?tab=logs/)
  assert.doesNotMatch(brokenLog, /Конец лога/, 'лог сборки тут ни при чём: она прошла')

  assert.equal(await main(['logout'], captureIO(), env), 0)
  const after = captureIO()
  assert.equal(await main(['whoami'], after, env), 1)
  assert.match(after.errLines.join('\n'), /Вы не вошли/)
})

test('appLimitText tells a missing plan from a full one', () => {
  assert.match(appLimitText('the current plan has no hosting: trial is not available: link Yandex ID to start the trial'), /settings\?tab=plan/)
  assert.match(appLimitText('hosting allows up to 3 apps per user'), /максимум приложений/)
})

test('projectFiles takes data files only with consent and refuses the home folder', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'airlok-data-'))
  await writeFile(join(dir, 'index.html'), '<h1>hi</h1>')
  await writeFile(join(dir, 'app.db'), 'd')
  await writeFile(join(dir, 'server.key'), 'k')
  const env = { HOME: await mkdtemp(join(tmpdir(), 'airlok-home-')) }
  const paths = (r) => r.files.map((f) => f.path)

  const quiet = captureIO()
  const r = await projectFiles(dir, {}, quiet, env)
  assert.deepEqual(paths(r), ['index.html'])
  assert.deepEqual(r.excluded, { secrets: ['server.key'], data: ['app.db'] })
  assert.match(quiet.errLines.join('\n'), /Не загружаю базы.*app\.db.*--include-data/)

  const flagged = await projectFiles(dir, { 'include-data': true }, captureIO(), env)
  assert.deepEqual(paths(flagged), ['index.html', 'app.db'])
  assert.deepEqual(flagged.excluded, { secrets: ['server.key'], data: [] })

  assert.deepEqual(paths(await projectFiles(dir, {}, captureIO(['да']), env)), ['index.html', 'app.db'], 'человек согласился')
  assert.deepEqual(paths(await projectFiles(dir, {}, captureIO(['n']), env)), ['index.html'])
  assert.deepEqual(paths(await projectFiles(dir, { json: true }, captureIO(['да']), env)), ['index.html'], 'с --json не спрашиваем')

  await assert.rejects(projectFiles(dir, {}, captureIO(), { HOME: dir }), /домашняя/)
  await assert.rejects(projectFiles(join(dir, '..'), {}, captureIO(), { HOME: dir }), /домашняя/)
})

test('deploy --temporary publishes without login and then updates the same site', { timeout: 30_000 }, async (t) => {
  const dash = await fakeDashboard()
  t.after(dash.close)
  const home = await mkdtemp(join(tmpdir(), 'airlok-home-'))
  const env = { XDG_CONFIG_HOME: home, AIRLOK_URL: dash.state.base }
  const dir = await mkdtemp(join(tmpdir(), 'airlok-temp-'))
  await writeFile(join(dir, 'index.html'), '<h1>hi</h1>')

  const first = captureIO()
  assert.equal(await main(['deploy', dir, '--temporary', '--json'], first, env), 0, first.errLines.join('\n'))
  const out = JSON.parse(first.outLines.at(-1))
  assert.equal(out.status, 'success')
  assert.equal(out.url, 'https://tmp-k3f9qz.airlok.site')
  assert.equal(out.claim_url, `${dash.state.base}/claim/tmp_token`)
  assert.equal(out.temporary, true)
  assert.deepEqual(dash.state.temporary[0], { method: 'POST', auth: '', title: `Папка ${dir.split('/').pop()}` }, 'без входа — без токена')
  const saved = JSON.parse(await readFile(join(dir, '.airlok', 'temporary.json'), 'utf8'))
  assert.equal(saved.token, 'tmp_token')
  assert.equal((await stat(join(dir, '.airlok', 'temporary.json'))).mode & 0o777, 0o600, 'токен сайта читает только владелец')

  const again = captureIO()
  assert.equal(await main(['deploy', dir, '--temporary'], again, env), 0)
  assert.deepEqual(dash.state.temporary[1], { method: 'PUT', auth: 'Bearer tmp_token', title: `Папка ${dir.split('/').pop()}` }, 'тот же сайт обновляется его токеном')
  assert.deepEqual(again.outLines, ['https://tmp-k3f9qz.airlok.site'])
  assert.match(again.errLines.join('\n'), /Чтобы оставить его себе, откройте ссылку и войдите:\n.*\/claim\/tmp_token/)

  const both = captureIO()
  assert.equal(await main(['deploy', dir, '--temporary', '--app', 'shop'], both, env), 1)
})

test('deploy --new creates no app for a folder it cannot publish', { timeout: 30_000 }, async (t) => {
  const dash = await fakeDashboard()
  t.after(dash.close)
  const env = { XDG_CONFIG_HOME: await mkdtemp(join(tmpdir(), 'airlok-home-')), AIRLOK_URL: dash.state.base, AIRLOK_TOKEN: 'airlok_pat_test' }
  const linked = async (dir) => Boolean(await stat(join(dir, '.airlok', 'project.json')).catch(() => null))

  const empty = await mkdtemp(join(tmpdir(), 'airlok-empty-'))
  const io = captureIO()
  assert.equal(await main(['deploy', empty, '--new', 'mysite'], io, env), 1)
  assert.match(io.errLines.join('\n'), /нет файлов проекта/)

  const heavy = await mkdtemp(join(tmpdir(), 'airlok-heavy-'))
  await writeFile(join(heavy, 'video.bin'), randomBytes(51 << 20))
  const big = captureIO()
  assert.equal(await main(['deploy', heavy, '--new', 'mysite'], big, env), 1)
  assert.match(big.errLines.join('\n'), /больше 50 МБ/)

  assert.deepEqual(dash.state.created, [], 'приложение не создано')
  assert.equal(await linked(empty), false)
  assert.equal(await linked(heavy), false)
})

test('deploy --new publishes a build folder from .gitignore and then points to the linked app', { timeout: 30_000 }, async (t) => {
  const dash = await fakeDashboard()
  t.after(dash.close)
  dash.state.commandPolls = 2
  const env = { XDG_CONFIG_HOME: await mkdtemp(join(tmpdir(), 'airlok-home-')), AIRLOK_URL: dash.state.base, AIRLOK_TOKEN: 'airlok_pat_test' }
  const repo = await mkdtemp(join(tmpdir(), 'airlok-repo-'))
  await writeFile(join(repo, '.gitignore'), 'dist\n')
  await mkdir(join(repo, 'dist', 'assets'), { recursive: true })
  await writeFile(join(repo, 'dist', 'index.html'), '<h1>hi</h1>')
  await writeFile(join(repo, 'dist', 'assets', 'app.js'), 'x')
  execFileSync('git', ['init', '-q'], { cwd: repo })
  const dist = join(repo, 'dist')

  const io = captureIO()
  assert.equal(await main(['deploy', dist, '--new', 'mysite', '--json'], io, env), 0)
  assert.match(io.errLines.join('\n'), /dist в \.gitignore/)
  assert.deepEqual(untar(gunzipSync(dash.state.archive)).map((e) => e.path), ['src/assets/app.js', 'src/index.html'])
  assert.equal(dash.state.created.length, 1)

  const again = captureIO()
  assert.equal(await main(['deploy', dist, '--new', 'mysite'], again, env), 1)
  assert.match(again.errLines.join('\n'), /mysite уже создано и привязано к этой папке/)
  const other = captureIO()
  assert.equal(await main(['deploy', repo, '--new', 'shop'], other, env), 1)
  assert.match(other.errLines.join('\n'), /--app shop/)
  assert.equal(dash.state.created.length, 1, 'повтор с --new не создаёт приложение')
})

// tokenDashboard записывает запросы: вход выдаёт airlok_pat_new и говорит, заменил ли прежний вход.
async function tokenDashboard({ revoke = 204 } = {}) {
  const state = { requests: [] }
  const server = createServer(async (req, res) => {
    const chunks = []
    for await (const c of req) chunks.push(c)
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks)) : null
    state.requests.push({ method: req.method, path: req.url, auth: req.headers.authorization ?? '', ua: req.headers['user-agent'], body })
    const send = (code, data) => {
      res.writeHead(code, { 'Content-Type': 'application/json' })
      res.end(data === undefined ? '' : JSON.stringify(data))
    }
    if (req.url === '/api/cli/v1/login') return send(201, { device_code: 'dev', user_code: 'BCDF-GHJK', url: 'http://x/cli', expires_in: 60, interval: 0 })
    if (req.url === '/api/cli/v1/login/token') return send(200, { token: 'airlok_pat_new', login: 'anna', replaced: Boolean(body.previous_token) })
    if (req.method === 'DELETE' && req.url === '/api/cli/v1/token') return send(revoke, revoke === 204 ? undefined : { error: { code: 'x', message: 'сбой' } })
    send(404, { error: { code: 'not_found', message: req.url } })
  })
  await new Promise((ok) => server.listen(0, '127.0.0.1', ok))
  return { state, url: `http://127.0.0.1:${server.address().port}`, close: () => new Promise((ok) => server.close(ok)) }
}

test('login replaces the previous login of this computer, but shows it only to its own dashboard', async (t) => {
  const dash = await tokenDashboard()
  t.after(dash.close)
  const home = await mkdtemp(join(tmpdir(), 'airlok-home-'))
  const env = { XDG_CONFIG_HOME: home, AIRLOK_URL: dash.url }

  await saveSession({ url: dash.url, token: 'airlok_pat_old', login: 'anna' }, env)
  const io = captureIO()
  assert.equal(await main(['login'], io, env), 0)
  const poll = dash.state.requests.find((r) => r.path === '/api/cli/v1/login/token')
  assert.equal(poll.body.previous_token, 'airlok_pat_old')
  assert.match(io.errLines.join('\n'), /Прежний вход этого компьютера отозван/)
  assert.equal(poll.ua, USER_AGENT)
  assert.match(USER_AGENT, /^airlok-cli\/\d+\.\d+\.\d+ node\/\S+ \S+$/, 'по версии дашборд видит, какие CLI ещё в ходу')

  dash.state.requests = []
  await saveSession({ url: 'https://other.example', token: 'airlok_pat_other', login: 'anna' }, env)
  const other = captureIO()
  assert.equal(await main(['login'], other, env), 0)
  const fresh = dash.state.requests.find((r) => r.path === '/api/cli/v1/login/token')
  assert.equal(fresh.body.previous_token, undefined, 'токен другого дашборда сюда не уходит')
  assert.doesNotMatch(other.errLines.join('\n'), /Прежний вход/)
})

test('logout revokes the saved login and keeps tokens it did not issue', async (t) => {
  const dash = await tokenDashboard()
  t.after(dash.close)
  const home = await mkdtemp(join(tmpdir(), 'airlok-home-'))
  const env = { XDG_CONFIG_HOME: home, AIRLOK_URL: dash.url }
  const cfg = join(home, 'airlok', 'config.json')
  const deletes = () => dash.state.requests.filter((r) => r.method === 'DELETE')

  await saveSession({ url: dash.url, token: 'airlok_pat_saved', login: 'anna' }, env)
  const io = captureIO()
  assert.equal(await main(['logout'], io, { ...env, AIRLOK_TOKEN: 'airlok_pat_ci' }), 0)
  assert.deepEqual(deletes().map((r) => r.auth), ['Bearer airlok_pat_saved'], 'отзывается сохранённый вход, а не AIRLOK_TOKEN')
  await assert.rejects(stat(cfg))
  assert.match(io.errLines.join('\n'), /токен входа отозван[\s\S]*AIRLOK_TOKEN по-прежнему действует/)

  dash.state.requests = []
  assert.equal(await main(['logout'], captureIO(), { ...env, AIRLOK_TOKEN: 'airlok_pat_ci' }), 0)
  assert.deepEqual(deletes(), [], 'без сохранённого входа отзывать нечего')

  await saveSession({ url: dash.url, token: 'airlok_pat_saved', login: 'anna' }, env)
  const keep = captureIO()
  assert.equal(await main(['logout', '--keep-token'], keep, env), 0)
  assert.deepEqual(deletes(), [])
  await assert.rejects(stat(cfg))
  assert.match(keep.errLines.join('\n'), /токен действует/)
})

test('login and logout keep a token that AI agents still use', async (t) => {
  const dash = await tokenDashboard()
  t.after(dash.close)
  const home = await mkdtemp(join(tmpdir(), 'airlok-home-'))
  const env = { XDG_CONFIG_HOME: home, HOME: home, AIRLOK_URL: dash.url }
  // Так setup CLI до 0.8.0 записывал агенту сам вход CLI.
  await mkdir(join(home, '.cursor'), { recursive: true })
  await writeFile(join(home, '.cursor', 'mcp.json'), JSON.stringify({ mcpServers: { airlok: { url: dash.url + '/mcp', headers: { Authorization: 'Bearer airlok_pat_old' } } } }))

  await saveSession({ url: dash.url, token: 'airlok_pat_old', login: 'anna' }, env)
  const io = captureIO()
  assert.equal(await main(['login'], io, env), 0)
  const poll = dash.state.requests.find((r) => r.path === '/api/cli/v1/login/token')
  assert.equal(poll.body.previous_token, undefined, 'токен агентов не отзывается при новом входе')
  assert.match(io.errLines.join('\n'), /Прежний вход не отозван[\s\S]*~\/\.cursor\/mcp\.json[\s\S]*setup/)

  await saveSession({ url: dash.url, token: 'airlok_pat_old', login: 'anna' }, env)
  const deletes = () => dash.state.requests.filter((r) => r.method === 'DELETE')
  const out = captureIO()
  assert.equal(await main(['logout'], out, env), 1)
  assert.deepEqual(deletes(), [], 'без --force токен агентов не отзывается')
  assert.match(out.errLines.join('\n'), /записан в настройках ИИ-агентов[\s\S]*--force/)
  await stat(join(home, 'airlok', 'config.json'))

  assert.equal(await main(['logout', '--force'], captureIO(), env), 0)
  assert.deepEqual(deletes().map((r) => r.auth), ['Bearer airlok_pat_old'])
})

test('logout keeps the login when the token could not be revoked', async (t) => {
  for (const [revoke, code, kept] of [[500, 1, true], [404, 0, false], [401, 0, false]]) {
    const dash = await tokenDashboard({ revoke })
    t.after(dash.close)
    const home = await mkdtemp(join(tmpdir(), 'airlok-home-'))
    const env = { XDG_CONFIG_HOME: home, AIRLOK_URL: dash.url }
    await saveSession({ url: dash.url, token: 'airlok_pat_saved', login: 'anna' }, env)
    const io = captureIO()
    assert.equal(await main(['logout'], io, env), code, `${revoke}: ${io.errLines.join('\n')}`)
    assert.equal(Boolean(await stat(join(home, 'airlok', 'config.json')).catch(() => null)), kept, `${revoke}`)
    if (revoke === 500) assert.match(io.errLines.join('\n'), /--keep-token/)
    if (revoke === 404) assert.match(io.errLines.join('\n'), /отзовите его в дашборде/, 'старый дашборд без отзыва')
  }
})
