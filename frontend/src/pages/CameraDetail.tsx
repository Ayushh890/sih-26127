/**
 * Camera detail — live MJPEG preview (server-side overlay), control, fault injection,
 * health intelligence + history, logs, configuration and road-network neighbours.
 * Data: /api/cameras/:id (+ /health, /health/history, /logs, /stream.mjpg, /snapshot.jpg,
 * start/stop/restart, simulate-fault/-disconnect, clear-faults, PATCH, DELETE),
 * /api/topology/cameras/:id/neighbours; live via camera_status_changed.
 */
import { useEffect, useMemo, useState, type FormEvent } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CartesianGrid, Legend, Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { api, errorText, mediaUrl } from "../lib/api";
import { P, useAuth } from "../lib/auth";
import { useLiveEvent } from "../lib/ws";
import { fmtAgo, fmtDuration, fmtKm, fmtNum, fmtTime, statusTone, type Tone } from "../lib/format";
import { ActionButton, Badge, Empty, ErrorBox, Explanation, Field, KV, Loading, PageHeader, Panel, QueryState, Tabs } from "../components/ui";
import { TestResultView, type TestResult } from "../components/cameras/AddCameraModal";
import {
  CAMERA_TYPES,
  COMPASS,
  FAULT_KINDS,
  SOURCE_HINT,
  SOURCE_TYPES_CREATE,
  applyStatusToCache,
  diffObject,
  num,
  numList,
  type Camera,
  type CameraRuntime,
  type CameraStatusEvent,
} from "../components/cameras/model";

interface HealthFactor {
  factor: string;
  value: number | string | null;
  impact: number;
  text: string;
}
interface HealthReport {
  camera_id: string;
  status: string;
  status_message: string | null;
  window_s: number;
  samples: number;
  enabled: boolean;
  score: number | null;
  grade: string;
  factors: HealthFactor[];
  recommendations: string[];
  runtime: CameraRuntime | null;
}
interface HealthSample {
  ts: string;
  status: string;
  input_fps: number | null;
  processing_fps: number | null;
  latency_ms: number | null;
  inference_ms: number | null;
  queue_depth: number | null;
  frames_processed: number | null;
  frames_dropped: number | null;
  vehicles_detected: number | null;
  plates_read: number | null;
  avg_ocr_confidence: number | null;
  blur_score: number | null;
  brightness: number | null;
  reconnects: number | null;
  error: string | null;
}
interface LogRow {
  id: number;
  ts: string;
  level: string;
  source: string;
  event_type: string;
  message: string;
  details: Record<string, unknown> | null;
}
interface Neighbour {
  camera_id: string;
  distance_m: number;
  min_travel_s: number;
  typical_travel_s: number;
  road_name: string | null;
}
interface Neighbours {
  camera_id: string;
  upstream: Neighbour[];
  downstream: Neighbour[];
}

// Chart series colours (validated for CVD separation on the dark surface).
const SERIES_A = "#3987e5";
const SERIES_B = "#d95926";

function gradeTone(g?: string): Tone {
  return g === "GOOD" ? "green" : g === "FAIR" ? "yellow" : g === "POOR" ? "red" : "slate";
}
function logTone(l?: string): Tone {
  const u = (l ?? "").toUpperCase();
  return u === "ERROR" || u === "CRITICAL" ? "red" : u === "WARNING" || u === "WARN" ? "yellow" : u === "INFO" ? "blue" : "slate";
}

type TabId = "health" | "history" | "logs" | "config" | "topology";

