/**
 * Global vehicle detail: identity summary, confidence-annotated trajectory on the offline road
 * map with time-proportional journey replay, per-stop explanation timeline and the next-camera
 * prediction. Refreshes live on trajectory_updated events for this vehicle.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { P, useAuth } from "../lib/auth";
import { useLiveEvent } from "../lib/ws";
import {
  confidenceTone,
  fmtAgo,
  fmtDuration,
  fmtKm,
  fmtNum,
  fmtPct,
  fmtTime,
  predictionTone,
  TONE_HEX,
} from "../lib/format";
import { buildTimeline, stopTime, type LatLon, type ReplayTimeline } from "../lib/replay";
import { Badge, Empty, ErrorBox, KV, Loading, PageHeader, Panel, SyntheticBanner } from "../components/ui";
import { edgePath, NetworkMap, useTopology, type MapCamera, type MapPath, type MapPoint } from "../components/NetworkMap";
import type { PredictionResponse, TrajectoryPoint, TrajectoryResponse, TrajectorySegment, VehicleDetailResponse } from "../components/vehicles/types";
import { EvidenceImg, ObsLink, PlateText, Reasons, SyntheticBadge } from "../components/vehicles/common";
import { useReplay } from "../components/vehicles/useReplay";

const SPEEDS = [1, 5, 20, 60] as const;
/** With "compress gaps", any interval between two sightings longer than this plays as this long. */
const GAP_CAP_S = 60;

function levelHex(level?: string | null): string {
  return TONE_HEX[confidenceTone(level)];
}

export default function VehicleDetail() {
  const { ref = "" } = useParams();
  const qc = useQueryClient();
  const detail = useQuery({ queryKey: ["vehicle", ref], queryFn: ({ signal }) => api.get<VehicleDetailResponse>(`/api/vehicles/${encodeURIComponent(ref)}`, undefined, signal) });
  const traj = useQuery({
    queryKey: ["vehicle-trajectory", ref],
    queryFn: ({ signal }) => api.get<TrajectoryResponse>(`/api/vehicles/${encodeURIComponent(ref)}/trajectory`, undefined, signal),
  });
  const pred = useQuery({
    queryKey: ["vehicle-prediction", ref],
    queryFn: ({ signal }) => api.get<PredictionResponse>(`/api/vehicles/${encodeURIComponent(ref)}/prediction`, undefined, signal),
  });
  const [liveAt, setLiveAt] = useState<number | null>(null);

  const v = detail.data;
  useLiveEvent("trajectory_updated", (ev) => {
    const d = ev.data as { global_vehicle_id?: number; vehicle_code?: string };
    const mine = (v && (d.global_vehicle_id === v.id || d.vehicle_code === v.code)) || (d.vehicle_code && d.vehicle_code === ref.toUpperCase());
    if (!mine) return;
    setLiveAt(Date.now());
    qc.invalidateQueries({ queryKey: ["vehicle", ref] });
    qc.invalidateQueries({ queryKey: ["vehicle-trajectory", ref] });
    qc.invalidateQueries({ queryKey: ["vehicle-prediction", ref] });
  });

  if (detail.isLoading) return <Loading label="Loading vehicle…" />;
  if (detail.error) return <ErrorBox error={detail.error} onRetry={() => detail.refetch()} />;
  if (!v) return <Empty>Vehicle not found.</Empty>;

  return (
    <div className="flex flex-col gap-2">
      <PageHeader
        title={`Vehicle ${v.code}`}
        subtitle={
          <span className="flex flex-wrap items-center gap-2">
            <PlateText display={v.plate_display} text={v.plate_text} />
            <span>
              {v.vehicle_class ?? "—"} · {v.vehicle_color ?? "—"}
            </span>
            <span>last seen {fmtAgo(v.last_seen_at)}</span>
            <SyntheticBadge show={v.is_demo} />
            {liveAt && <span className="text-emerald-300">live update {fmtTime(liveAt)}</span>}
          </span>
        }
        actions={
          <Link to="/vehicles/search" className="btn">
            ‹ Search
          </Link>
        }
      />
      {v.is_demo && <SyntheticBanner info={{ synthetic: true, notice: "This vehicle identity was built from synthetic demo sightings." }} />}

      <div className="grid gap-2 xl:grid-cols-[minmax(0,1fr)_360px]">
        {traj.isLoading ? (
          <Panel title="Trajectory">
            <Loading label="Loading trajectory…" />
          </Panel>
        ) : traj.error ? (
          <Panel title="Trajectory">
            <ErrorBox error={traj.error} onRetry={() => traj.refetch()} />
          </Panel>
        ) : traj.data ? (
          <TrajectoryView traj={traj.data} pred={pred.data} code={v.code} />
        ) : null}

        <div className="flex flex-col gap-2">
          <SummaryPanel v={v} journeys={traj.data?.journeys.length} />
          <PredictionPanel q={pred} />
        </div>
      </div>
    </div>
  );
}

