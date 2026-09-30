/**
 * Emergency corridor planner — SIMULATION / ADVISORY ONLY.
 * POST /api/corridor/plan returns a congestion-aware route over the camera graph, ETA per
 * junction and advisory actions for traffic personnel. NIRNAY never controls signals.
 */
import { useMemo, useState, type FormEvent } from "react";
import { useMutation } from "@tanstack/react-query";
import { api, errorText } from "../lib/api";
import { fmtDuration, fmtKm, fmtNum, fmtTime, fromLocalInput, levelTone, TONE_HEX } from "../lib/format";
import { edgePath, NetworkMap, useTopology, type MapCamera, type MapPath } from "../components/NetworkMap";
import { Badge, Empty, ErrorBox, Explanation, Field, Loading, PageHeader, Panel, Stat } from "../components/ui";
import { CameraSelect } from "../components/admin/common";

interface CorridorStep {
  camera_id: string;
  camera_name: string;
  latitude: number;
  longitude: number;
  eta_s: number;
  eta: string;
  eta_with_priority_s: number;
  congestion_level: string;
  congestion_score: number | null;
  advisory: string;
}
interface CorridorAlternative {
  cameras: string[];
  distance_m: number;
  estimated_s: number;
  avoids: string;
}
interface CorridorPlan {
  simulation: boolean;
  disclaimer: string;
  origin: string;
  destination: string;
  depart: string;
  route: { cameras: string[]; distance_m: number; min_travel_s: number; typical_travel_s: number; hops: number };
  estimated_s: number;
  estimated_with_priority_s: number;
  time_saved_s: number;
  steps: CorridorStep[];
  alternatives: CorridorAlternative[];
  geometry: { type: "LineString"; coordinates: [number, number][] };
  assumptions: string[];
}
interface PlanRequest {
  origin: string;
  destination: string;
  depart?: string;
  priority_speedup: number;
}

