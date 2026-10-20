import { maxBytes, saveUpload } from "../../lib/uploads";

export const config = { api: { bodyParser: false } };

function readBody(req, limit) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on("data", (chunk) => {
      size += chunk.length;
      if (size > limit) {
        reject(new Error("too_large"));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => resolve(Buffer.concat(chunks)));
    req.on("error", reject);
  });
}

export default async function handler(req, res) {
  if (req.method !== "POST") {
    res.status(405).json({ error: "method_not_allowed" });
    return;
  }
  let body;
  try {
    body = await readBody(req, maxBytes);
  } catch {
    res.status(413).json({ error: "too_large" });
    return;
  }
  const name = await saveUpload(body);
  if (!name) {
    res.status(400).json({ error: "unsupported_type" });
    return;
  }
  res.status(200).json({ url: `/uploads/${name}` });
}
