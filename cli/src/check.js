// Команда check: что airlok сделает с проектом — стек, команды, возможности и переменные —
// и что помешает выкатке. Проверяет дашборд тем же кодом, что и сборку, но ничего не собирает.

const stackLabel = { static: 'статический сайт', nextjs: 'Next.js', node: 'Node.js', python: 'Python', go: 'Go', docker: 'Dockerfile' }

function envLine(v, width) {
  const tags = [v.required ? 'обязательная' : v.default ? `по умолчанию ${v.default}` : 'необязательная']
  if (v.secret) tags.push('секрет')
  if (v.build) tags.push('в сборке')
  return `  ${v.name.padEnd(width)}  ${tags.join(', ')}${v.description ? ` — ${v.description}` : ''}`
}

// formatReport — отчёт для человека; пустые разделы не печатаются.
export function formatReport(r) {
  const lines = []
  const p = r.plan
  if (p) {
    const runtime = p.node ? `Node.js ${p.node}` : p.python ? `Python ${p.python}` : p.go ? `Go ${p.go}` : ''
    const what = [stackLabel[p.stack] ?? p.stack, p.framework && p.framework !== stackLabel[p.stack] ? p.framework : '', p.kind === 'worker' ? 'фоновое, без сайта' : 'сайт', runtime]
    lines.push(`Стек: ${what.filter(Boolean).join(' · ')}`)
    for (const [label, value] of [['установка', p.install], ['сборка', p.build], ['каталог сайта', p.output], ['запуск', p.start], ['перед запуском', p.release], ['порт', p.port]]) {
      if (value) lines.push(`  ${(label + ':').padEnd(16)}${value}`)
    }
    if (p.kind === 'web') lines.push(`  ${'проверка:'.padEnd(16)}${p.health ? `${p.health} — нужен ответ 2xx или 3xx` : 'главная — любой ответ, кроме 5xx'}`)
  }
  const caps = []
  if (r.database) caps.push('база PostgreSQL (DATABASE_URL)')
  if (r.uploads) caps.push(`загрузки: ${r.uploads.allowed.join(', ')}, до ${r.uploads.max_size_mb} МБ`)
  if (r.cron?.length) caps.push(`задач по расписанию: ${r.cron.length}`)
  if (caps.length) lines.push(`Возможности: ${caps.join('; ')}`)
  if (r.env.length) {
    const width = Math.max(...r.env.map((v) => v.name.length))
    lines.push(`Переменные (${r.env.length}):`, ...r.env.map((v) => envLine(v, width)))
  }
  const section = (title, items, mark) => items.length && lines.push('', title, ...items.map((s) => `  ${mark} ${s}`))
  section('Остановит выкатку:', r.errors, '✗')
  section('Проверьте:', r.warnings, '!')
  section('Подсказки:', r.notes, '·')
  section('На своём сервере:', r.server, '·')
  lines.push('', r.errors.length ? '✗ Выкатка с такими файлами не пройдёт.' : '✓ Можно выкатывать.')
  return lines.join('\n')
}