function SummaryPanel({ v, journeys }: { v: VehicleDetailResponse; journeys?: number }) {
  return (
    <Panel title="Identity">
      <KV
        rows={[
          ["Code", <span className="mono">{v.code}</span>],
          ["Plate", <PlateText display={v.plate_display} text={v.plate_text} />],
          ["Plate confidence", <span className="mono">{fmtPct(v.plate_confidence, 1)}</span>],
          ["Class / colour", `${v.vehicle_class ?? "—"} / ${v.vehicle_color ?? "—"}`],
          ["First seen", <span className="mono">{`${fmtTime(v.first_seen_at, true)} · ${v.first_camera_id ?? "—"}`}</span>],
          ["Last seen", <span className="mono">{`${fmtTime(v.last_seen_at, true)} · ${v.last_camera_id ?? "—"}`}</span>],
          ["Sightings", <span className="mono">{v.observation_count}</span>],
          [
            "Cameras",
            <span className="flex flex-wrap gap-1">
              <span className="mono">{v.camera_count}</span>
              {v.cameras.map((c) => (
                <Link key={c} to={`/cameras/${c}`} className="mono rounded border border-ink-600 px-1 text-[11px] text-slate-300 hover:text-cyan-300">
                  {c}
                </Link>
              ))}
            </span>,
          ],
          ["Journeys", <span className="mono">{journeys ?? "—"}</span>],
          ["Distance (road graph)", <span className="mono">{fmtKm(v.total_distance_m)}</span>],
          ["Data", v.is_demo ? <Badge tone="violet">Synthetic</Badge> : <Badge tone="green">Live</Badge>],
        ]}
      />
    </Panel>
  );
}

function PredictionPanel({ q }: { q: { isLoading: boolean; error: unknown; data?: PredictionResponse; refetch: () => unknown } }) {
  return (
    <Panel title="Next-camera prediction">
      {q.isLoading ? (
        <Loading />
      ) : q.error ? (
        <ErrorBox error={q.error} onRetry={() => q.refetch()} />
      ) : q.data ? (
        <div className="flex flex-col gap-2">
          {q.data.prediction ? (
            <KV
              rows={[
                [
                  "Predicted camera",
                  <Link to={`/cameras/${q.data.prediction.camera_id}`} className="mono text-cyan-300 hover:underline">
                    {q.data.prediction.camera_id}
                  </Link>,
                ],
                ["Probability", <span className="mono">{fmtPct(q.data.prediction.probability)}</span>],
                ["Status", <Badge tone={predictionTone(q.data.prediction.status)}>{q.data.prediction.status}</Badge>],
                [
                  "Expected window",
                  <span className="mono">
                    {fmtTime(q.data.prediction.window_start)} – {fmtTime(q.data.prediction.window_end)}
                  </span>,
                ],
                ["Based on sighting", <ObsLink id={q.data.prediction.from_observation_id} />],
              ]}
            />
          ) : (
            <div className="text-[12px] text-slate-500">No active prediction for this vehicle.</div>
          )}
          <div className="border-t border-ink-700 pt-1.5">
            <div className="label">This vehicle's prediction outcomes</div>
            {Object.keys(q.data.vehicle_history).length ? (
              <div className="flex flex-wrap gap-1.5">
                {Object.entries(q.data.vehicle_history).map(([k, n]) => (
                  <Badge key={k} tone={predictionTone(k)}>
                    {k} {n}
                  </Badge>
                ))}
              </div>
            ) : (
              <div className="text-[12px] text-slate-500">No evaluated predictions yet.</div>
            )}
          </div>
          <div className="border-t border-ink-700 pt-1.5">
            <div className="label">Network accuracy (24 h)</div>
            <div className="flex items-baseline gap-3 text-[12px]">
              <span className="mono text-lg text-slate-100">{fmtPct(q.data.network_accuracy_24h.accuracy, 1)}</span>
              <span className="text-emerald-300">{q.data.network_accuracy_24h.confirmed} confirmed</span>
              <span className="text-orange-300">{q.data.network_accuracy_24h.deviated} deviated</span>
            </div>
          </div>
        </div>
      ) : null}
    </Panel>
  );
}

