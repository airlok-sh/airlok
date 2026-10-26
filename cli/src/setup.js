// Разовая настройка ИИ-агентов: MCP-сервер airlok с токеном и навык airlok в каталог навыков
// каждого найденного агента. Повторный запуск ничего не дублирует.

import { execFile } from 'node:child_process'
import { access, copyFile, mkdir, readFile, writeFile } from 'node:fs/promises'
import { homedir } from 'node:os'
import { delimiter, dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'
import { promisify } from 'node:util'

const SERVER = 'airlok'

const exists = (p) => access(p).then(() => true, () => false)

// onPath — есть ли программа в PATH: так находим агентов, у которых нет своей папки в домашнем каталоге.
export async function onPath(cmd, env = process.env) {
  const exts = process.platform === 'win32' ? ['.cmd', '.exe', ''] : ['']
  for (const dir of (env.PATH ?? '').split(delimiter).filter(Boolean)) {
    for (const ext of exts) if (await exists(join(dir, cmd + ext))) return true
  }
  return false
}

// mergeServers вписывает сервер в JSON-конфиг с ключом mcpServers, не трогая остальное.
// Битый файл не перезаписываем: в нём могут быть настройки человека.
export function mergeServers(text, entry, file) {
  let cfg = {}
  if (text && text.trim()) {
    try {
      cfg = JSON.parse(text)
    } catch {
      throw new Error(`${file} — не JSON, править его вслепую не будем. Исправьте файл или добавьте airlok вручную.`)
    }
  }
  if (typeof cfg !== 'object' || cfg === null || Array.isArray(cfg)) throw new Error(`${file}: ожидался объект JSON`)
  cfg.mcpServers = { ...(cfg.mcpServers ?? {}), [SERVER]: entry }
  return JSON.stringify(cfg, null, 2) + '\n'
}

const tomlHeader = /^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$/

// upsertTomlServer заменяет раздел [mcp_servers.airlok] (с подразделами) в config.toml Codex.
export function upsertTomlServer(text, block) {
  const lines = (text ?? '').split('\n')
  const out = []
  let skipping = false
  for (const line of lines) {
    const m = line.match(tomlHeader)
    if (m) {
      const name = m[1].replace(/["']/g, '')
      skipping = name === `mcp_servers.${SERVER}` || name.startsWith(`mcp_servers.${SERVER}.`)
    }
    if (!skipping) out.push(line)
  }
  const body = out.join('\n').replace(/\n+$/, '')
  return (body ? body + '\n\n' : '') + block.trim() + '\n'
}

const tomlString = (s) => JSON.stringify(s)

// agents — кого умеем настраивать. Пути — из документации агентов: навыки в формате SKILL.md.
export function agents(home) {
  const gemini = join(home, '.gemini')
  return [
    {
      id: 'claude', label: 'Claude Code', bin: 'claude', dir: join(home, '.claude'),
      skills: [join(home, '.claude', 'skills')],
      async mcp({ url, token, run, hasBin }) {
        const entry = { type: 'http', url, headers: { Authorization: `Bearer ${token}` } }
        // Claude держит ~/.claude.json открытым и может перезаписать правку: надёжнее его собственная команда.
        if (hasBin) {
          await run('claude', ['mcp', 'remove', SERVER, '--scope', 'user']).catch(() => {})
          await run('claude', ['mcp', 'add-json', SERVER, JSON.stringify(entry), '--scope', 'user'])
          return 'claude mcp add-json --scope user'
        }
        const file = join(home, '.claude.json')
        await writeConfig(file, (t) => mergeServers(t, entry, file))
        return file
      },
    },
    {
      id: 'codex', label: 'Codex', bin: 'codex', dir: join(home, '.codex'),
      skills: [join(home, '.agents', 'skills')],
      async mcp({ url, token }) {
        const file = join(home, '.codex', 'config.toml')
        const block = `[mcp_servers.${SERVER}]\nurl = ${tomlString(url)}\nhttp_headers = { Authorization = ${tomlString(`Bearer ${token}`)} }`
        await writeConfig(file, (t) => upsertTomlServer(t, block))
        return file
      },
    },
    {
      id: 'antigravity', label: 'Antigravity', bin: 'antigravity', dir: gemini,
      skills: [join(gemini, 'config', 'skills'), join(gemini, 'antigravity-cli', 'skills')],
      async mcp({ url, token }) {
        const file = join(gemini, 'config', 'mcp_config.json')
        // Antigravity понимает только serverUrl; url и httpUrl он не принимает.
        await writeConfig(file, (t) => mergeServers(t, { serverUrl: url, headers: { Authorization: `Bearer ${token}` } }, file))
        return file
      },
    },
    {
      id: 'cursor', label: 'Cursor', bin: 'cursor-agent', dir: join(home, '.cursor'),
      // Cursor читает навыки и из ~/.agents/skills, общего с Codex.
      skills: [join(home, '.agents', 'skills')],
      async mcp({ url, token }) {
        const file = join(home, '.cursor', 'mcp.json')
        await writeConfig(file, (t) => mergeServers(t, { url, headers: { Authorization: `Bearer ${token}` } }, file))
        return file
      },
    },
  ]
}

// writeConfig правит файл настроек; прежняя версия остаётся рядом как .airlok-backup.
async function writeConfig(file, edit) {
  const before = await readFile(file, 'utf8').catch((err) => {
    if (err.code === 'ENOENT') return null
    throw err
  })
  const after = edit(before)
  if (after === before) return
  await mkdir(dirname(file), { recursive: true })
  if (before !== null) await writeFile(file + '.airlok-backup', before, { mode: 0o600 })
  await writeFile(file, after, { mode: 0o600 })
}

// SKILLS — навыки для агентов: работа с airlok и подготовка airlok.yml проекта.
export const SKILLS = ['airlok', 'airlok-yml']

// skillSources — SKILL.md навыков по именам: в опубликованном пакете они лежат в cli/skills,
// в репозитории — в корне.
export async function skillSources() {
  for (const rel of ['../skills/', '../../skills/']) {
    const dir = fileURLToPath(new URL(rel, import.meta.url))
    const paths = Object.fromEntries(SKILLS.map((name) => [name, join(dir, name, 'SKILL.md')]))
    if ((await Promise.all(Object.values(paths).map(exists))).every(Boolean)) return paths
  }
  throw new Error(`В пакете нет навыков ${SKILLS.join(', ')} (skills/<имя>/SKILL.md)`)
}

// detect — какие агенты стоят: по папке в домашнем каталоге или программе в PATH.
export async function detect(list, env = process.env) {
  const found = []
  for (const a of list) {
    const hasBin = await onPath(a.bin, env)
    if (hasBin || (await exists(a.dir))) found.push({ ...a, hasBin })
  }
  return found
}

const execAsync = promisify(execFile)

// setupAgents настраивает выбранных агентов и возвращает по строке отчёта на каждого.
export async function setupAgents({ chosen, url, token, run, skills }) {
  const sources = skills ?? (await skillSources())
  const exec = run ?? ((cmd, args) => execAsync(cmd, args, { shell: process.platform === 'win32' }))
  const done = new Set()
  const report = []
  for (const a of chosen) {
    try {
      const where = await a.mcp({ url: `${url}/mcp`, token, run: exec, hasBin: a.hasBin })
      const skills = []
      for (const dir of a.skills) {
        // Каталог CLI Antigravity — только если он уже есть: CLI может быть не установлен.
        if (dir.includes('antigravity-cli') && !(await exists(dirname(dir)))) continue
        for (const [name, source] of Object.entries(sources)) {
          const target = join(dir, name, 'SKILL.md')
          if (done.has(target)) continue
          await mkdir(dirname(target), { recursive: true })
          await copyFile(source, target)
          done.add(target)
        }
        skills.push(dir)
      }
      report.push({ agent: a.label, ok: true, mcp: where, skills })
    } catch (err) {
      report.push({ agent: a.label, ok: false, error: err.stderr?.toString().trim() || err.message })
    }
  }
  return report
}

export const homePath = (p, home = homedir()) => (p.startsWith(home) ? '~' + p.slice(home.length) : p)
