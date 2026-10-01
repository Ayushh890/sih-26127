/**
 * Command Center — the live operations view: congestion map with animated trajectory hops,
 * live detections, open alerts, KPIs, camera status and the demo scenario timeline.
 * Data: /api/cameras, /api/analytics/overview + summary, /api/alerts, /api/observations,
 * /api/demo/timeline; live via vehicle_matched, trajectory_updated, alert_*, camera_status_changed,
 * analytics_updated.
 */
import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { P, useAuth } from "../lib/auth";
import { useScope } from "../lib/scope";
import { useConnState, useLiveEvent } from "../lib/ws";
import {
  TONE_HEX,
  alertStatusTone,
  confidenceTone,
  fmtAgo,
  fmtDuration,
  fmtNum,
  fmtPct,
  fmtTime,
  levelTone,
  scoreHex,
  severityTone,
  statusTone,
} from "../lib/format";
import { ActionButton, Badge, Dot, Empty, ErrorBox, Explanation, KV, Loading, PageHeader, Panel, QueryState, Stat, SyntheticBanner } from "../components/ui";
import { LiveMap } from "../components/cameras/LiveMap";
import {
  applyStatusToCache,
  inScope,
  obsToDetection,
  pushDetection,
  upsertOpenAlert,
  type Alert,
  type AlertList,
  type Camera,
  type CameraList,
  type CameraStatusEvent,
  type Detection,
  type ObservationRow,
  type Overview,
  type OverviewCamera,
  type ScopeInfo,
  type ScopeSnapshot,
} from "../components/cameras/model";

const ALERT_LIMIT = 15;
const FEED_MAX = 60;

interface Summary extends ScopeInfo {
  since: string;
  until: string;
  has_data: boolean;
  message: string | null;
  totals: Record<string, number | string | null> | null;
  observations: number;
  plate_read_rate: number | null;
  by_class: Record<string, number>;
}

interface TimelineEvent {
  scenario: string;
  camera_id: string;
  plate: string | null;
  title: string;
  expected_outcome: string;
  at: string;
  scenario_t: number;
  cycle: number;
  status: string;
}
interface Timeline {
  enabled: boolean;
  synthetic: boolean;
  cycle_seconds?: number;
  cycle?: number;
  position_s?: number;
  cycle_started_at?: string;
  events: TimelineEvent[];
  notice?: string;
}

