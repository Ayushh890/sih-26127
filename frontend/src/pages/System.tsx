/**
 * System health: readiness checks, runtime, workers, bus, models, storage, system event
 * log, dead-letter queue and data retention. Auto-refreshes every 10 s.
 */
import { useState, type ReactNode } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, ApiError } from "../lib/api";
import { P, useAuth } from "../lib/auth";
import { fmtAgo, fmtDuration, fmtNum, fmtTime, statusTone, TONE_HEX, type Tone } from "../lib/format";
import { useTopology } from "../components/NetworkMap";
import { ActionButton, Badge, Dot, Empty, ErrorBox, KV, Loading, PageHeader, Panel, QueryState, Stat, Tabs } from "../components/ui";
import { CameraSelect, JsonToggle, prettyKey, scalar } from "../components/admin/common";

const REFRESH_MS = 10_000;

interface ModelInfo {
  role: string;
  configured?: string | null;
  active?: string | null;
  path?: string | null;
  fallback?: boolean;
  error?: string | null;
  [k: string]: unknown;
}
interface Worker {
  worker_id: string;
  last_heartbeat: string;
  age_s: number;
  alive: boolean;
  info: (Record<string, unknown> & { cameras?: Record<string, string>; models?: ModelInfo[] }) | null;
}
interface SystemStatus {
  app: { name: string; env: string; version: string; python: string };
  runtime: { mode: string; uptime_s: number; bus: string | null; stream_manager: boolean; ingestion: boolean; scheduler: boolean; startup_errors: string[] };
  hardware: Record<string, unknown>;
  database: { backend: string; latency_ms: number | null };
  bus: { backend: string | null; reachable: boolean; ingest_depth: number | null; publish_failures: number | null };
  ingestion: Record<string, unknown> | null;
  scheduler: Record<string, unknown> | null;
  workers: Worker[];
  models: ModelInfo[] | null;
  cameras: { by_status: Record<string, number>; total: number };
  dead_letters_open: number;
  evidence: { usage_mb: number; limit_mb: number; encrypted: boolean; files: number };
  throughput_1h: { observations: number; vehicles: number; alerts: number };
  privacy: { privacy_mode: boolean; pseudonymised_roles: string[] };
}
interface Ready {
  status: string;
  mode: string;
  checks: Record<string, { ok: boolean; [k: string]: unknown }>;
  startup_errors: string[];
}
interface Health {
  status: string;
  service: string;
  uptime_s: number;
}
interface SysEvent {
  id: number;
  ts: string;
  level: string;
  source: string;
  camera_id: string | null;
  event_type: string;
  message: string;
  details: unknown;
}
interface DeadLetter {
  id: number;
  ts: string;
  source: string;
  event_type: string;
  camera_id: string | null;
  error: string;
  attempts: number;
  resolved: boolean;
  resolved_at: string | null;
  resolution: string | null;
  payload_keys?: string[];
}

/** /ready answers 503 with the same body when not ready — that is a result, not a failure. */
async function fetchReady(): Promise<Ready> {
  try {
    return await api.get<Ready>("/ready");
  } catch (e) {
    if (e instanceof ApiError && e.status === 503 && e.detail && typeof e.detail === "object" && "checks" in (e.detail as object)) return e.detail as Ready;
    throw e;
  }
}

const LEVELS = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"];
function levelBadge(l: string): Tone {
  return l === "CRITICAL" || l === "ERROR" ? "red" : l === "WARNING" ? "yellow" : l === "INFO" ? "blue" : "slate";
}
const okBadge = (ok: boolean | null | undefined, yes = "OK", no = "FAIL") =>
  ok == null ? <Badge tone="slate">n/a</Badge> : ok ? <Badge tone="green">{yes}</Badge> : <Badge tone="red">{no}</Badge>;

