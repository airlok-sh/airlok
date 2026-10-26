import { execFile, spawn } from 'node:child_process'
import { readFile, stat } from 'node:fs/promises'
import { homedir, hostname } from 'node:os'
import { basename, resolve } from 'node:path'
import { createInterface } from 'node:readline/promises'
import { parseArgs, promisify } from 'node:util'

import { Api, ApiError } from './api.js'
import { forgetSession, readLink, saveLink, saveSession, session } from './config.js'
import { formatReport } from './check.js'
import { EnvError, envCommand } from './env.js'
import { checkFiles, listFiles, pack, PackError } from './pack.js'
import { agents, detect, homePath, onPath, setupAgents, SKILLS } from './setup.js'

const run = promisify(execFile)
const POLL_MS = 2000
const FOLLOW_LIMIT_MS = 30 * 60_000

const help = `airlok — выкатка проекта на хостинг airlok из терминала

  npx @airlok/cli setup             один раз: вход и подключение ИИ-агентов (Claude Code, Codex, Antigravity, Cursor)
  npx @airlok/cli login             войти через браузер
  npx @airlok/cli deploy [папка]    собрать и выкатить папку (по умолчанию текущую)
  npx @airlok/cli check [папка]     что airlok сделает с проектом и что помешает выкатке — без сборки
  npx @airlok/cli env list --app <имя>          переменные окружения приложения (без значений)
  npx @airlok/cli env set ИМЯ… --app <имя>      задать: значение спросит без показа на экране или прочитает из stdin
  npx @airlok/cli env set --app <имя> --from .env   задать все переменные из файла
  npx @airlok/cli env rm ИМЯ --app <имя>        удалить
  npx @airlok/cli whoami            под кем выполнен вход
  npx @airlok/cli logout            забыть вход на этом компьютере

Для deploy:
  --app <имя>       выкатить в существующее приложение
  --new <имя>       создать приложение с этим именем и выкатить
  --title <текст>   название выкатки (по умолчанию — последний коммит)
  --no-wait         не ждать конца сборки
  --json            итог одной строкой JSON — для скриптов и ИИ-агентов

Для env:
  --app <имя>       приложение; если имя есть на нескольких серверах — <сервер>/<имя>
  --from <файл>     взять значения из .env-файла (с именами — только эти)
  У приложения на своём сервере значения шифруются здесь, под ключ агента сервера: дашборд их не видит.

Для setup:
  --agents <список> каких агентов настроить: claude,codex,antigravity,cursor (по умолчанию — найденных)
  --yes             не спрашивать подтверждения

Переменные окружения: AIRLOK_TOKEN — токен вместо входа (для CI), AIRLOK_URL — адрес дашборда.`

export const stageLabel = { source: 'Код', detect: 'Стек', install: 'Зависимости', build: 'Сборка', image: 'Образ', release: 'Выкатка' }

export function duration(sec) {
  const s = Math.max(0, Math.round(sec))
  if (sec < 1) return '<1 с'
  if (s < 60) return `${s} с`
  return s % 60 ? `${Math.floor(s / 60)} мин ${s % 60} с` : `${s / 60} мин`
}

const seconds = (s, now) => (s.started_at ? ((s.finished_at ? Date.parse(s.finished_at) : now) - Date.parse(s.started_at)) / 1000 : null)

// stageUpdates — строки о том, что изменилось в этапах с прошлого опроса; seen помнит,
// какое состояние каждого этапа уже напечатано.
export function stageUpdates(seen, stages, now = Date.now()) {
  const lines = []
  for (const s of stages ?? []) {
    if (s.status === 'pending' || seen.get(s.key) === s.status) continue
    // Короткий этап может начаться и закончиться между опросами: печатаем только итог.
    seen.set(s.key, s.status)
    const label = stageLabel[s.key] ?? s.key
    const note = s.note ? `  ${s.note}` : ''
    const sec = seconds(s, now)
    const took = sec !== null && s.status !== 'running' ? `  · ${duration(sec)}` : ''
    const mark = { running: '→', done: '✓', failed: '✗', skipped: '–' }[s.status] ?? '·'
    lines.push(`  ${mark} ${label.padEnd(12)}${note}${took}`.trimEnd())
  }
  return lines
}

