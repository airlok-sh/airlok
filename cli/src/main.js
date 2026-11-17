import { execFile, spawn } from 'node:child_process'
import { readFile, realpath, stat } from 'node:fs/promises'
import { homedir, hostname } from 'node:os'
import { basename, resolve } from 'node:path'
import { createInterface } from 'node:readline/promises'
import { parseArgs, promisify } from 'node:util'

import { Api, ApiError } from './api.js'
import { forgetSession, readLink, readTemporary, savedLogin, saveLink, saveSession, saveTemporary, session } from './config.js'
import { formatReport, normalize } from './check.js'
import { DeviceError, deviceCommand } from './device.js'
import { DomainError, domainCommand } from './domain.js'
import { EnvError, envCommand } from './env.js'
import { checkFiles, homeProblem, listFiles, pack, PackError } from './pack.js'
import { plural, serverCommand, ServerError, system } from './server.js'
import { agents, detect, filesWithToken, homePath, onPath, setupAgents, SKILLS } from './setup.js'

const run = promisify(execFile)
const POLL_MS = 2000
const FOLLOW_LIMIT_MS = 30 * 60_000

const help = `Airlok — публикация проекта на хостинге Airlok или свой сервер из терминала

  npx @airlok/cli setup             один раз: вход и подключение ИИ-агентов (Claude Code, Codex, Antigravity, Cursor)
  npx @airlok/cli login             войти через браузер
  npx @airlok/cli deploy [папка]    собрать и опубликовать папку (по умолчанию текущую)
  npx @airlok/cli check [папка]     что Airlok сделает с проектом и что помешает публикации — без сборки
  npx @airlok/cli env list --app <имя>          переменные окружения приложения (без значений)
  npx @airlok/cli env set ИМЯ… --app <имя>      задать: значение спросит без показа на экране или прочитает из stdin
  npx @airlok/cli env set --app <имя> --from .env   задать все переменные из файла
  npx @airlok/cli env rm ИМЯ --app <имя>        удалить
  npx @airlok/cli domain add ДОМЕН --app <имя>  свой домен приложения на своём сервере: подключить или заменить
  npx @airlok/cli domain show|check|rm --app <имя>   что с доменом, проверить DNS сейчас, отключить
  npx @airlok/cli server connect root@IP        подключить свой сервер: поставить на него Airlok по SSH
  npx @airlok/cli server upgrade <имя>          поставить обновления системы на подключённом сервере
  npx @airlok/cli server reboot <имя>           перезагрузить его: сайты недоступны 1–2 минуты
  npx @airlok/cli device add [--server <сервер>]   добавить SSH-ключ этого компьютера в «Устройства» и открыть ему серверы
  npx @airlok/cli device list                   устройства и серверы, к которым у них есть доступ
  npx @airlok/cli whoami            под кем выполнен вход
  npx @airlok/cli logout            выйти на этом компьютере и отозвать токен входа (--keep-token — только забыть,
                                    --force — отозвать, даже если он записан в настройках ИИ-агентов)

Для deploy:
  --app <имя>       опубликовать в существующее приложение
  --new <имя>       создать приложение с этим именем и опубликовать
  --temporary       без входа: статический сайт на час по случайному адресу; ссылка «забрать себе» — в выводе
  --title <текст>   название публикации (по умолчанию — последний коммит)
  --no-wait         не ждать конца сборки
  --include-data    взять и файлы баз и выгрузок (*.db, *.sqlite, dump.sql, credentials.json…): без флага CLI спросит или пропустит их
  --json            итог одной строкой JSON — для скриптов и ИИ-агентов
  Ключи и доступы (закрытые ключи *.pem и *.key, id_rsa, .npmrc с токеном, .ssh…) не загружаются никогда.

Для env:
  --app <имя>       приложение; если имя есть на нескольких серверах — <сервер>/<имя>
  --from <файл>     взять значения из .env-файла (с именами — только эти)
  --allow-empty     разрешить пустое значение: без флага пустой stdin, ответ или строка ИМЯ= в .env — ошибка
  Из скрипта или ИИ-агента значение передаётся через stdin:
    npx @airlok/cli env set ИМЯ --app <имя> <<'AIRLOK_EOF'
    <значение>
    AIRLOK_EOF
  У приложения на своём сервере значения шифруются здесь, под ключ агента сервера: дашборд их не видит.

Для domain:
  --app <имя>       приложение на своём сервере; если имя есть на нескольких серверах — <сервер>/<имя>
  --json            итог одной строкой JSON
  У регистратора заводится A-запись домена на IP сервера; домен включится следующей публикацией.

Для server connect:
  --name <имя>      имя сервера в дашборде (по умолчанию — его адрес)
  --port <порт>     порт SSH, если не 22
  --letsencrypt     выпускать для сайтов сертификаты Let's Encrypt
  --email <адрес>   email для писем о сертификатах (включает --letsencrypt)
  --ssh-keys-only   закрыть вход по паролю: CLI сам проверит вход по ключу и подтвердит
  --json            итог одной строкой JSON
  Если сервер ещё не пускает по ключу, CLI создаст ~/.ssh/airlok_ed25519 и положит его на сервер:
  пароль от сервера спросит ssh, один раз и только в терминале человека.

Для device add:
  --name <имя>      название устройства (по умолчанию — имя компьютера)
  --key <файл.pub>  открытый ключ (по умолчанию — ~/.ssh/id_ed25519.pub или другой найденный)
  --server <имена>  открыть доступ к этим серверам, через запятую
  --json            итог одной строкой JSON
  Агент кладёт ключ в authorized_keys root на отмеченных серверах. Закрытый ключ не читается.

Для setup:
  --agents <список> каких агентов настроить: claude,codex,antigravity,cursor (по умолчанию — найденных)
  --yes             не спрашивать подтверждения

Переменные окружения: AIRLOK_TOKEN — токен вместо входа (для CI), AIRLOK_URL — адрес дашборда.`

