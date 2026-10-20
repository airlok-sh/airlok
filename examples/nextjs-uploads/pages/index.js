import { useState } from "react";
import { query } from "../lib/db";
import { listUploads } from "../lib/uploads";

export async function getServerSideProps() {
  await query("CREATE TABLE IF NOT EXISTS visits (id serial PRIMARY KEY, at timestamptz NOT NULL DEFAULT now())");
  await query("INSERT INTO visits DEFAULT VALUES");
  const { rows } = await query("SELECT count(*)::int AS n FROM visits");
  return { props: { visits: rows[0].n, files: await listUploads() } };
}

export default function Home({ visits, files }) {
  const [status, setStatus] = useState("");

  async function upload(e) {
    const file = e.target.files?.[0];
    if (!file) return;
    setStatus("Загружаю…");
    const res = await fetch("/api/upload", { method: "POST", body: file });
    const data = await res.json();
    setStatus(res.ok ? `Готово: ${data.url}` : `Ошибка: ${data.error}`);
  }

  return (
    <main style={{ fontFamily: "system-ui", maxWidth: 640, margin: "40px auto", padding: "0 16px" }}>
      <h1>Uploads demo</h1>
      <p>Посещений: {visits}</p>
      <input type="file" accept=".jpg,.jpeg,.png,.webp,.pdf" onChange={upload} />
      <p>{status}</p>
      <ul>
        {files.map((f) => (
          <li key={f}>
            <a href={`/uploads/${f}`}>{f}</a>
          </li>
        ))}
      </ul>
    </main>
  );
}