// suggestName — имя приложения из имени папки: «My Shop» → my-shop.
export function suggestName(folder) {
  const name = folder.toLowerCase().replace(/[^a-z0-9-]+/g, '-').replace(/^[^a-z]+/, '').replace(/-+$/, '').slice(0, 28).replace(/-+$/, '')
  return name.length >= 2 ? name : 'app'
}

const APP_NAME = /^[a-z][a-z0-9-]{1,27}$/

function makeIO(stdin = process.stdin, stdout = process.stdout, stderr = process.stderr) {
  return {
    interactive: Boolean(stdin.isTTY && stderr.isTTY),
    stdinIsTTY: Boolean(stdin.isTTY),
    out: (s) => stdout.write(s + '\n'),
    err: (s) => stderr.write(s + '\n'),
    async readStdin() {
      const chunks = []
      for await (const c of stdin) chunks.push(c)
      return Buffer.concat(chunks).toString('utf8')
    },
    // askSecret — значение без эха: секрет не должен остаться на экране или в записи терминала.
    askSecret(question) {
      return new Promise((done, fail) => {
        stderr.write(question)
        let value = ''
        const finish = (err) => {
          stdin.off('data', onData)
          stdin.setRawMode(false)
          stdin.pause()
          stderr.write('\n')
          if (err) fail(err)
          else done(value)
        }
        const onData = (chunk) => {
          for (const ch of chunk) {
            if (ch === '\r' || ch === '\n') return finish()
            if (ch === '\u0003') return finish(new UserError('Отменено.'))
            if (ch === '\u007f' || ch === '\b') value = Array.from(value).slice(0, -1).join('')
            else if (ch >= ' ') value += ch
          }
        }
        stdin.setRawMode(true)
        stdin.setEncoding('utf8')
        stdin.on('data', onData)
        stdin.resume()
      })
    },
    async ask(question) {
      const rl = createInterface({ input: stdin, output: stderr })
      try {
        return (await rl.question(question)).trim()
      } finally {
        rl.close()
      }
    },
  }
}

export async function main(argv, io = makeIO(), env = process.env) {
  let parsed
  try {
    parsed = parseArgs({
      args: argv,
      allowPositionals: true,
      options: {
        app: { type: 'string' }, new: { type: 'string' }, title: { type: 'string' }, agents: { type: 'string' }, yes: { type: 'boolean', short: 'y' },
        from: { type: 'string' },
        'no-wait': { type: 'boolean' }, json: { type: 'boolean' }, help: { type: 'boolean', short: 'h' }, version: { type: 'boolean', short: 'v' },
      },
    })
  } catch (err) {
    io.err(`${err.message}\n\n${help}`)
    return 2
  }
  const { values: opts, positionals } = parsed
  const command = positionals[0]
  try {
    if (opts.version) {
      io.out(await version())
      return 0
    }
    if (opts.help || !command || command === 'help') {
      io.out(help)
      return 0
    }
    switch (command) {
      case 'login':
        await login(io, env)
        return 0
      case 'logout':
        await forgetSession(env)
        io.err('Вход на этом компьютере забыт. Сам токен можно отозвать в дашборде: Настройки → токены.')
        return 0
      case 'whoami': {
        const s = await session(env)
        if (!s.token) throw new UserError('Вы не вошли: npx @airlok/cli login')
        const me = await new Api(s.url, s.token).me()
        io.out(`${me.login}${me.name ? ` (${me.name})` : ''} · ${s.url}`)
        return 0
      }
      case 'deploy':
        return await deploy(positionals[1] ?? '.', opts, io, env)
      case 'check':
        return await check(positionals[1] ?? '.', opts, io, env)
      case 'setup':
        return await setup(opts, io, env)
      case 'env': {
        const s = await session(env)
        if (!s.token) throw new UserError('Вы не вошли: npx @airlok/cli login (или задайте AIRLOK_TOKEN)')
        return await envCommand(new Api(s.url, s.token), positionals[1], positionals.slice(2), opts, io)
      }
      default:
        io.err(`Нет такой команды: ${command}\n\n${help}`)
        return 2
    }
  } catch (err) {
    if (opts.json) io.out(JSON.stringify({ status: 'error', message: err.message }))
    io.err(errorText(err))
    return 1
  }
}

