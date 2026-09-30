/** Formatting helpers and the colour vocabulary shared by every screen. */

export function fmtTime(v?: string | number | null, withDate = false): string {
  if (v == null || v === "") return "—";
  const d = typeof v === "number" ? new Date(v < 1e12 ? v * 1000 : v) : new Date(v);
  if (Number.isNaN(d.getTime())) return String(v);
  const opts: Intl.DateTimeFormatOptions = { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false };
  if (withDate) Object.assign(opts, { year: "numeric", month: "short", day: "2-digit" });
  return d.toLocaleString(undefined, opts);
}

export function fmtAgo(v?: string | number | null, now: number = Date.now()): string {
  if (v == null) return "—";
  const t = typeof v === "number" ? (v < 1e12 ? v * 1000 : v) : new Date(v).getTime();
  if (Number.isNaN(t)) return "—";
  const s = Math.round((now - t) / 1000);
  if (s < 0) return `in ${fmtDuration(-s)}`;
  if (s < 5) return "just now";
  return `${fmtDuration(s)} ago`;
}

export function fmtDuration(seconds?: number | null): string {
  if (seconds == null || Number.isNaN(seconds)) return "—";
  const s = Math.round(seconds);
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60 ? `${s % 60}s` : ""}`.trim();
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  return `${h}h ${m}m`;
}

export function fmtNum(v?: number | null, digits = 0, unit = ""): string {
  if (v == null || Number.isNaN(v)) return "—";
  return `${v.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits })}${unit ? ` ${unit}` : ""}`;
}

export function fmtPct(v?: number | null, digits = 0): string {
  if (v == null || Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(digits)}%`;
}

export function fmtKm(m?: number | null): string {
  if (m == null) return "—";
  return m >= 1000 ? `${(m / 1000).toFixed(2)} km` : `${Math.round(m)} m`;
}

/** Local datetime-input value (YYYY-MM-DDTHH:mm) <-> ISO. */
export function toLocalInput(d: Date): string {
  const p = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}
export function fromLocalInput(s: string): string | undefined {
  if (!s) return undefined;
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? undefined : d.toISOString();
}

export type Tone = "green" | "yellow" | "orange" | "red" | "blue" | "cyan" | "slate" | "violet";

export const TONE_CLASS: Record<Tone, string> = {
  green: "bg-emerald-500/15 text-emerald-300 border-emerald-500/40",
  yellow: "bg-amber-400/15 text-amber-200 border-amber-400/40",
  orange: "bg-orange-500/15 text-orange-300 border-orange-500/40",
  red: "bg-rose-500/15 text-rose-300 border-rose-500/40",
  blue: "bg-sky-500/15 text-sky-300 border-sky-500/40",
  cyan: "bg-cyan-500/15 text-cyan-300 border-cyan-500/40",
  violet: "bg-violet-500/15 text-violet-300 border-violet-500/40",
  slate: "bg-slate-500/15 text-slate-300 border-slate-500/40",
};

export const TONE_HEX: Record<Tone, string> = {
  green: "#34d399",
  yellow: "#fbbf24",
  orange: "#fb923c",
  red: "#fb7185",
  blue: "#38bdf8",
  cyan: "#22d3ee",
  violet: "#a78bfa",
  slate: "#94a3b8",
};

export function statusTone(s?: string | null): Tone {
  switch ((s ?? "").toUpperCase()) {
    case "ONLINE":
      return "green";
    case "DEGRADED":
      return "yellow";
    case "CONNECTING":
      return "blue";
    case "ERROR":
      return "red";
    case "OFFLINE":
      return "slate";
    default:
      return "slate";
  }
}

export function levelTone(level?: string | null): Tone {
  switch ((level ?? "").toUpperCase()) {
    case "FREE":
      return "green";
    case "MODERATE":
      return "yellow";
    case "HEAVY":
      return "orange";
    case "SEVERE":
      return "red";
    default:
      return "slate";
  }
}

export function severityTone(sev?: string | null): Tone {
  switch ((sev ?? "").toUpperCase()) {
    case "CRITICAL":
    case "HIGH":
      return "red";
    case "MEDIUM":
      return "orange";
    case "LOW":
      return "yellow";
    case "INFO":
      return "blue";
    default:
      return "slate";
  }
}

export function alertStatusTone(s?: string | null): Tone {
  switch ((s ?? "").toUpperCase()) {
    case "NEW":
      return "red";
    case "ACKNOWLEDGED":
      return "yellow";
    case "RESOLVED":
      return "green";
    default:
      return "slate";
  }
}

/** Identity-match confidence levels: HIGH / MEDIUM / LOW / NEW (first sighting). */
export function confidenceTone(level?: string | null): Tone {
  switch ((level ?? "").toUpperCase()) {
    case "HIGH":
      return "green";
    case "MEDIUM":
      return "yellow";
    case "LOW":
      return "orange";
    case "NEW":
      return "blue";
    default:
      return "slate";
  }
}

export function predictionTone(s?: string | null): Tone {
  switch ((s ?? "").toUpperCase()) {
    case "CONFIRMED":
      return "green";
    case "PENDING":
      return "blue";
    case "DEVIATED":
      return "orange";
    case "EXPIRED":
      return "slate";
    default:
      return "slate";
  }
}

export function scoreHex(score?: number | null): string {
  if (score == null) return TONE_HEX.slate;
  if (score >= 75) return TONE_HEX.red;
  if (score >= 55) return TONE_HEX.orange;
  if (score >= 30) return TONE_HEX.yellow;
  return TONE_HEX.green;
}
