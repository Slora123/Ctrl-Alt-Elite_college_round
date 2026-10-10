export type Json = any;

export class ApiError extends Error {
  constructor(message: string, public status: number) { super(message); }
}

/** URL prefix of the API. The UI is served at <prefix>/ui/, so this is '' standalone and '/paper-env' when mounted. */
export const BASE = location.pathname.replace(/\/ui(\/.*)?$/, '');

const TOKEN_KEY = 'cg.token'; // shared with the CrisisGrid website (same origin), so one login covers both
export const getToken = (): string | null => { try { return localStorage.getItem(TOKEN_KEY); } catch { return null; } };
export const setToken = (t: string | null) => { try { t ? localStorage.setItem(TOKEN_KEY, t) : localStorage.removeItem(TOKEN_KEY); } catch { /* storage blocked */ } };

let onUnauthorized: () => void = () => {};
export const setUnauthorizedHandler = (fn: () => void) => { onUnauthorized = fn; };

export const authHeaders = (): Record<string, string> => { const t = getToken(); return t ? { Authorization: `Bearer ${t}` } : {}; };
/** For URLs a browser fetches without our headers (downloads, WebSockets). */
export const withToken = (url: string): string => { const t = getToken(); return t ? url + (url.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(t) : url; };
export const wsUrl = (path: string): string => withToken(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}${BASE}${path}`);

async function parse(r: Response): Promise<any> {
  const txt = await r.text();
  try { return txt ? JSON.parse(txt) : null; } catch { return txt; }
}
function fail(r: Response, data: any): never {
  const d = data?.detail;
  if (r.status === 401) onUnauthorized();
  throw new ApiError(d ? (typeof d === 'string' ? d : JSON.stringify(d)) : r.statusText, r.status);
}

export async function api<T = Json>(method: string, path: string, body?: unknown): Promise<T> {
  const r = await fetch(BASE + path, {
    method, headers: { ...(body !== undefined ? { 'content-type': 'application/json' } : {}), ...authHeaders() },
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const data = await parse(r);
  if (!r.ok) fail(r, data);
  return data as T;
}

/** Upload a raw body (files). */
export async function put(path: string, body: Blob): Promise<Json> {
  const r = await fetch(BASE + path, { method: 'PUT', headers: authHeaders(), body });
  const data = await parse(r);
  if (!r.ok) fail(r, data);
  return data;
}

/** Download through fetch so the Authorization header is sent. */
export async function download(path: string, filename: string): Promise<void> {
  const r = await fetch(BASE + path, { headers: authHeaders() });
  if (!r.ok) fail(r, await parse(r));
  const url = URL.createObjectURL(await r.blob());
  const a = Object.assign(document.createElement('a'), { href: url, download: filename });
  document.body.append(a); a.click(); a.remove(); setTimeout(() => URL.revokeObjectURL(url), 5000);
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