class UserError extends Error {}

function errorText(err) {
  if (err instanceof UserError || err instanceof PackError || err instanceof EnvError) return err.message
  if (err instanceof ApiError) {
    if (err.status === 401) return 'Вход устарел или токен отозван: npx @airlok/cli login'
    if (err.code === 'read_only_token') return 'Этот токен только читает. Войдите заново (npx @airlok/cli login) или задайте токен с правом выкатывать.'
    return err.message
  }
  return `Ошибка: ${err.message}`
}

async function version() {
  const pkg = JSON.parse(await readFile(new URL('../package.json', import.meta.url), 'utf8'))
  return pkg.version
}

async function login(io, env) {
  const s = await session(env)
  const api = new Api(s.url)
  const start = await api.startLogin(hostname())
  io.err(`Откройте ссылку и подтвердите код ${start.user_code}:\n\n  ${start.url}\n`)
  // Открываем только адрес своего дашборда, а не то, что прислали.
  if (io.interactive && start.url.startsWith(`${s.url}/cli?code=`)) openBrowser(start.url)
  const deadline = Date.now() + start.expires_in * 1000
  while (Date.now() < deadline) {
    await sleep(start.interval * 1000)
    const done = await api.pollLogin(start.device_code)
    if (!done) continue
    const path = await saveSession({ url: s.url, token: done.token, login: done.login }, env)
    io.err(`Готово: вы вошли как ${done.login}. Токен сохранён в ${path}`)
    return
  }
  throw new UserError('Код входа истёк: запустите npx @airlok/cli login ещё раз.')
}

function openBrowser(url) {
  const [cmd, args] = process.platform === 'darwin' ? ['open', [url]] : process.platform === 'win32' ? ['cmd', ['/c', 'start', '', url]] : ['xdg-open', [url]]
  try {
    spawn(cmd, args, { stdio: 'ignore', detached: true }).on('error', () => {}).unref()
  } catch {
    // Браузер не открылся — ссылка уже напечатана.
  }
}

// setup — вход и подключение агентов: MCP-сервер airlok с токеном и навык airlok.
async function setup(opts, io, env) {
  let s = await session(env)
  if (!s.token) {
    await login(io, env)
    s = await session(env)
  }
  const home = env.HOME || env.USERPROFILE || homedir()
  const list = agents(home)
  let chosen
  if (opts.agents) {
    const ids = opts.agents.split(',').map((x) => x.trim()).filter(Boolean)
    const unknown = ids.filter((id) => !list.some((a) => a.id === id))
    if (unknown.length) throw new UserError(`Не знаю таких агентов: ${unknown.join(', ')}. Есть: ${list.map((a) => a.id).join(', ')}.`)
    chosen = []
    for (const a of list.filter((a) => ids.includes(a.id))) chosen.push({ ...a, hasBin: await onPath(a.bin, env) })
  } else {
    chosen = await detect(list, env)
    if (chosen.length === 0) {
      throw new UserError(`Не нашёл ни одного агента. Укажите явно: --agents ${list.map((a) => a.id).join(',')}`)
    }
  }
  io.err(`Подключу к airlok (${s.url}): ${chosen.map((a) => a.label).join(', ')}.`)
  if (io.interactive && !opts.yes) {
    const answer = (await io.ask('Продолжить? [Y/n] ')).toLowerCase()
    if (answer && !['y', 'yes', 'д', 'да'].includes(answer)) return 0
  }
  const report = await setupAgents({ chosen, url: s.url, token: s.token, home })
  for (const r of report) {
    if (r.ok) io.err(`  ✓ ${r.agent.padEnd(12)} MCP: ${homePath(r.mcp, home)} · навыки ${SKILLS.join(', ')}: ${r.skills.map((d) => homePath(d, home)).join(', ')}`)
    else io.err(`  ✗ ${r.agent.padEnd(12)} ${r.error}`)
  }
  io.err('\nПерезапустите агентов, чтобы они увидели airlok. Проверка: спросите агента «какие у меня приложения на airlok?».')
  return report.every((r) => r.ok) ? 0 : 1
}