export default function CameraDetail() {
  const { id = "" } = useParams();
  const { has } = useAuth();
  const qc = useQueryClient();
  const [tab, setTab] = useState<TabId>("health");
  const [streamKey, setStreamKey] = useState(0);

  const q = useQuery({
    queryKey: ["camera", id],
    queryFn: () => api.get<Camera>(`/api/cameras/${encodeURIComponent(id)}`),
    refetchInterval: 10_000,
  });

  useLiveEvent("camera_status_changed", (ev) => {
    const d = ev.data as unknown as CameraStatusEvent;
    applyStatusToCache(qc, d);
    if (d?.camera_id !== id) return;
    qc.invalidateQueries({ queryKey: ["camera", id, "logs"] });
    qc.invalidateQueries({ queryKey: ["camera", id, "health"] });
    if (d.status === "ONLINE" || d.status === "DEGRADED") setStreamKey((k) => k + 1); // reconnect the preview
  });

  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} onRetry={() => q.refetch()} />;
  const c = q.data;
  if (!c) return <Empty>Camera not found.</Empty>;

  const tabs: { id: TabId; label: string }[] = [
    { id: "health", label: "Health" },
    { id: "history", label: "History" },
    { id: "logs", label: "Logs" },
    { id: "config", label: has(P.CAMERAS_WRITE) ? "Configuration" : "Configuration (read-only)" },
    { id: "topology", label: "Neighbours" },
  ];

  return (
    <div className="flex flex-col gap-2">
      <PageHeader
        title={`${c.id} · ${c.name}`}
        subtitle={
          <span className="flex flex-wrap items-center gap-1.5">
            <Link to="/cameras" className="text-cyan-400 hover:underline">
              ← Cameras
            </Link>
            <Badge tone={statusTone(c.status)}>{c.status}</Badge>
            {c.status_message && <span className="text-slate-400">{c.status_message}</span>}
            {c.is_demo && <Badge tone="violet">Synthetic</Badge>}
            {!c.enabled && <Badge tone="slate">Disabled</Badge>}
            <span className="text-slate-500">last seen {fmtAgo(c.last_seen_at)}</span>
          </span>
        }
        actions={<Controls cam={c} onChanged={() => q.refetch()} />}
      />
      {c.is_demo && (
        <div className="rounded border border-violet-500/40 bg-violet-950/40 px-2 py-1 text-[12px] text-violet-200">
          <Badge tone="violet">Synthetic</Badge> This is a demo camera fed by the synthetic traffic scenario; detections are produced by the real pipeline.
        </div>
      )}

      <div className="grid gap-2 xl:grid-cols-12">
        <Panel title="Live view" className="xl:col-span-7" bodyClass="p-2">
          {has(P.STREAM_VIEW) ? (
            <LiveView cam={c} streamKey={streamKey} onRetry={() => setStreamKey((k) => k + 1)} />
          ) : (
            <Empty>Your role cannot view live video ({P.STREAM_VIEW}).</Empty>
          )}
        </Panel>
        <div className="flex flex-col gap-2 xl:col-span-5">
          <Panel title="Status & runtime">
            <RuntimeKV cam={c} />
          </Panel>
          {has(P.CAMERAS_CONTROL) && <FaultInjection cam={c} />}
        </div>
      </div>

      <Panel bodyClass="p-0">
        <Tabs tabs={tabs} value={tab} onChange={setTab} />
        <div className="p-2">
          {tab === "health" && <HealthTab id={c.id} />}
          {tab === "history" && <HistoryTab id={c.id} />}
          {tab === "logs" && <LogsTab id={c.id} />}
          {tab === "config" && <ConfigTab cam={c} />}
          {tab === "topology" && <NeighboursTab id={c.id} />}
        </div>
      </Panel>
    </div>
  );
}

/* --------------------------------------------------------------------------- controls */

function Controls({ cam, onChanged }: { cam: Camera; onChanged: () => void }) {
  const { has } = useAuth();
  const qc = useQueryClient();
  const nav = useNavigate();
  const [msg, setMsg] = useState<string | null>(null);
  const base = `/api/cameras/${encodeURIComponent(cam.id)}`;
  const run = async (path: string, label: string) => {
    await api.post(`${base}/${path}`);
    setMsg(`${label} requested`);
    onChanged();
    qc.invalidateQueries({ queryKey: ["cameras", "list"] });
  };
  return (
    <>
      {msg && <span className="text-[11px] text-emerald-300">{msg}</span>}
      {has(P.CAMERAS_CONTROL) && (
        <>
          {cam.enabled ? (
            <ActionButton className="btn" onClick={() => run("stop", "Stop")} confirm={`Stop processing ${cam.id}?`}>
              ■ Stop
            </ActionButton>
          ) : (
            <ActionButton className="btn btn-primary" onClick={() => run("start", "Start")}>
              ▶ Start
            </ActionButton>
          )}
          <ActionButton className="btn" onClick={() => run("restart", "Restart")} disabled={!cam.enabled} title={cam.enabled ? "Restart the camera worker" : "Start the camera first"}>
            ↻ Restart
          </ActionButton>
        </>
      )}
      {has(P.CAMERAS_WRITE) && (
        <ActionButton
          className="btn btn-danger"
          confirm={`Delete camera ${cam.id} and ALL of its observations and evidence? This cannot be undone.`}
          onClick={async () => {
            await api.del(base);
            await qc.invalidateQueries({ queryKey: ["cameras"] });
            qc.invalidateQueries({ queryKey: ["topology"] });
            qc.removeQueries({ queryKey: ["camera", cam.id] });
            nav("/cameras");
          }}
        >
          Delete
        </ActionButton>
      )}
    </>
  );
}

/* --------------------------------------------------------------------------- live view */