// ---------------------------------------------------------------------------------------------
// Trajectory: map + replay + timeline
// ---------------------------------------------------------------------------------------------

function TrajectoryView({ traj, pred, code }: { traj: TrajectoryResponse; pred?: PredictionResponse; code: string }) {
  const topo = useTopology();
  const { has } = useAuth();
  const [journey, setJourney] = useState<"all" | number>("all");
  const [speed, setSpeed] = useState<number>(20);
  const [compress, setCompress] = useState(false);

  useEffect(() => {
    if (journey !== "all" && !traj.journeys.includes(journey)) setJourney("all");
  }, [traj.journeys, journey]);

  const points = useMemo(() => traj.points.filter((p) => journey === "all" || p.journey_index === journey), [traj.points, journey]);
  const segments = useMemo(() => {
    const seqs = new Set(points.map((p) => p.seq));
    return traj.segments.filter((s) => seqs.has(s.to_seq));
  }, [traj.segments, points]);
  const segByTo = useMemo(() => new Map(segments.map((s) => [s.to_seq, s])), [segments]);

  const tl: ReplayTimeline = useMemo(
    () =>
      buildTimeline(points, segments, {
        compressGapsS: compress ? GAP_CAP_S : null,
        resolvePath: (a, b) => {
          const path = edgePath(topo.data, a, b, []);
          return path.length >= 2 ? path : null;
        },
      }),
    [points, segments, compress, topo.data],
  );
  const replay = useReplay(tl, speed);
  const st = replay.state;
  const pointBySeq = useMemo(() => new Map(points.map((p) => [p.seq, p])), [points]);

  // Map layers -------------------------------------------------------------------------------
  const prediction = pred?.prediction ?? traj.vehicle.prediction;
  const stopNums = useMemo(() => {
    const m = new Map<string, number[]>();
    tl.stops.forEach((s, i) => m.set(s.cameraId, [...(m.get(s.cameraId) ?? []), i + 1]));
    return m;
  }, [tl]);
  const currentCam = st && st.stopIndex >= 0 ? tl.stops[st.stopIndex]?.cameraId : null;

  const cameras: MapCamera[] = useMemo(() => {
    const cams = topo.data?.cameras ?? [];
    return cams.map((c) => {
      const nums = stopNums.get(c.id);
      const isPred = prediction?.camera_id === c.id;
      return {
        id: c.id,
        lat: c.lat,
        lon: c.lon,
        color: nums ? TONE_HEX.cyan : "#475569",
        label: nums ? `${nums.join(",")} · ${c.id}` : c.id,
        tooltip: `${c.id} · ${c.name}${nums ? ` · stop ${nums.join(", ")}` : ""}${isPred ? ` · predicted next (${fmtPct(prediction?.probability)} · ${prediction?.status})` : ""}`,
        ring: isPred ? TONE_HEX[predictionTone(prediction?.status)] : undefined,
        pulse: isPred && prediction?.status === "PENDING",
      };
    });
  }, [topo.data, stopNums, prediction]);

  const legIdx = st?.legIndex ?? null;
  const paths: MapPath[] = useMemo(
    () =>
      tl.legs
        .filter((l) => l.kind === "road")
        .map((l) => {
          const to = pointBySeq.get(tl.stops[l.to].seq);
          const seg = to ? segByTo.get(to.seq) : undefined;
          const unobs = seg?.unobserved_cameras ?? [];
          return {
            id: `leg-${l.index}`,
            coords: l.coords,
            color: levelHex(to?.confidence_level),
            weight: l.index === legIdx ? 7 : 4,
            dash: unobs.length ? "8 6" : undefined,
            tooltip: `${tl.stops[l.from].cameraId} → ${tl.stops[l.to].cameraId} · ${to?.confidence_level ?? "—"} · link ${to?.link_score?.toFixed(3) ?? "—"} · ${fmtDuration(to?.segment_travel_s)} · ${fmtNum(to?.segment_speed_kmh, 1, "km/h")}${unobs.length ? ` · via ${unobs.join(", ")} (not observed)` : ""}`,
          };
        }),
    [tl, legIdx, pointBySeq, segByTo],
  );

  const mapPoints: MapPoint[] = useMemo(() => {
    if (!st?.coord) return [];
    return [{ id: "replay", lat: st.coord[0], lon: st.coord[1], color: "#f472b6", radius: 7, label: `${code} ${fmtTime(st.realMs)}` }];
  }, [st?.coord, st?.realMs, code]);

  const fitTo = useMemo<LatLon[] | null>(() => {
    const all: LatLon[] = [];
    for (const l of tl.legs) all.push(...l.coords);
    for (const s of tl.stops) all.push(s.coord);
    return all.length ? all : null;
    // Refit only when the journey selection or number of stops changes (not on every live refetch).
  }, [journey, tl.stops.length, code]);

  // Timeline auto-scroll to the current stop while playing.
  const rowRefs = useRef<(HTMLTableRowElement | null)[]>([]);
  const curIdx = st?.stopIndex ?? -1;
  useEffect(() => {
    if (!replay.playing || curIdx < 0) return;
    rowRefs.current[curIdx]?.scrollIntoView?.({ block: "nearest" });
  }, [curIdx, replay.playing]);

  const legendKeys = Object.keys(traj.legend ?? {});

  return (
    <div className="flex min-w-0 flex-col gap-2">
      <Panel
        title="Trajectory"
        subtitle={`${tl.stops.length} sighting(s) · ${traj.geometry_source}`}
        actions={
          traj.journeys.length > 1 ? (
            <select
              className="input !w-auto !py-0.5 text-[12px]"
              value={String(journey)}
              onChange={(e) => {
                replay.reset();
                setJourney(e.target.value === "all" ? "all" : Number(e.target.value));
              }}
              aria-label="Journey"
            >
              <option value="all">All journeys ({traj.journeys.length})</option>
              {traj.journeys.map((j) => {
                const js = traj.points.filter((p) => p.journey_index === j);
                return (
                  <option key={j} value={j}>
                    Journey {j + 1} · {js.length} stop(s) · {fmtTime(js[0]?.ts)}
                  </option>
                );
              })}
            </select>
          ) : (
            <span className="text-[11px] text-slate-500">{traj.journeys.length === 1 ? "1 journey" : ""}</span>
          )
        }
        bodyClass="p-0"
      >
        {tl.stops.length === 0 ? (
          <Empty>No trajectory points recorded for this vehicle.</Empty>
        ) : (
          <>
            <NetworkMap cameras={cameras} paths={paths} points={mapPoints} selectedId={currentCam} fitTo={fitTo} className="h-[440px]" />
            <ReplayBar tl={tl} replay={replay} speed={speed} setSpeed={setSpeed} compress={compress} setCompress={setCompress} />
            <div className="flex flex-wrap items-center gap-3 border-t border-ink-700 px-2.5 py-1 text-[11px] text-slate-400">
              {legendKeys.map((k) => (
                <span key={k} className="flex items-center gap-1" title={traj.legend[k]}>
                  <span className="inline-block h-1 w-4 rounded" style={{ background: levelHex(k) }} />
                  <span className="text-slate-300">{k}</span>
                  <span>{traj.legend[k]}</span>
                </span>
              ))}
              <span className="flex items-center gap-1">
                <span className="inline-block w-4 border-t-2 border-dashed border-slate-400" />
                route skips a camera (not observed there)
              </span>
              {prediction && (
                <span className="flex items-center gap-1">
                  <span className="inline-block h-2.5 w-2.5 rounded-full border-2 border-dashed" style={{ borderColor: TONE_HEX[predictionTone(prediction.status)] }} />
                  predicted next camera
                </span>
              )}
            </div>
          </>
        )}
      </Panel>

      {tl.stops.length > 0 && (
        <Panel title="Sighting timeline" subtitle="Click a row to jump the replay to that sighting" bodyClass="p-0">
          <div className="max-h-[520px] overflow-auto">
            <table className="w-full text-[12px]">
              <thead>
                <tr>
                  <th className="th">#</th>
                  <th className="th">Camera</th>
                  <th className="th">Time</th>
                  <th className="th">Segment</th>
                  <th className="th">Link</th>
                  <th className="th">Prediction</th>
                  <th className="th">Read here</th>
                  <th className="th w-[30%]">Explanation</th>
                  <th className="th">Evidence</th>
                </tr>
              </thead>
              <tbody>
                {tl.stops.map((s, i) => {
                  const p = pointBySeq.get(s.seq);
                  if (!p) return null;
                  const seg = segByTo.get(p.seq);
                  const isAt = st?.atStop === i;
                  const isCur = curIdx === i;
                  const newJourney = i > 0 && tl.stops[i - 1].journey !== s.journey;
                  return (
                    <TimelineRow
                      key={p.id}
                      rowRef={(el) => {
                        rowRefs.current[i] = el;
                      }}
                      n={i + 1}
                      p={p}
                      seg={seg}
                      active={isAt}
                      current={isCur && !isAt}
                      newJourney={newJourney}
                      showEvidence={has(P.EVIDENCE_READ)}
                      onClick={() => replay.seek(stopTime(tl, i))}
                    />
                  );
                })}
              </tbody>
            </table>
          </div>
        </Panel>
      )}
    </div>
  );
}

