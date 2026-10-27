// Команда server connect: подключить свой сервер к airlok с этого компьютера, не заходя на него
// руками. Токен агента CLI получает под входом пользователя и передаёт на сервер через stdin ssh:
// ни на экран, ни в промпт ИИ-агента он не попадает.

import { spawn } from 'node:child_process'
import { mkdir, readFile, stat } from 'node:fs/promises'
import { hostname } from 'node:os'
import { dirname, join } from 'node:path'

import { ApiError } from './api.js'

export class ServerError extends Error {}

export const INSTALL_URL = 'https://airlok.ru/install.sh'
const KEY_NAME = 'airlok_ed25519'
const ONLINE_WAIT_MS = 120_000
const OUTPUT_TAIL = 64 << 10

// Как у дашборда: email и адрес попадают в команду для оболочки сервера.
const EMAIL = /^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/
const ORIGIN = /^https?:\/\/[A-Za-z0-9.-]+(:\d+)?$/
const SERVER_NAME = /^[\p{L}\p{N}][\p{L}\p{N} ._-]{0,63}$/u
const USER = /^[a-z_][a-z0-9_-]{0,31}$/
const HOST = /^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$|^\[?[0-9A-Fa-f:]+\]?$/

// parseTarget — [пользователь@]адрес. Адрес не может начинаться с «-»: иначе ssh принял бы его за опцию.
export function parseTarget(target) {
  if (!target) throw new ServerError('Укажите сервер: npx @airlok/cli server connect root@IP-СЕРВЕРА')
  const at = target.lastIndexOf('@')
  const user = at === -1 ? 'root' : target.slice(0, at)
  const host = (at === -1 ? target : target.slice(at + 1)).replace(/^\[(.*)\]$/, '$1')
  if (!USER.test(user)) throw new ServerError(`Не похоже на имя пользователя: ${user}`)
  if (!HOST.test(host) || !/[A-Za-z0-9]/.test(host)) throw new ServerError(`Не похоже на адрес сервера: ${host}`)
  return { user, host, dest: `${user}@${host}` }
}

// defaultName — имя сервера в дашборде, если не задано: его адрес. Двоеточия IPv6 в имя не берутся.
export function defaultName(host) {
  return host.replace(/:/g, '-').slice(0, 64)
}

// installCommand — что выполнить на сервере. Токен в ней не участвует: он придёт через stdin.
export function installCommand(origin, { letsencrypt = false, email = '', keysOnly = false, root = true } = {}) {
  if (!ORIGIN.test(origin)) throw new ServerError(`Странный адрес дашборда: ${origin}`)
  if (email && !EMAIL.test(email)) throw new ServerError(`Это не похоже на email: ${email}`)
  let cmd = `bash -c "$(curl -fsSL ${INSTALL_URL})" install ${origin}`
  if (letsencrypt) cmd += email ? ` --letsencrypt ${email}` : ' --letsencrypt'
  // Вход по ключу CLI подтверждает сам (confirmKeysOnly): установщику не нужно звать человека во второе окно.
  if (keysOnly) cmd = `env AIRLOK_SSH_CONFIRM=cli ${cmd} --ssh-keys-only`
  // sudo -n: спросить пароль sudo негде, stdin занят токеном.
  return root ? cmd : `sudo -n ${cmd}`
}

// sshFailure — почему ssh не пустил, по его сообщению: от этого зависит, что советовать человеку.
export function sshFailure(output) {
  if (/REMOTE HOST IDENTIFICATION HAS CHANGED|Host key verification failed/i.test(output)) return 'hostkey'
  if (/Permission denied|Too many authentication failures|No more authentication methods/i.test(output)) return 'auth'
  if (/Could not resolve|Connection refused|timed out|No route to host|Network is unreachable|Connection closed|Connection reset/i.test(output)) return 'network'
  return 'other'
}

// system — внешние программы. run возвращает код и хвост вывода; с out ещё и печатает вывод по ходу.
export const system = {
  run(cmd, args, { input = '', out } = {}) {
    return new Promise((done, fail) => {
      let output = ''
      const child = spawn(cmd, args, { stdio: ['pipe', 'pipe', 'pipe'] })
      const collect = (chunk) => {
        output = (output + chunk.toString('utf8')).slice(-OUTPUT_TAIL)
        out?.(chunk)
      }
      child.stdout.on('data', collect)
      child.stderr.on('data', collect)
      child.on('error', (err) => fail(err.code === 'ENOENT' ? new ServerError(`Не найдена программа ${cmd}: установите OpenSSH-клиент.`) : err))
      child.on('close', (code) => done({ code: code ?? 1, output }))
      // Сервер может закрыть соединение, не дочитав stdin: это не ошибка.
      child.stdin.on('error', () => {})
      child.stdin.end(input)
    })
  },
  readFile: (path) => readFile(path, 'utf8'),
  sleep: (ms) => new Promise((ok) => setTimeout(ok, ms)),
}