export default function Corridor() {
  const topo = useTopology();
  const cams = topo.data?.cameras ?? [];
  const [origin, setOrigin] = useState("");
  const [destination, setDestination] = useState("");
  const [depart, setDepart] = useState("");
  const [speedup, setSpeedup] = useState(0.35);
  const [showAlt, setShowAlt] = useState<number | null>(null);

  const plan = useMutation({
    mutationFn: (body: PlanRequest) => api.post<CorridorPlan>("/api/corridor/plan", body),
    onSuccess: () => setShowAlt(null),
  });
  const p = plan.data;

  function submit(e: FormEvent) {
    e.preventDefault();
    plan.mutate({ origin, destination, depart: fromLocalInput(depart), priority_speedup: speedup });
  }

  const camPos = useMemo(() => new Map(cams.map((c) => [c.id, c])), [cams]);
  const stepById = useMemo(() => new Map((p?.steps ?? []).map((s) => [s.camera_id, s])), [p]);

  const mapCams: MapCamera[] = cams.map((c) => {
    const s = stepById.get(c.id);
    const isEnd = c.id === (p?.origin ?? origin) || c.id === (p?.destination ?? destination);
    return {
      id: c.id,
      lat: c.lat,
      lon: c.lon,
      label: s ? `${c.id} · T+${fmtDuration(s.eta_with_priority_s)}` : c.id,
      color: s ? TONE_HEX[levelTone(s.congestion_level)] : "#475569",
      ring: isEnd ? "#fb7185" : undefined,
      tooltip: s
        ? `${c.name} · ETA ${fmtTime(s.eta)} · ${s.congestion_level}${s.congestion_score != null ? ` (${fmtNum(s.congestion_score)})` : ""}`
        : `${c.id} · ${c.name}${c.is_demo ? " · SYNTHETIC" : ""}`,
    };
  });

  const paths: MapPath[] = useMemo(() => {
    const out: MapPath[] = [];
    if (!p) return out;
    if (showAlt != null) {
      const alt = p.alternatives[showAlt];
      const coords: [number, number][] = [];
      const ids = alt?.cameras ?? [];
      for (let i = 1; i < ids.length; i++) {
        const a = camPos.get(ids[i - 1]);
        const b = camPos.get(ids[i]);
        if (!a || !b) continue;
        const seg = edgePath(topo.data, a.id, b.id, [
          [a.lat, a.lon],
          [b.lat, b.lon],
        ]);
        coords.push(...(coords.length ? seg.slice(1) : seg));
      }
      if (coords.length > 1) out.push({ id: "alt", coords, color: TONE_HEX.blue, weight: 4, dash: "6 6", tooltip: `Alternative avoiding ${alt.avoids}` });
    }
    const main = p.geometry.coordinates.map(([lon, lat]) => [lat, lon] as [number, number]);
    if (main.length > 1) out.push({ id: "route", coords: main, color: TONE_HEX.red, weight: 6, tooltip: `Primary corridor ${p.origin} → ${p.destination}` });
    return out;
  }, [p, showAlt, camPos, topo.data]);
  const fitTo = useMemo(() => (paths.length ? paths[paths.length - 1].coords : null), [paths]);

  return (
    <div className="flex h-full flex-col gap-2">
      <PageHeader title="Emergency Corridor Planner" subtitle="Congestion-aware route planning with per-junction advisories for traffic personnel" />
      <div role="note" className="flex items-start gap-2 rounded border-2 border-amber-500/70 bg-amber-950/50 px-3 py-2 text-amber-100">
        <Badge tone="yellow">Simulation only</Badge>
        <div className="text-[12px] leading-snug">
          <b>Advisory output — NIRNAY never controls traffic signals or any field equipment.</b> The plan below is a simulation for traffic
          personnel to act on manually; nothing is sent to signal controllers.
          {p?.disclaimer && <div className="mt-0.5 text-amber-200/90">{p.disclaimer}</div>}
        </div>
      </div>

      <div className="grid min-h-0 flex-1 grid-cols-1 gap-2 xl:grid-cols-[340px_minmax(0,1fr)]">
        <div className="flex min-h-0 flex-col gap-2 overflow-auto">
          <Panel title="Plan request">
            {topo.isLoading ? (
              <Loading label="Loading camera graph…" />
            ) : topo.error ? (
              <ErrorBox error={topo.error} onRetry={() => topo.refetch()} />
            ) : cams.length === 0 ? (
              <Empty>No cameras in the road graph.</Empty>
            ) : (
              <form onSubmit={submit} className="space-y-2">
                <Field label="Origin (dispatch point)">
                  <CameraSelect cameras={cams} value={origin} onChange={setOrigin} required />
                </Field>
                <Field label="Destination">
                  <CameraSelect cameras={cams} value={destination} onChange={setDestination} required />
                </Field>
                <Field label="Departure time" hint="Blank = now">
                  <input className="input" type="datetime-local" value={depart} onChange={(e) => setDepart(e.target.value)} />
                </Field>
                <Field label={`Priority clearance speed-up: ${Math.round(speedup * 100)}%`} hint="Assumed segment-time reduction when personnel clear the way (0–80%); never below the physical minimum.">
                  <input className="w-full accent-cyan-500" type="range" min={0} max={0.8} step={0.05} value={speedup} onChange={(e) => setSpeedup(Number(e.target.value))} />
                </Field>
                <div className="flex gap-1.5">
                  <button className="btn btn-primary" disabled={!origin || !destination || origin === destination || plan.isPending}>
                    {plan.isPending ? "Planning…" : "Plan corridor (simulation)"}
                  </button>
                  {p && (
                    <button type="button" className="btn btn-ghost" onClick={() => plan.reset()}>
                      Clear
                    </button>
                  )}
                </div>
                {plan.error && (
                  <div className="text-[12px] text-rose-300" role="alert">
                    {errorText(plan.error)}
                  </div>
                )}
              </form>
            )}
          </Panel>
          {p && (
            <>
              <div className="grid grid-cols-2 gap-2">
                <Stat label="ETA (normal)" value={fmtDuration(p.estimated_s)} hint={`arrive ${fmtTime(new Date(new Date(p.depart).getTime() + p.estimated_s * 1000).toISOString())}`} />
                <Stat label="ETA (priority)" value={fmtDuration(p.estimated_with_priority_s)} tone={TONE_HEX.green} hint={`arrive ${fmtTime(new Date(new Date(p.depart).getTime() + p.estimated_with_priority_s * 1000).toISOString())}`} />
                <Stat label="Time saved" value={fmtDuration(p.time_saved_s)} tone={TONE_HEX.cyan} />
                <Stat label="Distance" value={fmtKm(p.route.distance_m)} hint={`${p.route.hops} hops · min ${fmtDuration(p.route.min_travel_s)}`} />
              </div>
              <Panel title="Assumptions & explanation">
                <Explanation
                  lines={[
                    `Route ${p.route.cameras.join(" → ")} departing ${fmtTime(p.depart, true)}.`,
                    `Topology prior for this route: typical ${fmtDuration(p.route.typical_travel_s)}, physical minimum ${fmtDuration(p.route.min_travel_s)}.`,
                    ...p.assumptions,
                  ]}
                />
              </Panel>
              <Panel title="Alternative routes" subtitle="Each avoids one intermediate junction of the primary route">
                {p.alternatives.length === 0 ? (
                  <Empty>No alternative route exists that avoids an intermediate junction.</Empty>
                ) : (
                  <ul className="space-y-1">
                    {p.alternatives.map((a, i) => (
                      <li key={i}>
                        <button
                          type="button"
                          onClick={() => setShowAlt(showAlt === i ? null : i)}
                          className={`w-full rounded border px-2 py-1 text-left text-[12px] ${showAlt === i ? "border-sky-500 bg-sky-950/40" : "border-ink-700 hover:bg-ink-800"}`}
                        >
                          <div className="mono">{a.cameras.join(" → ")}</div>
                          <div className="text-slate-400">
                            avoids <b className="text-slate-200">{a.avoids}</b> · {fmtKm(a.distance_m)} · {fmtDuration(a.estimated_s)}
                            {showAlt === i ? " · shown on map" : ""}
                          </div>
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
              </Panel>
            </>
          )}
        </div>

        <div className="flex min-h-0 flex-col gap-2">
          <Panel title="Corridor map" subtitle={p ? "Red = primary corridor; junction colour = current congestion level" : "Choose origin and destination to plan a corridor"} bodyClass="p-0" className="min-h-[320px] flex-[3]">
            <NetworkMap cameras={mapCams} paths={paths} fitTo={fitTo} className="h-full min-h-[300px]" />
          </Panel>
          <Panel title="Junction advisories" subtitle="For traffic personnel — manual action only" className="min-h-[200px] flex-[2]" bodyClass="overflow-auto p-0">
            {plan.isPending ? (
              <Loading label="Planning corridor…" />
            ) : !p ? (
              <Empty>No plan yet.</Empty>
            ) : (
              <table className="w-full text-[12px]">
                <thead>
                  <tr>
                    <th className="th">#</th>
                    <th className="th">Junction</th>
                    <th className="th text-right">ETA</th>
                    <th className="th text-right">ETA w/ priority</th>
                    <th className="th">Congestion</th>
                    <th className="th">Advisory</th>
                  </tr>
                </thead>
                <tbody>
                  {p.steps.map((s, i) => (
                    <tr key={s.camera_id} className="hover:bg-ink-800/60">
                      <td className="td mono text-slate-500">{i + 1}</td>
                      <td className="td">
                        <div className="mono">{s.camera_id}</div>
                        <div className="text-[11px] text-slate-400">{s.camera_name}</div>
                      </td>
                      <td className="td mono text-right">
                        <div>T+{fmtDuration(s.eta_s)}</div>
                        <div className="text-[11px] text-slate-500">{fmtTime(s.eta)}</div>
                      </td>
                      <td className="td mono text-right text-emerald-300">T+{fmtDuration(s.eta_with_priority_s)}</td>
                      <td className="td">
                        <Badge tone={levelTone(s.congestion_level)}>{s.congestion_level.replace("_", " ")}</Badge>
                        {s.congestion_score != null && <span className="mono ml-1 text-slate-400">{fmtNum(s.congestion_score)}</span>}
                      </td>
                      <td className="td text-slate-200">{s.advisory}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </Panel>
        </div>
      </div>
    </div>
  );
}
