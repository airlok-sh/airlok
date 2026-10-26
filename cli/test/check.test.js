import assert from 'node:assert/strict'
import { mkdtemp, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'node:test'
import { gunzipSync } from 'node:zlib'

import { formatReport } from '../src/check.js'
import { main } from '../src/main.js'
import { untar } from './untar.js'

const report = {
  manifest: true, errors: [], warnings: ['airlok.yml, строка 2: неизвестное поле capabilites'], notes: [], server: ['cron только на хостинге'],
  plan: { stack: 'nextjs', framework: 'Next.js', kind: 'web', node: '22', install: 'npm ci', build: 'npm run build', start: 'next start', health: '/api/health' },
  env: [
    { name: 'OPENAI_API_KEY', required: true, secret: true, description: 'Ключ API', source: 'airlok.yml' },
    { name: 'NEXT_PUBLIC_TITLE', required: false, default: 'Магазин', build: true, source: '.env.example' },
  ],
  database: true, uploads: { enabled: true, allowed: ['jpg', 'png'], max_size_mb: 10 },
}

test('formatReport shows what airlok will do', () => {
  const text = formatReport(report)
  assert.match(text, /^Стек: Next\.js · сайт · Node\.js 22$/m)
  assert.match(text, /запуск: {9}next start/)
  assert.match(text, /проверка: +\/api\/health — нужен ответ 2xx или 3xx/)
  assert.match(text, /Возможности: база PostgreSQL \(DATABASE_URL\); загрузки: jpg, png, до 10 МБ/)
  assert.match(text, /OPENAI_API_KEY {5}обязательная, секрет — Ключ API/)
  assert.match(text, /NEXT_PUBLIC_TITLE {2}по умолчанию Магазин, в сборке/)
  assert.match(text, /Проверьте:\n {2}! airlok\.yml, строка 2/)
  assert.match(text, /На своём сервере:\n {2}· cron только на хостинге/)
  assert.match(text, /✓ Можно выкатывать\.$/)
  assert.doesNotMatch(text, /Остановит выкатку/)
  assert.match(formatReport({ ...report, notes: null, server: null, warnings: null }), /✓ Можно выкатывать\.$/, 'null вместо пустого списка не ломает отчёт')
  const failed = formatReport({ ...report, plan: undefined, errors: ['airlok.yml: ожидается version: 1'], warnings: [], server: [] })
  assert.match(failed, /Остановит выкатку:\n {2}✗ airlok\.yml: ожидается version: 1/)
  assert.match(failed, /✗ Выкатка с такими файлами не пройдёт\.$/)
})

test('check packs the folder and exits 1 when the deploy would stop', async (t) => {
  const state = { archive: null, reply: report }
  const server = createServer(async (req, res) => {
    const chunks = []
    for await (const c of req) chunks.push(c)
    if (req.method === 'POST' && req.url === '/api/cli/v1/check' && req.headers.authorization === 'Bearer t') {
      state.archive = Buffer.concat(chunks)
      res.writeHead(200, { 'Content-Type': 'application/json' })
      return res.end(JSON.stringify(state.reply))
    }
    res.writeHead(404).end()
  })
  await new Promise((ok) => server.listen(0, '127.0.0.1', ok))
  t.after(() => new Promise((ok) => server.close(ok)))
  const env = { AIRLOK_URL: `http://127.0.0.1:${server.address().port}`, AIRLOK_TOKEN: 't', XDG_CONFIG_HOME: tmpdir() }

  const dir = await mkdtemp(join(tmpdir(), 'airlok-check-'))
  await writeFile(join(dir, 'package.json'), '{}')
  await writeFile(join(dir, '.env'), 'SECRET=1')
  const io = { outLines: [], errLines: [], out: (s) => io.outLines.push(s), err: (s) => io.errLines.push(s) }
  assert.equal(await main(['check', dir, '--json'], io, env), 0)
  assert.deepEqual(untar(gunzipSync(state.archive)).map((e) => e.path).sort(), ['src/package.json'], '.env на проверку не уходит')
  assert.equal(JSON.parse(io.outLines[0]).status, 'ok')

  state.reply = { ...report, errors: ['не нашли package main'] }
  const bad = { outLines: [], errLines: [], out: (s) => bad.outLines.push(s), err: (s) => bad.errLines.push(s) }
  assert.equal(await main(['check', dir], bad, env), 1)
  assert.match(bad.outLines.join('\n'), /✗ не нашли package main/)
  assert.match(bad.errLines.join('\n'), /архив проверяется на сервере и сразу удаляется/)
})
