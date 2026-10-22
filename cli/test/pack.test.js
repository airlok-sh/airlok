import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import { chmod, mkdir, mkdtemp, symlink, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import { gunzipSync } from 'node:zlib'

import { checkFiles, listFiles, pack, skipped, splitName } from '../src/pack.js'
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

test('listFiles follows .gitignore inside a git repository', async () => {
  const dir = await project({ '.gitignore': 'dist/\n*.log\n', 'index.js': 'x', 'dist/out.js': 'y', 'debug.log': 'z', 'web/app.js': 'w' })
  execFileSync('git', ['init', '-q'], { cwd: dir })
  const { files, git } = await listFiles(dir)
  assert.equal(git, true)
  assert.deepEqual(files.map((f) => f.path), ['.gitignore', 'index.js', 'web/app.js'])
  // Подпапка репозитория — её содержимое с путями от неё самой.
  assert.deepEqual((await listFiles(join(dir, 'web'))).files.map((f) => f.path), ['app.js'])
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
