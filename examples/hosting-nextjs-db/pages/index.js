import { db } from '../lib/db'

export async function getServerSideProps() {
  const pool = await db()
  const { rows } = await pool.query('SELECT id, body, created_at::text AS created_at FROM notes ORDER BY id DESC LIMIT 20')
  return { props: { notes: rows, greeting: process.env.GREETING ?? '' } }
}

export default function Home({ notes, greeting }) {
  return (
    <main style={{ maxWidth: 560, margin: '10vh auto', padding: 24, font: '17px/1.6 system-ui, sans-serif' }}>
      <h1>{greeting || 'Гостевая книга'}</h1>
      <p>Next.js и Postgres на хостинге airlok. Записи хранятся в базе приложения.</p>
      <form method="post" action="/api/notes" style={{ display: 'flex', gap: 8, margin: '24px 0' }}>
        <input name="body" required maxLength={500} placeholder="Оставьте запись" style={{ flex: 1, padding: 10, font: 'inherit' }} />
        <button type="submit" style={{ padding: '10px 16px', font: 'inherit' }}>Добавить</button>
      </form>
      <ul>
        {notes.map((n) => <li key={n.id}>{n.body} <small style={{ color: 'gray' }}>{n.created_at.slice(0, 16)}</small></li>)}
      </ul>
    </main>
  )
}
