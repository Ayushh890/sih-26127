/**
 * Types and pure helpers shared by the Command Center and camera pages.
 * Shapes follow the real backend responses (app/api/routes/cameras.py, analytics.py,
 * app/services/scheduler.py analytics_snapshot, app/services/ingestion.py events).
 */
import type { QueryClient } from "@tanstack/react-query";

export interface ScopeInfo {
  scope?: string;
  synthetic?: boolean;
  notice?: string | null;
}

export interface CameraRuntime {
  input_fps?: number | null;
  processing_fps?: number | null;
  target_processing_fps?: number | null;
  latency_ms?: number | null;
  inference_ms?: number | null;
  stage_ms?: Record<string, number> | null;
  queue_depth?: number | null;
  max_queue_size?: number | null;
  frames_in?: number | null;
  frames_processed?: number | null;
  frames_dropped?: number | null;
  resolution?: string | null;
  reconnects?: number | null;
  sharpness?: number | null;
  brightness?: number | null;
  active_tracks?: number | null;
  faults?: unknown;
  uptime_s?: number | null;
  worker_id?: string | null;
  status_since?: number | string | null;
  updated_at?: number | null;
  last_error?: unknown;
  status?: string;
  message?: string | null;
  native_fps?: number | null;
}

export interface Processing {
  processing_fps?: number;
  confidence_threshold?: number;
  min_plate_vehicle_width?: number;
  frame_skip?: number;
  max_queue_size?: number;
  source_options?: Record<string, unknown>;
  [k: string]: unknown;
}

export interface Calibration {
  reference_width?: number;
  focal_px?: number;
  flow_toward_camera?: boolean;
  one_way?: boolean;
  vanishing_point?: number[];
  lane_boundaries?: number[];
  camera_height_m?: number;
  speed_limit_kmh?: number;
  source?: string;
  [k: string]: unknown;
}

export interface Camera {
  id: string;
  name: string;
  location: string | null;
  latitude: number;
  longitude: number;
  source_type: string;
  source_uri: string;
  has_credentials: boolean;
  username_hint: string | null;
  resolution: string | null;
  lane_count: number | null;
  direction: string | null;
  road_name: string | null;
  zone: string | null;
  camera_type: string | null;
  enabled: boolean;
  status: string;
  status_message: string | null;
  last_seen_at: string | null;
  processing: Processing;
  calibration: Calibration;
  is_demo: boolean;
  created_at: string | null;
  updated_at: string | null;
  runtime: CameraRuntime | null;
}

export interface CameraList extends ScopeInfo {
  cameras: Camera[];
}

export interface RuntimeAll {
  cameras: Record<string, CameraRuntime>;
  ts: number;
}

export interface CongestionComponent {
  value: number;
  raw: number;
  text: string;
  segments?: { from: string; median_s: number; baseline_s: number; baseline_source: string; samples: number }[];
}

export interface OverviewCamera {
  camera_id: string;
  camera_name: string;
  latitude: number;
  longitude: number;
  window_s: number;
  status: string;
  is_demo: boolean;
  score: number | null;
  level: string;
  components: Record<string, CongestionComponent>;
  weights?: Record<string, number>;
  explanation: string[];
  metrics: Record<string, number | string | null> | null;
}

export interface ScopeSnapshot {
  cameras: number;
  online: number;
  totals: Record<string, number | string | null> | null;
  unique_vehicles: number;
  open_alerts: Record<string, number>;
  network_score: number | null;
  worst_camera_id: string | null;
}

export interface Overview {
  generated_at: string;
  window_s: number;
  cameras: OverviewCamera[];
  scopes: Partial<Record<"live" | "demo", ScopeSnapshot>>;
}

export interface Prediction {
  camera_id: string;
  probability: number | null;
  status: string;
  window_start: string | null;
  window_end: string | null;
  from_observation_id?: number | null;
}

/** vehicle_matched event payload (also produced from GET /api/observations rows). */
export interface Detection {
  observation_id: number;
  camera_id: string;
  camera_name: string | null;
  observed_at: string;
  plate_text: string | null;
  plate_confidence: number | null;
  vehicle_class: string | null;
  vehicle_color: string | null;
  speed_kmh: number | null;
  direction?: string | null;
  motion?: string | null;
  lane?: number | null;
  global_vehicle_id: number | null;
  vehicle_code: string | null;
  match_score: number | null;
  confidence_level: string | null;
  match_reasons: string[] | null;
  evidence_id?: string | null;
  is_demo: boolean;
  prediction?: Prediction | null;
  new_vehicle?: boolean;
}