export const stageLabel = { source: 'Код', detect: 'Стек', install: 'Зависимости', build: 'Сборка', image: 'Образ', release: 'Публикация' }

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

// logUpdate — новые целые строки лога сборки с прошлого опроса. Дашборд отдаёт конец лога
// (до 32 КБ), и окно сдвигается: где остановились, ищем по концу уже напечатанного. shown —
// уже напечатанная часть текущего лога; недописанная последняя строка ждёт следующего опроса.
export function logUpdate(shown, log) {
  let from = 0
  let gap = false
  if (log.startsWith(shown)) {
    from = shown.length
  } else if (shown) {
    // Последние три строки: начало напечатанного могло уже уйти из окна.
    const anchor = shown.split('\n').slice(-4).join('\n').slice(-1000)
    const i = log.lastIndexOf(anchor)
    if (i >= 0) from = i + anchor.length
    else gap = true
  }
  const end = log.lastIndexOf('\n')
  if (end < from) return { lines: [], shown: log.slice(0, from) }
  const lines = log.slice(from, end).split('\n')
  return { lines: gap ? ['…', ...lines] : lines, shown: log.slice(0, end + 1) }
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

export async function main(argv, io = makeIO(), env = process.env, sys = system) {
  let parsed
  try {
    parsed = parseArgs({
      args: argv,
      allowPositionals: true,
      options: {
        app: { type: 'string' }, new: { type: 'string' }, title: { type: 'string' }, agents: { type: 'string' }, yes: { type: 'boolean', short: 'y' },
        from: { type: 'string' }, 'allow-empty': { type: 'boolean' }, name: { type: 'string' }, port: { type: 'string' }, email: { type: 'string' },
        letsencrypt: { type: 'boolean' }, 'ssh-keys-only': { type: 'boolean' }, key: { type: 'string' }, server: { type: 'string' },
        'no-wait': { type: 'boolean' }, 'include-data': { type: 'boolean' }, json: { type: 'boolean' }, help: { type: 'boolean', short: 'h' }, version: { type: 'boolean', short: 'v' },
        'keep-token': { type: 'boolean' }, force: { type: 'boolean' }, temporary: { type: 'boolean' },
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
        await logout(opts, io, env)
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
      case 'domain': {
        const s = await session(env)
        if (!s.token) throw new UserError('Вы не вошли: npx @airlok/cli login (или задайте AIRLOK_TOKEN)')
        return await domainCommand(new Api(s.url, s.token), positionals[1], positionals.slice(2), opts, io)
      }
      case 'server': {
        let s = await session(env)
        if (!s.token) {
          if (!io.interactive) throw new UserError('Вы не вошли: npx @airlok/cli login (или задайте AIRLOK_TOKEN)')
          await login(io, env)
          s = await session(env)
        }
        if (positionals[1] === 'connect') s = await cliLoginSession(s, io, env)
        const home = env.HOME || env.USERPROFILE || homedir()
        return await serverCommand(new Api(s.url, s.token), s.url, positionals[1], positionals.slice(2), opts, io, home, sys)
      }
      case 'device': {
        let s = await session(env)
        if (!s.token) throw new UserError('Вы не вошли: npx @airlok/cli login (или задайте AIRLOK_TOKEN)')
        if (positionals[1] === 'add') s = await cliLoginSession(s, io, env)
        const home = env.HOME || env.USERPROFILE || homedir()
        return await deviceCommand(new Api(s.url, s.token), s.url, positionals[1], positionals.slice(2), opts, io, home, sys)
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

export function errorText(err) {
  if (err instanceof UserError || err instanceof PackError || err instanceof EnvError || err instanceof DomainError || err instanceof ServerError ||
    err instanceof DeviceError) return err.message
  if (err instanceof ApiError) {
    if (err.status === 401) return 'Вход устарел или токен отозван: npx @airlok/cli login'
    if (err.code === 'cli_login_required') return CLI_LOGIN_REQUIRED
    if (err.code === 'read_only_token') return 'Этот токен только читает. Войдите заново (npx @airlok/cli login) или задайте токен с правом публиковать.'
    if (err.code === 'check_too_large') return 'Проект после распаковки слишком большой для проверки: уберите из папки данные и результаты сборки или добавьте их в .gitignore.'
    if (err.code === 'archive_too_large') return 'Архив больше 50 МБ: уберите из папки данные и результаты сборки или добавьте их в .gitignore.'
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
  const saved = await savedLogin(env)
  // Прежний токен показываем только дашборду, который его выдал, и не отзываем, пока он записан
  // в настройках ИИ-агентов.
  let previous = saved.url === s.url ? saved.token : ''
  const usedByAgents = await filesWithToken(previous, homeOf(env))
  if (usedByAgents.length) previous = ''
  const api = new Api(s.url)
  const start = await api.startLogin(hostname())
  io.err(`Откройте ссылку и подтвердите код ${start.user_code}:\n\n  ${start.url}\n`)
  // Открываем только адрес своего дашборда, а не то, что прислали.
  if (io.interactive && start.url.startsWith(`${s.url}/cli?code=`)) openBrowser(start.url)
  const deadline = Date.now() + start.expires_in * 1000
  while (Date.now() < deadline) {
    await sleep(start.interval * 1000)
    const done = await api.pollLogin(start.device_code, previous)
    if (!done) continue
    const path = await saveSession({ url: s.url, token: done.token, login: done.login }, env)
    io.err(`Готово: вы вошли как ${done.login}. Токен сохранён в ${path}`)
    if (done.replaced) io.err(`Прежний вход этого компьютера отозван. Если ИИ-агенты перестанут видеть Airlok, запустите ${SETUP_AGAIN}.`)
    if (usedByAgents.length) {
      io.err(`Прежний вход не отозван: его токен записан в настройках ИИ-агентов (${usedByAgents.map((f) => homePath(f, homeOf(env))).join(', ')}). ` +
        `Запустите ${SETUP_AGAIN} — агенты получат свой токен, а прежний вход потом ${REVOKE_IN_DASHBOARD}.`)
    }
    return
  }
  throw new UserError('Код входа истёк: запустите npx @airlok/cli login ещё раз.')
}

const SETUP_AGAIN = 'npx @airlok/cli setup'
const homeOf = (env) => env.HOME || env.USERPROFILE || homedir()
const REVOKE_IN_DASHBOARD = 'отзовите его в дашборде: Настройки → «ИИ-агенты»'

// logout отзывает сохранённый вход и забывает его. Токен из AIRLOK_TOKEN не трогаем: его задали
// снаружи, и он может быть нужен CI. Если отозвать не вышло, файл остаётся — logout можно повторить.
async function logout(opts, io, env) {
  const saved = await savedLogin(env)
  if (!saved.token) {
    io.err(env.AIRLOK_TOKEN ? `Сохранённого входа нет. Токен из AIRLOK_TOKEN logout не отзывает — ${REVOKE_IN_DASHBOARD}.` : 'На этом компьютере вход не сохранён.')
    return
  }
  if (opts['keep-token']) {
    await forgetSession(env)
    io.err(`Вход на этом компьютере забыт, но токен действует — ${REVOKE_IN_DASHBOARD}.`)
    return
  }
  const usedByAgents = await filesWithToken(saved.token, homeOf(env))
  if (usedByAgents.length && !opts.force) {
    throw new UserError(`Токен этого входа записан в настройках ИИ-агентов: ${usedByAgents.map((f) => homePath(f, homeOf(env))).join(', ')}. ` +
      `Отзыв отключит им Airlok. Сначала запустите ${SETUP_AGAIN} — агенты получат свой токен, — потом logout. ` +
      'Отозвать всё равно: npx @airlok/cli logout --force; только забыть вход на этом компьютере: --keep-token.')
  }
  try {
    await new Api(saved.url, saved.token).revokeToken()
  } catch (err) {
    if (!(err instanceof ApiError) || ![401, 404, 405].includes(err.status)) {
      throw new UserError(`Не удалось отозвать токен: ${err.message}. Повторите logout позже или выйдите без отзыва: ` +
        `npx @airlok/cli logout --keep-token — тогда ${REVOKE_IN_DASHBOARD}.`)
    }
    await forgetSession(env)
    if (err.status === 401) io.err('Вход на этом компьютере забыт: его токен уже не действовал.')
    else io.err(`Вход на этом компьютере забыт. Дашборд пока не умеет отзывать токен из CLI — ${REVOKE_IN_DASHBOARD}.`)
    return
  }
  await forgetSession(env)
  io.err(`Вы вышли: токен входа отозван. Если ИИ-агенты перестанут видеть Airlok, запустите ${SETUP_AGAIN}.`)
  if (env.AIRLOK_TOKEN) io.err('Токен из AIRLOK_TOKEN по-прежнему действует: logout его не отзывает.')
}

function openBrowser(url) {
  const [cmd, args] = process.platform === 'darwin' ? ['open', [url]] : process.platform === 'win32' ? ['cmd', ['/c', 'start', '', url]] : ['xdg-open', [url]]
  try {
    spawn(cmd, args, { stdio: 'ignore', detached: true }).on('error', () => {}).unref()
  } catch {
    // Браузер не открылся — ссылка уже напечатана.
  }
}

// liveSession — сессия с токеном, который сервер принимает: отозванный агентам записывать нельзя.
async function liveSession(io, env) {
  const s = await session(env)
  if (s.token && !(await tokenRejected(s))) return s
  if (s.token && env.AIRLOK_TOKEN) throw new UserError('Токен из AIRLOK_TOKEN отозван или неверный: задайте действующий.')
  if (s.token) io.err('Сохранённый вход больше не действует — войдите заново.')
  await login(io, env)
  return session(env)
}

const CLI_LOGIN_REQUIRED = 'Этот токен — для ИИ-агентов и CI: подключать серверы, класть SSH-ключи устройств и перевыпускать токен агента можно только со входом CLI. ' +
  'Войдите: npx @airlok/cli login — дальше CLI возьмёт этот вход сам, даже если задан AIRLOK_TOKEN.'

// cliLoginSession — сессия для server connect и device add: серверы и SSH-ключи открывает только вход CLI.
// Токен из настроек дашборда часто лежит в AIRLOK_TOKEN для Codex и Cursor и перекрывает вход: тогда
// берём сохранённый вход. Проверка — до SSH-шагов, чтобы не класть ключ на сервер впустую.
async function cliLoginSession(s, io, env) {
  if (!env.AIRLOK_TOKEN || (await new Api(s.url, s.token).tokenScope()) !== 'mcp') return s
  const own = { ...env, AIRLOK_TOKEN: '' }
  const saved = await session(own)
  if (saved.token && (await isCLILogin(saved))) {
    io.err(`AIRLOK_TOKEN — токен для ИИ-агентов, серверы и SSH-ключи он не открывает: беру вход ${saved.login || 'этого компьютера'}.`)
    return saved
  }
  if (!io.interactive) throw new UserError(CLI_LOGIN_REQUIRED)
  io.err('AIRLOK_TOKEN — токен для ИИ-агентов, серверы и SSH-ключи он не открывает. Войдите в Airlok CLI:')
  await login(io, own)
  return session(own)
}

async function isCLILogin(s) {
  try {
    return (await new Api(s.url, s.token).tokenScope()) === 'cli'
  } catch (err) {
    if (err instanceof ApiError && err.status === 401) return false
    throw err
  }
}

// Только 401 говорит о самом токене: нет связи или сбой сервера — ещё не повод входить заново.
async function tokenRejected(s) {
  try {
    await new Api(s.url, s.token).me()
    return false
  } catch (err) {
    return err instanceof ApiError && err.status === 401
  }
}

// setup — вход и подключение агентов: MCP-сервер airlok с токеном и навык airlok.
async function setup(opts, io, env) {
  const s = await liveSession(io, env)
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
  io.err(`Подключу к Airlok (${s.url}): ${chosen.map((a) => a.label).join(', ')}.`)
  if (io.interactive && !opts.yes) {
    const answer = (await io.ask('Продолжить? [Y/n] ')).toLowerCase()
    if (answer && !['y', 'yes', 'д', 'да'].includes(answer)) return 0
  }
  const token = await agentToken(new Api(s.url, s.token), hostname(), io)
  const report = await setupAgents({ chosen, url: s.url, token, home })
  for (const r of report) {
    if (r.ok) {
      io.err(`  ✓ ${r.agent.padEnd(12)} MCP: ${homePath(r.mcp, home)} · навыки ${SKILLS.join(', ')}: ${r.skills.map((d) => homePath(d, home)).join(', ')}`)
      if (r.note) io.err(`    ${''.padEnd(12)} ${r.note}`)
    } else io.err(`  ✗ ${r.agent.padEnd(12)} ${r.error}`)
  }
  io.err('\nПерезапустите агентов, чтобы они увидели Airlok. Проверка: спросите агента «какие у меня приложения на Airlok?».')
  return report.every((r) => r.ok) ? 0 : 1
}

// agentToken — токен, который setup пишет в конфиги ИИ-агентов. Вход CLI туда не попадает: он
// подключает серверы и кладёт SSH-ключи, а конфиги агентов утекают чаще. Если в AIRLOK_TOKEN уже
// токен для агентов, пишем его; дашборд без этой возможности (404) получает вход CLI, как раньше.
export async function agentToken(api, client, io) {
  try {
    return (await api.createAgentToken(client)).token
  } catch (err) {
    if (err instanceof ApiError && err.code === 'cli_login_required') return api.token
    if (err instanceof ApiError && err.status === 404) {
      io.err('Дашборд пока не выпускает отдельный токен для ИИ-агентов — записываю им вход CLI. Повторите setup после обновления Airlok.')
      return api.token
    }
    throw err
  }
}

async function deploy(target, opts, io, env) {
  const dir = resolve(target)
  if (!(await stat(dir).catch(() => null))?.isDirectory()) throw new UserError(`Нет такой папки: ${dir}`)
  if (opts.temporary) return deployTemporary(dir, opts, io, env)
  let s = await session(env)
  if (!s.token) {
    if (!io.interactive) throw new UserError('Вы не вошли: npx @airlok/cli login (или задайте AIRLOK_TOKEN)')
    await login(io, env)
    s = await session(env)
  }
  const api = new Api(s.url, s.token)
  // Папку разбираем и упаковываем до выбора приложения: --new не должен создавать приложение
  // для папки, которую не опубликовать (пустой или с архивом больше 50 МБ).
  const { files, excluded, git } = await projectFiles(dir, opts, io, env)
  io.err(`Упаковываю ${files.length} ${plural(files.length, 'файл', 'файла', 'файлов')}…`)
  const archive = await pack(dir, files, { gitignore: git })
  const app = await chooseApp(api, dir, opts, io)

  const title = opts.title || (await gitTitle(dir)) || `Папка ${basename(dir)}`
  io.err(`Загружаю ${mb(archive.length)} в ${app.name}…`)
  const cmd = await api.deployArchive(app.id, archive, title)
  if (opts['no-wait']) {
    if (opts.json) io.out(JSON.stringify({ status: cmd.status, command_id: cmd.id, app: app.name, excluded }))
    else io.err(`Публикация поставлена в очередь. Ход сборки — в дашборде: ${s.url}/apps/${app.id}?tab=deploys`)
    return 0
  }
  // С --json вывод читает ИИ-агент: ему хватает этапов, а лог целиком — при ошибке.
  const result = await follow(api, cmd.id, io, !opts.json)
  const url = app.domain ? `https://${app.domain}` : ''
  const failedStage = result.status === 'failed' ? result.stages?.find((st) => st.status === 'failed')?.key : undefined
  if (opts.json) {
    io.out(JSON.stringify({ status: result.status, command_id: cmd.id, app: app.name, url, message: result.message, failed_stage: failedStage, excluded }))
  }
  if (result.status === 'success') {
    const took = result.started_at && result.finished_at ? ` за ${duration((Date.parse(result.finished_at) - Date.parse(result.started_at)) / 1000)}` : ''
    io.err(`\n✓ Опубликовано${took}${url ? `: ${url}` : ''}`)
    if (!opts.json && url) io.out(url)
    return 0
  }
  if (result.status === 'canceled') {
    io.err('\nЭту публикацию заменила более новая.')
    return 1
  }
  io.err(`\n✗ ${result.message || 'Сборка не удалась'}`)
  if (failedStage === 'release') {
    // Сборка прошла, а новая версия не запустилась: причина в выводе приложения, а не в логе сборки.
    io.err(`\nЛоги приложения: ${s.url}/apps/${app.id}?tab=logs`)
    return 1
  }
  const tail = (result.log ?? '').trimEnd().split('\n').slice(-25)
  if (opts.json && tail.length > 1) io.err(`\nКонец лога:\n${tail.map((l) => `  ${l}`).join('\n')}`)
  io.err(`\nПолный лог: ${s.url}/apps/${app.id}?tab=deploys`)
  return 1
}

// TEMPORARY_FRESH_MS — сайт, которому осталось меньше, не обновляем, а публикуем заново: иначе он
// пропал бы почти сразу после публикации.
const TEMPORARY_FRESH_MS = 5 * 60_000

// deployTemporary публикует папку без входа: статический сайт живёт час по случайному адресу, а по
// ссылке его забирают в свой аккаунт. Повторный запуск в той же папке обновляет тот же сайт, пока он жив.
async function deployTemporary(dir, opts, io, env) {
  if (opts.app || opts.new) throw new UserError('--temporary публикует без входа на случайный адрес: --app и --new с ним не указывают.')
  const s = await session(env)
  const { files, excluded, git } = await projectFiles(dir, opts, io, env)
  io.err(`Упаковываю ${files.length} ${plural(files.length, 'файл', 'файла', 'файлов')}…`)
  const archive = await pack(dir, files, { gitignore: git })
  const title = opts.title || (await gitTitle(dir)) || `Папка ${basename(dir)}`

  let site = await readTemporary(dir)
  let cmd
  if (site && Date.parse(site.expires_at) - Date.now() > TEMPORARY_FRESH_MS) {
    io.err(`Обновляю сайт без входа ${site.app}…`)
    try {
      cmd = (await new Api(s.url, site.token).updateTemporary(archive, title)).command
    } catch (err) {
      if (!(err instanceof ApiError) || err.status !== 404) throw err
      cmd = undefined // срок вышел раньше, чем думал CLI: публикуем заново
    }
  }
  if (!cmd) {
    io.err(`Загружаю ${mb(archive.length)} без входа…`)
    const res = await new Api(s.url).createTemporary(archive, title)
    site = { app: res.app, url: res.url, token: res.token, claim_url: s.url + res.claim_path, expires_at: res.expires_at }
    await saveTemporary(dir, site)
    cmd = res.command
  }
  const api = new Api(s.url, site.token)
  const until = new Date(site.expires_at).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })
  const out = { app: site.app, url: site.url, claim_url: site.claim_url, expires_at: site.expires_at, excluded, temporary: true }
  if (opts['no-wait']) {
    if (opts.json) io.out(JSON.stringify({ status: cmd.status, command_id: cmd.id, ...out }))
    else io.err(`Публикация поставлена в очередь. Сайт: ${site.url}\nЗабрать его в свой аккаунт: ${site.claim_url}`)
    return 0
  }
  const result = await follow(api, cmd.id, io, !opts.json, (id) => api.temporaryCommand(id))
  const failedStage = result.status === 'failed' ? result.stages?.find((st) => st.status === 'failed')?.key : undefined
  if (opts.json) io.out(JSON.stringify({ status: result.status, command_id: cmd.id, message: result.message, failed_stage: failedStage, ...out }))
  if (result.status !== 'success') {
    io.err(`\n✗ ${result.message || 'Сборка не удалась'}`)
    return 1
  }
  io.err(`\n✓ Опубликовано без входа: ${site.url}`)
  io.err(`Сайт временный и удалится в ${until}. Чтобы оставить его себе, откройте ссылку и войдите:\n${site.claim_url}`)
  if (!opts.json) io.out(site.url)
  return 0
}

// check упаковывает папку так же, как deploy, и печатает отчёт дашборда. Код 1 — выкатка не пройдёт.
async function check(target, opts, io, env) {
  const dir = resolve(target)
  if (!(await stat(dir).catch(() => null))?.isDirectory()) throw new UserError(`Нет такой папки: ${dir}`)
  const s = await session(env)
  if (!s.token) throw new UserError('Вы не вошли: npx @airlok/cli login (или задайте AIRLOK_TOKEN)')
  const { files, excluded, git } = await projectFiles(dir, opts, io, env)
  if (!opts.json) io.err(`Проверяю ${files.length} ${plural(files.length, 'файл', 'файла', 'файлов')}: архив проверяется на сервере и сразу удаляется…`)
  const report = normalize(await new Api(s.url, s.token).check(await pack(dir, files, { gitignore: git })))
  const ok = report.errors.length === 0
  if (opts.json) io.out(JSON.stringify({ status: ok ? 'ok' : 'error', ...report, excluded }))
  else io.out(formatReport(report))
  return ok ? 0 : 1
}

// projectFiles — что из папки уйдёт в архив. Ключи и доступы не уходят никогда, базы и выгрузки —
// с --include-data или по ответу человека: сборке они бывают нужны, а молча пропущенный файл
// сломал бы проект. excluded — что не ушло, для --json; undefined, если ушло всё.
export async function projectFiles(dir, opts, io, env) {
  const home = env.HOME || env.USERPROFILE || homedir()
  const refused = homeProblem(await realpath(dir), await realpath(home).catch(() => resolve(home)))
  if (refused) throw new UserError(refused)
  const { files, secrets, data, git, ignored, allIgnored } = await listFiles(dir)
  if (ignored) io.err(`Папка ${basename(dir)} в .gitignore — публикую все её файлы, кроме зависимостей, .env и ключей.`)
  if (allIgnored) io.err(`Все файлы папки ${basename(dir)} в .gitignore — публикую их все, кроме зависимостей, .env и ключей.`)
  if (secrets.length) io.err(`Не загружаю ключи и доступы: ${listed(secrets)}. Секреты задайте переменными окружения приложения.`)
  const dataPaths = data.map((f) => f.path)
  let withData = Boolean(opts['include-data'])
  if (data.length && !withData && io.interactive && !opts.json) {
    io.err(`Похоже на базы, выгрузки или файлы входа: ${listed(dataPaths)}. Обычно им не место в публикации: данные хранят в базе, а доступы — в переменных окружения. ` +
      'Статический сайт и сайт на PHP не отдают такие файлы по адресу: базе для sql.js дайте другое расширение, например .bin.')
    withData = ['y', 'yes', 'д', 'да'].includes((await io.ask('Загрузить и их? [y/N] ')).trim().toLowerCase())
  }
  if (data.length && !withData) io.err(`Не загружаю базы, выгрузки и файлы входа: ${listed(dataPaths)}. Если они нужны сборке или приложению, добавьте --include-data.`)
  const chosen = withData ? [...files, ...data] : files
  const problem = checkFiles(chosen)
  if (problem) throw new UserError(problem)
  const skippedData = withData ? [] : dataPaths
  const excluded = secrets.length || skippedData.length ? { secrets, data: skippedData } : undefined
  return { files: chosen, excluded, git }
}

function listed(paths) {
  const limit = 10
  return paths.length <= limit ? paths.join(', ') : `${paths.slice(0, limit).join(', ')} и ещё ${paths.length - limit}`
}

// chooseApp — куда выкатывать: из флагов, из привязки папки или по вопросу человеку.
async function chooseApp(api, dir, opts, io) {
  const apps = await api.apps()
  let app
  if (opts.new) {
    const existing = apps.find((a) => a.name === opts.new)
    if (existing) {
      const linked = (await readLink(dir))?.app_id === existing.id
      throw new UserError(linked
        ? `Приложение ${existing.name} уже создано и привязано к этой папке: опубликовать в него — npx @airlok/cli deploy`
        : `Приложение ${existing.name} у вас уже есть: опубликовать в него — --app ${existing.name}`)
    }
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
    io.err('Куда опубликовать эту папку?')
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

// appLimitText: нет хостинга и пробник сам не включился — чинится на странице тарифа, а не удалением.
export function appLimitText(message) {
  return message.includes('no hosting')
    ? 'На вашем тарифе нет хостинга, а пробный период не включился. Что сделать — на странице https://app.airlok.ru/settings?tab=plan. Свой сервер работает и без тарифа.'
    : 'На хостинге уже максимум приложений. Удалите ненужное в дашборде.'
}

async function create(api, name) {
  if (!APP_NAME.test(name)) throw new UserError('Имя: латиница в нижнем регистре, цифры и дефис, от 2 до 28 символов, начинается с буквы.')
  try {
    const id = await api.createApp(name)
    const app = (await api.apps()).find((a) => a.id === id)
    return app ?? { id, name, domain: '' }
  } catch (err) {
    if (err instanceof ApiError && err.code === 'name_taken') throw new UserError(`Имя ${name} уже занято на хостинге — выберите другое.`)
    if (err instanceof ApiError && err.code === 'app_limit') throw new UserError(appLimitText(err.message))
    throw err
  }
}

// follow печатает ход сборки, пока она не закончится: этапы и, если withLog, новые строки лога.
// get — откуда брать выкатку: у сайта без входа свой адрес и его токен.
async function follow(api, id, io, withLog, get = (id) => api.command(id)) {
  const seen = new Map()
  let queued = false
  let shown = ''
  const deadline = Date.now() + FOLLOW_LIMIT_MS
  for (;;) {
    const cmd = await get(id)
    if (cmd.status === 'queued' && !queued) {
      queued = true
      io.err('Ждём сборщика…')
    }
    for (const line of stageUpdates(seen, cmd.stages)) io.err(line)
    if (withLog) {
      const update = logUpdate(shown, cmd.log ?? '')
      shown = update.shown
      for (const line of update.lines) io.err(`    ${line}`)
    }
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

