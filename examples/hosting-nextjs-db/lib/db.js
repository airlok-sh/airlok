import pg from 'pg'

// Один пул на процесс: приложение засыпает между запросами, соединений нужно немного.
const pool = globalThis.__pool ?? new pg.Pool({ connectionString: process.env.DATABASE_URL, max: 5 })
globalThis.__pool = pool

let ready
// Таблица создаётся при первом запросе: миграции здесь — одна строка.
export function db() {
  ready ??= pool.query('CREATE TABLE IF NOT EXISTS notes (id serial PRIMARY KEY, body text NOT NULL, created_at timestamptz NOT NULL DEFAULT now())')
  return ready.then(() => pool)
}