function LiveView({ cam, streamKey, onRetry }: { cam: Camera; streamKey: number; onRetry: () => void }) {
  const [overlay, setOverlay] = useState(true);
  const [fps, setFps] = useState(6);
  const [state, setState] = useState<"loading" | "ok" | "error">("loading");
  const [snapTs, setSnapTs] = useState(() => Date.now());
  const key = `${streamKey}-${overlay}-${fps}`;
  const [shownKey, setShownKey] = useState(key);
  if (shownKey !== key) {
    // New stream parameters: show the connecting state again (React "adjust state on prop change" pattern).
    setShownKey(key);
    setState("loading");
  }
  const base = `/api/cameras/${encodeURIComponent(cam.id)}`;
  const src = mediaUrl(`${base}/stream.mjpg`, { overlay: overlay ? 1 : 0, fps, k: streamKey });
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-[12px]">
        <label className="flex items-center gap-1.5 text-slate-300">
          <input type="checkbox" checked={overlay} onChange={(e) => setOverlay(e.target.checked)} /> Overlay (detections, tracks, plates)
        </label>
        <label className="flex items-center gap-1.5 text-slate-400">
          Preview fps
          <select className="input !w-auto !py-0.5" value={fps} onChange={(e) => setFps(Number(e.target.value))} aria-label="Preview fps">
            {[1, 2, 4, 6, 10, 15].map((v) => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        </label>
        <div className="flex-1" />
        <button className="btn" onClick={onRetry}>
          ↻ Reconnect
        </button>
        <a
          className="btn"
          href={mediaUrl(`${base}/snapshot.jpg`, { overlay: overlay ? 1 : 0, t: snapTs })}
          download={`${cam.id}-${new Date(snapTs).toISOString().replace(/[:.]/g, "-")}.jpg`}
          onClick={() => setTimeout(() => setSnapTs(Date.now()), 0)}
        >
          ⤓ Snapshot
        </a>
      </div>
      <div className="relative flex aspect-video items-center justify-center overflow-hidden rounded border border-ink-700 bg-black">
        {state !== "error" && (
          <img
            key={key}
            src={src}
            alt={`${cam.id} live preview`}
            className="h-full w-full object-contain"
            onLoad={() => setState("ok")}
            onError={() => setState("error")}
          />
        )}
        {state === "loading" && <div className="absolute inset-0 flex items-center justify-center text-[12px] text-slate-500">Connecting to preview…</div>}
        {state === "error" && (
          <div className="p-4 text-center text-[12px] text-slate-400" role="alert">
            <div className="text-slate-200">No live video</div>
            <div>
              The camera is {cam.status.toLowerCase()}
              {cam.status_message ? ` (${cam.status_message})` : ""} or is not being processed by any worker.
            </div>
            <button className="btn mt-2" onClick={onRetry}>
              Retry
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function RuntimeKV({ cam }: { cam: Camera }) {
  const r = cam.runtime;
  const faults = r?.faults && typeof r.faults === "object" ? Object.entries(r.faults as Record<string, number>) : [];
  return (
    <div className="space-y-2">
      <KV
        rows={[
          ["Source", <span className="mono">{`${cam.source_type} · ${cam.source_uri}`}</span>],
          ["Credentials", cam.has_credentials ? `set${cam.username_hint ? ` (user ${cam.username_hint})` : ""}` : "none"],
          ["Location", [cam.location, cam.road_name, cam.zone].filter(Boolean).join(" · ") || "—"],
          ["Position", `${cam.latitude.toFixed(5)}, ${cam.longitude.toFixed(5)} · dir ${cam.direction ?? "—"} · ${cam.lane_count ?? "—"} lanes`],
          ["Configured", `${cam.resolution ?? "native"} · ${fmtNum(cam.processing?.processing_fps, 1)} fps · conf ${fmtNum(cam.processing?.confidence_threshold, 2)}`],
        ]}
      />
      {!r ? (
        <div className="text-[12px] text-slate-500">No worker is reporting runtime metrics for this camera right now.</div>
      ) : (
        <KV
          rows={[
            ["Worker", `${r.worker_id ?? "—"} · up ${fmtDuration(r.uptime_s)}`],
            ["Input", `${fmtNum(r.input_fps, 1)} fps · ${r.resolution ?? "—"}`],
            ["Processing", `${fmtNum(r.processing_fps, 2)} / ${fmtNum(r.target_processing_fps, 1)} fps`],
            ["Latency", `${fmtNum(r.latency_ms, 0)} ms (inference ${fmtNum(r.inference_ms, 0)} ms)`],
            [
              "Stages",
              r.stage_ms && Object.keys(r.stage_ms).length
                ? Object.entries(r.stage_ms)
                    .map(([k, v]) => `${k} ${fmtNum(v, 0)}ms`)
                    .join(" · ")
                : "—",
            ],
            ["Queue", `${fmtNum(r.queue_depth)} / ${fmtNum(r.max_queue_size)}`],
            ["Frames", `${fmtNum(r.frames_in)} in · ${fmtNum(r.frames_processed)} processed · ${fmtNum(r.frames_dropped)} dropped`],
            ["Image", `sharpness ${fmtNum(r.sharpness, 0)} · brightness ${fmtNum(r.brightness, 0)}`],
            ["Tracks", fmtNum(r.active_tracks)],
            ["Reconnects", fmtNum(r.reconnects)],
            ["Last error", r.last_error ? <span className="text-rose-300">{String(r.last_error)}</span> : "—"],
            [
              "Active faults",
              faults.length ? (
                <span className="flex flex-wrap gap-1">
                  {faults.map(([k, s]) => (
                    <Badge key={k} tone="orange">
                      {k} {fmtDuration(s)}
                    </Badge>
                  ))}
                </span>
              ) : (
                "none"
              ),
            ],
          ]}
        />
      )}
    </div>
  );
}

/* --------------------------------------------------------------------------- fault injection */

function FaultInjection({ cam }: { cam: Camera }) {
  const qc = useQueryClient();
  const [kind, setKind] = useState<string>("offline");
  const [seconds, setSeconds] = useState("30");
  const [note, setNote] = useState<string | null>(null);
  const base = `/api/cameras/${encodeURIComponent(cam.id)}`;
  const secs = Math.min(600, Math.max(1, Number(seconds) || 30));
  const refresh = () => qc.invalidateQueries({ queryKey: ["camera", cam.id] });
  return (
    <Panel title="Fault injection" subtitle="Resilience testing — affects only this camera's input">
      <div className="flex flex-wrap items-end gap-2">
        <Field label="Fault">
          <select className="input" value={kind} onChange={(e) => setKind(e.target.value)}>
            {FAULT_KINDS.map((k) => (
              <option key={k}>{k}</option>
            ))}
          </select>
        </Field>
        <Field label="Seconds">
          <input className="input mono !w-20" type="number" min={1} max={600} value={seconds} onChange={(e) => setSeconds(e.target.value)} />
        </Field>
        <ActionButton
          className="btn"
          onClick={async () => {
            const r = await api.post<{ note?: string }>(`${base}/simulate-fault`, { kind, seconds: secs });
            setNote(`Injected ${kind} for ${secs}s. ${r.note ?? ""}`);
            refresh();
          }}
        >
          Inject fault
        </ActionButton>
        <ActionButton
          className="btn"
          onClick={async () => {
            await api.post(`${base}/simulate-disconnect`, undefined, { seconds: secs });
            setNote(`Simulated a ${secs}s stream disconnect.`);
            refresh();
          }}
        >
          Simulate disconnect
        </ActionButton>
        <ActionButton
          className="btn"
          onClick={async () => {
            await api.post(`${base}/clear-faults`);
            setNote("Faults cleared.");
            refresh();
          }}
        >
          Clear faults
        </ActionButton>
      </div>
      {note && <div className="mt-1 text-[11px] text-emerald-300">{note}</div>}
    </Panel>
  );
}

/* --------------------------------------------------------------------------- health */

function HealthTab({ id }: { id: string }) {
  const [win, setWin] = useState(60);
  const q = useQuery({
    queryKey: ["camera", id, "health", win],
    queryFn: () => api.get<HealthReport>(`/api/cameras/${encodeURIComponent(id)}/health`, { window_minutes: win }),
    refetchInterval: 30_000,
  });
  return (
    <div className="space-y-2">
      <label className="flex items-center gap-1.5 text-[12px] text-slate-400">
        Window
        <select className="input !w-auto !py-0.5" value={win} onChange={(e) => setWin(Number(e.target.value))} aria-label="Health window">
          {[15, 60, 360, 1440].map((m) => (
            <option key={m} value={m}>
              {fmtDuration(m * 60)}
            </option>
          ))}
        </select>
      </label>
      <QueryState q={q}>
        {(h) => (
          <div className="grid gap-3 md:grid-cols-[200px_1fr_1fr]">
            <div className="rounded border border-ink-700 p-2">
              <div className="label">Health score</div>
              <div className="font-mono text-2xl font-semibold text-slate-100">{h.score == null ? "—" : fmtNum(h.score, 0)}</div>
              <Badge tone={gradeTone(h.grade)}>{h.grade}</Badge>
              <div className="mt-1 text-[11px] text-slate-500">
                {h.samples} samples over {fmtDuration(h.window_s)}
              </div>
            </div>
            <div>
              <div className="label">Factors</div>
              {h.factors.length === 0 ? (
                <div className="text-[12px] text-slate-500">No factors reduce the score.</div>
              ) : (
                <ul className="space-y-1 text-[12px]">
                  {h.factors.map((f) => (
                    <li key={f.factor} className="flex gap-2">
                      <span className={`w-10 shrink-0 text-right font-mono ${f.impact < 0 ? "text-rose-300" : "text-emerald-300"}`}>{f.impact > 0 ? `+${f.impact}` : f.impact}</span>
                      <span className="text-slate-300">{f.text}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
            <div>
              <div className="label">Recommendations</div>
              {h.recommendations.length ? <Explanation lines={h.recommendations} /> : <div className="text-[12px] text-slate-500">None.</div>}
            </div>
          </div>
        )}
      </QueryState>
    </div>
  );
}

function HistoryTab({ id }: { id: string }) {
  const [minutes, setMinutes] = useState(60);
  const q = useQuery({
    queryKey: ["camera", id, "health", "history", minutes],
    queryFn: () => api.get<HealthSample[]>(`/api/cameras/${encodeURIComponent(id)}/health/history`, { minutes }),
    refetchInterval: 30_000,
  });
  const data = useMemo(() => (q.data ?? []).map((s) => ({ ...s, t: new Date(s.ts).getTime() })), [q.data]);
  const errors = data.filter((s) => s.error);
  return (
    <div className="space-y-2">
      <label className="flex items-center gap-1.5 text-[12px] text-slate-400">
        Period
        <select className="input !w-auto !py-0.5" value={minutes} onChange={(e) => setMinutes(Number(e.target.value))} aria-label="History period">
          {[15, 60, 360, 1440, 10080].map((m) => (
            <option key={m} value={m}>
              {fmtDuration(m * 60)}
            </option>
          ))}
        </select>
      </label>
      <QueryState q={q} isEmpty={(d) => d.length === 0}>
        {() => (
          <>
            <div className="grid gap-2 lg:grid-cols-3">
              <HistoryChart title="Frame rate (fps)" data={data} a={["input_fps", "Input"]} b={["processing_fps", "Processing"]} />
              <HistoryChart title="Latency (ms)" data={data} a={["latency_ms", "End-to-end"]} b={["inference_ms", "Inference"]} />
              <HistoryChart title="Detections per sample" data={data} a={["vehicles_detected", "Vehicles"]} b={["plates_read", "Plates"]} />
            </div>
            {errors.length > 0 && (
              <div>
                <div className="label">Errors in this period</div>
                <ul className="space-y-0.5 text-[12px]">
                  {errors.slice(-10).map((s) => (
                    <li key={s.ts}>
                      <span className="mono text-slate-500">{fmtTime(s.ts, true)}</span> <span className="text-rose-300">{s.error}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </>
        )}
      </QueryState>
    </div>
  );
}

type Row = HealthSample & { t: number };
function HistoryChart({ title, data, a, b }: { title: string; data: Row[]; a: [keyof HealthSample, string]; b: [keyof HealthSample, string] }) {
  return (
    <div className="rounded border border-ink-700 p-1.5">
      <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-slate-400">{title}</div>
      <div className="h-[180px]">
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: -12 }}>
            <CartesianGrid stroke="#1c2a44" vertical={false} />
            <XAxis
              dataKey="t"
              type="number"
              scale="time"
              domain={["dataMin", "dataMax"]}
              tickFormatter={(v: number) => fmtTime(v).slice(0, 5)}
              stroke="#475569"
              tick={{ fontSize: 10, fill: "#94a3b8" }}
            />
            <YAxis stroke="#475569" tick={{ fontSize: 10, fill: "#94a3b8" }} width={40} />
            <Tooltip
              contentStyle={{ background: "#0f1829", border: "1px solid #2a3b5c", fontSize: 11, color: "#e2e8f0" }}
              labelFormatter={(v) => fmtTime(Number(v), true)}
              formatter={(v) => (typeof v === "number" ? fmtNum(v, 1) : String(v ?? "—"))}
            />
            <Legend wrapperStyle={{ fontSize: 11, color: "#cbd5e1" }} iconType="plainline" />
            <Line type="monotone" dataKey={a[0]} name={a[1]} stroke={SERIES_A} strokeWidth={2} dot={false} connectNulls={false} isAnimationActive={false} />
            <Line type="monotone" dataKey={b[0]} name={b[1]} stroke={SERIES_B} strokeWidth={2} dot={false} connectNulls={false} isAnimationActive={false} />
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

/* --------------------------------------------------------------------------- logs */

function LogsTab({ id }: { id: string }) {
  const [limit, setLimit] = useState(100);
  const q = useQuery({
    queryKey: ["camera", id, "logs", limit],
    queryFn: () => api.get<LogRow[]>(`/api/cameras/${encodeURIComponent(id)}/logs`, { limit }),
    refetchInterval: 30_000,
  });
  return (
    <div className="space-y-2">
      <label className="flex items-center gap-1.5 text-[12px] text-slate-400">
        Show last
        <select className="input !w-auto !py-0.5" value={limit} onChange={(e) => setLimit(Number(e.target.value))} aria-label="Log limit">
          {[50, 100, 500, 1000].map((m) => (
            <option key={m}>{m}</option>
          ))}
        </select>
      </label>
      <QueryState q={q} isEmpty={(d) => d.length === 0} empty="No log entries for this camera.">
        {(rows) => (
          <div className="max-h-[420px] overflow-auto">
            <table className="w-full text-[12px]">
              <thead>
                <tr>
                  <th className="th">Time</th>
                  <th className="th">Level</th>
                  <th className="th">Source</th>
                  <th className="th">Event</th>
                  <th className="th">Message</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id}>
                    <td className="td mono whitespace-nowrap">{fmtTime(r.ts, true)}</td>
                    <td className="td">
                      <Badge tone={logTone(r.level)}>{r.level}</Badge>
                    </td>
                    <td className="td mono text-slate-400">{r.source}</td>
                    <td className="td mono text-slate-400">{r.event_type}</td>
                    <td className="td text-slate-200">
                      {r.message}
                      {r.details && (r.details as { intentional?: boolean }).intentional === true && <span className="ml-1 text-[11px] text-slate-500">(intentional)</span>}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </QueryState>
    </div>
  );
}

/* --------------------------------------------------------------------------- configuration */

function formFrom(c: Camera) {
  const p = c.processing ?? {};
  const k = c.calibration ?? {};
  const s = (v: unknown) => (v == null ? "" : String(v));
  return {
    name: c.name,
    location: c.location ?? "",
    latitude: s(c.latitude),
    longitude: s(c.longitude),
    road_name: c.road_name ?? "",
    zone: c.zone ?? "",
    lane_count: s(c.lane_count),
    direction: c.direction ?? "N",
    camera_type: c.camera_type ?? "ANPR",
    resolution: c.resolution ?? "",
    source_type: c.source_type,
    source_uri: c.source_uri,
    username: "",
    password: "",
    clear_credentials: false,
    processing_fps: s(p.processing_fps),
    confidence_threshold: s(p.confidence_threshold),
    frame_skip: s(p.frame_skip),
    max_queue_size: s(p.max_queue_size),
    reference_width: s(k.reference_width),
    focal_px: s(k.focal_px),
    camera_height_m: s(k.camera_height_m),
    speed_limit_kmh: s(k.speed_limit_kmh),
    flow_toward_camera: !!k.flow_toward_camera,
    one_way: !!k.one_way,
    vanishing_point: Array.isArray(k.vanishing_point) ? k.vanishing_point.join(", ") : "",
    lane_boundaries: Array.isArray(k.lane_boundaries) ? k.lane_boundaries.join(", ") : "",
  };
}
type Form = ReturnType<typeof formFrom>;

/** Build the PATCH body for the edited form; only changed fields are sent. Throws on invalid input. */
export function buildCameraPatch(c: Camera, f: Form): Record<string, unknown> {
  const top: Record<string, unknown> = {
    name: f.name.trim(),
    location: f.location,
    latitude: num(f.latitude),
    longitude: num(f.longitude),
    road_name: f.road_name,
    zone: f.zone,
    lane_count: num(f.lane_count),
    direction: f.direction,
    camera_type: f.camera_type,
    resolution: f.resolution.trim() || null,
  };
  if (!c.is_demo) {
    top.source_type = f.source_type;
    top.source_uri = f.source_uri.trim();
  }
  const body = diffObject(c as unknown as Record<string, unknown>, top);
  if (body.resolution === null && !c.resolution) delete body.resolution;
  if (!c.is_demo && f.username.trim()) body.username = f.username.trim();
  if (!c.is_demo && f.password) body.password = f.password;
  if (f.clear_credentials) body.clear_credentials = true;

  const proc = diffObject(c.processing ?? {}, {
    processing_fps: num(f.processing_fps),
    confidence_threshold: num(f.confidence_threshold),
    frame_skip: num(f.frame_skip),
    max_queue_size: num(f.max_queue_size),
  });
  if (Object.keys(proc).length) body.processing = proc;

  const vp = numList(f.vanishing_point);
  const lb = numList(f.lane_boundaries);
  if (vp === null || (vp && vp.length !== 2)) throw new Error("Vanishing point must be two numbers: x, y (frame fractions).");
  if (lb === null) throw new Error("Lane boundaries must be a comma separated list of numbers.");
  const cal = diffObject(c.calibration ?? {}, {
    reference_width: num(f.reference_width),
    focal_px: num(f.focal_px),
    camera_height_m: num(f.camera_height_m),
    speed_limit_kmh: num(f.speed_limit_kmh),
    // unset booleans read back as false; only send them when the operator actually toggled them
    flow_toward_camera: f.flow_toward_camera === !!c.calibration?.flow_toward_camera ? undefined : f.flow_toward_camera,
    one_way: f.one_way === !!c.calibration?.one_way ? undefined : f.one_way,
    vanishing_point: vp,
    lane_boundaries: lb,
  });
  if (Object.keys(cal).length) body.calibration = cal;
  return body;
}

function ConfigTab({ cam }: { cam: Camera }) {
  const { has } = useAuth();
  const qc = useQueryClient();
  const canEdit = has(P.CAMERAS_WRITE);
  const [f, setF] = useState<Form>(() => formFrom(cam));
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [ok, setOk] = useState<string | null>(null);
  const [test, setTest] = useState<TestResult | null>(null);
  const [testing, setTesting] = useState(false);

  // Follow server updates while the operator has not started editing.
  useEffect(() => {
    if (!dirty) setF(formFrom(cam));
  }, [cam, dirty]);

  const set = <K extends keyof Form>(k: K, v: Form[K]) => {
    setDirty(true);
    setOk(null);
    setF((p) => ({ ...p, [k]: v }));
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setErr(null);
    setOk(null);
    let body: Record<string, unknown>;
    try {
      body = buildCameraPatch(cam, f);
    } catch (e2) {
      setErr(errorText(e2));
      return;
    }
    if (Object.keys(body).length === 0) {
      setOk("Nothing changed.");
      return;
    }
    setBusy(true);
    try {
      const updated = await api.patch<Camera>(`/api/cameras/${encodeURIComponent(cam.id)}`, body);
      qc.setQueryData(["camera", cam.id], updated);
      qc.invalidateQueries({ queryKey: ["cameras", "list"] });
      qc.invalidateQueries({ queryKey: ["topology"] });
      setDirty(false);
      setF(formFrom(updated));
      setOk(`Saved (${Object.keys(body).join(", ")}). The worker restarts with the new configuration.`);
    } catch (e2) {
      setErr(errorText(e2));
    } finally {
      setBusy(false);
    }
  };

  const runTest = async () => {
    setTesting(true);
    setTest(null);
    setErr(null);
    try {
      setTest(
        await api.post<TestResult>("/api/cameras/test-connection", {
          source_type: f.source_type,
          source_uri: f.source_uri.trim(),
          username: f.username || undefined,
          password: f.password || undefined,
          camera_id: cam.id,
        }),
      );
    } catch (e2) {
      setErr(errorText(e2));
    } finally {
      setTesting(false);
    }
  };

  const inp = (k: keyof Form, props: Record<string, unknown> = {}) => (
    <input className="input mono" disabled={!canEdit} value={f[k] as string} onChange={(e) => set(k, e.target.value as never)} {...props} />
  );

  return (
    <form onSubmit={submit} className="space-y-3">
      <fieldset className="grid grid-cols-2 gap-2 md:grid-cols-6">
        <legend className="label">General</legend>
        <div className="col-span-2">
          <Field label="Name">{inp("name", { required: true, maxLength: 128, className: "input" })}</Field>
        </div>
        <div className="col-span-2">
          <Field label="Location">{inp("location", { maxLength: 255, className: "input" })}</Field>
        </div>
        <Field label="Latitude">{inp("latitude", { type: "number", step: "any", min: -90, max: 90, required: true })}</Field>
        <Field label="Longitude">{inp("longitude", { type: "number", step: "any", min: -180, max: 180, required: true })}</Field>
        <Field label="Road name">{inp("road_name", { maxLength: 128, className: "input" })}</Field>
        <Field label="Zone">{inp("zone", { maxLength: 64, className: "input" })}</Field>
        <Field label="Lanes">{inp("lane_count", { type: "number", min: 1, max: 12 })}</Field>
        <Field label="Direction">
          <select className="input" disabled={!canEdit} value={f.direction} onChange={(e) => set("direction", e.target.value)}>
            {COMPASS.map((d) => (
              <option key={d}>{d}</option>
            ))}
          </select>
        </Field>
        <Field label="Type">
          <select className="input" disabled={!canEdit} value={f.camera_type} onChange={(e) => set("camera_type", e.target.value)}>
            {CAMERA_TYPES.map((d) => (
              <option key={d}>{d}</option>
            ))}
          </select>
        </Field>
        <Field label="Resolution" hint="blank = native">
          {inp("resolution", { pattern: "\\d{3,4}x\\d{3,4}", placeholder: "1280x720" })}
        </Field>
      </fieldset>

      <fieldset className="space-y-2">
        <legend className="label">Source {cam.is_demo && <span className="normal-case text-slate-500">(demo camera source cannot be changed)</span>}</legend>
        <div className="grid grid-cols-2 gap-2 md:grid-cols-6">
          <Field label="Source type">
            {cam.is_demo ? (
              <input className="input mono" disabled value={cam.source_type} />
            ) : (
              <select className="input" disabled={!canEdit} value={f.source_type} onChange={(e) => set("source_type", e.target.value)}>
                {SOURCE_TYPES_CREATE.map((t) => (
                  <option key={t}>{t}</option>
                ))}
              </select>
            )}
          </Field>
          <div className="col-span-2 md:col-span-3">
            <Field label="Source URI" hint={SOURCE_HINT[f.source_type]}>
              {inp("source_uri", { disabled: !canEdit || cam.is_demo, required: true })}
            </Field>
          </div>
          {canEdit && !cam.is_demo && (
            <div className="flex items-end">
              <button type="button" className="btn" disabled={testing || !f.source_uri.trim()} onClick={runTest}>
                {testing && <span className="h-2.5 w-2.5 animate-spin rounded-full border border-slate-400 border-t-transparent" />}
                Test connection
              </button>
            </div>
          )}
        </div>
        {test && <TestResultView r={test} />}
        {!cam.is_demo && (
          <div className="grid grid-cols-2 gap-2 md:grid-cols-4">
            <Field label="Username" hint={cam.has_credentials ? `current: ${cam.username_hint ?? "set"} — blank keeps it` : "none set"}>
              <input className="input" autoComplete="off" disabled={!canEdit} value={f.username} onChange={(e) => set("username", e.target.value)} />
            </Field>
            <Field label="New password" hint="write-only; blank keeps the stored password">
              <input className="input" type="password" autoComplete="new-password" disabled={!canEdit} value={f.password} onChange={(e) => set("password", e.target.value)} />
            </Field>
            {cam.has_credentials && (
              <label className="flex items-end gap-2 pb-1 text-[12px] text-slate-300">
                <input type="checkbox" disabled={!canEdit} checked={f.clear_credentials} onChange={(e) => set("clear_credentials", e.target.checked)} /> Clear stored credentials
              </label>
            )}
          </div>
        )}
      </fieldset>

      <fieldset className="grid grid-cols-2 gap-2 md:grid-cols-4">
        <legend className="label">Processing</legend>
        <Field label="Processing fps" hint="0.5–30">
          {inp("processing_fps", { type: "number", step: 0.5, min: 0.5, max: 30 })}
        </Field>
        <Field label="Confidence threshold" hint="0.05–0.95">
          {inp("confidence_threshold", { type: "number", step: 0.01, min: 0.05, max: 0.95 })}
        </Field>
        <Field label="Frame skip" hint="0–30">
          {inp("frame_skip", { type: "number", min: 0, max: 30 })}
        </Field>
        <Field label="Max queue size" hint="1–64">
          {inp("max_queue_size", { type: "number", min: 1, max: 64 })}
        </Field>
      </fieldset>

      <fieldset className="grid grid-cols-2 gap-2 md:grid-cols-4">
        <legend className="label">Calibration (speed estimation & lanes){cam.calibration?.source ? ` · source: ${cam.calibration.source}` : ""}</legend>
        <Field label="Reference width (px)">{inp("reference_width", { type: "number", min: 160, max: 7680 })}</Field>
        <Field label="Focal length (px)">{inp("focal_px", { type: "number", step: "any", min: 0, max: 20000 })}</Field>
        <Field label="Camera height (m)">{inp("camera_height_m", { type: "number", step: "any", min: 0, max: 50 })}</Field>
        <Field label="Speed limit (km/h)">{inp("speed_limit_kmh", { type: "number", step: "any", min: 0, max: 200 })}</Field>
        <Field label="Vanishing point" hint="x, y as frame fractions">
          {inp("vanishing_point", { placeholder: "0.5, 0.15" })}
        </Field>
        <div className="col-span-2">
          <Field label="Lane boundaries" hint="comma separated frame fractions (2–13 values)">
            {inp("lane_boundaries")}
          </Field>
        </div>
        <div className="flex flex-col justify-end gap-1 pb-1 text-[12px] text-slate-300">
          <label className="flex items-center gap-2">
            <input type="checkbox" disabled={!canEdit} checked={f.flow_toward_camera} onChange={(e) => set("flow_toward_camera", e.target.checked)} /> Traffic flows toward camera
          </label>
          <label className="flex items-center gap-2">
            <input type="checkbox" disabled={!canEdit} checked={f.one_way} onChange={(e) => set("one_way", e.target.checked)} /> One-way road
          </label>
        </div>
      </fieldset>

      {err && (
        <div className="rounded border border-rose-700/60 bg-rose-950/40 p-2 text-[12px] text-rose-200" role="alert">
          {err}
        </div>
      )}
      {ok && <div className="text-[12px] text-emerald-300">{ok}</div>}
      {canEdit && (
        <div className="flex gap-2">
          <button type="submit" className="btn btn-primary" disabled={busy || !dirty}>
            {busy && <span className="h-2.5 w-2.5 animate-spin rounded-full border border-slate-200 border-t-transparent" />}
            Save changes
          </button>
          <button
            type="button"
            className="btn"
            disabled={!dirty}
            onClick={() => {
              setF(formFrom(cam));
              setDirty(false);
              setErr(null);
              setTest(null);
            }}
          >
            Discard
          </button>
        </div>
      )}
    </form>
  );
}

/* --------------------------------------------------------------------------- neighbours */

function NeighboursTab({ id }: { id: string }) {
  const q = useQuery({
    queryKey: ["topology", "neighbours", id],
    queryFn: () => api.get<Neighbours>(`/api/topology/cameras/${encodeURIComponent(id)}/neighbours`),
  });
  return (
    <QueryState q={q}>
      {(n) => (
        <div className="grid gap-3 md:grid-cols-2">
          <NeighbourTable title="Upstream (vehicles arrive from)" rows={n.upstream} />
          <NeighbourTable title="Downstream (vehicles continue to)" rows={n.downstream} />
        </div>
      )}
    </QueryState>
  );
}

function NeighbourTable({ title, rows }: { title: string; rows: Neighbour[] }) {
  return (
    <div>
      <div className="label">{title}</div>
      {rows.length === 0 ? (
        <div className="text-[12px] text-slate-500">No linked cameras in the road topology.</div>
      ) : (
        <table className="w-full text-[12px]">
          <thead>
            <tr>
              <th className="th">Camera</th>
              <th className="th">Road</th>
              <th className="th text-right">Distance</th>
              <th className="th text-right">Min travel</th>
              <th className="th text-right">Typical</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.camera_id}>
                <td className="td">
                  <Link to={`/cameras/${encodeURIComponent(r.camera_id)}`} className="mono text-cyan-300 hover:underline">
                    {r.camera_id}
                  </Link>
                </td>
                <td className="td text-slate-300">{r.road_name || "—"}</td>
                <td className="td mono text-right">{fmtKm(r.distance_m)}</td>
                <td className="td mono text-right">{fmtDuration(r.min_travel_s)}</td>
                <td className="td mono text-right">
                  {fmtDuration(r.typical_travel_s)}
                  <span className="ml-1 text-slate-500">({fmtNum((r.distance_m / 1000 / r.typical_travel_s) * 3600, 0)} km/h)</span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
