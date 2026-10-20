import { Pool } from "pg";

// В dev-режиме модуль перезагружается, пул храним глобально, чтобы не плодить соединения.
const pool = globalThis.pgPool ?? (globalThis.pgPool = new Pool({ connectionString: process.env.DATABASE_URL }));

export const query = (text, params) => pool.query(text, params);
