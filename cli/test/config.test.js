import assert from 'node:assert/strict'
import { mkdtemp, mkdir, readFile, writeFile } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'

import { linkPath, readLink, saveLink } from '../src/config.js'

test('saveLink keeps fields written by the agent', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'airlok-link-'))
  await mkdir(join(dir, '.airlok'))
  await writeFile(linkPath(dir), JSON.stringify({ app_id: 'old', app: 'old', secrets_in_chat: true }))

  await saveLink(dir, { app_id: 'new', app: 'shop' })

  assert.deepEqual(JSON.parse(await readFile(linkPath(dir), 'utf8')), { app_id: 'new', app: 'shop', secrets_in_chat: true })
  assert.equal(await readFile(join(dir, '.airlok', '.gitignore'), 'utf8'), '*\n')
})

test('saveLink replaces a broken file', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'airlok-link-'))
  await mkdir(join(dir, '.airlok'))
  await writeFile(linkPath(dir), '{ сломано')

  await saveLink(dir, { app_id: 'id', app: 'shop' })

  assert.deepEqual(await readLink(dir), { app_id: 'id', app: 'shop' })
})