async function deploy(target, opts, io, env) {
  const dir = resolve(target)
  if (!(await stat(dir).catch(() => null))?.isDirectory()) throw new UserError(`Нет такой папки: ${dir}`)
  let s = await session(env)
  if (!s.token) {
    if (!io.interactive) throw new UserError('Вы не вошли: npx @airlok/cli login (или задайте AIRLOK_TOKEN)')
    await login(io, env)
    s = await session(env)
  }
  const api = new Api(s.url, s.token)
  const app = await chooseApp(api, dir, opts, io)

  const { files } = await listFiles(dir)
  const problem = checkFiles(files)
  if (problem) throw new UserError(problem)
  io.err(`Упаковываю ${files.length} ${plural(files.length, 'файл', 'файла', 'файлов')}…`)
  const archive = await pack(dir, files)
  const title = opts.title || (await gitTitle(dir)) || `Папка ${basename(dir)}`
  io.err(`Загружаю ${mb(archive.length)} в ${app.name}…`)
  const cmd = await api.deployArchive(app.id, archive, title)
  if (opts['no-wait']) {
    if (opts.json) io.out(JSON.stringify({ status: cmd.status, command_id: cmd.id, app: app.name }))
    else io.err(`Выкатка поставлена в очередь. Ход сборки — в дашборде: ${s.url}/apps/${app.id}?tab=deploys`)
    return 0
  }
  const result = await follow(api, cmd.id, io)
  const url = app.domain ? `https://${app.domain}` : ''
  if (opts.json) {
    io.out(JSON.stringify({ status: result.status, command_id: cmd.id, app: app.name, url, message: result.message }))
  }
  if (result.status === 'success') {
    const took = result.started_at && result.finished_at ? ` за ${duration((Date.parse(result.finished_at) - Date.parse(result.started_at)) / 1000)}` : ''
    io.err(`\n✓ Выкачено${took}${url ? `: ${url}` : ''}`)
    if (!opts.json && url) io.out(url)
    return 0
  }
  if (result.status === 'canceled') {
    io.err('\nЭту выкатку заменила более новая.')
    return 1
  }
  io.err(`\n✗ ${result.message || 'Сборка не удалась'}`)
  const tail = (result.log ?? '').trimEnd().split('\n').slice(-25)
  if (tail.length > 1) io.err(`\nКонец лога:\n${tail.map((l) => `  ${l}`).join('\n')}`)
  io.err(`\nПолный лог: ${s.url}/apps/${app.id}?tab=deploys`)
  return 1
}

// check упаковывает папку так же, как deploy, и печатает отчёт дашборда. Код 1 — выкатка не пройдёт.
async function check(target, opts, io, env) {
  const dir = resolve(target)
  if (!(await stat(dir).catch(() => null))?.isDirectory()) throw new UserError(`Нет такой папки: ${dir}`)
  const s = await session(env)
  if (!s.token) throw new UserError('Вы не вошли: npx @airlok/cli login (или задайте AIRLOK_TOKEN)')
  const { files } = await listFiles(dir)
  const problem = checkFiles(files)
  if (problem) throw new UserError(problem)
  if (!opts.json) io.err(`Проверяю ${files.length} ${plural(files.length, 'файл', 'файла', 'файлов')}: архив проверяется на сервере и сразу удаляется…`)
  const report = await new Api(s.url, s.token).check(await pack(dir, files))
  const ok = report.errors.length === 0
  if (opts.json) io.out(JSON.stringify({ status: ok ? 'ok' : 'error', ...report }))
  else io.out(formatReport(report))
  return ok ? 0 : 1
}

