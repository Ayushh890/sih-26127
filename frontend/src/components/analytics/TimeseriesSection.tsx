/** Bucketed traffic metrics as small multiples (one metric per chart, one y-axis each). */
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Area, AreaChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api } from "../../lib/api";
import { fmtDuration, fmtNum, fmtTime } from "../../lib/format";
import { Empty, Panel, QueryState, SyntheticBanner } from "../ui";
import { GRID, WindowNote, axisProps, tooltipProps, winQuery, type Envelope, type Win } from "./common";

interface Row {
  ts: string;
  vehicles: number;
  rate_per_min: number;
  avg_speed_kmh: number | null;
  occupancy: number;
  queue: number;
  density: number;
}
interface Series extends Envelope {
  bucket_s: number;
  series: Row[];
}

const METRICS: { key: keyof Row; label: string; unit: string; digits: number; scale?: number }[] = [
  { key: "vehicles", label: "Vehicles per bucket", unit: "veh", digits: 0 },
  { key: "rate_per_min", label: "Flow rate", unit: "veh/min", digits: 2 },
  { key: "avg_speed_kmh", label: "Average speed", unit: "km/h", digits: 1 },
  { key: "occupancy", label: "Occupancy", unit: "%", digits: 1, scale: 100 },
  { key: "queue", label: "Queue (stationary in view)", unit: "veh", digits: 2 },
  { key: "density", label: "Density (vehicles in frame)", unit: "veh", digits: 2 },
];

const BUCKETS = [
  { v: 0, label: "Auto" },
  { v: 60, label: "1 min" },
  { v: 300, label: "5 min" },
  { v: 900, label: "15 min" },
  { v: 3600, label: "1 h" },
  { v: 21600, label: "6 h" },
];

export function TimeseriesSection({ win, scope, cameraId }: { win: Win; scope?: string; cameraId?: string }) {
  const [bucket, setBucket] = useState(0);
  const q = useQuery({
    queryKey: ["analytics", "timeseries", win, scope, cameraId, bucket],
    queryFn: () => api.get<Series>("/api/analytics/timeseries", { ...winQuery(win), scope, camera_id: cameraId, bucket_s: bucket || undefined }),
  });
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <label className="block">
          <span className="label">Bucket</span>
          <select className="input w-auto" value={bucket} onChange={(e) => setBucket(Number(e.target.value))}>
            {BUCKETS.map((b) => (
              <option key={b.v} value={b.v}>
                {b.label}
              </option>
            ))}
          </select>
        </label>
        <WindowNote env={q.data} />
      </div>
      <QueryState q={q}>
        {(s) =>
          !s.has_data || s.series.length === 0 ? (
            <Panel title="Time series">
              <Empty />
            </Panel>
          ) : (
            <>
              <SyntheticBanner info={s} />
              <div className="text-[11px] text-slate-500">
                {s.series.length} buckets of {fmtDuration(s.bucket_s)}{cameraId ? ` · camera ${cameraId}` : " · all cameras in scope"}. Buckets without data are not drawn.
              </div>
              <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
                {METRICS.map((m) => (
                  <MetricChart key={m.key} rows={s.series} m={m} />
                ))}
              </div>
            </>
          )
        }
      </QueryState>
    </div>
  );
}

function MetricChart({ rows, m }: { rows: Row[]; m: (typeof METRICS)[number] }) {
  const data = rows.map((r) => {
    const v = r[m.key] as number | null;
    return { t: new Date(r.ts).getTime(), v: v == null ? null : v * (m.scale ?? 1) };
  });
  const vals = data.map((d) => d.v).filter((v): v is number => v != null);
  if (!vals.length) {
    return (
      <Panel title={m.label}>
        <Empty />
      </Panel>
    );
  }
  const last = vals[vals.length - 1];
  const id = `ts-${String(m.key)}`;
  return (
    <Panel title={m.label} subtitle={`latest ${fmtNum(last, m.digits)} ${m.unit} · peak ${fmtNum(Math.max(...vals), m.digits)} ${m.unit}`}>
      <div className="h-40">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: -12 }}>
            <defs>
              <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#22d3ee" stopOpacity={0.35} />
                <stop offset="100%" stopColor="#22d3ee" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid stroke={GRID} vertical={false} />
            <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} tickFormatter={(t) => fmtTime(t).slice(0, 5)} {...axisProps} minTickGap={30} />
            <YAxis {...axisProps} width={44} tickFormatter={(v) => fmtNum(v, m.digits > 1 ? 1 : m.digits)} />
            <Tooltip
              {...tooltipProps}
              labelFormatter={(t) => fmtTime(Number(t), true)}
              formatter={(v) => [`${fmtNum(v as number, m.digits)} ${m.unit}`, m.label]}
            />
            <Area type="monotone" dataKey="v" stroke="#22d3ee" strokeWidth={2} fill={`url(#${id})`} connectNulls={false} isAnimationActive={false} dot={false} activeDot={{ r: 4 }} />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </Panel>
  );
}
