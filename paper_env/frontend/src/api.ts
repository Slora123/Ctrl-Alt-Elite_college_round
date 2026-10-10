export type Json = any;

export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}

export async function api<T = Json>(method: string, path: string, body?: unknown): Promise<T> {
  const r = await fetch(path, {
    method, headers: body !== undefined ? { 'content-type': 'application/json' } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const txt = await r.text();
  let data: any = null;
  try { data = txt ? JSON.parse(txt) : null; } catch { data = txt; }
  if (!r.ok) {
    const d = data?.detail;
    throw new ApiError(d ? (typeof d === 'string' ? d : JSON.stringify(d)) : r.statusText, r.status);
  }
  return data as T;
}

export const enc = (p: string) => p.split('/').map(encodeURIComponent).join('/');

export function toBase64(buf: ArrayBuffer | ArrayBufferView): string {
  const u8 = buf instanceof ArrayBuffer ? new Uint8Array(buf) : new Uint8Array(buf.buffer, buf.byteOffset, buf.byteLength);
  let s = '';
  for (let i = 0; i < u8.length; i += 0x8000) s += String.fromCharCode(...u8.subarray(i, i + 0x8000));
  return btoa(s);
}
export function fromBase64(b64: string): DataView {
  const s = atob(b64); const u8 = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) u8[i] = s.charCodeAt(i);
  return new DataView(u8.buffer);
}
