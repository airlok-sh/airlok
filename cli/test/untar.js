import assert from 'node:assert/strict'

// untar разбирает tar обратно и заодно проверяет контрольные суммы заголовков.
export function untar(data) {
  const out = []
  for (let at = 0; at + 512 <= data.length; ) {
    const h = data.subarray(at, at + 512)
    if (h.every((b) => b === 0)) break
    const field = (from, len) => h.subarray(from, from + len).toString('utf8').replace(/\0.*$/s, '')
    const size = parseInt(field(124, 12), 8)
    let calc = 0
    for (let i = 0; i < 512; i++) calc += i >= 148 && i < 156 ? 32 : h[i]
    assert.equal(parseInt(field(148, 8), 8), calc)
    const prefix = field(345, 155)
    out.push({ path: (prefix ? prefix + '/' : '') + field(0, 100), mode: parseInt(field(100, 8), 8), body: data.subarray(at + 512, at + 512 + size).toString('utf8') })
    at += 512 + Math.ceil(size / 512) * 512
  }
  return out
}
