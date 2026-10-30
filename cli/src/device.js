// Команда device: открытый SSH-ключ этого компьютера — устройство в дашборде, которому галочками
// открывают доступ к своим серверам. Читается только файл .pub: закрытый ключ с компьютера не уходит.

import { hostname } from 'node:os'
import { join } from 'node:path'

import { ApiError } from './api.js'

export class DeviceError extends Error {}

// Сначала ключи человека, свой ключ airlok — если других нет.
const KEY_FILES = ['id_ed25519', 'id_ecdsa', 'id_ed25519_sk', 'id_ecdsa_sk', 'id_rsa', 'airlok_ed25519']
const KEY_LINE = /^(ssh-ed25519|ecdsa-sha2-nistp(?:256|384|521)|ssh-rsa|sk-ssh-ed25519@openssh\.com|sk-ecdsa-sha2-nistp256@openssh\.com) ([A-Za-z0-9+/]+={0,2})(?: .*)?$/
const NAME_UNSAFE = /[^\p{L}\p{N} ._()+#@-]+/gu

// parsePublicKey — «тип base64» из содержимого .pub, без комментария.
export function parsePublicKey(text) {
  const t = text.trim()
  if (t.includes('PRIVATE KEY')) throw new DeviceError('Это закрытый ключ: он остаётся на компьютере. Укажите файл .pub.')
  const m = /[\r\n]/.test(t) ? null : KEY_LINE.exec(t.replace(/\s+/g, ' '))
  if (!m) throw new DeviceError('В файле нет открытого SSH-ключа: нужна одна строка вида ssh-ed25519 AAAA…')
  return `${m[1]} ${m[2]}`
}

// deviceName — имя компьютера в виде, который примет дашборд.
export function deviceName(host = hostname()) {
  const name = host.replace(/\.local$/, '').replace(NAME_UNSAFE, '-').replace(/^[^\p{L}\p{N}]+/u, '').slice(0, 64).trim()
  return name || 'Компьютер'
}

export async function findPublicKey(home, sys) {
  for (const f of KEY_FILES) {
    const path = join(home, '.ssh', `${f}.pub`)
    const text = await sys.readFile(path).catch(() => null)
    if (text !== null) return { path, key: parsePublicKey(text) }
  }
  return null
}

// ensureDevice заводит ключ устройством или находит уже заведённое и добавляет ему серверы.
// autoName — имя придумали мы, а не человек: занятое имя дополняется номером.
export async function ensureDevice(api, key, name, serverIds, { autoName = false } = {}) {
  const names = autoName ? [name, `${name} (2)`, `${name} (3)`] : [name]
  for (const n of names) {
    try {
      return { device: await api.createDevice(n, key, serverIds), created: true }
    } catch (err) {
      if (!(err instanceof ApiError)) throw err
      if (err.code === 'key_taken') break
      if (err.code === 'name_taken' && n !== names.at(-1)) continue
      if (err.code === 'name_taken') throw new DeviceError(`Устройство «${n}» уже есть: задайте другое имя через --name.`)
      throw err
    }
  }
  const device = (await api.devices()).devices.find((d) => d.public_key === key)
  if (!device) throw new DeviceError('Этот ключ уже добавлен, но устройство не нашлось: проверьте раздел «Устройства» в дашборде.')
  const ids = [...new Set([...device.server_ids, ...serverIds])]
  if (ids.length !== device.server_ids.length) await api.setDeviceServers(device.id, ids)
  return { device: { ...device, server_ids: ids }, created: false }
}

async function serversByName(api, list) {
  if (!list) return []
  const wanted = list.split(',').map((s) => s.trim()).filter(Boolean)
  const servers = await api.servers()
  const unknown = wanted.filter((n) => !servers.some((s) => s.name === n))
  if (unknown.length) {
    const known = servers.map((s) => s.name).join(', ') || 'своих серверов пока нет'
    throw new DeviceError(`Нет таких серверов: ${unknown.join(', ')}. Есть: ${known}.`)
  }
  return servers.filter((s) => wanted.includes(s.name))
}

export async function deviceCommand(api, origin, sub, rest, opts, io, home, sys) {
  if (rest.length) throw new DeviceError('Лишние аргументы: device add [--name <имя>] [--key <файл.pub>] [--server <сервер,…>]')
  if (sub === 'list') return list(api, opts, io)
  if (sub !== 'add') throw new DeviceError('device add [--name <имя>] [--key <файл.pub>] [--server <сервер,…>] или device list')

  let found
  if (opts.key) {
    // Закрытый ключ даже не читаем: путь без .pub почти всегда он.
    if (!opts.key.endsWith('.pub')) throw new DeviceError(`Нужен открытый ключ — файл .pub, а не ${opts.key}.`)
    const text = await sys.readFile(opts.key).catch(() => {
      throw new DeviceError(`Не прочитать ${opts.key}.`)
    })
    found = { path: opts.key, key: parsePublicKey(text) }
  } else {
    found = await findPublicKey(home, sys)
  }
  if (!found) throw new DeviceError('В ~/.ssh нет SSH-ключа. Создайте его: ssh-keygen -t ed25519 — и запустите команду снова.')

  const servers = await serversByName(api, opts.server)
  const { device, created } = await ensureDevice(api, found.key, opts.name ?? deviceName(), servers.map((s) => s.id), { autoName: !opts.name })
  if (opts.json) {
    io.out(JSON.stringify({ status: 'ok', device: device.name, id: device.id, created, key: found.path, servers: servers.map((s) => s.name) }))
  }
  io.err(created ? `✓ Устройство «${device.name}» добавлено: ключ ${found.path}` : `Ключ ${found.path} уже добавлен как «${device.name}».`)
  if (servers.length) io.err(`Агент положит ключ на ${servers.map((s) => s.name).join(', ')} за несколько секунд, если сервер на связи.`)
  else if (device.server_ids.length === 0) io.err(`Откройте ему серверы: --server <имя> или в дашборде ${origin}/devices`)
  return 0
}

async function list(api, opts, io) {
  const [{ devices }, servers] = await Promise.all([api.devices(), api.servers()])
  const nameOf = (id) => servers.find((s) => s.id === id)?.name ?? id
  if (opts.json) {
    io.out(JSON.stringify({
      status: 'ok',
      devices: devices.map((d) => ({ name: d.name, key_type: d.key_type, fingerprint: d.fingerprint, disabled: d.disabled, servers: d.server_ids.map(nameOf) })),
    }))
    return 0
  }
  if (devices.length === 0) {
    io.err('Устройств нет. Добавить этот компьютер: npx @airlok/cli device add')
    return 0
  }
  for (const d of devices) {
    const on = d.server_ids.length ? d.server_ids.map(nameOf).join(', ') : 'ни одного сервера'
    io.out(`${d.name}${d.disabled ? ' (отключено)' : ''}\n  ${d.fingerprint}\n  серверы: ${on}`)
  }
  return 0
}
