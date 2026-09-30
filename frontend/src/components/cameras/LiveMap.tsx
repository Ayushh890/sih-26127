/**
 * Command Center map: cameras coloured by live congestion score, trajectory hops animated
 * along the road network as they arrive over the WebSocket, and a ring on each camera
 * that is the predicted next camera of at least one vehicle.
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { NetworkMap, along, edgePath, useTopology, type MapCamera, type MapPath, type MapPoint } from "../NetworkMap";
import { useLiveEvent } from "../../lib/ws";
import { TONE_HEX, confidenceTone, scoreHex, statusTone } from "../../lib/format";
import { inScope, partialPath, progress, type Camera, type OverviewCamera, type TrajectoryEvent } from "./model";

export const TRAVEL_MS = 3000;
export const FADE_MS = 3000;
export const MAX_ANIMS = 20;
const MAX_PREDICTIONS = 300;
const PRED_FALLBACK_MS = 10 * 60_000;

interface Anim {
  key: string;
  coords: [number, number][];
  start: number;
  color: string;
  label: string;
  hasPath: boolean;
}

interface Pred {
  camera_id: string;
  until: number;
}

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}

function alphaHex(hex: string, a: number): string {
  const v = Math.round(Math.min(1, Math.max(0, a)) * 255)
    .toString(16)
    .padStart(2, "0");
  return `${hex}${v}`;
}

export function LiveMap({
  cameras,
  overview,
  selectedId,
  onSelect,
  scope,
  className = "",
}: {
  cameras: Camera[];
  overview: Map<string, OverviewCamera>;
  selectedId: string | null;
  onSelect: (id: string) => void;
  scope: string | undefined;
  className?: string;
}) {
  const topo = useTopology();
  const anims = useRef<Anim[]>([]);
  const preds = useRef(new Map<number, Pred>());
  const [now, setNow] = useState(() => Date.now());
  const [ringKey, setRingKey] = useState("");
  const [hops, setHops] = useState(0);

  const camPos = useMemo(() => new Map(cameras.map((c) => [c.id, [c.latitude, c.longitude] as [number, number]])), [cameras]);

  const recomputeRings = () => {
    const t = Date.now();
    for (const [k, p] of preds.current) if (p.until < t) preds.current.delete(k);
    const counts = new Map<string, number>();
    for (const p of preds.current.values()) counts.set(p.camera_id, (counts.get(p.camera_id) ?? 0) + 1);
    setRingKey(
      [...counts.entries()]
        .sort()
        .map(([k, n]) => `${k}:${n}`)
        .join(","),
    );
  };

  useLiveEvent("trajectory_updated", (ev) => {
    const d = ev.data as unknown as TrajectoryEvent;
    if (!d || !d.camera_id || !inScope(scope, d.is_demo)) return;
    const to = camPos.get(d.camera_id) ?? (Number.isFinite(d.latitude) ? ([d.latitude, d.longitude] as [number, number]) : null);
    if (to) {
      const from = d.from_camera_id ? camPos.get(d.from_camera_id) : undefined;
      const coords = d.from_camera_id && from ? edgePath(topo.data, d.from_camera_id, d.camera_id, [from, to]) : [to];
      const color = TONE_HEX[confidenceTone(d.confidence_level)];
      const label = d.plate_text ? `${d.vehicle_code} · ${d.plate_text}` : d.vehicle_code;
      const a: Anim = { key: `${d.global_vehicle_id}-${d.seq}-${Date.now()}`, coords, start: Date.now(), color, label, hasPath: coords.length > 1 };
      anims.current = [...anims.current.filter((x) => !x.key.startsWith(`${d.global_vehicle_id}-`)), a].slice(-MAX_ANIMS);
      setHops((n) => n + 1);
      setNow(Date.now());
    }
    // predicted next camera ring
    const p = d.prediction;
    if (p && p.camera_id && (p.status ?? "").toUpperCase() === "PENDING") {
      const end = p.window_end ? new Date(p.window_end).getTime() + 30_000 : Date.now() + PRED_FALLBACK_MS;
      preds.current.set(d.global_vehicle_id, { camera_id: p.camera_id, until: Number.isFinite(end) ? end : Date.now() + PRED_FALLBACK_MS });
      if (preds.current.size > MAX_PREDICTIONS) {
        const first = preds.current.keys().next().value;
        if (first !== undefined) preds.current.delete(first);
      }
    } else {
      preds.current.delete(d.global_vehicle_id);
    }
    recomputeRings();
  });

  // Animation clock: runs only while something is animating.
  const active = anims.current.length > 0;
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => {
      const n = Date.now();
      anims.current = anims.current.filter((a) => n - a.start < TRAVEL_MS + FADE_MS);
      setNow(n);
    }, 80);
    return () => clearInterval(t);
  }, [active, hops]);

  // Expire prediction rings.
  useEffect(() => {
    const t = setInterval(recomputeRings, 5000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Reset on scope change.
  useEffect(() => {
    anims.current = [];
    preds.current.clear();
    setRingKey("");
  }, [scope]);

  const ringCounts = useMemo(() => new Map(ringKey ? ringKey.split(",").map((s) => [s.split(":")[0], Number(s.split(":")[1])] as [string, number]) : []), [ringKey]);

  const mapCams = useMemo<MapCamera[]>(
    () =>
      cameras.map((c) => {
        const o = overview.get(c.id);
        const score = o?.score ?? null;
        const ring = ringCounts.get(c.id);
        const tip = [
          `${c.id} · ${c.name}`,
          `Status: ${c.status}${c.status_message ? ` (${c.status_message})` : ""}`,
          o ? `Congestion: ${score == null ? "no score" : `${Math.round(score)}/100`} · ${o.level}` : null,
          o?.explanation?.[0] ?? null,
          ring ? `Predicted next camera for ${ring} vehicle${ring > 1 ? "s" : ""}` : null,
        ]
          .filter((x): x is string => !!x)
          .map(escapeHtml)
          .join("<br>"); // Leaflet renders tooltip strings as HTML
        return {
          id: c.id,
          lat: c.latitude,
          lon: c.longitude,
          color: o ? scoreHex(score) : TONE_HEX[statusTone(c.status)],
          label: c.id,
          tooltip: tip,
          ring: ring ? TONE_HEX.blue : undefined,
          pulse: o?.level === "SEVERE",
        };
      }),
    [cameras, overview, ringCounts],
  );

  const { paths, points } = useMemo(() => {
    const ps: MapPath[] = [];
    const pts: MapPoint[] = [];
    for (const a of anims.current) {
      const age = now - a.start;
      const f = progress(a.start, TRAVEL_MS, now);
      if (a.hasPath) {
        const fade = age <= TRAVEL_MS ? 1 : 1 - (age - TRAVEL_MS) / FADE_MS;
        const partial = partialPath(a.coords, f);
        if (partial.length >= 2 && fade > 0)
          ps.push({ id: a.key, coords: partial, color: alphaHex(a.color, 0.25 + 0.75 * fade), weight: 2 + 2 * fade, tooltip: a.label });
      }
      if (age < TRAVEL_MS) {
        const [lat, lon] = along(a.coords, f);
        pts.push({ id: a.key, lat, lon, color: a.color, radius: 5 });
      }
    }
    return { paths: ps, points: pts };
  }, [now]);

  return (
    <div className={`relative ${className}`}>
      <NetworkMap cameras={mapCams} paths={paths} points={points} selectedId={selectedId} onCameraClick={onSelect} className="h-full w-full" />
      <div className="pointer-events-none absolute bottom-2 left-2 z-[500] space-y-0.5 rounded border border-ink-700 bg-ink-950/85 px-2 py-1 text-[10.5px] text-slate-400">
        <div className="flex items-center gap-2">
          <span>Congestion</span>
          {[
            ["<30", scoreHex(0)],
            ["30-54", scoreHex(30)],
            ["55-74", scoreHex(55)],
            ["≥75", scoreHex(75)],
            ["no data", scoreHex(null)],
          ].map(([l, c]) => (
            <span key={l} className="flex items-center gap-1">
              <span className="inline-block h-2 w-2 rounded-full" style={{ background: c }} />
              {l}
            </span>
          ))}
        </div>
        <div className="flex items-center gap-2">
          <span className="inline-block h-2.5 w-2.5 rounded-full border border-dashed" style={{ borderColor: TONE_HEX.blue }} /> predicted next camera
          <span className="ml-2">moving dot = live trajectory hop (colour = link confidence)</span>
        </div>
        <div>
          Active hops: <span className="font-mono text-slate-300">{points.length}</span> · pending predictions:{" "}
          <span className="font-mono text-slate-300">{[...ringCounts.values()].reduce((a, b) => a + b, 0)}</span>
        </div>
      </div>
    </div>
  );
}