function ReplayBar({
  tl,
  replay,
  speed,
  setSpeed,
  compress,
  setCompress,
}: {
  tl: ReplayTimeline;
  replay: ReturnType<typeof useReplay>;
  speed: number;
  setSpeed: (n: number) => void;
  compress: boolean;
  setCompress: (b: boolean) => void;
}) {
  const st = replay.state;
  const canPlay = tl.duration > 0;
  let where = "—";
  if (st) {
    if (st.kind === "stop" && st.atStop != null) where = `At ${tl.stops[st.atStop].cameraId} (stop ${st.atStop + 1}/${tl.stops.length})`;
    else if (st.legIndex != null) {
      const leg = tl.legs[st.legIndex];
      const a = tl.stops[leg.from].cameraId;
      const b = tl.stops[leg.to].cameraId;
      where =
        leg.kind === "road"
          ? `${a} → ${b} · ${fmtPct(st.frac)} of segment (interpolated)`
          : tl.stops[leg.from].journey !== tl.stops[leg.to].journey
            ? `Between journeys: not observed after ${a}`
            : `No route known ${a} → ${b}: holding at last sighting`;
    }
  }
  const elapsedReal = st?.realMs != null && tl.startMs != null ? (st.realMs - tl.startMs) / 1000 : null;
  return (
    <div className="flex flex-wrap items-center gap-2 border-t border-ink-700 px-2.5 py-1.5">
      <button className="btn btn-primary w-24 justify-center" onClick={replay.toggle} disabled={!canPlay} title={canPlay ? undefined : "Only one sighting — nothing to replay"}>
        {replay.playing ? "❚❚ Pause" : "▶ Play Journey"}
      </button>
      <div className="flex items-center gap-0.5" role="group" aria-label="Replay speed">
        {SPEEDS.map((s) => (
          <button key={s} className={`btn !px-1.5 ${speed === s ? "!border-cyan-500 !text-cyan-200" : ""}`} onClick={() => setSpeed(s)} aria-pressed={speed === s}>
            {s}×
          </button>
        ))}
      </div>
      <label className="flex items-center gap-1 text-[11px] text-slate-400" title={`Intervals longer than ${GAP_CAP_S}s between sightings play as ${GAP_CAP_S}s; the time readout stays real.`}>
        <input type="checkbox" checked={compress} onChange={(e) => setCompress(e.target.checked)} />
        compress gaps &gt; {GAP_CAP_S}s
      </label>
      <input
        type="range"
        className="min-w-[160px] flex-1 accent-cyan-400"
        min={0}
        max={tl.duration || 0}
        step={Math.max(0.1, tl.duration / 1000)}
        value={replay.v}
        onChange={(e) => replay.seek(Number(e.target.value))}
        disabled={!canPlay}
        aria-label="Replay position"
      />
      <div className="min-w-[220px] text-right leading-tight">
        <div className="mono text-slate-100">{st?.realMs != null ? fmtTime(st.realMs, true) : "—"}</div>
        <div className="text-[11px] text-slate-400">
          +{fmtDuration(elapsedReal)} of {fmtDuration(tl.realDuration)} · {where}
        </div>
      </div>
    </div>
  );
}