export interface ObservationRow {
  id: number;
  camera_id: string;
  camera_name: string | null;
  observed_at: string;
  plate_text: string | null;
  plate_confidence: number | null;
  vehicle_class: string | null;
  vehicle_color: string | null;
  speed_kmh: number | null;
  direction?: string | null;
  motion?: string | null;
  lane?: number | null;
  global_vehicle_id: number | null;
  vehicle_code: string | null;
  match_score: number | null;
  match_confidence_level: string | null;
  match_reasons: string[] | null;
  evidence_id?: string | null;
  is_demo: boolean;
  new_vehicle?: boolean;
  previous_observation_id?: number | null;
}

export interface TrajectoryEvent {
  global_vehicle_id: number;
  vehicle_code: string;
  seq: number;
  journey_index: number;
  camera_id: string;
  from_camera_id: string | null;
  ts: string;
  latitude: number;
  longitude: number;
  link_score: number | null;
  confidence_level: string | null;
  segment_travel_s: number | null;
  segment_speed_kmh: number | null;
  prediction_status: string | null;
  explanation: string[] | string | null;
  prediction: Prediction | null;
  plate_text: string | null;
  is_demo: boolean;
}

export interface CameraStatusEvent {
  camera_id: string;
  old_status: string;
  status: string;
  message: string | null;
  ts: number | string;
  is_demo: boolean;
  intentional: boolean;
}

export interface Alert {
  id: number;
  code: string;
  type: string;
  severity: string;
  status: string;
  created_at: string | null;
  updated_at: string | null;
  camera_id: string | null;
  title: string;
  reason: string;
  occurrences: number;
  confidence: number | null;
  is_demo: boolean;
}

export interface AlertList extends ScopeInfo {
  total: number;
  results: Alert[];
}

/* --------------------------------------------------------------------------- helpers */

/** Resolved data scope ("live" | "demo" | "all") → does a record with this is_demo flag belong to it? */
export function inScope(scope: string | undefined, isDemo: boolean | undefined): boolean {
  if (scope === "live") return !isDemo;
  if (scope === "demo") return !!isDemo;
  return true;
}

export function obsToDetection(o: ObservationRow): Detection {
  return {
    observation_id: o.id,
    camera_id: o.camera_id,
    camera_name: o.camera_name,
    observed_at: o.observed_at,
    plate_text: o.plate_text,
    plate_confidence: o.plate_confidence,
    vehicle_class: o.vehicle_class,
    vehicle_color: o.vehicle_color,
    speed_kmh: o.speed_kmh,
    direction: o.direction,
    motion: o.motion,
    lane: o.lane,
    global_vehicle_id: o.global_vehicle_id,
    vehicle_code: o.vehicle_code,
    match_score: o.match_score,
    confidence_level: o.match_confidence_level,
    match_reasons: o.match_reasons,
    evidence_id: o.evidence_id,
    is_demo: o.is_demo,
    new_vehicle: o.new_vehicle ?? o.match_confidence_level === "NEW",
  };
}

/** Prepend a detection to the feed, de-duplicated by observation id and bounded. */
export function pushDetection(feed: Detection[], d: Detection, max = 60): Detection[] {
  const rest = feed.filter((x) => x.observation_id !== d.observation_id);
  return [d, ...rest].slice(0, max);
}

/** Apply a camera_status_changed event to a camera record. */
export function applyStatus<T extends { status: string; status_message?: string | null; last_seen_at?: string | null }>(c: T, ev: CameraStatusEvent): T {
  const ts = typeof ev.ts === "number" ? new Date(ev.ts < 1e12 ? ev.ts * 1000 : ev.ts).toISOString() : ev.ts;
  return { ...c, status: ev.status, status_message: ev.message, last_seen_at: ts ?? c.last_seen_at };
}

/** Update every cached camera list / camera detail with a status transition. */
export function applyStatusToCache(qc: QueryClient, ev: CameraStatusEvent): void {
  qc.setQueriesData<CameraList>({ queryKey: ["cameras", "list"] }, (d) =>
    d && Array.isArray(d.cameras) ? { ...d, cameras: d.cameras.map((c) => (c.id === ev.camera_id ? applyStatus(c, ev) : c)) } : d,
  );
  qc.setQueryData<Camera>(["camera", ev.camera_id], (d) => (d ? applyStatus(d, ev) : d));
  qc.setQueryData<Overview>(["analytics", "overview"], (d) =>
    d ? { ...d, cameras: d.cameras.map((c) => (c.camera_id === ev.camera_id ? { ...c, status: ev.status } : c)) } : d,
  );
}

