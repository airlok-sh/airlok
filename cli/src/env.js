// Команда env: переменные окружения приложения. На своём сервере значения шифруются здесь,
// под ключ агента этого сервера, и дашборд их не видит; на хостинге их шифрует дашборд.

import { readFile } from 'node:fs/promises'

import { ApiError } from './api.js'
import { keyId, seal } from './envseal.js'

export class EnvError extends Error {}

const MAX_VALUE = 32 << 10

// parseDotEnv — тот же разбор .env, что у дашборда: KEY=VALUE, export, комментарии, кавычки.
// В двойных кавычках работают \n и \", одинарные берутся как есть.
export function parseDotEnv(text) {
  const vars = {}
  const bad = []
  text.split(/\r?\n/).forEach((raw, i) => {
    const line = raw.trim()
    if (!line || line.startsWith('#')) return
    const m = /^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/.exec(line)
    if (!m) return void bad.push(i + 1)
    let value = m[2]
    if (value.length >= 2 && value.startsWith('"') && value.endsWith('"')) {
      value = value.slice(1, -1).replace(/\\n/g, '\n').replace(/\\"/g, '"')
    } else if (value.length >= 2 && value.startsWith("'") && value.endsWith("'")) {
      value = value.slice(1, -1)
    } else {
      value = value.replace(/\s+#.*$/, '')
    }
    vars[m[1]] = value
  })
  return { vars, bad }
}

// findApp — приложение по имени или «сервер/имя»: на разных серверах имена могут совпадать.
export function findApp(apps, ref) {
  const [server, name] = ref.includes('/') ? ref.split('/', 2) : ['', ref]
  const found = apps.filter((a) => a.name === name && (!server || a.server_name === server || (server === 'hosting' && a.hosted)))
  if (found.length === 1) return found[0]
  if (found.length === 0) throw new EnvError(`Нет приложения ${ref}. Список — в дашборде или у ИИ-агента: list_apps.`)
  throw new EnvError(`Приложений ${name} несколько: укажите сервер — ${found.map((a) => `${a.hosted ? 'hosting' : a.server_name}/${a.name}`).join(', ')}`)
}

// values — значения для set: из файла, из stdin или по вопросу человеку. В аргументах
// значения не принимаются: их было бы видно в истории оболочки и в списке процессов.
async function values(names, opts, io) {
  if (opts.from) {
    const { vars, bad } = parseDotEnv(await readFile(opts.from, 'utf8'))
    if (bad.length) throw new EnvError(`В ${opts.from} не разобраны строки: ${bad.join(', ')}`)
    const picked = names.length ? Object.fromEntries(names.map((n) => [n, vars[n]])) : vars
    const absent = Object.keys(picked).filter((n) => picked[n] === undefined)
    if (absent.length) throw new EnvError(`В ${opts.from} нет ${absent.join(', ')}`)
    if (Object.keys(picked).length === 0) throw new EnvError(`В ${opts.from} нет переменных`)
    return picked
  }
  if (names.length === 0) throw new EnvError('Укажите имена переменных или файл: env set ИМЯ --app <имя> или env set --app <имя> --from .env')
  if (!io.stdinIsTTY) {
    if (names.length > 1) throw new EnvError('Из stdin читается одно значение: задайте переменные по одной или из файла (--from .env)')
    return { [names[0]]: (await io.readStdin()).replace(/\r?\n$/, '') }
  }
  const out = {}
  for (const name of names) out[name] = await io.askSecret(`${name}: `)
  return out
}

export async function envCommand(api, sub, names, opts, io) {
  if (!opts.app) throw new EnvError('Укажите приложение: --app <имя> или --app <сервер>/<имя>')
  const app = findApp(await api.allApps(), opts.app)
  const json = (data) => opts.json && io.out(JSON.stringify({ status: 'ok', app: app.name, ...data }))
  switch (sub) {
    case 'list':
      return app.hosted ? listHosted(api, app, io, json, opts.json) : listServer(api, app, io, json, opts.json)
    case 'set': {
      const vars = await values(names, opts, io)
      const big = Object.keys(vars).filter((n) => Buffer.byteLength(vars[n]) > MAX_VALUE)
      if (big.length) throw new EnvError(`Значение больше 32 КБ: ${big.join(', ')}`)
      const set = app.hosted ? await setHosted(api, app, vars) : await setServer(api, app, vars, io)
      json({ set })
      io.err(`✓ Заданы: ${set.join(', ')}. ${app.hosted ? 'Применятся со следующего запроса к сайту.' : 'Попадут на сервер при следующей выкатке.'}`)
      return 0
    }
    case 'rm':
      if (names.length !== 1) throw new EnvError('Удаляется одна переменная: env rm ИМЯ --app <имя>')
      await (app.hosted ? api.deleteEnv(app.id, names[0]) : api.deleteServerEnv(app.id, names[0])).catch((err) => {
        if (err instanceof ApiError && err.status === 404) throw new EnvError(`У ${app.name} нет переменной ${names[0]}`)
        throw err
      })
      json({ deleted: names[0] })
      io.err(`✓ ${names[0]} удалена${app.hosted ? '' : ': с сервера пропадёт при следующей выкатке'}.`)
      return 0
    default:
      throw new EnvError('env list | set | rm — см. npx @airlok/cli help')
  }
}

async function listHosted(api, app, io, json, asJSON) {
  const vars = await api.env(app.id)
  json({ vars })
  if (!asJSON) for (const v of vars) io.out(v.name)
  return 0
}

async function listServer(api, app, io, json, asJSON) {
  const st = await api.serverEnv(app.id)
  const id = st.key ? await keyId(st.key.public_key) : ''
  json({ key_id: id, vars: st.vars })
  if (asJSON) return 0
  for (const v of st.vars) io.out(v.stale ? `${v.name}  (под прежним ключом — задайте заново)` : v.name)
  io.err(id ? `Ключ сервера ${app.server_name}: ${id}` : `Агент сервера ${app.server_name} ещё не прислал ключ.`)
  io.err(`Значения не видны никому, кроме сервера. Вписанное прямо в /srv/apps/${app.name}/shared/user.env здесь не показывается.`)
  return 0
}

async function setHosted(api, app, vars) {
  await api.putEnv(app.id, vars).catch(explain)
  return Object.keys(vars).sort()
}

async function setServer(api, app, vars, io) {
  const st = await api.serverEnv(app.id)
  if (!st.key) {
    throw new EnvError(`Агент сервера ${app.server_name} ещё не прислал ключ для переменных: обновите агента в дашборде (Серверы → Обновить агента) и повторите.`)
  }
  const id = await keyId(st.key.public_key)
  if (id !== st.key.key_id) throw new EnvError('Дашборд прислал ключ с чужим отпечатком: переменные не отправлены.')
  io.err(`Шифрую под ключ сервера ${app.server_name}: ${id}`)
  io.err('Сверить на сервере: journalctl -u airlok-agent | grep ключ')
  const sealed = {}
  for (const [name, value] of Object.entries(vars)) sealed[name] = await seal(st.key.public_key, app.name, name, value)
  await api.putServerEnv(app.id, id, sealed).catch(explain)
  return Object.keys(vars).sort()
}

function explain(err) {
  if (!(err instanceof ApiError)) throw err
  switch (err.code) {
    case 'invalid_name':
      throw new EnvError(`Имя не подходит: ${err.message}`)
    case 'key_changed':
      throw new EnvError('Ключ сервера только что сменился: повторите команду.')
    case 'too_many':
      throw new EnvError('У приложения может быть до 100 переменных.')
    default:
      throw err
  }
}
