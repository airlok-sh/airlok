import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { createHash, randomBytes } from 'node:crypto'
import { chmod, mkdir, mkdtemp, symlink, utimes, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import { gunzipSync } from 'node:zlib'

import { certOnly, checkFiles, homeProblem, ignoredBy, ignoreFilter, ignoreRules, listFiles, npmrcSecret, pack, privateKind, skipped, splitName } from '../src/pack.js'
import { untar } from './untar.js'

async function project(files) {
  const dir = await mkdtemp(join(tmpdir(), 'airlok-cli-'))
  for (const [path, body] of Object.entries(files)) {
    await mkdir(join(dir, path, '..'), { recursive: true })
    await writeFile(join(dir, path), body)
  }
  return dir
}

test('skipped drops dependencies, builds, git, secrets and the cli link', () => {
  for (const p of ['node_modules/react/index.js', 'web/node_modules/x.js', '.git/config', '.next/x.js', '.env', '.env.local', '.airlok/project.json']) {
    assert.equal(skipped(p), true, p)
  }
  for (const p of ['package.json', 'src/App.tsx', '.env.example', 'dist/index.html', '.gitignore']) {
    assert.equal(skipped(p), false, p)
  }
})

test('listFiles walks a plain folder and keeps the exec bit', async () => {
  const dir = await project({ 'index.html': 'hi', 'run.sh': '#!/bin/sh', 'node_modules/x/i.js': 'x', '.env': 'SECRET=1', 'src/a.js': 'a' })
  await chmod(join(dir, 'run.sh'), 0o755)
  await symlink('/etc/passwd', join(dir, 'passwd'))
  const { files, git } = await listFiles(dir)
  assert.equal(git, false)
  assert.deepEqual(files.map((f) => f.path), ['index.html', 'run.sh', 'src/a.js'])
  assert.equal(files.find((f) => f.path === 'run.sh').mode, 0o755)
})

test('privateKind tells keys from data and leaves the rest', () => {
  for (const p of ['server.pem', 'keys/Site.KEY', 'cert.p12', 'id_rsa', 'deploy/id_ed25519', '.pypirc', '.netrc', '.git-credentials', '.envrc',
    '.dev.vars', '.streamlit/secrets.toml', 'client_secret_123.apps.json', 'service-account.json', '.ssh/config', 'home/.aws/credentials']) {
    assert.equal(privateKind(p), 'secret', p)
  }
  for (const p of ['data.db', 'prisma/dev.sqlite', 'dump.sql', 'backups/db_backup.sql', 'backup.dump', 'credentials.json', 'token.json', 'db.sqlite3']) {
    assert.equal(privateKind(p), 'data', p)
  }
  for (const p of ['index.html', 'id_rsa.pub', 'id_ed25519.pub', 'keyboard.js', 'tokens.json', 'src/db.js', 'schema.prisma', '.npmrc', 'migrations/001.sql.go',
    'schema.sql', 'seed.sql', 'db/migrations/0001_init.sql', 'prisma/migrations/20250101_init/migration.sql', 'supabase/migrations/1_dump.sql']) {
    assert.equal(privateKind(p), '', p)
  }
  assert.equal(privateKind('export.sql', 2 << 20), 'data', 'большой .sql — выгрузка')
  assert.equal(privateKind('supabase/migrations/1_remote_schema.sql', 2 << 20), '', 'миграция любого размера — код')
})

test('certOnly lets through certificates but not keys or unknown files', () => {
  const cert = '-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n'
  assert.equal(certOnly(cert + cert), true)
  assert.equal(certOnly(cert + '-----BEGIN PRIVATE KEY-----\nMIIE\n-----END PRIVATE KEY-----\n'), false)
  assert.equal(certOnly('-----BEGIN OPENSSH PRIVATE KEY-----\nb3Bl\n'), false)
  assert.equal(certOnly('\x30\x82\x01'), false)
})

test('npmrcSecret finds a written token but not settings or ${VAR}', () => {
  assert.equal(npmrcSecret('//registry.npmjs.org/:_authToken=npm_abc123\n'), true)
  assert.equal(npmrcSecret('_auth = dXNlcjpwYXNz'), true)
  assert.equal(npmrcSecret('legacy-peer-deps=true\nregistry=https://registry.npmmirror.com/\n'), false)
  assert.equal(npmrcSecret('//npm.pkg.github.com/:_authToken=${NPM_TOKEN}\n'), false)
  assert.equal(npmrcSecret('//npm.pkg.github.com/:_authToken="${NPM_TOKEN}"\n'), false)
  assert.equal(npmrcSecret("_authToken='npm_abc'"), true)
})

test('listFiles holds back keys and data in a plain folder', async () => {
  const dir = await project({
    'index.html': 'hi', 'id_rsa': 'k', 'id_rsa.pub': 'p', '.ssh/config': 'c', 'certs/server.pem': 'x', '.npmrc': '//r/:_authToken=npm_x',
    'web/.npmrc': 'legacy-peer-deps=true', 'data.db': 'd', 'credentials.json': '{}',
  })
  const { files, secrets, data } = await listFiles(dir)
  assert.deepEqual(files.map((f) => f.path), ['id_rsa.pub', 'index.html', 'web/.npmrc'])
  assert.deepEqual(secrets, ['.npmrc', '.ssh/', 'certs/server.pem', 'id_rsa'])
  assert.deepEqual(data.map((f) => f.path), ['credentials.json', 'data.db'])
})

test('listFiles holds back untracked keys in a git repository', async () => {
  const dir = await project({ 'index.js': 'x', 'id_ed25519': 'k', '.ssh/known_hosts': 'h', '.ssh/config': 'c', 'dump.sql': 's' })
  execFileSync('git', ['init', '-q'], { cwd: dir })
  const { files, secrets, data } = await listFiles(dir)
  assert.deepEqual(files.map((f) => f.path), ['index.js'])
  assert.deepEqual(secrets, ['.ssh/', 'id_ed25519'])
  assert.deepEqual(data.map((f) => f.path), ['dump.sql'])
})

test('listFiles keeps migrations and CA certificates, holds back dumps and private keys', async () => {
  const cert = '-----BEGIN CERTIFICATE-----\nMIIB\n-----END CERTIFICATE-----\n'
  const dir = await project({
    'package.json': '{}', 'schema.sql': 'create table t();', 'db/migrations/0001_init.sql': 'create table a();',
    'prisma/migrations/20250101_init/migration.sql': 'create table b();', 'prisma/migrations/migration_lock.toml': 'x',
    'backup.sql': 'insert', 'certs/global-bundle.pem': cert + cert, 'certs/server.pem': cert + '-----BEGIN RSA PRIVATE KEY-----\nx\n',
    'big.sql': 'x'.repeat((1 << 20) + 1),
  })
  execFileSync('git', ['init', '-q'], { cwd: dir })
  execFileSync('git', ['add', '.'], { cwd: dir })
  const { files, secrets, data } = await listFiles(dir)
  assert.deepEqual(files.map((f) => f.path), ['certs/global-bundle.pem', 'db/migrations/0001_init.sql', 'package.json',
    'prisma/migrations/20250101_init/migration.sql', 'prisma/migrations/migration_lock.toml', 'schema.sql'])
  assert.deepEqual(secrets, ['certs/server.pem'])
  assert.deepEqual(data.map((f) => f.path), ['backup.sql', 'big.sql'])
})

test('homeProblem refuses the home folder, its parents and the root', () => {
  assert.match(homeProblem('/home/anna', '/home/anna'), /домашняя/)
  assert.match(homeProblem('/home', '/home/anna'), /домашняя/)
  assert.match(homeProblem('/', '/home/anna'), /домашняя/)
  assert.equal(homeProblem('/home/anna/site', '/home/anna'), '')
  assert.equal(homeProblem('/srv/site', '/home/anna'), '')
  assert.equal(homeProblem('/home/annabel', '/home/anna'), '')
})

test('listFiles follows .gitignore inside a git repository', async () => {
  const dir = await project({ '.gitignore': 'dist/\n*.log\n', 'index.js': 'x', 'dist/out.js': 'y', 'debug.log': 'z', 'web/app.js': 'w' })
  execFileSync('git', ['init', '-q'], { cwd: dir })
  const { files, git } = await listFiles(dir)
  assert.equal(git, true)
  assert.deepEqual(files.map((f) => f.path), ['.gitignore', 'index.js', 'web/app.js'])
  // Подпапка репозитория — её содержимое с путями от неё самой.
  assert.deepEqual((await listFiles(join(dir, 'web'))).files.map((f) => f.path), ['app.js'])
})

test('listFiles takes a folder from .gitignore whole', async () => {
  const dir = await project({
    '.gitignore': 'dist\n*.log\nbuild/\n', 'index.js': 'x', 'dist/index.html': 'h', 'dist/assets/a.js': 'a', 'dist/debug.log': 'l',
    'dist/node_modules/x.js': 'n', 'build/site/i.html': 'i', 'web/app.js': 'w', 'empty/.keep.log': 'k',
  })
  execFileSync('git', ['init', '-q'], { cwd: dir })
  const dist = await listFiles(join(dir, 'dist'))
  assert.equal(dist.ignored, true)
  assert.equal(dist.git, false)
  assert.deepEqual(dist.files.map((f) => f.path), ['assets/a.js', 'debug.log', 'index.html'])
  assert.deepEqual((await listFiles(join(dir, 'build', 'site'))).files.map((f) => f.path), ['i.html'], 'папка внутри игнорируемой')
  const web = await listFiles(join(dir, 'web'))
  assert.equal(web.ignored, false)
  assert.equal(web.git, true)
  assert.equal(web.allIgnored, false)
  // Сама папка не в .gitignore, но git не дал ни одного файла — обходим её сами и говорим об этом.
  const empty = await listFiles(join(dir, 'empty'))
  assert.deepEqual(empty.files.map((f) => f.path), ['.keep.log'])
  assert.equal(empty.ignored, false)
  assert.equal(empty.allIgnored, true)
  assert.equal((await listFiles(dir)).ignored, false, 'корень репозитория')
})

test('listFiles keeps .gitignore for a folder whose contents are ignored but not itself', async () => {
  const dir = await project({
    '.gitignore': 'public/*\n!public/index.html\n', 'public/index.html': 'h', 'public/uploads/private.txt': 'p', 'public/.gitkeep': '',
  })
  execFileSync('git', ['init', '-q'], { cwd: dir })
  const pub = await listFiles(join(dir, 'public'))
  assert.equal(pub.ignored, false)
  assert.equal(pub.allIgnored, false)
  assert.equal(pub.git, true)
  assert.deepEqual(pub.files.map((f) => f.path), ['index.html'])
})

// Те же случаи, что в тестах дашборда (web/src/folder.test.ts): правила должны совпадать.
test('ignoredBy excludes what git would and skips lines it does not understand', () => {
  const rules = ignoreRules([
    '# comment', '', 'leads.csv', '*.log', 'uploads/', '/root-only.txt', 'docs/*.md', '!docs/keep.md', '**/cache', 'out/**',
    'a/**/z.txt', 'tmp', '!tmp/keep.txt', 'b[0-9].txt', 'weird\\ name', 'trail.txt   ',
  ].join('\n'))
  for (const p of ['leads.csv', 'data/leads.csv', 'app.log', 'logs/x/app.log', 'uploads/a.png', 'web/uploads/b.png', 'root-only.txt',
    'docs/a.md', 'cache/x', 'web/cache/x', 'out/index.html', 'out/a/b.js', 'a/z.txt', 'a/b/c/z.txt', 'tmp/keep.txt', 'trail.txt']) {
    assert.equal(ignoredBy(rules, p), true, p)
  }
  for (const p of ['index.html', 'uploads', 'web/root-only.txt', 'docs/keep.md', 'docs/sub/a.md', 'out', 'outer/a.js', 'b1.txt', 'weird name', 'log.txt']) {
    assert.equal(ignoredBy(rules, p), false, p)
  }
})

test('ignoreFilter drops .gitignore that would leave nothing, but always applies .airlokignore', () => {
  const paths = ['.gitignore', 'index.html', 'app.js', 'notes.txt', 'node_modules/x.js']
  const kept = (git, own) => {
    const ignored = ignoreFilter(paths, git, own)
    return paths.filter((p) => !ignored(p))
  }
  assert.deepEqual(kept('notes.txt', ''), ['.gitignore', 'index.html', 'app.js', 'node_modules/x.js'])
  assert.deepEqual(kept('*\n!.gitignore', ''), paths)
  assert.deepEqual(kept('*', 'app.js'), ['.gitignore', 'index.html', 'notes.txt', 'node_modules/x.js'])
})

test('listFiles follows .gitignore without git and .airlokignore always', async () => {
  const plain = await project({ '.gitignore': 'leads.csv\nuploads/\n', 'index.html': 'h', 'leads.csv': 'phone', 'uploads/a.png': 'p', '.airlokignore': 'notes.md', 'notes.md': 'n' })
  assert.deepEqual((await listFiles(plain)).files.map((f) => f.path), ['.airlokignore', '.gitignore', 'index.html'])
  const repo = await project({ '.gitignore': 'debug.log\n', 'index.js': 'x', 'debug.log': 'l', '.airlokignore': 'docs/\n', 'docs/a.md': 'd' })
  execFileSync('git', ['init', '-q'], { cwd: repo })
  const { files, git } = await listFiles(repo)
  assert.equal(git, true)
  assert.deepEqual(files.map((f) => f.path), ['.airlokignore', '.gitignore', 'index.js'])
  const site = await project({ '.gitignore': '*\n!.gitignore\n', 'index.html': 'h', 'a.js': 'a' })
  assert.deepEqual((await listFiles(site)).files.map((f) => f.path), ['.gitignore', 'a.js', 'index.html'], '.gitignore папки сборки не учитываем')
})

test('pack makes a gzipped ustar archive under one top folder', async () => {
  const deep = `components/${'c'.repeat(70)}/${'d'.repeat(50)}.tsx`
  const dir = await project({ 'index.html': '<h1>Привет</h1>', [deep]: 'x'.repeat(700) })
  const { files } = await listFiles(dir)
  const entries = untar(gunzipSync(await pack(dir, files)))
  assert.deepEqual(entries, [
    { path: `src/${deep}`, mode: 0o644, body: 'x'.repeat(700) },
    { path: 'src/index.html', mode: 0o644, body: '<h1>Привет</h1>' },
  ])
})

test('splitName moves folders to prefix and refuses long file names', () => {
  const long = `src/${'a'.repeat(60)}/${'b'.repeat(60)}.tsx`
  const { name, prefix } = splitName(long)
  assert.equal(`${prefix}/${name}`, long)
  assert.throws(() => splitName(`src/${'я'.repeat(60)}.txt`), /Слишком длинный путь/)
})

test('checkFiles explains an empty folder', () => {
  assert.match(checkFiles([]), /нет файлов/)
  assert.equal(checkFiles([{ path: 'a', size: 1 }]), '')
})

test('pack suggests .gitignore only when git listed the files', async () => {
  const dir = await project({ 'video.bin': randomBytes(51 << 20) })
  const { files } = await listFiles(dir)
  await assert.rejects(pack(dir, files), (err) => /больше 50 МБ/.test(err.message) && !/gitignore/.test(err.message))
  await assert.rejects(pack(dir, files, { gitignore: true }), /добавьте их в \.gitignore/)
})

test('pack gives the same archive for the same code', async (t) => {
  const dir = await project({ 'index.html': '<h1>hi</h1>', 'src/app.js': 'x', 'run.sh': '#!/bin/sh' })
  const { files } = await listFiles(dir)
  const sha = (b) => createHash('sha256').update(b).digest('hex')
  const first = sha(await pack(dir, files))
  // Ни время файлов, ни текущее время, ни порядок в списке не меняют архив.
  await utimes(join(dir, 'index.html'), new Date(Date.UTC(2030, 0, 1)), new Date(Date.UTC(2030, 0, 1)))
  t.mock.method(Date, 'now', () => Date.UTC(2031, 0, 1))
  assert.equal(sha(await pack(dir, [...files].reverse())), first)

  await writeFile(join(dir, 'index.html'), '<h1>hello</h1>')
  const changed = (await listFiles(dir)).files
  assert.notEqual(sha(await pack(dir, changed)), first, 'другой код — другой архив')
})
