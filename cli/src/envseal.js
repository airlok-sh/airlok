// Шифрование переменных для приложения на своём сервере: под открытый ключ агента этого
// сервера, на этом компьютере. Дашборд хранит только шифротекст и прочитать его не может;
// расшифровывает агент (recipes/envkey.py). Тот же формат у дашборда в браузере.
//
// Значение — "v1:" и base64 блоков RSA-OAEP (SHA-256) по 384 байта, в каждом до 318 байт
// текста. Метка блока airlok-env/v1/<приложение>/<ИМЯ>/<номер>/<всего> не даёт подменить
// блок блоком другой переменной, переставить или отрезать.

import { webcrypto } from 'node:crypto'

const CHUNK = 318
const OAEP = { name: 'RSA-OAEP', hash: 'SHA-256' }
const utf8 = new TextEncoder()

const fromB64 = (s) => Uint8Array.from(Buffer.from(s, 'base64'))
const toB64 = (b) => Buffer.from(b).toString('base64')

// keyId — отпечаток ключа в том виде, что печатает агент на сервере: его считаем сами, а не
// берём у дашборда, чтобы человек сверял именно тот ключ, под который шифруем.
export async function keyId(publicKey, subtle = webcrypto.subtle) {
  const sum = await subtle.digest('SHA-256', fromB64(publicKey))
  return 'SHA256:' + toB64(new Uint8Array(sum)).replace(/=+$/, '')
}

export async function seal(publicKey, app, name, value, subtle = webcrypto.subtle) {
  const key = await subtle.importKey('spki', fromB64(publicKey), OAEP, false, ['encrypt'])
  const data = utf8.encode(value)
  const n = Math.max(1, Math.ceil(data.length / CHUNK))
  const out = new Uint8Array(n * 384)
  for (let i = 0; i < n; i++) {
    const label = utf8.encode(`airlok-env/v1/${app}/${name}/${i}/${n}`)
    const block = await subtle.encrypt({ name: 'RSA-OAEP', label }, key, data.subarray(i * CHUNK, (i + 1) * CHUNK))
    out.set(new Uint8Array(block), i * 384)
  }
  return 'v1:' + toB64(out)
}