export default function CommandCenter() {
  const { has } = useAuth();
  const { param } = useScope();
  const qc = useQueryClient();
  const [selected, setSelected] = useState<string | null>(null);

  const camerasQ = useQuery({
    queryKey: ["cameras", "list", param],
    queryFn: () => api.get<CameraList>("/api/cameras", { scope: param }),
    refetchInterval: 60_000,
  });
  const scope = camerasQ.data?.scope ?? param;

  const overviewQ = useQuery({
    queryKey: ["analytics", "overview"],
    queryFn: () => api.get<Overview>("/api/analytics/overview"),
    enabled: has(P.ANALYTICS_READ),
    refetchInterval: 60_000, // slow fallback; analytics_updated keeps it fresh
  });

  useLiveEvent("analytics_updated", (ev) => {
    const d = ev.data as unknown as Overview;
    if (d && Array.isArray(d.cameras)) qc.setQueryData(["analytics", "overview"], d);
  });
  useLiveEvent("camera_status_changed", (ev) => applyStatusToCache(qc, ev.data as unknown as CameraStatusEvent));

  const cameras = useMemo(() => camerasQ.data?.cameras ?? [], [camerasQ.data]);
  const overviewByCam = useMemo(() => {
    const m = new Map<string, OverviewCamera>();
    for (const c of overviewQ.data?.cameras ?? []) if (inScope(scope, c.is_demo)) m.set(c.camera_id, c);
    return m;
  }, [overviewQ.data, scope]);
  const snapshots = useMemo(() => {
    const s = overviewQ.data?.scopes ?? {};
    const keys = scope === "live" ? (["live"] as const) : scope === "demo" ? (["demo"] as const) : (["live", "demo"] as const);
    return keys.filter((k) => s[k]).map((k) => [k, s[k]!] as [string, ScopeSnapshot]);
  }, [overviewQ.data, scope]);

  const selCam = cameras.find((c) => c.id === selected) ?? null;

  return (
    <div className="flex flex-col gap-2">
      <PageHeader
        title="Command Center"
        subtitle={
          <span>
            Live network view · congestion window {overviewQ.data ? fmtDuration(overviewQ.data.window_s) : "—"} · snapshot{" "}
            {overviewQ.data ? fmtAgo(overviewQ.data.generated_at) : "—"}
          </span>
        }
      />
      <SyntheticBanner info={camerasQ.data} />

      <KpiRow scope={param} snapshots={snapshots} overviewError={overviewQ.error} />

      <CameraStrip q={camerasQ} overview={overviewByCam} onSelect={setSelected} selected={selected} />

      <div className="grid gap-2 xl:grid-cols-12">
        <Panel
          title="City network"
          subtitle="Cameras coloured by live congestion score · click a camera for details"
          className="xl:col-span-8"
          bodyClass="p-0"
          actions={overviewQ.isFetching ? <span className="text-[11px] text-slate-500">refreshing…</span> : null}
        >
          {camerasQ.isLoading ? (
            <Loading />
          ) : camerasQ.error ? (
            <ErrorBox error={camerasQ.error} onRetry={() => camerasQ.refetch()} />
          ) : cameras.length === 0 ? (
            <Empty>No cameras in this data scope.</Empty>
          ) : (
            <>
              {overviewQ.error ? <div className="px-2 pt-1 text-[11px] text-rose-300">Congestion overview unavailable — map shows camera status only.</div> : null}
              <LiveMap cameras={cameras} overview={overviewByCam} selectedId={selected} onSelect={setSelected} scope={scope} className="h-[540px]" />
            </>
          )}
        </Panel>
        <div className="flex min-h-0 flex-col gap-2 xl:col-span-4">
          <SelectedCamera cam={selCam} cong={selCam ? overviewByCam.get(selCam.id) : undefined} onClose={() => setSelected(null)} />
          {has(P.ALERTS_READ) && <OpenAlerts scope={param} resolvedScope={scope} />}
        </div>
      </div>

      <div className="grid gap-2 xl:grid-cols-12">
        <DetectionsFeed scope={param} resolvedScope={scope} className="xl:col-span-8" />
        <DemoTimeline className="xl:col-span-4" />
      </div>
    </div>
  );
}

/* --------------------------------------------------------------------------- KPIs */