/** Upsert a live alert into an open-alert list (resolved alerts drop out). */
export function upsertOpenAlert(list: AlertList, a: Alert, limit: number): AlertList {
  const exists = list.results.some((x) => x.id === a.id);
  if (a.status === "RESOLVED") {
    return exists ? { ...list, results: list.results.filter((x) => x.id !== a.id), total: Math.max(0, list.total - 1) } : list;
  }
  if (exists) return { ...list, results: list.results.map((x) => (x.id === a.id ? { ...x, ...a } : x)) };
  return { ...list, results: [a, ...list.results].slice(0, limit), total: list.total + 1 };
}

export interface CameraFilter {
  text: string;
  status: string;
  zone: string;
  source: string;
}

export function filterCameras(cams: Camera[], f: CameraFilter): Camera[] {
  const t = f.text.trim().toLowerCase();
  return cams.filter((c) => {
    if (f.status && c.status !== f.status) return false;
    if (f.zone && (c.zone ?? "") !== f.zone) return false;
    if (f.source && c.source_type !== f.source) return false;
    if (!t) return true;
    return [c.id, c.name, c.location, c.road_name, c.zone].some((v) => (v ?? "").toLowerCase().includes(t));
  });
}

/** Distinct non-empty values, sorted. */
export function distinct(values: (string | null | undefined)[]): string[] {
  return [...new Set(values.filter((v): v is string => !!v))].sort();
}

/** Fraction elapsed 0..1 of an animation. */
export function progress(start: number, duration: number, now: number): number {
  if (duration <= 0) return 1;
  return Math.min(1, Math.max(0, (now - start) / duration));
}

/** The leading part of a polyline covering fraction 0..1 of its length (ends exactly at along(coords, frac)). */
export function partialPath(coords: [number, number][], frac: number): [number, number][] {
  if (coords.length < 2 || frac >= 1) return coords;
  if (frac <= 0) return [coords[0]];
  const seg: number[] = [];
  let total = 0;
  for (let i = 1; i < coords.length; i++) {
    const d = Math.hypot(coords[i][0] - coords[i - 1][0], coords[i][1] - coords[i - 1][1]);
    seg.push(d);
    total += d;
  }
  let target = total * frac;
  const out: [number, number][] = [coords[0]];
  for (let i = 0; i < seg.length; i++) {
    if (target <= seg[i]) {
      const r = seg[i] ? target / seg[i] : 0;
      out.push([coords[i][0] + (coords[i + 1][0] - coords[i][0]) * r, coords[i][1] + (coords[i + 1][1] - coords[i][1]) * r]);
      return out;
    }
    target -= seg[i];
    out.push(coords[i + 1]);
  }
  return out;
}

/** Compute a minimal PATCH body: only keys whose value differs from the original. */
export function diffObject(orig: Record<string, unknown>, next: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(next)) {
    if (v === undefined) continue;
    if (JSON.stringify(orig[k] ?? null) !== JSON.stringify(v ?? null)) out[k] = v;
  }
  return out;
}

/** Parse an optional numeric form value ("" → undefined). */
export function num(v: string): number | undefined {
  if (v.trim() === "") return undefined;
  const n = Number(v);
  return Number.isFinite(n) ? n : undefined;
}

/** Parse a comma separated list of numbers ("" → undefined, invalid → null). */
export function numList(v: string): number[] | undefined | null {
  if (!v.trim()) return undefined;
  const parts = v.split(",").map((s) => Number(s.trim()));
  return parts.every((n) => Number.isFinite(n)) ? parts : null;
}

export const SOURCE_TYPES_CREATE = ["rtsp", "http", "webcam", "browser", "file"] as const;
export const COMPASS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"] as const;
export const CAMERA_TYPES = ["ANPR", "SURVEILLANCE", "PTZ", "OVERVIEW"] as const;
export const FAULT_KINDS = ["offline", "blur", "low_fps", "dark"] as const;

export const SOURCE_HINT: Record<string, string> = {
  rtsp: "rtsp://192.168.1.64:554/Streaming/Channels/101 (credentials go in the fields below)",
  http: "http(s)://host/video.mjpg — MJPEG or HTTP video stream",
  webcam: "device index (0, 1, …) or /dev/videoN of a camera attached to the server",
  browser: "browser://local — frames are sent from the Local Camera page of this console",
  file: "path to a video file on the server (absolute or relative to DATA_DIR)",
  demo: "demo://CAM-XX",
};