// chooseApp — куда выкатывать: из флагов, из привязки папки или по вопросу человеку.
async function chooseApp(api, dir, opts, io) {
  const apps = await api.apps()
  let app
  if (opts.new) {
    app = await create(api, opts.new)
  } else if (opts.app) {
    app = apps.find((a) => a.name === opts.app)
    if (!app) throw new UserError(`На хостинге нет приложения ${opts.app}. Создать новое: --new ${opts.app}`)
  } else {
    const link = await readLink(dir)
    const linked = link ? apps.find((a) => a.id === link.app_id) : undefined
    if (linked) return linked
    if (!io.interactive) throw new UserError('Папка ещё не привязана к приложению: укажите --app <имя> или --new <имя>.')
    app = await ask(api, apps, dir, io)
  }
  const path = await saveLink(dir, { app_id: app.id, app: app.name })
  io.err(`Папка привязана к ${app.name} (${path}): дальше хватит npx @airlok/cli deploy.`)
  return app
}

async function ask(api, apps, dir, io) {
  if (apps.length > 0) {
    io.err('Куда выкатить эту папку?')
    apps.forEach((a, i) => io.err(`  ${i + 1}. ${a.name}${a.domain ? `  ${a.domain}` : ''}`))
    io.err(`  ${apps.length + 1}. новое приложение`)
    const pick = Number(await io.ask(`Номер [${apps.length + 1}]: `)) || apps.length + 1
    if (pick >= 1 && pick <= apps.length) return apps[pick - 1]
  }
  const suggested = suggestName(basename(dir))
  for (;;) {
    const name = (await io.ask(`Имя нового приложения [${suggested}]: `)) || suggested
    try {
      return await create(api, name)
    } catch (err) {
      if (!(err instanceof UserError)) throw err
      io.err(err.message)
    }
  }
}

async function create(api, name) {
  if (!APP_NAME.test(name)) throw new UserError('Имя: латиница в нижнем регистре, цифры и дефис, от 2 до 28 символов, начинается с буквы.')
  try {
    const id = await api.createApp(name)
    const app = (await api.apps()).find((a) => a.id === id)
    return app ?? { id, name, domain: '' }
  } catch (err) {
    if (err instanceof ApiError && err.code === 'name_taken') throw new UserError(`Имя ${name} уже занято на хостинге — выберите другое.`)
    if (err instanceof ApiError && err.code === 'app_limit') throw new UserError('На хостинге уже максимум приложений. Удалите ненужное в дашборде.')
    throw err
  }
}

// follow печатает ход сборки, пока она не закончится.
async function follow(api, id, io) {
  const seen = new Map()
  let queued = false
  const deadline = Date.now() + FOLLOW_LIMIT_MS
  for (;;) {
    const cmd = await api.command(id)
    if (cmd.status === 'queued' && !queued) {
      queued = true
      io.err('Ждём сборщика…')
    }
    for (const line of stageUpdates(seen, cmd.stages)) io.err(line)
    if (cmd.status !== 'queued' && cmd.status !== 'running') return cmd
    if (Date.now() > deadline) throw new UserError('Сборка идёт дольше 30 минут — следите за ней в дашборде.')
    await sleep(POLL_MS)
  }
}

// gitTitle — заголовок последнего коммита, а если есть незакоммиченные правки — с пометкой.
async function gitTitle(dir) {
  try {
    const { stdout: subject } = await run('git', ['log', '-1', '--format=%s'], { cwd: dir })
    const { stdout: dirty } = await run('git', ['status', '--porcelain'], { cwd: dir })
    return subject.trim() + (dirty.trim() ? ' + незакоммиченные правки' : '')
  } catch {
    return ''
  }
}

const sleep = (ms) => new Promise((ok) => setTimeout(ok, ms))
const mb = (n) => (n < 1 << 20 ? `${Math.max(1, Math.round(n / 1024))} КБ` : `${(n / (1 << 20)).toFixed(1).replace('.', ',')} МБ`)

function plural(n, one, few, many) {
  const m10 = n % 10
  const m100 = n % 100
  if (m10 === 1 && m100 !== 11) return one
  if (m10 >= 2 && m10 <= 4 && (m100 < 12 || m100 > 14)) return few
  return many
}
