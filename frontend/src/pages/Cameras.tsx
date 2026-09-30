/**
 * Cameras — every registered camera with live status and processing metrics.
 * Data: GET /api/cameras (scope-aware), GET /api/cameras/runtime (live fps/queue),
 * snapshot thumbnails, camera_status_changed events; "Add camera" → POST /api/cameras.
 */
import { useMemo, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, mediaUrl } from "../lib/api";
import { P, useAuth } from "../lib/auth";
import { useScope } from "../lib/scope";
import { useLiveEvent } from "../lib/ws";
import { fmtAgo, fmtNum, statusTone } from "../lib/format";
import { Badge, Empty, ErrorBox, Loading, PageHeader, Panel, SyntheticBanner } from "../components/ui";
import { AddCameraModal } from "../components/cameras/AddCameraModal";
import {
  applyStatusToCache,
  distinct,
  filterCameras,
  type Camera,
  type CameraFilter,
  type CameraList,
  type CameraRuntime,
  type CameraStatusEvent,
  type RuntimeAll,
} from "../components/cameras/model";

export default function Cameras() {
  const { has } = useAuth();
  const { param } = useScope();
  const qc = useQueryClient();
  const nav = useNavigate();
  const [filter, setFilter] = useState<CameraFilter>({ text: "", status: "", zone: "", source: "" });
  const [thumbs, setThumbs] = useState(true);
  const [adding, setAdding] = useState(false);
  const [thumbTick, setThumbTick] = useState(() => Date.now());

  const q = useQuery({
    queryKey: ["cameras", "list", param],
    queryFn: () => api.get<CameraList>("/api/cameras", { scope: param }),
    refetchInterval: 30_000,
  });
  const rt = useQuery({
    queryKey: ["cameras", "runtime"],
    queryFn: () => api.get<RuntimeAll>("/api/cameras/runtime"),
    refetchInterval: 5_000,
  });
  useLiveEvent("camera_status_changed", (ev) => applyStatusToCache(qc, ev.data as unknown as CameraStatusEvent));

  const all = useMemo(() => q.data?.cameras ?? [], [q.data]);
  const rows = useMemo(() => filterCameras(all, filter), [all, filter]);
  const statuses = useMemo(() => distinct(all.map((c) => c.status)), [all]);
  const zones = useMemo(() => distinct(all.map((c) => c.zone)), [all]);
  const sources = useMemo(() => distinct(all.map((c) => c.source_type)), [all]);
  const runtimeOf = (c: Camera): CameraRuntime | null => rt.data?.cameras?.[c.id] ?? c.runtime;
  const counts = all.reduce<Record<string, number>>((a, c) => ((a[c.status] = (a[c.status] ?? 0) + 1), a), {});
  const running = rt.data ? Object.keys(rt.data.cameras).length : null;

  return (
    <div className="flex flex-col gap-2">
      <PageHeader
        title="Cameras"
        subtitle={
          <span>
            {all.length} camera{all.length === 1 ? "" : "s"} in scope
            {running != null && ` · ${running} reporting live runtime`}
            {rt.error ? " · runtime metrics unavailable" : ""}
          </span>
        }
        actions={
          <>
            {has(P.STREAM_VIEW) && (
              <>
                <label className="flex items-center gap-1.5 text-[12px] text-slate-400">
                  <input type="checkbox" checked={thumbs} onChange={(e) => setThumbs(e.target.checked)} /> Thumbnails
                </label>
                {thumbs && (
                  <button className="btn" onClick={() => setThumbTick(Date.now())} title="Reload snapshot thumbnails">
                    ↻ Snapshots
                  </button>
                )}
              </>
            )}
            {has(P.CAMERAS_WRITE) && (
              <button className="btn btn-primary" onClick={() => setAdding(true)}>
                + Add camera
              </button>
            )}
          </>
        }
      />
      <SyntheticBanner info={q.data} />

      <div className="flex flex-wrap items-end gap-2 rounded-md border border-ink-700 bg-ink-900/80 p-2">
        <label className="w-64">
          <span className="label">Search</span>
          <input className="input" placeholder="ID, name, road, zone…" value={filter.text} onChange={(e) => setFilter({ ...filter, text: e.target.value })} />
        </label>
        <label>
          <span className="label">Status</span>
          <select className="input" value={filter.status} onChange={(e) => setFilter({ ...filter, status: e.target.value })} aria-label="Status filter">
            <option value="">All</option>
            {statuses.map((s) => (
              <option key={s} value={s}>
                {s} ({counts[s]})
              </option>
            ))}
          </select>
        </label>
        <label>
          <span className="label">Zone</span>
          <select className="input" value={filter.zone} onChange={(e) => setFilter({ ...filter, zone: e.target.value })} aria-label="Zone filter">
            <option value="">All</option>
            {zones.map((z) => (
              <option key={z}>{z}</option>
            ))}
          </select>
        </label>
        <label>
          <span className="label">Source</span>
          <select className="input" value={filter.source} onChange={(e) => setFilter({ ...filter, source: e.target.value })} aria-label="Source filter">
            <option value="">All</option>
            {sources.map((z) => (
              <option key={z}>{z}</option>
            ))}
          </select>
        </label>
        {(filter.text || filter.status || filter.zone || filter.source) && (
          <button className="btn btn-ghost" onClick={() => setFilter({ text: "", status: "", zone: "", source: "" })}>
            Clear filters
          </button>
        )}
        <div className="flex-1" />
        <div className="flex flex-wrap gap-1">
          {Object.entries(counts).map(([s, n]) => (
            <Badge key={s} tone={statusTone(s)}>
              {s} {n}
            </Badge>
          ))}
        </div>
      </div>

      <Panel bodyClass="overflow-auto">
        {q.isLoading ? (
          <Loading />
        ) : q.error ? (
          <ErrorBox error={q.error} onRetry={() => q.refetch()} />
        ) : all.length === 0 ? (
          <Empty>No cameras registered in this data scope.{has(P.CAMERAS_WRITE) ? " Use “Add camera” to register one." : ""}</Empty>
        ) : rows.length === 0 ? (
          <Empty>No cameras match the filters.</Empty>
        ) : (
          <table className="w-full text-[12px]">
            <thead>
              <tr>
                {thumbs && has(P.STREAM_VIEW) && <th className="th w-[120px]">Snapshot</th>}
                <th className="th">Camera</th>
                <th className="th">Status</th>
                <th className="th">Source</th>
                <th className="th">Location</th>
                <th className="th text-right">Input fps</th>
                <th className="th text-right">Proc fps</th>
                <th className="th text-right">Queue</th>
                <th className="th text-right">Latency</th>
                <th className="th text-right">Dropped</th>
                <th className="th">Last seen</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((c) => {
                const r = runtimeOf(c);
                const target = r?.target_processing_fps ?? c.processing?.processing_fps;
                const slow = r?.processing_fps != null && target != null && r.processing_fps < target * 0.8;
                return (
                  <tr key={c.id} className="hover:bg-ink-850">
                    {thumbs && has(P.STREAM_VIEW) && (
                      <td className="td">
                        <Thumb id={c.id} tick={thumbTick} />
                      </td>
                    )}
                    <td className="td">
                      <Link to={`/cameras/${encodeURIComponent(c.id)}`} className="mono font-semibold text-cyan-300 hover:underline">
                        {c.id}
                      </Link>
                      <div className="text-slate-200">{c.name}</div>
                      <div className="flex gap-1 pt-0.5">
                        {c.camera_type && <Badge tone="slate">{c.camera_type}</Badge>}
                        {c.is_demo && <Badge tone="violet">Synthetic</Badge>}
                        {!c.enabled && <Badge tone="slate">Disabled</Badge>}
                      </div>
                    </td>
                    <td className="td">
                      <Badge tone={statusTone(c.status)}>{c.status}</Badge>
                      {c.status_message && <div className="max-w-[220px] text-[11px] text-slate-500">{c.status_message}</div>}
                    </td>
                    <td className="td">
                      <div className="mono text-slate-300">{c.source_type}</div>
                      <div className="mono max-w-[220px] truncate text-[11px] text-slate-500" title={c.source_uri}>
                        {c.source_uri}
                      </div>
                      {c.has_credentials && <div className="text-[11px] text-slate-500">credentials set{c.username_hint ? ` (${c.username_hint})` : ""}</div>}
                    </td>
                    <td className="td">
                      <div className="text-slate-300">{c.road_name || c.location || "—"}</div>
                      <div className="text-[11px] text-slate-500">
                        {[c.zone, c.direction && `dir ${c.direction}`, c.lane_count != null && `${c.lane_count} lanes`].filter(Boolean).join(" · ")}
                      </div>
                    </td>
                    <td className="td mono text-right">{fmtNum(r?.input_fps, 1)}</td>
                    <td className={`td mono text-right ${slow ? "text-amber-300" : ""}`} title={target != null ? `target ${target} fps` : undefined}>
                      {fmtNum(r?.processing_fps, 1)}
                      {target != null && <span className="text-slate-500"> / {fmtNum(target, 1)}</span>}
                    </td>
                    <td className="td mono text-right">
                      {r?.queue_depth != null ? `${r.queue_depth}/${r.max_queue_size ?? c.processing?.max_queue_size ?? "—"}` : "—"}
                    </td>
                    <td className="td mono text-right">{r?.latency_ms != null ? `${fmtNum(r.latency_ms, 0)} ms` : "—"}</td>
                    <td className="td mono text-right">{fmtNum(r?.frames_dropped)}</td>
                    <td className="td whitespace-nowrap text-slate-400" title={c.last_seen_at ?? undefined}>
                      {fmtAgo(c.last_seen_at)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Panel>

      {adding && (
        <AddCameraModal
          onClose={() => setAdding(false)}
          onCreated={async (c) => {
            setAdding(false);
            await qc.invalidateQueries({ queryKey: ["cameras"] });
            await qc.invalidateQueries({ queryKey: ["topology"] });
            nav(`/cameras/${encodeURIComponent(c.id)}`);
          }}
        />
      )}
    </div>
  );
}

/** Lazy-loaded latest-frame thumbnail; the endpoint answers 503 when no recent frame exists. */
function Thumb({ id, tick }: { id: string; tick: number }) {
  const [failed, setFailed] = useState<number | null>(null);
  if (failed === tick)
    return <div className="flex h-[60px] w-[106px] items-center justify-center rounded border border-ink-700 bg-ink-950 text-[10px] text-slate-600">no recent frame</div>;
  return (
    <img
      src={mediaUrl(`/api/cameras/${encodeURIComponent(id)}/snapshot.jpg`, { overlay: 0, t: tick })}
      loading="lazy"
      alt={`${id} snapshot`}
      className="h-[60px] w-[106px] rounded border border-ink-700 bg-ink-950 object-cover"
      onError={() => setFailed(tick)}
    />
  );
}
