import { db } from '../../lib/db'

export default async function handler(req, res) {
  if (req.method !== 'POST') return res.status(405).end()
  const body = String(req.body?.body ?? '').trim().slice(0, 500)
  if (body) {
    const pool = await db()
    await pool.query('INSERT INTO notes (body) VALUES ($1)', [body])
  }
  res.redirect(303, '/')
}
