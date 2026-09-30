/**
 * Journey-replay math (pure, no React / Leaflet).
 *
 * A trajectory is a list of camera observations ("stops") with timestamps plus, for each
 * consecutive pair inside one journey, a road-following polyline. The replay runs on a
 * *virtual* clock: normally identical to real time, but with `compressGapsS` any interval
 * between two stops longer than that cap is squeezed to the cap so long gaps don't stall
 * playback. Within an interval the marker moves time-proportionally (constant speed) along
 * the road geometry; the real timestamp shown to the operator is always mapped back linearly.
 *
 * Between journeys (different journey_index) or where no route is known, the vehicle was not
 * observed: the marker holds at the last stop instead of inventing a path.
 */

export type LatLon = [number, number];

export interface ReplayPointInput {
  seq: number;
  journey_index: number;
  ts: string | number;
  latitude: number;
  longitude: number;
  camera_id: string;
}

export interface ReplaySegmentInput {
  from_seq: number;
  to_seq: number;
  geometry?: number[][] | null;
}

export interface ReplayStop {
  index: number;
  seq: number;
  cameraId: string;
  journey: number;
  /** Real time (epoch ms). */
  t: number;
  /** Virtual time offset from replay start (seconds). */
  v: number;
  coord: LatLon;
}

export interface ReplayLeg {
  index: number;
  from: number;
  to: number;
  /** "road": moving along known geometry; "gap": not observed (holds at the previous stop). */
  kind: "road" | "gap";
  t0: number;
  t1: number;
  v0: number;
  v1: number;
  coords: LatLon[];
  /** Cumulative length at each vertex (same length as coords). */
  cum: number[];
  length: number;
}

export interface ReplayTimeline {
  stops: ReplayStop[];
  legs: ReplayLeg[];
  startMs: number | null;
  endMs: number | null;
  /** Virtual duration in seconds. */
  duration: number;
  /** Real duration in seconds. */
  realDuration: number;
}

export interface ReplayOptions {
  /** Cap (seconds) for any interval between stops; null/undefined = real time everywhere. */
  compressGapsS?: number | null;
  /** Fallback road geometry between two cameras when the segment has none. */
  resolvePath?: (fromCameraId: string, toCameraId: string) => LatLon[] | null | undefined;
}

export interface ReplayState {
  coord: LatLon | null;
  /** Real epoch ms at this virtual time. */
  realMs: number | null;
  /** Index of the stop most recently reached (or the current one). */
  stopIndex: number;
  /** Stop the vehicle is currently at (within the interval start), or null while moving. */
  atStop: number | null;
  legIndex: number | null;
  /** Progress along the current leg 0..1. */
  frac: number;
  kind: "road" | "gap" | "stop";
}

const EPS = 1e-9;

export function toMs(ts: string | number): number {
  if (typeof ts === "number") return ts < 1e12 ? ts * 1000 : ts;
  return new Date(ts).getTime();
}

/** Planar distance with longitude scaled by cos(latitude) (good enough over a city). */
export function dist(a: LatLon, b: LatLon): number {
  const k = Math.cos((((a[0] + b[0]) / 2) * Math.PI) / 180);
  return Math.hypot(b[0] - a[0], (b[1] - a[1]) * k);
}

function samePt(a: LatLon, b: LatLon): boolean {
  return Math.abs(a[0] - b[0]) < 1e-7 && Math.abs(a[1] - b[1]) < 1e-7;
}

function cleanCoords(raw: number[][] | LatLon[] | null | undefined): LatLon[] {
  if (!raw) return [];
  const out: LatLon[] = [];
  for (const c of raw) {
    if (!Array.isArray(c) || c.length < 2) continue;
    const p: LatLon = [Number(c[0]), Number(c[1])];
    if (!Number.isFinite(p[0]) || !Number.isFinite(p[1])) continue;
    if (out.length && samePt(out[out.length - 1], p)) continue;
    out.push(p);
  }
  return out;
}

function cumulative(coords: LatLon[]): number[] {
  const cum = [0];
  for (let i = 1; i < coords.length; i++) cum.push(cum[i - 1] + dist(coords[i - 1], coords[i]));
  return cum;
}

/** Interpolate along a polyline at fraction 0..1 of its length. */
export function pointAlong(coords: LatLon[], cum: number[], frac: number): LatLon {
  if (coords.length === 0) return [0, 0];
  const total = cum[cum.length - 1] ?? 0;
  if (coords.length === 1 || frac <= 0 || total <= EPS) return coords[0];
  if (frac >= 1) return coords[coords.length - 1];
  const target = total * frac;
  let lo = 0;
  let hi = cum.length - 1;
  while (hi - lo > 1) {
    const mid = (lo + hi) >> 1;
    if (cum[mid] <= target) lo = mid;
    else hi = mid;
  }
  const seg = cum[hi] - cum[lo];
  const r = seg > EPS ? (target - cum[lo]) / seg : 0;
  return [coords[lo][0] + (coords[hi][0] - coords[lo][0]) * r, coords[lo][1] + (coords[hi][1] - coords[lo][1]) * r];
}

