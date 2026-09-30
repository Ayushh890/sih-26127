/** Shared types, window handling and chart styling for the analytics sections. */
import type { ReactNode } from "react";
import { fmtTime } from "../../lib/format";

export const NO_DATA = "No data available for this period.";
export const PERIODS = ["15m", "1h", "6h", "24h", "7d", "30d"] as const;
export type Period = (typeof PERIODS)[number];
export const PERIOD_SECONDS: Record<Period, number> = { "15m": 900, "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "30d": 2592000 };

/** Time window sent to period-based endpoints: either a preset period or custom since/until (ISO). */
export interface Win {
  period: Period;
  since?: string;
  until?: string;
}

export function winQuery(w: Win): { period: Period; since?: string; until?: string } {
  return w.since ? { period: w.period, since: w.since, until: w.until } : { period: w.period, until: w.until };
}

export interface Envelope {
  scope?: string;
  synthetic?: boolean;
  notice?: string | null;
  since?: string;
  until?: string;
  has_data: boolean;
  message: string | null;
}

export interface Metrics {
  vehicles: number;
  rate_per_min: number;
  avg_speed_kmh: number | null;
  occupancy: number;
  queue: number;
  density: number;
  max_in_frame: number;
  buckets: number;
  covered_s: number;
  from: string;
  to: string;
}

export function WindowNote({ env }: { env?: { since?: string; until?: string } | null }) {
  if (!env?.since) return null;
  return (
    <span className="font-mono text-[11px] text-slate-500">
      {fmtTime(env.since, true)} → {fmtTime(env.until, true)}
    </span>
  );
}

/** recharts dark styling */
export const GRID = "#1e293b";
export const AXIS = "#94a3b8";
export const axisProps = { stroke: GRID, tick: { fill: AXIS, fontSize: 11 }, tickLine: false } as const;
export const tooltipProps = {
  contentStyle: { background: "#0f1829", border: "1px solid #2a3b5c", borderRadius: 4, fontSize: 12, color: "#e2e8f0" },
  labelStyle: { color: "#94a3b8" },
  itemStyle: { color: "#e2e8f0" },
  cursor: { stroke: "#475569", strokeWidth: 1 },
} as const;

export function SectionNote({ children }: { children: ReactNode }) {
  return <div className="text-[11px] text-slate-500">{children}</div>;
}
