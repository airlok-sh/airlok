// Упаковка папки проекта в tar.gz для выкатки. Архив устроен как у GitHub: всё лежит в одном
// верхнем каталоге, его сборщик отбрасывает. Те же правила, что у дашборда (web/src/folder.ts).

import { execFile } from 'node:child_process'
import { createReadStream } from 'node:fs'
import { lstat, readdir, readFile } from 'node:fs/promises'
import { isAbsolute, join, parse, relative, sep } from 'node:path'
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

// Ключи и учётные данные в архив не идут никогда, а базы и выгрузки — только с согласия
// человека: сборке они бывают нужны. Те же правила у дашборда (web/src/folder.ts), а сборщик
// статики и edge не отдают такие файлы с сайта (cluster.PrivateFile).
const privateDirs = new Set(['.ssh', '.aws', '.gnupg'])
const secretNames = new Set(['.pypirc', '.netrc', '.git-credentials', '.envrc', '.dev.vars', 'secrets.toml'])
const secretExts = ['.pem', '.key', '.p12', '.pfx', '.kdbx']
const sshKeys = ['id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519']
const cloudKeys = ['client_secret', 'service-account', 'service_account']
const dataNames = new Set(['credentials.json', 'token.json'])
const dataExts = ['.sqlite', '.sqlite3', '.db', '.dump']
// .sql — чаще код проекта (миграции, схема, начальные данные), чем выгрузка базы. Выгрузку
// узнаём по имени или размеру, а в каталогах миграций .sql всегда код.
const sqlCodeDirs = new Set(['migrations', 'migration', 'prisma', 'supabase', 'drizzle'])
const SQL_DUMP_SIZE = 1 << 20
// .pem и .key с одними сертификатами (CA базы вроде global-bundle.pem) открыты и нужны приложению;
// больше такого размера сертификаты не бывают — такой файл не читаем и считаем ключом.
export const CERT_READ_LIMIT = 1 << 20

// privateKind — 'secret' для ключа или учётных данных, 'data' для базы или выгрузки, иначе ''.
// size нужен для .sql: большой файл — выгрузка.
export function privateKind(path, size = 0) {
  const parts = path.toLowerCase().split('/')
  const name = parts[parts.length - 1]
  const dirs = parts.slice(0, -1)
  if (dirs.some((p) => privateDirs.has(p))) return 'secret'
  if (secretNames.has(name) || secretExts.some((e) => name.endsWith(e))) return 'secret'
  if (sshKeys.some((k) => name.startsWith(k))) return name.endsWith('.pub') ? '' : 'secret'
  if (name.endsWith('.json') && cloudKeys.some((k) => name.startsWith(k))) return 'secret'
  if (dataNames.has(name) || dataExts.some((e) => name.endsWith(e))) return 'data'
  if (name.endsWith('.sql') && !dirs.some((p) => sqlCodeDirs.has(p)) && (/dump|backup/.test(name) || size > SQL_DUMP_SIZE)) return 'data'
  return ''
}

// certCheck — .pem или .key, по содержимому которого видно, ключ это или сертификат.
export function certCheck(path) {
  const parts = path.toLowerCase().split('/')
  return /\.(pem|key)$/.test(parts[parts.length - 1]) && !parts.slice(0, -1).some((p) => privateDirs.has(p))
}

// certOnly — в файле одни сертификаты и нет закрытого ключа. Неизвестное содержимое (DER,
// пустой файл) — не сертификат: лучше не загрузить лишнее, чем выложить ключ.
export function certOnly(text) {
  return /-----BEGIN (?:TRUSTED )?CERTIFICATE-----/.test(text) && !/PRIVATE KEY/.test(text)
}