function KpiRow({ scope, snapshots, overviewError }: { scope: string | undefined; snapshots: [string, ScopeSnapshot][]; overviewError: unknown }) {
  const { has } = useAuth();
  const [period, setPeriod] = useState("1h");
  const q = useQuery({
    queryKey: ["analytics", "summary", "cc", scope, period],
    queryFn: () => api.get<Summary>("/api/analytics/summary", { scope, period }),
    enabled: has(P.ANALYTICS_READ),
    refetchInterval: 30_000,
  });
  if (!has(P.ANALYTICS_READ)) return null;
  const s = q.data;
  const t = s?.totals ?? null;
  const n = (k: string) => (t && typeof t[k] === "number" ? (t[k] as number) : null);
  const classes = s ? Object.entries(s.by_class).sort((a, b) => b[1] - a[1]) : [];
  return (
    <div className="space-y-1">
      <div className="flex items-center gap-2 text-[11px] text-slate-500">
        <span>Traffic summary for the last</span>
        <select className="input !w-auto !py-0 text-[11px]" value={period} onChange={(e) => setPeriod(e.target.value)} aria-label="Summary period">
          {["15m", "1h", "6h", "24h"].map((p) => (
            <option key={p}>{p}</option>
          ))}
        </select>
        {s && (
          <span>
            {fmtTime(s.since)} → {fmtTime(s.until)}
          </span>
        )}
        {q.isFetching && <span>refreshing…</span>}
      </div>
      {q.isLoading ? (
        <Loading />
      ) : q.error ? (
        <ErrorBox error={q.error} onRetry={() => q.refetch()} />
      ) : (
        <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 xl:grid-cols-8">
          {snapshots.length === 0 ? (
            <Stat label="Network score" value="—" hint={overviewError ? "overview unavailable" : "no cameras in scope"} />
          ) : (
            snapshots.map(([k, sn]) => (
              <Stat
                key={k}
                label={`Network score${snapshots.length > 1 ? ` (${k})` : ""}`}
                value={sn.network_score == null ? "—" : `${fmtNum(sn.network_score, 0)}/100`}
                tone={sn.network_score == null ? undefined : scoreHex(sn.network_score)}
                hint={`${sn.online}/${sn.cameras} cameras online${sn.worst_camera_id ? ` · worst ${sn.worst_camera_id}` : ""} · ${fmtNum(sn.unique_vehicles)} unique veh. (window)`}
              />
            ))
          )}
          {s && !s.has_data ? (
            <div className="col-span-2 flex items-center rounded-md border border-ink-700 bg-ink-900/80 px-3 text-[12px] text-slate-500 sm:col-span-3 xl:col-span-7">
              {s.message ?? "No data available for this period."}
            </div>
          ) : (
            <>
              <Stat label="Vehicles" value={fmtNum(n("vehicles"))} hint={`${fmtNum(n("rate_per_min"), 1)} / min`} />
              <Stat label="Observations" value={fmtNum(s?.observations)} hint={`plate read rate ${fmtPct(s?.plate_read_rate, 1)}`} />
              <Stat label="Avg speed" value={n("avg_speed_kmh") == null ? "—" : fmtNum(n("avg_speed_kmh"), 1, "km/h")} />
              <Stat label="Occupancy" value={fmtPct(n("occupancy"), 1)} hint={`density ${fmtNum(n("density"), 2)} veh/frame`} />
              <Stat label="Queue" value={fmtNum(n("queue"), 2)} hint={`max ${fmtNum(n("max_in_frame"))} vehicles in frame`} />
              <Stat
                label="Class mix"
                value={classes.length ? fmtNum(classes.reduce((a, [, v]) => a + v, 0)) : "—"}
                hint={classes.map(([k, v]) => `${k} ${v}`).join(" · ") || "no classified vehicles"}
              />
              <Stat label="Coverage" value={fmtDuration(n("covered_s"))} hint={`${fmtNum(n("buckets"))} traffic buckets`} />
            </>
          )}
        </div>
      )}
    </div>
  );
}

/* --------------------------------------------------------------------------- camera strip */

