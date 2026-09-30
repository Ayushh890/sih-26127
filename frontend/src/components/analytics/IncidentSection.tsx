/** Before/during comparison for an incident at a camera (POST /api/analytics/incident-impact). */
import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, errorText } from "../../lib/api";
import { P, useAuth } from "../../lib/auth";
import { fmtDuration, fmtNum, fmtPct, fmtTime, fromLocalInput, severityTone } from "../../lib/format";
import { Badge, Empty, Explanation, Field, Panel, Stat } from "../ui";
import type { Metrics } from "./common";

interface Cmp {
  camera_id: string;
  during: Metrics | null;
  before: Metrics | null;
  delta: Partial<Record<"rate_per_min" | "avg_speed_kmh" | "occupancy" | "queue", number>>;
}
interface Impact {
  camera_id: string;
  start: string;
  end: string;
  baseline_window: [string, string];
  site: Cmp;
  upstream: Cmp[];
  downstream: Cmp[];
  segments: { from: string; to: string; median_s: number; before_s: number | null; samples: number; extra_s_per_vehicle: number }[];
  extra_delay_vehicle_hours: number;
  vehicles_affected: number;
  alerts: { code: string; type: string; severity: string; created_at: string; title: string }[];
  summary: string[];
  has_data: boolean;
  message: string | null;
}

const ROWS: { k: keyof Cmp["delta"]; label: string; fmt: (v: number | null | undefined) => string }[] = [
  { k: "rate_per_min", label: "Flow (veh/min)", fmt: (v) => fmtNum(v, 2) },
  { k: "avg_speed_kmh", label: "Avg speed (km/h)", fmt: (v) => fmtNum(v, 1) },
  { k: "occupancy", label: "Occupancy", fmt: (v) => fmtPct(v, 1) },
  { k: "queue", label: "Queue (veh)", fmt: (v) => fmtNum(v, 2) },
];

export function IncidentSection() {
  const { has } = useAuth();
  const [cameraId, setCameraId] = useState("");
  const [start, setStart] = useState("");
  const [end, setEnd] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [res, setRes] = useState<Impact | null>(null);
  const cams = useQuery({
    queryKey: ["cameras", "lite", "all"],
    queryFn: () => api.get<{ cameras: { id: string; name: string; is_demo: boolean }[] }>("/api/cameras", { scope: "all" }),
    enabled: has(P.CAMERAS_READ),
    staleTime: 60_000,
  });

  const submit = async () => {
    setBusy(true);
    setErr(null);
    try {
      const s = fromLocalInput(start);
      if (!s) throw new Error("Choose the incident start time.");
      setRes(await api.post<Impact>("/api/analytics/incident-impact", { camera_id: cameraId, start: s, end: fromLocalInput(end) ?? null }));
    } catch (e) {
      setErr(errorText(e));
      setRes(null);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-2">
      <Panel title="Incident window" subtitle="Compared with the preceding period of equal length (at least 15 min) at the site and its upstream / downstream cameras">
        <form
          className="grid grid-cols-1 items-end gap-2 md:grid-cols-4"
          onSubmit={(e) => {
            e.preventDefault();
            void submit();
          }}
        >
          <Field label="Camera">
            {cams.data ? (
              <select className="input" required value={cameraId} onChange={(e) => setCameraId(e.target.value)} aria-label="Incident camera">
                <option value="">Select…</option>
                {cams.data.cameras.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.id} · {c.name}
                    {c.is_demo ? " (synthetic)" : ""}
                  </option>
                ))}
              </select>
            ) : (
              <input className="input mono" required placeholder="camera id" value={cameraId} onChange={(e) => setCameraId(e.target.value.trim())} aria-label="Incident camera" />
            )}
          </Field>
          <Field label="Start">
            <input type="datetime-local" className="input" required value={start} onChange={(e) => setStart(e.target.value)} aria-label="Incident start" />
          </Field>
          <Field label="End" hint="Empty = now">
            <input type="datetime-local" className="input" value={end} onChange={(e) => setEnd(e.target.value)} aria-label="Incident end" />
          </Field>
          <div>
            <button type="submit" className="btn btn-primary" disabled={busy || !cameraId || !start}>
              {busy ? "Analysing…" : "Analyse impact"}
            </button>
          </div>
        </form>
        {err && (
          <div className="mt-1.5 text-[12px] text-rose-300" role="alert">
            {err}
          </div>
        )}
      </Panel>

      {res && !res.has_data && (
        <Panel title="Impact">
          <Empty />
        </Panel>
      )}
      {res && res.has_data && <ImpactResult r={res} />}
    </div>
  );
}

