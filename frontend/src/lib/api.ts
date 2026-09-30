/**
 * Thin client for the NIRNAY REST API. Every request carries the bearer token; failures
 * surface as ApiError with the server's `detail` so screens can show the real reason.
 * The API is same-origin (Vite proxy in development, nginx in Docker).
 */

const TOKEN_KEY = "nirnay.token";

export class ApiError extends Error {
  status: number;
  detail: unknown;
  constructor(status: number, detail: unknown, message?: string) {
    super(message ?? describe(detail) ?? `HTTP ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

function describe(detail: unknown): string | undefined {
  if (detail == null) return undefined;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    // FastAPI validation errors: [{loc, msg}]
    return detail
      .map((d) => {
        const loc = Array.isArray(d?.loc) ? d.loc.filter((x: unknown) => x !== "body").join(".") : "";
        return loc ? `${loc}: ${d?.msg}` : String(d?.msg ?? d);
      })
      .join("; ");
  }
  if (typeof detail === "object" && "message" in (detail as object)) return String((detail as { message: unknown }).message);
  return JSON.stringify(detail);
}

let unauthorizedHandler: (() => void) | null = null;
export function onUnauthorized(fn: (() => void) | null): void {
  unauthorizedHandler = fn;
}

export const tokenStore = {
  get: (): string | null => sessionStorage.getItem(TOKEN_KEY),
  set: (t: string) => sessionStorage.setItem(TOKEN_KEY, t),
  clear: () => sessionStorage.removeItem(TOKEN_KEY),
};

export type Query = Record<string, string | number | boolean | null | undefined | (string | number)[]>;

export function qs(params?: Query): string {
  if (!params) return "";
  const sp = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "") continue;
    if (Array.isArray(v)) v.forEach((x) => sp.append(k, String(x)));
    else sp.set(k, String(v));
  }
  const s = sp.toString();
  return s ? `?${s}` : "";
}

export async function request<T = unknown>(method: string, path: string, opts: { query?: Query; body?: unknown; signal?: AbortSignal } = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  const token = tokenStore.get();
  if (token) headers.Authorization = `Bearer ${token}`;
  let body: BodyInit | undefined;
  if (opts.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(opts.body);
  }
  const res = await fetch(path + qs(opts.query), { method, headers, body, signal: opts.signal });
  const text = await res.text();
  let data: unknown = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = text;
    }
  }
  if (!res.ok) {
    if (res.status === 401 && token && unauthorizedHandler) unauthorizedHandler();
    const detail = data && typeof data === "object" && "detail" in (data as object) ? (data as { detail: unknown }).detail : data;
    throw new ApiError(res.status, detail);
  }
  return data as T;
}

export const api = {
  get: <T = unknown>(path: string, query?: Query, signal?: AbortSignal) => request<T>("GET", path, { query, signal }),
  post: <T = unknown>(path: string, body?: unknown, query?: Query) => request<T>("POST", path, { body, query }),
  patch: <T = unknown>(path: string, body?: unknown) => request<T>("PATCH", path, { body }),
  del: <T = unknown>(path: string, query?: Query) => request<T>("DELETE", path, { query }),
};

/** URL for media endpoints (<img>, MJPEG) — they accept the token as a query parameter. */
export function mediaUrl(path: string, query?: Query): string {
  return path + qs({ ...(query ?? {}), token: tokenStore.get() ?? undefined });
}

export function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.status === 403 ? `Not permitted: ${e.message}` : e.message;
  if (e instanceof Error) return e.message;
  return String(e);
}