function CameraStrip({
  q,
  overview,
  onSelect,
  selected,
}: {
  q: { isLoading: boolean; error: unknown; data: CameraList | undefined; refetch: () => unknown };
  overview: Map<string, OverviewCamera>;
  onSelect: (id: string) => void;
  selected: string | null;
}) {
  if (q.isLoading || q.error || !q.data) return null; // the map panel shows the loading/error state
  const cams = q.data.cameras;
  const counts = cams.reduce<Record<string, number>>((a, c) => ((a[c.status] = (a[c.status] ?? 0) + 1), a), {});
  return (
    <div className="flex flex-wrap items-center gap-1.5 rounded-md border border-ink-700 bg-ink-900/80 px-2 py-1.5" data-testid="camera-strip">
      <span className="mr-1 text-[11px] font-semibold uppercase tracking-wide text-slate-400">Cameras</span>
      {Object.entries(counts).map(([s, n]) => (
        <Badge key={s} tone={statusTone(s)}>
          {s} {n}
        </Badge>
      ))}
      <span className="mx-1 h-4 w-px bg-ink-700" />
      {cams.map((c) => {
        const o = overview.get(c.id);
        return (
          <button
            key={c.id}
            onClick={() => onSelect(c.id)}
            title={`${c.name} — ${c.status}${c.status_message ? `: ${c.status_message}` : ""}${o ? ` · congestion ${o.score == null ? "no data" : Math.round(o.score)} (${o.level})` : ""}`}
            className={`flex items-center gap-1.5 rounded border px-1.5 py-0.5 text-[11px] ${selected === c.id ? "border-cyan-500 bg-cyan-500/10" : "border-ink-700 bg-ink-850 hover:border-ink-600"}`}
          >
            <Dot color={TONE_HEX[statusTone(c.status)]} pulse={c.status === "CONNECTING"} />
            <span className="font-mono text-slate-200">{c.id}</span>
            <span className="text-slate-500">{c.status}</span>
            {o && o.score != null && (
              <span className="font-mono" style={{ color: scoreHex(o.score) }}>
                {Math.round(o.score)}
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

/* --------------------------------------------------------------------------- selected camera */

function SelectedCamera({ cam, cong, onClose }: { cam: Camera | null; cong: OverviewCamera | undefined; onClose: () => void }) {
  if (!cam)
    return (
      <Panel title="Camera">
        <Empty>Select a camera on the map or in the status strip to see its congestion breakdown.</Empty>
      </Panel>
    );
  const m = cong?.metrics ?? null;
  const mv = (k: string) => (m && typeof m[k] === "number" ? (m[k] as number) : null);
  return (
    <Panel
      title={
        <span>
          {cam.id} · {cam.name}
        </span>
      }
      subtitle={[cam.road_name, cam.zone].filter(Boolean).join(" · ") || cam.location}
      actions={
        <>
          <Link to={`/cameras/${encodeURIComponent(cam.id)}`} className="btn">
            Open camera
          </Link>
          <button className="btn btn-ghost" onClick={onClose} aria-label="Close camera panel">
            ✕
          </button>
        </>
      }
      bodyClass="p-2 space-y-2 max-h-[420px] overflow-y-auto"
    >
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge tone={statusTone(cam.status)}>{cam.status}</Badge>
        {cong && <Badge tone={levelTone(cong.level)}>{cong.level}</Badge>}
        {cong && (
          <span className="font-mono text-[15px] font-semibold" style={{ color: scoreHex(cong.score) }}>
            {cong.score == null ? "—" : `${fmtNum(cong.score, 1)}/100`}
          </span>
        )}
        {cam.is_demo && <Badge tone="violet">Synthetic</Badge>}
      </div>
      {cam.status_message && <div className="text-[11px] text-slate-400">{cam.status_message}</div>}
      {!cong ? (
        <Empty>No congestion score for this camera.</Empty>
      ) : (
        <>
          <Explanation lines={cong.explanation} />
          {Object.keys(cong.components).length > 0 && (
            <div className="space-y-1">
              <div className="label">Score components</div>
              {Object.entries(cong.components).map(([k, c]) => (
                <div key={k} className="text-[11.5px]">
                  <div className="flex items-center gap-2">
                    <span className="w-20 capitalize text-slate-400">{k.replace("_", " ")}</span>
                    <div className="h-1.5 flex-1 rounded bg-ink-700">
                      <div className="h-1.5 rounded" style={{ width: `${Math.round(Math.min(1, c.value) * 100)}%`, background: scoreHex(c.value * 100) }} />
                    </div>
                    <span className="w-10 text-right font-mono text-slate-300">{fmtPct(c.value)}</span>
                    {cong.weights?.[k] != null && <span className="w-12 text-right font-mono text-slate-500">×{cong.weights[k]}</span>}
                  </div>
                  <div className="pl-[88px] text-slate-500">{c.text}</div>
                </div>
              ))}
            </div>
          )}
          {m && (
            <KV
              rows={[
                ["Vehicles", `${fmtNum(mv("vehicles"))} (${fmtNum(mv("rate_per_min"), 1)}/min)`],
                ["Avg speed", mv("avg_speed_kmh") == null ? "—" : fmtNum(mv("avg_speed_kmh"), 1, "km/h")],
                ["Occupancy", fmtPct(mv("occupancy"), 1)],
                ["Queue", fmtNum(mv("queue"), 2)],
                ["Density", fmtNum(mv("density"), 2)],
                ["Max in frame", fmtNum(mv("max_in_frame"))],
                ["Window", m.from && m.to ? `${fmtTime(String(m.from))} → ${fmtTime(String(m.to))}` : "—"],
              ]}
            />
          )}
        </>
      )}
    </Panel>
  );
}

/* --------------------------------------------------------------------------- alerts */

function OpenAlerts({ scope, resolvedScope }: { scope: string | undefined; resolvedScope: string | undefined }) {
  const qc = useQueryClient();
  const key = ["alerts", "cc-open", scope];
  const q = useQuery({
    queryKey: key,
    queryFn: () => api.get<AlertList>("/api/alerts", { status: "OPEN", limit: ALERT_LIMIT, scope }),
    refetchInterval: 60_000,
  });
  useLiveEvent(["alert_created", "alert_updated"], (ev) => {
    const a = ev.data as unknown as Alert;
    if (!a || a.id == null || !inScope(resolvedScope, a.is_demo)) return;
    qc.setQueryData<AlertList>(key, (d) => (d ? upsertOpenAlert(d, a, ALERT_LIMIT) : d));
  });
  return (
    <Panel
      title="Open alerts"
      subtitle={q.data ? `${fmtNum(q.data.total)} open` : undefined}
      actions={
        <Link to="/alerts?status=OPEN" className="btn btn-ghost">
          All alerts →
        </Link>
      }
      bodyClass="max-h-[300px] overflow-y-auto"
    >
      <QueryState q={q} isEmpty={(d) => d.results.length === 0} empty="No open alerts.">
        {(d) => (
          <ul className="divide-y divide-ink-700/70">
            {d.results.map((a) => (
              <li key={a.id} className="px-2 py-1.5">
                <div className="flex items-center gap-1.5">
                  <Badge tone={severityTone(a.severity)}>{a.severity}</Badge>
                  <Badge tone={alertStatusTone(a.status)}>{a.status}</Badge>
                  {a.is_demo && <Badge tone="violet">Synthetic</Badge>}
                  <span className="ml-auto text-[11px] text-slate-500" title={fmtTime(a.created_at, true)}>
                    {fmtAgo(a.updated_at ?? a.created_at)}
                  </span>
                </div>
                <Link to={`/alerts/${encodeURIComponent(a.code)}`} className="mt-0.5 block text-[12px] text-slate-200 hover:text-cyan-300">
                  {a.title}
                </Link>
                <div className="line-clamp-2 text-[11px] text-slate-500" title={a.reason}>
                  <span className="mono text-slate-400">{a.type}</span>
                  {a.camera_id ? ` · ${a.camera_id}` : ""}
                  {a.occurrences > 1 ? ` · ×${a.occurrences}` : ""} · {a.reason}
                </div>
              </li>
            ))}
          </ul>
        )}
      </QueryState>
    </Panel>
  );
}

/* --------------------------------------------------------------------------- detections feed */

function DetectionsFeed({ scope, resolvedScope, className }: { scope: string | undefined; resolvedScope: string | undefined; className?: string }) {
  const { has } = useAuth();
  const conn = useConnState();
  const canRead = has(P.TRAJECTORY_READ);
  const [live, setLive] = useState<Detection[]>([]);
  const [paused, setPaused] = useState(false);
  useEffect(() => {
    setLive([]);
  }, [scope]);

  // Seed with the most recent persisted sightings so the feed is not blank on load.
  const seed = useQuery({
    queryKey: ["observations", "cc-seed", scope],
    queryFn: () => api.get<ScopeInfo & { results: ObservationRow[] }>("/api/observations", { scope, limit: 30 }),
    enabled: canRead,
    staleTime: Infinity,
  });

  useLiveEvent("vehicle_matched", (ev) => {
    if (paused) return;
    const d = ev.data as unknown as Detection;
    if (!d || d.observation_id == null || !inScope(resolvedScope, d.is_demo)) return;
    setLive((f) => pushDetection(f, d, FEED_MAX));
  });

  const rows = useMemo(() => {
    let out = live;
    for (const o of seed.data?.results ?? []) out = out.some((x) => x.observation_id === o.id) ? out : [...out, obsToDetection(o)];
    return out.slice(0, FEED_MAX);
  }, [live, seed.data]);
  const liveIds = useMemo(() => new Set(live.map((d) => d.observation_id)), [live]);

  return (
    <Panel
      title="Live detections"
      subtitle={
        canRead
          ? `${live.length} received live${conn !== "open" ? ` · stream ${conn}` : ""} · earlier rows are the latest stored sightings`
          : undefined
      }
      className={className}
      bodyClass="max-h-[420px] overflow-auto"
      actions={
        canRead ? (
          <button className="btn" onClick={() => setPaused((p) => !p)} aria-pressed={paused}>
            {paused ? "Resume" : "Pause"}
          </button>
        ) : null
      }
    >
      {!canRead ? (
        <Empty>Your role does not receive individual vehicle detections ({P.TRAJECTORY_READ}).</Empty>
      ) : seed.isLoading && live.length === 0 ? (
        <Loading />
      ) : (
        <>
          {seed.error ? <ErrorBox error={seed.error} onRetry={() => seed.refetch()} /> : null}
          {rows.length === 0 ? (
            <Empty>No detections yet. New sightings appear here as cameras report them.</Empty>
          ) : (
            <table className="w-full text-[12px]">
              <thead>
                <tr>
                  <th className="th">Time</th>
                  <th className="th">Camera</th>
                  <th className="th">Plate</th>
                  <th className="th">Vehicle</th>
                  <th className="th">Speed</th>
                  <th className="th">Identity</th>
                  <th className="th">Match</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((d) => (
                  <tr key={d.observation_id} className={liveIds.has(d.observation_id) ? "" : "opacity-70"}>
                    <td className="td mono whitespace-nowrap" title={fmtTime(d.observed_at, true)}>
                      {fmtTime(d.observed_at)}
                    </td>
                    <td className="td whitespace-nowrap">
                      <Link to={`/cameras/${encodeURIComponent(d.camera_id)}`} className="mono text-slate-200 hover:text-cyan-300" title={d.camera_name ?? undefined}>
                        {d.camera_id}
                      </Link>
                    </td>
                    <td className="td whitespace-nowrap">
                      {d.plate_text ? (
                        <span className="mono text-slate-100">{d.plate_text}</span>
                      ) : (
                        <span className="text-slate-500">no plate</span>
                      )}
                      {d.plate_confidence != null && <span className="ml-1 text-[10.5px] text-slate-500">OCR {fmtPct(d.plate_confidence)}</span>}
                    </td>
                    <td className="td whitespace-nowrap text-slate-300">
                      {[d.vehicle_color, d.vehicle_class].filter(Boolean).join(" ") || "—"}
                      {d.is_demo && (
                        <Badge tone="violet" className="ml-1">
                          Synthetic
                        </Badge>
                      )}
                    </td>
                    <td className="td mono whitespace-nowrap">{d.speed_kmh == null ? "—" : fmtNum(d.speed_kmh, 0, "km/h")}</td>
                    <td className="td whitespace-nowrap">
                      {d.vehicle_code ? (
                        <Link to={`/vehicles/${encodeURIComponent(d.vehicle_code)}`} className="mono text-cyan-300 hover:underline">
                          {d.vehicle_code}
                        </Link>
                      ) : (
                        "—"
                      )}
                      {d.new_vehicle && (
                        <Badge tone="blue" className="ml-1">
                          New
                        </Badge>
                      )}
                    </td>
                    <td className="td">
                      <div className="flex items-center gap-1.5">
                        <Badge tone={confidenceTone(d.confidence_level)}>{d.confidence_level ?? "—"}</Badge>
                        {d.match_score != null && <span className="mono text-slate-300">{d.match_score.toFixed(2)}</span>}
                      </div>
                      {d.match_reasons?.[0] && (
                        <div className="max-w-[320px] truncate text-[11px] text-slate-500" title={d.match_reasons.join("\n")}>
                          {d.match_reasons[0]}
                        </div>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </Panel>
  );
}

/* --------------------------------------------------------------------------- demo timeline */

function DemoTimeline({ className }: { className?: string }) {
  const { has } = useAuth();
  const qc = useQueryClient();
  const [result, setResult] = useState<string | null>(null);
  const q = useQuery({
    queryKey: ["demo", "timeline"],
    queryFn: () => api.get<Timeline>("/api/demo/timeline"),
    refetchInterval: 15_000,
  });
  if (q.isLoading) return null;
  if (q.error)
    return (
      <Panel title="Demo scenario" className={className}>
        <ErrorBox error={q.error} onRetry={() => q.refetch()} />
      </Panel>
    );
  const t = q.data;
  if (!t || !t.enabled || t.events.length === 0) return null;
  const nextIdx = t.events.findIndex((e) => e.status !== "done");
  const pos = t.position_s ?? 0;
  const cyc = t.cycle_seconds ?? 0;
  return (
    <Panel
      title="Demo scenario timeline"
      subtitle={`cycle ${t.cycle ?? 0} · ${fmtDuration(pos)} / ${fmtDuration(cyc)}`}
      className={className}
      actions={
        has(P.CAMERAS_CONTROL) ? (
          <ActionButton
            className="btn btn-danger"
            confirm="Restart the demo scenario? All synthetic demo data (sightings, trajectories, demo alerts) is purged and the scenario restarts from t=0. Live data is untouched."
            onClick={async () => {
              const r = await api.post<{ purged: unknown; cameras: string[] }>("/api/demo/restart");
              const purged =
                r.purged && typeof r.purged === "object"
                  ? Object.entries(r.purged as Record<string, unknown>)
                      .map(([k, v]) => `${k} ${String(v)}`)
                      .join(", ")
                  : String(r.purged ?? "");
              setResult(`Restarted on ${r.cameras.length} camera(s)${purged ? ` · purged: ${purged}` : ""}`);
              await qc.invalidateQueries();
            }}
          >
            Restart demo
          </ActionButton>
        ) : null
      }
      bodyClass="p-2 space-y-2"
    >
      <SyntheticBanner info={{ synthetic: t.synthetic, notice: t.notice }} />
      {cyc > 0 && (
        <div className="h-1.5 rounded bg-ink-700" title={`${Math.round((pos / cyc) * 100)}% through the cycle`}>
          <div className="h-1.5 rounded bg-violet-400" style={{ width: `${Math.min(100, (pos / cyc) * 100)}%` }} />
        </div>
      )}
      {result && <div className="text-[11px] text-emerald-300">{result}</div>}
      <ol className="max-h-[330px] space-y-0.5 overflow-y-auto pr-1">
        {t.events.map((e, i) => (
          <li
            key={`${e.cycle}-${e.scenario_t}-${e.camera_id}-${i}`}
            className={`rounded border px-1.5 py-1 text-[11.5px] ${i === nextIdx ? "border-cyan-600 bg-cyan-500/10" : "border-transparent"} ${e.status === "done" ? "opacity-60" : ""}`}
          >
            <div className="flex items-center gap-1.5">
              <span className="mono text-slate-400" title={fmtTime(e.at, true)}>
                {fmtTime(e.at)}
              </span>
              <Badge tone={e.status === "done" ? "slate" : i === nextIdx ? "cyan" : "blue"}>{i === nextIdx ? "next" : e.status}</Badge>
              <span className="mono text-slate-400">{e.camera_id}</span>
              <span className="ml-auto text-[10.5px] text-slate-500">{e.scenario}</span>
            </div>
            <div className="text-slate-200">{e.title}</div>
            <div className="text-[11px] text-slate-500">expected: {e.expected_outcome}</div>
          </li>
        ))}
      </ol>
    </Panel>
  );
}