function sshArgs(key, target, opts = {}) {
  const args = ['-o', 'ConnectTimeout=15', '-o', 'StrictHostKeyChecking=accept-new', '-o', 'ServerAliveInterval=30']
  if (opts.batch !== false) args.push('-o', 'BatchMode=yes')
  if (key) args.push('-i', key)
  if (opts.port) args.push('-p', String(opts.port))
  return [...args, '--', target.dest]
}

export async function serverCommand(api, origin, sub, rest, opts, io, home, sys = system) {
  if (sub !== 'connect') throw new ServerError('server connect [пользователь@]IP — см. npx @airlok/cli help')
  if (rest.length > 1) throw new ServerError('Подключается один сервер за раз: server connect root@IP')
  const target = parseTarget(rest[0])
  const port = opts.port ? Number(opts.port) : 0
  if (opts.port && !(Number.isInteger(port) && port > 0 && port < 65536)) throw new ServerError(`Не похоже на порт: ${opts.port}`)
  const name = opts.name ?? defaultName(target.host)
  if (!SERVER_NAME.test(name)) throw new ServerError('Имя сервера: буквы, цифры, пробелы, точки и дефисы, до 64 символов.')
  const remote = installCommand(origin, { letsencrypt: Boolean(opts.letsencrypt || opts.email), email: opts.email ?? '', keysOnly: Boolean(opts['ssh-keys-only']), root: target.user === 'root' })
  const echo = (chunk) => io.err(chunk.toString('utf8').replace(/\n$/, ''))

  const key = join(home, '.ssh', KEY_NAME)
  const usedKey = await ensureAccess(target, key, port, io, sys, echo)

  const server = await registerServer(api, name)
  io.err(`\nСтавлю airlok на ${target.host} — несколько минут: Docker, nginx, файрвол, агент.\n`)
  const install = await sys.run('ssh', [...sshArgs(usedKey, target, { port }), remote], { input: `${server.token}\n`, out: echo })
  if (install.code !== 0) {
    if (target.user !== 'root' && /sudo: a (terminal|password) is required/.test(install.output)) {
      throw new ServerError(`У ${target.user} sudo спрашивает пароль. Подключитесь как root (server connect root@${target.host}) или разрешите ${target.user} sudo без пароля.`)
    }
    throw new ServerError(`Установка не закончилась (код ${install.code}): причина — в выводе выше. Команду можно запустить ещё раз, она продолжит с того же места.`)
  }

  let keysOnly = false
  if (opts['ssh-keys-only']) keysOnly = await confirmKeysOnly(target, usedKey, port, io, sys)

  io.err('\nЖду, когда агент выйдет на связь…')
  const online = await waitOnline(api, server.id, sys)
  const login = `ssh${usedKey ? ` -i ${usedKey}` : ''}${port ? ` -p ${port}` : ''} ${target.dest}`
  if (opts.json) io.out(JSON.stringify({ status: online ? 'ok' : 'pending', server: name, server_id: server.id, host: target.host, ssh: login, password_login_closed: keysOnly }))
  if (online) io.err(`\n✓ Сервер ${name} подключён: ${origin}/servers`)
  else io.err(`\nУстановка прошла, но агент пока не на связи. Проверьте на сервере: journalctl -u airlok-agent -n 50`)
  io.err(`Зайти на сервер: ${login}`)
  if (keysOnly) io.err('Вход по паролю закрыт: на сервер пускает только по ключу.')
  return online ? 0 : 1
}

