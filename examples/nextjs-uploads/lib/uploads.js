import { randomBytes } from "crypto";
import { mkdir, readdir, writeFile } from "fs/promises";
import path from "path";

const dir = process.env.UPLOAD_DIR || "/tmp/uploads";
export const maxBytes = Number(process.env.UPLOAD_MAX_BYTES || 5 * 1024 * 1024);

// Тип определяется по первым байтам файла: имени и Content-Type от клиента верить нельзя.
const signatures = [
  { ext: "jpg", test: (b) => b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff },
  { ext: "png", test: (b) => b.subarray(0, 8).equals(Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])) },
  { ext: "webp", test: (b) => b.toString("latin1", 0, 4) === "RIFF" && b.toString("latin1", 8, 12) === "WEBP" },
  { ext: "pdf", test: (b) => b.toString("latin1", 0, 5) === "%PDF-" },
];

export function detectType(buf) {
  return signatures.find((s) => s.test(buf))?.ext ?? null;
}

export async function saveUpload(buf) {
  const ext = detectType(buf);
  if (!ext) return null;
  await mkdir(dir, { recursive: true });
  const name = `${randomBytes(12).toString("hex")}.${ext}`;
  await writeFile(path.join(dir, name), buf, { flag: "wx" });
  return name;
}

export async function listUploads() {
  try {
    return (await readdir(dir)).filter((f) => /^[0-9a-f]{24}\.[a-z]+$/.test(f)).slice(0, 50);
  } catch {
    return [];
  }
}