export default function SystemPage() {
  const status = useQuery({ queryKey: ["system", "status"], queryFn: () => api.get<SystemStatus>("/api/system/status"), refetchInterval: REFRESH_MS });
  const ready = useQuery({ queryKey: ["system", "ready"], queryFn: fetchReady, refetchInterval: REFRESH_MS, retry: false });
  const health = useQuery({ queryKey: ["system", "health"], queryFn: () => api.get<Health>("/health"), refetchInterval: REFRESH_MS, retry: false });
  const s = status.data;

  return (
    <div className="flex flex-col gap-2">
      <PageHeader
        title="System Health"
        subtitle={
          <span className="inline-flex items-center gap-1.5">
            <Dot color={status.error ? TONE_HEX.red : TONE_HEX.green} pulse={status.isFetching} />
            Auto-refresh every {REFRESH_MS / 1000} s{status.dataUpdatedAt ? ` · updated ${fmtTime(status.dataUpdatedAt)}` : ""}
          </span>
        }
        actions={
          <button
            className="btn"
            onClick={() => {
              status.refetch();
              ready.refetch();
              health.refetch();
            }}
          >
            Refresh now
          </button>
        }
      />

      <div className="grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-8">
        <Stat
          label="Readiness"
          value={ready.data ? ready.data.status.replace("_", " ").toUpperCase() : ready.error ? "ERROR" : "…"}
          tone={ready.data?.status === "ready" ? TONE_HEX.green : TONE_HEX.red}
          hint={ready.data ? `mode ${ready.data.mode}` : undefined}
        />
        <Stat label="API liveness" value={health.data?.status?.toUpperCase() ?? (health.error ? "DOWN" : "…")} tone={health.data?.status === "ok" ? TONE_HEX.green : TONE_HEX.red} hint={health.data ? `up ${fmtDuration(health.data.uptime_s)}` : undefined} />
        <Stat label="DB latency" value={s?.database.latency_ms != null ? fmtNum(s.database.latency_ms, 1, "ms") : "—"} hint={s?.database.backend} />
        <Stat label="Bus" value={s ? (s.bus.reachable ? "UP" : "DOWN") : "…"} tone={s ? (s.bus.reachable ? TONE_HEX.green : TONE_HEX.red) : undefined} hint={s ? `${s.bus.backend ?? "none"} · depth ${s.bus.ingest_depth ?? "—"}` : undefined} />
        <Stat label="Workers alive" value={s ? `${s.workers.filter((w) => w.alive).length}/${s.workers.length}` : "…"} tone={s && s.workers.some((w) => w.alive) ? TONE_HEX.green : TONE_HEX.orange} />
        <Stat label="Dead letters" value={s ? fmtNum(s.dead_letters_open) : "…"} tone={s?.dead_letters_open ? TONE_HEX.orange : undefined} hint="open" />
        <Stat label="Obs. last 1 h" value={s ? fmtNum(s.throughput_1h.observations) : "…"} hint={s ? `${fmtNum(s.throughput_1h.vehicles)} vehicles` : undefined} />
        <Stat label="Alerts last 1 h" value={s ? fmtNum(s.throughput_1h.alerts) : "…"} />
      </div>

      <div className="grid grid-cols-1 gap-2 lg:grid-cols-3">
        <Panel title="Readiness checks" subtitle="GET /ready">
          <QueryState q={ready}>
            {(r) => (
              <div className="space-y-2">
                <table className="w-full text-[12px]">
                  <tbody>
                    {Object.entries(r.checks).map(([name, c]) => (
                      <tr key={name}>
                        <td className="td font-medium">{prettyKey(name)}</td>
                        <td className="td">{okBadge(c.ok)}</td>
                        <td className="td text-slate-400">
                          {Object.entries(c)
                            .filter(([k]) => k !== "ok")
                            .map(([k, v]) => (
                              <div key={k}>
                                {prettyKey(k)}: <span className="mono text-slate-300">{k === "last_heartbeat" && typeof v === "string" ? `${fmtTime(v, true)} (${fmtAgo(v)})` : scalar(v)}</span>
                              </div>
                            ))}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {r.startup_errors.length > 0 ? (
                  <div className="rounded border border-rose-700/60 bg-rose-950/40 p-1.5 text-[12px] text-rose-200">
                    <div className="font-semibold">Startup errors</div>
                    <ul className="list-inside list-disc">
                      {r.startup_errors.map((e, i) => (
                        <li key={i}>{e}</li>
                      ))}
                    </ul>
                  </div>
                ) : (
                  <div className="text-[11px] text-slate-500">No startup errors.</div>
                )}
              </div>
            )}
          </QueryState>
        </Panel>

        <Panel title="Runtime & hardware" subtitle="GET /api/system/status">
          <QueryState q={status}>
            {(d) => (
              <KV
                rows={[
                  ["Application", `${d.app.name} ${d.app.version} · ${d.app.env}`],
                  ["Python", d.app.python],
                  ["Process mode", <Badge tone="blue">{d.runtime.mode}</Badge>],
                  ["Uptime", fmtDuration(d.runtime.uptime_s)],
                  ["Stream manager", okBadge(d.runtime.stream_manager, "running", "not in this process")],
                  ["Ingestion", okBadge(d.runtime.ingestion, "running", "not in this process")],
                  ["Scheduler", okBadge(d.runtime.scheduler, "running", "not in this process")],
                  ...Object.entries(d.hardware).map(([k, v]) => [prettyKey(k), <span className="mono">{scalar(v)}</span>] as [string, ReactNode]),
                  ["Bus publish failures", <span className="mono">{scalar(d.bus.publish_failures)}</span>],
                ]}
              />
            )}
          </QueryState>
        </Panel>

        <Panel title="Cameras, storage & privacy">
          <QueryState q={status}>
            {(d) => (
              <div className="space-y-2">
                <div>
                  <div className="label">Cameras by status ({d.cameras.total})</div>
                  <div className="flex flex-wrap gap-1">
                    {Object.keys(d.cameras.by_status).length === 0 ? (
                      <span className="text-[12px] text-slate-500">No cameras configured.</span>
                    ) : (
                      Object.entries(d.cameras.by_status).map(([st, n]) => (
                        <Badge key={st} tone={statusTone(st)}>
                          {st} · {n}
                        </Badge>
                      ))
                    )}
                  </div>
                </div>
                <div>
                  <div className="label">Evidence storage</div>
                  <div className="h-2 overflow-hidden rounded bg-ink-800">
                    <div
                      className="h-full"
                      style={{
                        width: `${d.evidence.limit_mb ? Math.min(100, (d.evidence.usage_mb / d.evidence.limit_mb) * 100) : 0}%`,
                        background: d.evidence.limit_mb && d.evidence.usage_mb / d.evidence.limit_mb > 0.9 ? TONE_HEX.red : TONE_HEX.cyan,
                      }}
                    />
                  </div>
                  <div className="mt-0.5 text-[11px] text-slate-400">
                    {fmtNum(d.evidence.usage_mb, 1)} / {fmtNum(d.evidence.limit_mb, 1)} MB · {fmtNum(d.evidence.files)} files · {d.evidence.encrypted ? "encrypted at rest" : "not encrypted"}
                  </div>
                </div>
                <KV
                  rows={[
                    ["Privacy mode", d.privacy.privacy_mode ? <Badge tone="green">on</Badge> : <Badge tone="yellow">off</Badge>],
                    ["Pseudonymised roles", d.privacy.pseudonymised_roles.length ? d.privacy.pseudonymised_roles.join(", ") : "none"],
                  ]}
                />
              </div>
            )}
          </QueryState>
        </Panel>
      </div>

      <div className="grid grid-cols-1 gap-2 lg:grid-cols-2">
        <Panel title="Worker heartbeats" subtitle="Stale after 35 s without a heartbeat" bodyClass="overflow-auto p-0">
          <QueryState q={status} isEmpty={(d) => d.workers.length === 0} empty="No worker has ever reported a heartbeat.">
            {(d) => (
              <table className="w-full text-[12px]">
                <thead>
                  <tr>
                    <th className="th">Worker</th>
                    <th className="th">State</th>
                    <th className="th">Last heartbeat</th>
                    <th className="th">Cameras (last report)</th>
                    <th className="th">Info</th>
                  </tr>
                </thead>
                <tbody>
                  {d.workers.map((w) => (
                    <tr key={w.worker_id}>
                      <td className="td mono">{w.worker_id}</td>
                      <td className="td">{w.alive ? <Badge tone="green">alive</Badge> : <Badge tone="red">stale</Badge>}</td>
                      <td className="td">
                        <div className="mono">{fmtTime(w.last_heartbeat)}</div>
                        <div className="text-[11px] text-slate-500">{fmtDuration(w.age_s)} ago</div>
                      </td>
                      <td className="td">
                        <div className="flex flex-wrap gap-0.5">
                          {Object.entries(w.info?.cameras ?? {}).map(([cid, st]) => (
                            <Badge key={cid} tone={statusTone(st)} title={st}>
                              {cid}
                            </Badge>
                          ))}
                        </div>
                      </td>
                      <td className="td">
                        <div className="text-[11px] text-slate-400">
                          {(["uptime_s", "spool_depth", "spooled", "replayed", "evidence_written", "evidence_skipped_disk_full", "bus"] as const)
                            .filter((k) => w.info && k in w.info)
                            .map((k) => (
                              <div key={k}>
                                {prettyKey(k)}: <span className="mono text-slate-300">{k === "uptime_s" ? fmtDuration(Number(w.info![k])) : scalar(w.info![k])}</span>
                              </div>
                            ))}
                        </div>
                        <JsonToggle value={w.info} label="raw" />
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </QueryState>
        </Panel>

        <Panel title="Models" subtitle="Detector / OCR / Re-ID model registry" bodyClass="overflow-auto p-0">
          <QueryState q={status}>
            {(d) => {
              const live = d.models ?? [];
              const reported = live.length ? null : d.workers.find((w) => w.info?.models?.length);
              const rows = live.length ? live : reported?.info?.models ?? [];
              if (!rows.length) return <Empty>No model registry in this process and no worker has reported its models.</Empty>;
              return (
                <>
                  {reported && (
                    <div className="border-b border-ink-700 px-2 py-1 text-[11px] text-amber-200">
                      No live registry here — showing the last report of {reported.worker_id} ({reported.alive ? "alive" : `stale, ${fmtDuration(reported.age_s)} old`}).
                    </div>
                  )}
                  <table className="w-full text-[12px]">
                    <thead>
                      <tr>
                        <th className="th">Role</th>
                        <th className="th">Configured</th>
                        <th className="th">Active</th>
                        <th className="th">State</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((m, i) => (
                        <tr key={`${m.role}-${i}`}>
                          <td className="td">{prettyKey(m.role)}</td>
                          <td className="td mono">{m.configured ?? "—"}</td>
                          <td className="td mono" title={m.path ?? undefined}>
                            {m.active ?? "—"}
                          </td>
                          <td className="td">
                            {m.error ? <Badge tone="red" title={m.error}>error</Badge> : m.fallback ? <Badge tone="yellow">fallback</Badge> : <Badge tone="green">ok</Badge>}
                            {m.error && <div className="text-[11px] text-rose-300">{m.error}</div>}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </>
              );
            }}
          </QueryState>
        </Panel>
      </div>

      {s && (s.ingestion || s.scheduler) && (
        <div className="grid grid-cols-1 gap-2 lg:grid-cols-2">
          {s.ingestion && (
            <Panel title="Ingestion service">
              <KV rows={Object.entries(s.ingestion).map(([k, v]) => [prettyKey(k), <span className="mono">{scalar(v)}</span>])} />
            </Panel>
          )}
          {s.scheduler && (
            <Panel title="Scheduler">
              <KV rows={Object.entries(s.scheduler).map(([k, v]) => [prettyKey(k), <span className="mono break-all">{scalar(v)}</span>])} />
            </Panel>
          )}
        </div>
      )}

      <EventsPanel />
      <div className="grid grid-cols-1 gap-2 lg:grid-cols-[minmax(0,2fr)_minmax(0,1fr)]">
        <DeadLettersPanel />
        <RetentionPanel />
      </div>
    </div>
  );
}

function EventsPanel() {
  const topo = useTopology();
  const [level, setLevel] = useState("");
  const [source, setSource] = useState("");
  const [camera, setCamera] = useState("");
  const [limit, setLimit] = useState(100);
  const q = useQuery({
    queryKey: ["system", "events", level, source, camera, limit],
    queryFn: () => api.get<{ results: SysEvent[] }>("/api/system/events", { level, source, camera_id: camera, limit }),
    refetchInterval: REFRESH_MS,
  });
  const [sources, setSources] = useState<string[]>([]);
  const seen = q.data?.results.map((e) => e.source) ?? [];
  if (seen.some((x) => !sources.includes(x))) setSources([...new Set([...sources, ...seen])].sort());

  return (
    <Panel
      title="System events"
      subtitle="Camera status changes, failures, restarts"
      bodyClass="max-h-[420px] overflow-auto p-0"
      actions={
        <div className="flex flex-wrap items-center gap-1">
          <select className="input !w-28 !py-0.5" value={level} onChange={(e) => setLevel(e.target.value)} aria-label="Level">
            <option value="">All levels</option>
            {LEVELS.map((l) => (
              <option key={l}>{l}</option>
            ))}
          </select>
          <select className="input !w-36 !py-0.5" value={source} onChange={(e) => setSource(e.target.value)} aria-label="Source">
            <option value="">All sources</option>
            {sources.map((x) => (
              <option key={x}>{x}</option>
            ))}
          </select>
          <div className="w-48">
            <CameraSelect cameras={topo.data?.cameras ?? []} value={camera} onChange={setCamera} placeholder="All cameras" />
          </div>
          <select className="input !w-20 !py-0.5" value={limit} onChange={(e) => setLimit(Number(e.target.value))} aria-label="Limit">
            {[50, 100, 250, 500, 1000].map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </div>
      }
    >
      <QueryState q={q} isEmpty={(d) => d.results.length === 0} empty="No system events match these filters.">
        {(d) => (
          <table className="w-full text-[12px]">
            <thead>
              <tr>
                <th className="th">Time</th>
                <th className="th">Level</th>
                <th className="th">Source</th>
                <th className="th">Camera</th>
                <th className="th">Event</th>
                <th className="th">Message</th>
                <th className="th">Details</th>
              </tr>
            </thead>
            <tbody>
              {d.results.map((e) => (
                <tr key={e.id} className="hover:bg-ink-800/60">
                  <td className="td mono whitespace-nowrap">{fmtTime(e.ts, true)}</td>
                  <td className="td">
                    <Badge tone={levelBadge(e.level)}>{e.level}</Badge>
                  </td>
                  <td className="td mono">{e.source}</td>
                  <td className="td mono">{e.camera_id ?? "—"}</td>
                  <td className="td mono">{e.event_type}</td>
                  <td className="td">{e.message}</td>
                  <td className="td">
                    <JsonToggle value={e.details} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </QueryState>
    </Panel>
  );
}

function DeadLettersPanel() {
  const { has } = useAuth();
  const canAct = has(P.SETTINGS_WRITE);
  const qc = useQueryClient();
  const [tab, setTab] = useState<"open" | "resolved">("open");
  const q = useQuery({
    queryKey: ["system", "dead-letters", tab],
    queryFn: () => api.get<{ results: DeadLetter[] }>("/api/system/dead-letters", { resolved: tab === "resolved", limit: 200 }),
    refetchInterval: REFRESH_MS,
  });
  const [notice, setNotice] = useState<string | null>(null);
  const refresh = () => qc.invalidateQueries({ queryKey: ["system"] });

  return (
    <Panel title="Dead-letter queue" subtitle="Events that failed ingestion after retries" bodyClass="p-0">
      <Tabs
        tabs={[
          { id: "open", label: "Open" },
          { id: "resolved", label: "Resolved" },
        ]}
        value={tab}
        onChange={setTab}
      />
      {notice && <div className="border-b border-ink-700 px-2 py-1 text-[12px] text-cyan-200">{notice}</div>}
      <div className="max-h-80 overflow-auto">
        <QueryState q={q} isEmpty={(d) => d.results.length === 0} empty={tab === "open" ? "No open dead letters — ingestion is keeping up." : "No resolved dead letters."}>
          {(d) => (
            <table className="w-full text-[12px]">
              <thead>
                <tr>
                  <th className="th">Time</th>
                  <th className="th">Source / event</th>
                  <th className="th">Camera</th>
                  <th className="th">Error</th>
                  <th className="th text-right">Attempts</th>
                  <th className="th">{tab === "open" ? "Payload" : "Resolution"}</th>
                  {tab === "open" && canAct && <th className="th" />}
                </tr>
              </thead>
              <tbody>
                {d.results.map((x) => (
                  <tr key={x.id}>
                    <td className="td mono whitespace-nowrap">{fmtTime(x.ts, true)}</td>
                    <td className="td mono">
                      {x.source}
                      <div className="text-slate-400">{x.event_type}</div>
                    </td>
                    <td className="td mono">{x.camera_id ?? "—"}</td>
                    <td className="td max-w-xs break-words text-rose-200">{x.error}</td>
                    <td className="td mono text-right">{x.attempts}</td>
                    <td className="td text-[11px] text-slate-400">
                      {tab === "open" ? (x.payload_keys?.length ? x.payload_keys.join(", ") : "—") : `${x.resolution ?? "—"} · ${fmtTime(x.resolved_at, true)}`}
                    </td>
                    {tab === "open" && canAct && (
                      <td className="td whitespace-nowrap">
                        <span className="inline-flex gap-1">
                          <ActionButton
                            className="btn !py-0"
                            onClick={async () => {
                              const r = await api.post<{ replayed: boolean; mode?: string }>(`/api/system/dead-letters/${x.id}/replay`);
                              const how =
                                r.mode === "requeued"
                                  ? "requeued to the ingest stream (a worker will re-process it; if it fails again it returns as a new dead letter)"
                                  : r.replayed
                                    ? "replayed in-process successfully"
                                    : "replayed in-process but failed again";
                              setNotice(`Dead letter #${x.id}: ${how}.`);
                              await refresh();
                            }}
                          >
                            Replay
                          </ActionButton>
                          <ActionButton
                            className="btn btn-danger !py-0"
                            confirm={`Discard dead letter #${x.id}? The event will not be ingested.`}
                            onClick={async () => {
                              await api.post(`/api/system/dead-letters/${x.id}/discard`);
                              setNotice(`Dead letter #${x.id} discarded.`);
                              await refresh();
                            }}
                          >
                            Discard
                          </ActionButton>
                        </span>
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </QueryState>
      </div>
    </Panel>
  );
}

function RetentionPanel() {
  const { has } = useAuth();
  const canRun = has(P.SETTINGS_WRITE);
  const qc = useQueryClient();
  const settings = useQuery({ queryKey: ["settings"], queryFn: () => api.get<{ sections: Record<string, Record<string, unknown>> }>("/api/settings") });
  const [result, setResult] = useState<{ deleted: Record<string, number>; policy: Record<string, unknown> } | null>(null);
  const policy = result?.policy ?? settings.data?.sections.retention;

  return (
    <Panel
      title="Data retention"
      subtitle="Edit the policy under Settings → retention"
      actions={
        canRun ? (
          <ActionButton
            className="btn btn-danger"
            confirm="Run retention now? Records older than the retention policy are permanently deleted."
            onClick={async () => {
              setResult(await api.post("/api/system/retention/run"));
              await qc.invalidateQueries({ queryKey: ["system"] });
            }}
          >
            Run retention now
          </ActionButton>
        ) : undefined
      }
    >
      {settings.isLoading ? (
        <Loading />
      ) : settings.error ? (
        <ErrorBox error={settings.error} onRetry={() => settings.refetch()} />
      ) : !policy ? (
        <Empty>No retention policy returned.</Empty>
      ) : (
        <KV rows={Object.entries(policy).map(([k, v]) => [prettyKey(k), <span className="mono">{scalar(v)}</span>])} />
      )}
      {result && (
        <div className="mt-2 rounded border border-ink-600 bg-ink-850 p-1.5" role="status">
          <div className="label">Deleted in this run</div>
          <KV rows={Object.entries(result.deleted).map(([k, v]) => [prettyKey(k), <span className="mono">{fmtNum(v)}</span>])} />
        </div>
      )}
    </Panel>
  );
}