function TimelineRow({
  rowRef,
  n,
  p,
  seg,
  active,
  current,
  newJourney,
  showEvidence,
  onClick,
}: {
  rowRef: (el: HTMLTableRowElement | null) => void;
  n: number;
  p: TrajectoryPoint;
  seg?: TrajectorySegment;
  active: boolean;
  current: boolean;
  newJourney: boolean;
  showEvidence: boolean;
  onClick: () => void;
}) {
  const cls = active ? "bg-cyan-900/40 outline outline-1 -outline-offset-1 outline-cyan-500" : current ? "bg-ink-800/80" : "hover:bg-ink-800/50";
  return (
    <>
      {newJourney && (
        <tr>
          <td colSpan={9} className="td bg-ink-850 text-[11px] font-semibold uppercase tracking-wide text-slate-400">
            Journey {p.journey_index + 1}
          </td>
        </tr>
      )}
      <tr ref={rowRef} className={`cursor-pointer ${cls}`} onClick={onClick}>
        <td className="td mono">
          <span className={`inline-flex h-5 w-5 items-center justify-center rounded-full text-[11px] ${active ? "bg-cyan-400 text-ink-950" : "bg-ink-700 text-slate-200"}`}>{n}</span>
        </td>
        <td className="td whitespace-nowrap">
          <Link to={`/cameras/${p.camera_id}`} className="mono text-slate-100 hover:text-cyan-300" onClick={(e) => e.stopPropagation()}>
            {p.camera_id}
          </Link>
          {p.camera_name && <div className="text-[11px] text-slate-500">{p.camera_name}</div>}
        </td>
        <td className="td mono whitespace-nowrap">
          {fmtTime(p.ts, true)}
          <div>
            <ObsLink id={p.observation_id}>obs #{p.observation_id}</ObsLink>
          </div>
        </td>
        <td className="td mono whitespace-nowrap text-[11px]">
          {p.from_camera_id ? (
            <>
              <div className="text-slate-300">from {p.from_camera_id}</div>
              <div>
                {fmtDuration(p.segment_travel_s)} · {fmtKm(p.segment_distance_m)}
              </div>
              <div>{fmtNum(p.segment_speed_kmh, 1, "km/h")} avg</div>
              {seg && seg.unobserved_cameras.length > 0 && <div className="text-amber-300">via {seg.unobserved_cameras.join(", ")} (not observed)</div>}
            </>
          ) : (
            <span className="text-slate-500">journey start</span>
          )}
        </td>
        <td className="td whitespace-nowrap">
          <Badge tone={confidenceTone(p.confidence_level)}>{p.confidence_level ?? "—"}</Badge>
          {p.link_score != null && <div className="mono mt-0.5 text-slate-300">{p.link_score.toFixed(3)}</div>}
        </td>
        <td className="td">
          {p.prediction_status ? (
            <Badge tone={predictionTone(p.prediction_status)} title="Outcome of the prediction made at the previous sighting">
              {p.prediction_status}
            </Badge>
          ) : (
            <span className="text-slate-500">—</span>
          )}
        </td>
        <td className="td text-[11px]">
          <PlateText text={p.plate_text} />
          <div className="text-slate-400">
            {p.vehicle_class ?? "—"} / {p.vehicle_color ?? "—"}
          </div>
          <div className="mono text-slate-500">{fmtNum(p.speed_kmh, 1, "km/h")} spot</div>
        </td>
        <td className="td" onClick={(e) => e.stopPropagation()}>
          <Reasons reasons={p.match_reasons} fallback={p.explanation} />
        </td>
        <td className="td" onClick={(e) => e.stopPropagation()}>
          {showEvidence && p.evidence_id ? (
            <Link to={`/observations/${p.observation_id}`} title="Open sighting with full evidence package">
              <EvidenceImg evidenceId={p.evidence_id} role="vehicle" className="h-14 w-20" alt={`Vehicle at ${p.camera_id}`} />
            </Link>
          ) : (
            <span className="text-[11px] text-slate-500">{showEvidence ? "none" : "no access"}</span>
          )}
        </td>
      </tr>
    </>
  );
}