// npmrcSecret — в .npmrc записан сам токен реестра. Без токена .npmrc нужен сборке
// (legacy-peer-deps, адрес реестра), а ссылка на переменную ${NPM_TOKEN} секрета не содержит.
export function npmrcSecret(text) {
  return /^\s*(?:\/\/\S*?:)?(?:_authToken|_auth|_password|password)\s*=\s*(?!["']?\$\{)\S/im.test(text)
}

// secretShown — как показать скрытый файл: из каталога с ключами — сам каталог.
function secretShown(path) {
  const parts = path.split('/')
  const i = parts.findIndex((p, j) => j < parts.length - 1 && privateDirs.has(p.toLowerCase()))
  return i < 0 ? path : `${parts.slice(0, i + 1).join('/')}/`
}

// homeProblem — dir (без ссылок) — корень диска, домашняя папка или папка над ней: там ключи
// и личные файлы, а не проект.
export function homeProblem(dir, home) {
  const rel = relative(dir, home)
  if (dir !== parse(dir).root && (rel.split(sep)[0] === '..' || isAbsolute(rel))) return ''
  return `Папка ${dir} — домашняя или выше неё: в ней ключи и личные файлы. Перейдите в папку самого проекта и публикуйте её.`
}

// listFiles — файлы проекта с путями через «/». В git-репозитории список даёт git, и
// .gitignore учитывается точно; иначе обходим папку сами. Папку из .gitignore (dist, build)
// git не перечислит, а публикуют её как раз целиком — тогда тоже обходим сами, ignored = true.
// allIgnored — сама папка не в .gitignore, но git не дал из неё ни одного файла (в ней свой
// .gitignore с «*»): её тоже обходим сами, а человеку об этом сообщаем.
// secrets — пропущенные ключи и учётные данные, data — базы и выгрузки: в files их нет,
// взять их решает вызывающий.
export async function listFiles(dir) {
  const fromGit = await gitFiles(dir)
  const ignored = fromGit !== null && (await gitIgnored(dir))
  const useGit = fromGit !== null && fromGit.length > 0 && !ignored
  const paths = useGit ? fromGit : await walk(dir, '')
  const allIgnored = fromGit !== null && !useGit && !ignored && paths.length > 0
  const files = []
  const data = []
  const secrets = new Set()
  for (const path of paths) {
    if (path.endsWith('/')) {
      secrets.add(path)
      continue
    }
    if (skipped(path)) continue
    const full = join(dir, ...path.split('/'))
    const st = await lstat(full).catch(() => null)
    // Ссылки не берём: сборщик их всё равно пропускает, а ссылка могла бы увести за пределы проекта.
    if (!st?.isFile()) continue
    let kind = privateKind(path, st.size)
    if (!kind && path.split('/').pop() === '.npmrc' && npmrcSecret(await readFile(full, 'utf8').catch(() => ''))) kind = 'secret'
    if (kind === 'secret' && certCheck(path) && st.size <= CERT_READ_LIMIT && certOnly(await readFile(full, 'latin1').catch(() => ''))) kind = ''
    if (kind === 'secret') {
      secrets.add(secretShown(path))
      continue
    }
    const entry = { path, size: st.size, mode: st.mode & 0o111 ? 0o755 : 0o644 }
    if (kind === 'data') data.push(entry)
    else files.push(entry)
  }
  const byPath = (a, b) => (a.path < b.path ? -1 : 1)
  files.sort(byPath)
  data.sort(byPath)
  return { files, git: useGit, ignored, allIgnored, secrets: [...secrets].sort(), data }
}

// gitIgnored — папка сама или через родительскую в .gitignore. Путь проверяем от корня
// репозитория и без «/» в конце: «.» git превращает в «папка/», а с ним совпадает шаблон
// «папка/*», который игнорирует содержимое, но не саму папку.
async function gitIgnored(dir) {
  try {
    const { stdout } = await run('git', ['rev-parse', '--show-toplevel', '--show-prefix'], { cwd: dir })
    const [top, prefix = ''] = stdout.split('\n')
    const path = prefix.replace(/\/$/, '')
    if (!path) return false
    await run('git', ['check-ignore', '-q', '--', path], { cwd: top })
    return true
  } catch {
    return false
  }
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
      // В каталог с ключами не заходим: его покажем целиком.
      if (privateDirs.has(e.name.toLowerCase())) out.push(`${path}/`)
      else if (!skipDirs.has(e.name)) out.push(...(await walk(root, path)))
    } else if (e.isFile()) {
      out.push(path)
    }
  }
  return out
}

// checkFiles — понятная причина, почему папку не выкатить, ещё до упаковки.
export function checkFiles(files) {
  if (files.length === 0) return 'В папке нет файлов проекта.'
  if (files.length > MAX_FILES) return 'В папке больше 50 тысяч файлов — публикуйте папку самого проекта.'
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
// разошёлся бы с заголовками. gitignore — список дал git, и совет про .gitignore поможет.
export async function pack(dir, files, { top = 'src', gitignore = false } = {}) {
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
    const hint = gitignore ? ' или добавьте их в .gitignore' : ''
    throw new PackError(`Архив больше 50 МБ. Уберите из папки тяжёлые файлы (видео, архивы, выгрузки базы)${hint}.`)
  }
  return Buffer.concat(chunks)
}