// ensureAccess добивается входа по ключу и возвращает, с каким ключом входить ('' — хватает ключей
// человека). Пароль от сервера спрашивает сам ssh в терминале: CLI и ИИ-агент его не видят.
async function ensureAccess(target, key, port, io, sys, echo) {
  const hasKey = Boolean(await stat(key).catch(() => null))
  const probe = await sys.run('ssh', [...sshArgs(hasKey ? key : '', target, { port }), 'true'])
  if (probe.code === 0) return hasKey ? key : ''
  explainProbe(probe.output, target)

  if (!io.interactive) {
    throw new ServerError(
      `Сервер ${target.host} не пускает по ключу, а пароль здесь ввести негде.\n` +
        `Пусть человек один раз выполнит эту же команду в своём терминале (Терминал на macOS, PowerShell на Windows): ` +
        `ssh спросит пароль от сервера, дальше вход будет по ключу ${key}.`,
    )
  }
  if (!hasKey) {
    await mkdir(dirname(key), { recursive: true, mode: 0o700 })
    const gen = await sys.run('ssh-keygen', ['-q', '-t', 'ed25519', '-N', '', '-C', `airlok@${hostname()}`, '-f', key])
    if (gen.code !== 0) throw new ServerError(`Не удалось создать SSH-ключ ${key}: ${gen.output.trim()}`)
    io.err(`Создал SSH-ключ ${key}`)
  }
  const pub = (await sys.readFile(`${key}.pub`)).trim()
  io.err(`Кладу ключ на ${target.host}. ssh спросит пароль от сервера — это последний раз.`)
  // Ключ приходит через stdin; дубль в authorized_keys не добавляем.
  const add = 'read -r k; umask 077; mkdir -p ~/.ssh && touch ~/.ssh/authorized_keys && { grep -qxF "$k" ~/.ssh/authorized_keys || printf "%s\\n" "$k" >> ~/.ssh/authorized_keys; }'
  const copy = await sys.run('ssh', [...sshArgs('', target, { port, batch: false }), add], { input: `${pub}\n`, out: echo })
  if (copy.code !== 0) {
    if (sshFailure(copy.output) === 'auth') throw new ServerError('Пароль не подошёл. Его присылает хостер в письме о сервере или показывает в панели управления.')
    throw new ServerError(`Не удалось положить ключ на сервер (код ${copy.code}).`)
  }
  const check = await sys.run('ssh', [...sshArgs(key, target, { port }), 'true'])
  if (check.code !== 0) throw new ServerError(`Ключ добавлен, но сервер всё равно не пускает по нему: ${check.output.trim()}`)
  return key
}

function explainProbe(output, target) {
  switch (sshFailure(output)) {
    case 'auth':
      return
    case 'hostkey':
      throw new ServerError(
        `Ключ сервера ${target.host} не совпадает с тем, что этот компьютер помнит. Если сервер переустанавливали, ` +
          `забудьте старый ключ: ssh-keygen -R ${target.host}. Если нет — не подключайтесь, это может быть подмена.`,
      )
    case 'network':
      throw new ServerError(`Не достучаться до ${target.host} по SSH: проверьте адрес и что сервер включён.\n${output.trim()}`)
    default:
      throw new ServerError(`ssh не подключился к ${target.host}:\n${output.trim()}`)
  }
}

// registerServer заводит сервер в дашборде. Сервер с тем же именем, ни разу не выходивший на
// связь, — прошлая попытка: ему выпускается новый токен вместо второй записи.
async function registerServer(api, name) {
  const existing = (await api.servers()).find((s) => s.name === name)
  if (existing?.last_seen_at) {
    throw new ServerError(`Сервер ${name} уже подключён к airlok. Подключить другой сервер с этим адресом — задайте имя: --name <имя>.`)
  }
  try {
    const created = existing ? await api.rotateServerToken(existing.id) : await api.createServer(name)
    return { id: created.server.id, token: created.agent_token }
  } catch (err) {
    if (err instanceof ApiError && err.code === 'name_taken') throw new ServerError(`Имя ${name} уже занято: задайте другое через --name.`)
    throw err
  }
}

// confirmKeysOnly — проверка «войдите по ключу в новом окне», которую установщик просит у человека:
// новое соединение по ключу и есть эта проверка. Не подтвердить — пароль вернётся сам через 3 минуты.
async function confirmKeysOnly(target, key, port, io, sys) {
  const sudo = target.user === 'root' ? '' : 'sudo -n '
  const confirm = `if systemctl is-active --quiet airlok-ssh-rollback.timer; then ${sudo}/opt/airlok/recipes/server-base.sh confirm-ssh; fi; ${sudo}sshd -T 2>/dev/null | grep -qx 'passwordauthentication no'`
  const res = await sys.run('ssh', [...sshArgs(key, target, { port }), confirm])
  if (res.code === 0) {
    io.err('\n✓ Проверил вход по ключу в новом соединении и подтвердил: окно открывать не нужно.')
    return true
  }
  io.err('\nВход по паролю оставлен: подтвердить вход по ключу не получилось, настройки SSH откатятся сами.')
  return false
}

async function waitOnline(api, id, sys) {
  const deadline = Date.now() + ONLINE_WAIT_MS
  for (;;) {
    const s = (await api.servers()).find((x) => x.id === id)
    if (s?.last_seen_at) return true
    if (Date.now() > deadline) return false
    await sys.sleep(3000)
  }
}
