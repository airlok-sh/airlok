// Где CLI хранит вход и как папка проекта привязана к приложению.

import { mkdir, readFile, rm, writeFile } from 'node:fs/promises'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'

export const DEFAULT_URL = 'https://app.airlok.ru'

export function configPath(env = process.env) {
  if (process.platform === 'win32' && env.APPDATA) return join(env.APPDATA, 'airlok', 'config.json')
  return join(env.XDG_CONFIG_HOME || join(homedir(), '.config'), 'airlok', 'config.json')
}

async function readJSON(path) {
  try {
    return JSON.parse(await readFile(path, 'utf8'))
  } catch (err) {
    if (err.code === 'ENOENT') return null
    throw new Error(`Не удалось прочитать ${path}: ${err.message}`)
  }
}

// session — адрес дашборда и токен. Переменные окружения важнее файла: так CLI работает в CI.
export async function session(env = process.env) {
  const saved = (await readJSON(configPath(env))) ?? {}
  return {
    url: (env.AIRLOK_URL || saved.url || DEFAULT_URL).replace(/\/+$/, ''),
    token: env.AIRLOK_TOKEN || saved.token || '',
    login: env.AIRLOK_TOKEN ? '' : saved.login || '',
  }
}

// Токен даёт право выкатывать, поэтому файл читает только владелец.
export async function saveSession(s, env = process.env) {
  const path = configPath(env)
  await mkdir(dirname(path), { recursive: true, mode: 0o700 })
  await writeFile(path, JSON.stringify(s, null, 2) + '\n', { mode: 0o600 })
  return path
}

export async function forgetSession(env = process.env) {
  await rm(configPath(env), { force: true })
}

// Привязка лежит в самой папке: следующий deploy выкатывает туда же без вопросов.
export const linkPath = (dir) => join(dir, '.airlok', 'project.json')

export async function readLink(dir) {
  const link = await readJSON(linkPath(dir))
  return link?.app_id ? link : null
}

export async function saveLink(dir, link) {
  const path = linkPath(dir)
  await mkdir(dirname(path), { recursive: true })
  await writeFile(path, JSON.stringify(link, null, 2) + '\n')
  // Сам файл не секретный, но в git ему делать нечего: у каждого своя привязка.
  await writeFile(join(dirname(path), '.gitignore'), '*\n')
  return path
}