export function buildTimeline(points: ReplayPointInput[], segments: ReplaySegmentInput[] = [], opts: ReplayOptions = {}): ReplayTimeline {
  const cap = opts.compressGapsS != null && opts.compressGapsS > 0 ? opts.compressGapsS : null;
  const pts = points
    .filter((p) => Number.isFinite(p.latitude) && Number.isFinite(p.longitude) && Number.isFinite(toMs(p.ts)))
    .slice()
    .sort((a, b) => toMs(a.ts) - toMs(b.ts) || a.seq - b.seq);
  const segByTo = new Map<number, ReplaySegmentInput>();
  for (const s of segments) segByTo.set(s.to_seq, s);

  const stops: ReplayStop[] = [];
  const legs: ReplayLeg[] = [];
  let v = 0;
  pts.forEach((p, i) => {
    const t = toMs(p.ts);
    const coord: LatLon = [p.latitude, p.longitude];
    if (i > 0) {
      const prev = stops[i - 1];
      const realS = Math.max(0, (t - prev.t) / 1000);
      const virtS = cap != null ? Math.min(realS, cap) : realS;
      const v0 = v;
      v += virtS;
      let coords: LatLon[] = [];
      let kind: ReplayLeg["kind"] = "gap";
      if (prev.journey === p.journey_index) {
        const seg = segByTo.get(p.seq);
        coords = cleanCoords(seg && seg.from_seq === prev.seq ? seg.geometry : null);
        if (coords.length < 2) coords = cleanCoords(opts.resolvePath?.(prev.cameraId, p.camera_id) ?? null);
        if (coords.length >= 2) {
          kind = "road";
          if (!samePt(coords[0], prev.coord)) coords.unshift(prev.coord);
          if (!samePt(coords[coords.length - 1], coord)) coords.push(coord);
        }
      }
      if (kind === "gap") coords = [prev.coord, coord];
      const cum = cumulative(coords);
      legs.push({ index: legs.length, from: i - 1, to: i, kind, t0: prev.t, t1: t, v0, v1: v, coords, cum, length: cum[cum.length - 1] });
    }
    stops.push({ index: i, seq: p.seq, cameraId: p.camera_id, journey: p.journey_index, t, v, coord });
  });

  return {
    stops,
    legs,
    startMs: stops.length ? stops[0].t : null,
    endMs: stops.length ? stops[stops.length - 1].t : null,
    duration: v,
    realDuration: stops.length ? (stops[stops.length - 1].t - stops[0].t) / 1000 : 0,
  };
}

/** Vehicle position / state at virtual time `v` (seconds from replay start; clamped). */
export function positionAt(tl: ReplayTimeline, v: number): ReplayState {
  const { stops, legs } = tl;
  if (!stops.length) return { coord: null, realMs: null, stopIndex: -1, atStop: null, legIndex: null, frac: 0, kind: "stop" };
  const vv = Math.max(0, Math.min(Number.isFinite(v) ? v : 0, tl.duration));
  const last = stops[stops.length - 1];
  if (!legs.length || vv >= tl.duration) {
    // End of the replay (or a single sighting): at the final stop.
    return { coord: last.coord, realMs: last.t, stopIndex: last.index, atStop: last.index, legIndex: null, frac: 0, kind: "stop" };
  }
  // first leg whose end is strictly after vv (zero-length legs are skipped naturally)
  let lo = 0;
  let hi = legs.length - 1;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (legs[mid].v1 > vv) hi = mid;
    else lo = mid + 1;
  }
  const leg = legs[lo];
  const span = leg.v1 - leg.v0;
  const frac = span > EPS ? Math.max(0, Math.min(1, (vv - leg.v0) / span)) : 1;
  const realMs = leg.t0 + (leg.t1 - leg.t0) * frac;
  if (frac <= EPS) {
    return { coord: stops[leg.from].coord, realMs: leg.t0, stopIndex: leg.from, atStop: leg.from, legIndex: leg.index, frac: 0, kind: "stop" };
  }
  const coord = leg.kind === "road" ? pointAlong(leg.coords, leg.cum, frac) : stops[leg.from].coord;
  return { coord, realMs, stopIndex: leg.from, atStop: null, legIndex: leg.index, frac, kind: leg.kind };
}

/** Virtual time at which the vehicle reaches stop `i`. */
export function stopTime(tl: ReplayTimeline, i: number): number {
  return tl.stops[Math.max(0, Math.min(i, tl.stops.length - 1))]?.v ?? 0;
}

/** Map a real epoch-ms instant to virtual replay seconds (clamped to the journey). */
export function realToVirtual(tl: ReplayTimeline, ms: number): number {
  if (!tl.stops.length) return 0;
  if (ms <= tl.stops[0].t) return 0;
  for (const leg of tl.legs) {
    if (ms <= leg.t1) {
      const span = leg.t1 - leg.t0;
      const f = span > 0 ? (ms - leg.t0) / span : 1;
      return leg.v0 + (leg.v1 - leg.v0) * f;
    }
  }
  return tl.duration;
}
