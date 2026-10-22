// Упаковка папки проекта в tar.gz для выкатки. Архив устроен как у GitHub: всё лежит в одном
// верхнем каталоге, его сборщик отбрасывает. Те же правила, что у дашборда (web/src/folder.ts).

import { execFile } from 'node:child_process'
import { createReadStream } from 'node:fs'
import { lstat, readdir } from 'node:fs/promises'
import { join } from 'node:path'
import { promisify } from 'node:util'
import { createGzip } from 'node:zlib'

const run = promisify(execFile)

export const MAX_ARCHIVE = 50 << 20
const MAX_UNPACKED = 1 << 30
const MAX_FILES = 50_000

// Зависимости, сборки и кэши сборщик сделает сам, а git, секреты и привязка CLI в архиве не нужны.
const skipDirs = new Set([
  'node_modules', '.git', '.next', '.nuxt', '.svelte-kit', '.turbo', '.cache', '.parcel-cache', '.vercel', '.output',
  '.venv', 'venv', '__pycache__', '.pytest_cache', '.mypy_cache', '.idea', '.vscode', '.airlok',
])

// MTIME — время файлов в архиве. Постоянное, чтобы одинаковый код давал одинаковый архив:
// по хешу архива дашборд не пересобирает уже собранную версию. 2000-01-01, а не 1970:
// некоторые инструменты сборки недолюбливают нулевое время.
export const MTIME = 946_684_800

export class PackError extends Error {}

// skipped — файл не попадает в архив. .env.example и подобные оставляем: в них нет секретов,
// а по ним видно, какие переменные нужны.
export function skipped(path) {
  const parts = path.split('/')
  const name = parts[parts.length - 1]
  if (parts.slice(0, -1).some((p) => skipDirs.has(p))) return true
  if (name === '.DS_Store' || name === 'Thumbs.db') return true
  return /^\.env(\..+)?$/.test(name) && !/\.(example|sample|template)$/.test(name)
}

// listFiles — файлы проекта с путями через «/». В git-репозитории список даёт git, и
// .gitignore учитывается точно; иначе обходим папку сами.
export async function listFiles(dir) {
  const fromGit = await gitFiles(dir)
  const paths = fromGit ?? (await walk(dir, ''))
  const files = []
  for (const path of paths) {
    if (skipped(path)) continue
    const st = await lstat(join(dir, ...path.split('/'))).catch(() => null)
    // Ссылки не берём: сборщик их всё равно пропускает, а ссылка могла бы увести за пределы проекта.
    if (!st?.isFile()) continue
    files.push({ path, size: st.size, mode: st.mode & 0o111 ? 0o755 : 0o644 })
  }
  files.sort((a, b) => (a.path < b.path ? -1 : 1))
  return { files, git: fromGit !== null }
}

async function gitFiles(dir) {
  try {
    const { stdout } = await run('git', ['ls-files', '--cached', '--others', '--exclude-standard', '-z'], { cwd: dir, maxBuffer: 64 << 20 })
    return stdout.split('\0').filter(Boolean)
  } catch {
    return null
  }
}

async function walk(root, prefix) {
  const out = []
  for (const e of await readdir(join(root, prefix), { withFileTypes: true })) {
    const path = prefix ? `${prefix}/${e.name}` : e.name
    if (e.isDirectory()) {
      if (!skipDirs.has(e.name)) out.push(...(await walk(root, path)))
    } else if (e.isFile()) {
      out.push(path)
    }
  }
  return out
}

// checkFiles — понятная причина, почему папку не выкатить, ещё до упаковки.
export function checkFiles(files) {
  if (files.length === 0) return 'В папке нет файлов проекта.'
  if (files.length > MAX_FILES) return 'В папке больше 50 тысяч файлов — выкатывайте папку самого проекта.'
  if (files.reduce((n, f) => n + f.size, 0) > MAX_UNPACKED) return 'Файлы проекта занимают больше 1 ГБ.'
  return ''
}

// splitName раскладывает путь по полям name (100 байт) и prefix (155 байт) заголовка ustar.
export function splitName(path) {
  if (Buffer.byteLength(path) <= 100) return { name: path, prefix: '' }
  for (let i = path.indexOf('/'); i > 0; i = path.indexOf('/', i + 1)) {
    const prefix = path.slice(0, i)
    const name = path.slice(i + 1)
    if (Buffer.byteLength(prefix) <= 155 && Buffer.byteLength(name) <= 100) return { name, prefix }
  }
  throw new PackError(`Слишком длинный путь в проекте: ${path}`)
}

const octal = (n, width) => n.toString(8).padStart(width - 1, '0') + '\0'

export function tarHeader(path, size, mode, mtime) {
  const h = Buffer.alloc(512)
  const { name, prefix } = splitName(path)
  h.write(name, 0)
  h.write(octal(mode, 8), 100)
  h.write(octal(0, 8), 108)
  h.write(octal(0, 8), 116)
  h.write(octal(size, 12), 124)
  h.write(octal(mtime, 12), 136)
  h.write('        ', 148)
  h.write('0', 156)
  h.write('ustar\0', 257)
  h.write('00', 263)
  h.write(prefix, 345)
  let sum = 0
  for (const b of h) sum += b
  h.write(sum.toString(8).padStart(6, '0') + '\0 ', 148)
  return h
}

// pack собирает tar.gz из файлов папки dir. Размер файла берётся из списка: если файл
// успел вырасти, лишнее отрезается, если уменьшиться — дополняется нулями, иначе архив
// разошёлся бы с заголовками.
export async function pack(dir, files, top = 'src') {
  const gz = createGzip({ level: 6 })
  const chunks = []
  let total = 0
  gz.on('data', (c) => {
    total += c.length
    chunks.push(c)
  })
  const done = new Promise((ok, fail) => {
    gz.on('end', ok)
    gz.on('error', fail)
  })
  const write = (buf) => new Promise((ok) => (gz.write(buf) ? ok() : gz.once('drain', ok)))
  // Порядок файлов — тоже часть архива: сортируем, чтобы он не зависел от вызывающего.
  for (const f of [...files].sort((a, b) => (a.path < b.path ? -1 : 1))) {
    if (total > MAX_ARCHIVE) break
    await write(tarHeader(`${top}/${f.path}`, f.size, f.mode, MTIME))
    let left = f.size
    for await (const chunk of createReadStream(join(dir, ...f.path.split('/')))) {
      const part = chunk.subarray(0, left)
      left -= part.length
      await write(part)
      if (left === 0) break
    }
    const pad = left + ((512 - (f.size % 512)) % 512)
    if (pad) await write(Buffer.alloc(pad))
  }
  await write(Buffer.alloc(1024))
  gz.end()
  await done
  if (total > MAX_ARCHIVE) {
    throw new PackError(`Архив больше 50 МБ. Уберите из папки тяжёлые файлы (видео, архивы, выгрузки базы) или добавьте их в .gitignore.`)
  }
  return Buffer.concat(chunks)
}