function ImpactResult({ r }: { r: Impact }) {
  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
        <Stat label="Site" value={r.camera_id} hint={`${fmtTime(r.start, true)} → ${fmtTime(r.end, true)}`} />
        <Stat label="Speed change" value={r.site.delta.avg_speed_kmh == null ? "—" : `${r.site.delta.avg_speed_kmh > 0 ? "+" : ""}${fmtNum(r.site.delta.avg_speed_kmh, 1)}`} hint="km/h at the site" />
        <Stat label="Extra delay" value={fmtNum(r.extra_delay_vehicle_hours, 2)} hint={`vehicle-hours on ${r.segments.length} linked segment(s)`} />
        <Stat label="Vehicles affected" value={fmtNum(r.vehicles_affected)} hint="distinct at site + upstream" />
      </div>
      <Panel title="Summary" subtitle={`baseline window ${fmtTime(r.baseline_window[0], true)} → ${fmtTime(r.baseline_window[1], true)}`}>
        <Explanation lines={r.summary} />
      </Panel>
      <div className="grid gap-2 xl:grid-cols-2">
        <Panel title="Before vs during" bodyClass="p-0 overflow-x-auto">
          <CmpTable rows={[{ role: "site", c: r.site }, ...r.upstream.map((c) => ({ role: "upstream", c })), ...r.downstream.map((c) => ({ role: "downstream", c }))]} />
        </Panel>
        <div className="space-y-2">
          <Panel title="Linked segments" bodyClass="p-0 overflow-x-auto">
            {r.segments.length === 0 ? (
              <Empty />
            ) : (
              <table className="w-full text-[12px]">
                <thead>
                  <tr>
                    <th className="th">Segment</th>
                    <th className="th text-right">Before</th>
                    <th className="th text-right">During</th>
                    <th className="th text-right">Extra / veh</th>
                    <th className="th text-right">Samples</th>
                  </tr>
                </thead>
                <tbody>
                  {r.segments.map((s) => (
                    <tr key={`${s.from}-${s.to}`}>
                      <td className="td mono">
                        {s.from} → {s.to}
                      </td>
                      <td className="td text-right mono">{fmtDuration(s.before_s)}</td>
                      <td className="td text-right mono">{fmtDuration(s.median_s)}</td>
                      <td className="td text-right mono">{fmtDuration(s.extra_s_per_vehicle)}</td>
                      <td className="td text-right mono">{s.samples}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Panel>
          <Panel title="Alerts during the incident" bodyClass="p-0">
            {r.alerts.length === 0 ? (
              <Empty>No alerts at the site or its neighbours in this window.</Empty>
            ) : (
              <ul className="divide-y divide-ink-700 text-[12px]">
                {r.alerts.map((a) => (
                  <li key={a.code} className="flex items-center gap-2 px-2 py-1">
                    <Badge tone={severityTone(a.severity)}>{a.severity}</Badge>
                    <Link className="mono text-cyan-300 hover:underline" to={`/alerts/${a.code}`}>
                      {a.code}
                    </Link>
                    <span className="truncate text-slate-300">{a.title}</span>
                    <span className="ml-auto font-mono text-[11px] text-slate-500">{fmtTime(a.created_at)}</span>
                  </li>
                ))}
              </ul>
            )}
          </Panel>
        </div>
      </div>
    </div>
  );
}

function CmpTable({ rows }: { rows: { role: string; c: Cmp }[] }) {
  return (
    <table className="w-full text-[12px]">
      <thead>
        <tr>
          <th className="th">Camera</th>
          <th className="th">Metric</th>
          <th className="th text-right">Before</th>
          <th className="th text-right">During</th>
          <th className="th text-right">Δ</th>
        </tr>
      </thead>
      <tbody>
        {rows.map(({ role, c }) => {
          if (!c.during && !c.before) {
            return (
              <tr key={`${role}-${c.camera_id}`}>
                <td className="td mono">{c.camera_id}</td>
                <td className="td text-slate-500" colSpan={4}>
                  {role} · no traffic data in either window
                </td>
              </tr>
            );
          }
          return ROWS.map((m, i) => {
            const d = c.delta[m.k];
            let cell: ReactNode = "—";
            if (d != null) cell = `${d > 0 ? "+" : ""}${m.fmt(d)}`;
            return (
              <tr key={`${role}-${c.camera_id}-${m.k}`} className={i === 0 ? "border-t-2 border-ink-600" : undefined}>
                {i === 0 && (
                  <td className="td align-top" rowSpan={ROWS.length}>
                    <Link className="mono text-cyan-300 hover:underline" to={`/cameras/${c.camera_id}`}>
                      {c.camera_id}
                    </Link>
                    <div className="text-[11px] text-slate-500">{role}</div>
                  </td>
                )}
                <td className="td text-slate-400">{m.label}</td>
                <td className="td text-right mono">{c.before ? m.fmt(c.before[m.k]) : "—"}</td>
                <td className="td text-right mono">{c.during ? m.fmt(c.during[m.k]) : "—"}</td>
                <td className="td text-right mono">{cell}</td>
              </tr>
            );
          });
        })}
      </tbody>
    </table>
  );
}
