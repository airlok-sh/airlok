// Страница рендерится на каждый запрос: видно, что работает сервер, а не статика.
export async function getServerSideProps() {
  return { props: { now: new Date().toISOString(), node: process.version } }
}

export default function Home({ now, node }) {
  return (
    <main style={{ maxWidth: 560, margin: '15vh auto', padding: 24, font: '17px/1.6 system-ui, sans-serif' }}>
      <h1>Next.js на хостинге airlok</h1>
      <p>Страница собрана на сервере в {now} (Node {node}).</p>
      <p><a href="/api/hello">/api/hello</a> — пример API-маршрута.</p>
    </main>
  )
}
