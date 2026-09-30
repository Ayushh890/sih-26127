/** Response shapes of the vehicle / observation / evidence endpoints (backend app/services/views.py). */

export interface ScopeInfo {
  scope?: string;
  synthetic?: boolean;
  notice?: string | null;
}

export interface Observation {
  id: number;
  camera_id: string;
  camera_name: string | null;
  observed_at: string | null;
  first_seen_at: string | null;
  last_seen_at: string | null;
  plate_text: string | null;
  plate_display: string | null;
  plate_raw: string | null;
  plate_confidence: number | null;
  plate_valid: boolean | null;
  plate_votes: number | null;
  vehicle_class: string | null;
  class_confidence: number | null;
  vehicle_color: string | null;
  speed_kmh: number | null;
  speed_is_estimate: boolean;
  direction: string | null;
  heading_deg: number | null;
  motion: string | null;
  lane: number | null;
  global_vehicle_id: number | null;
  vehicle_code: string | null;
  previous_observation_id: number | null;
  match_score: number | null;
  match_confidence_level: string | null;
  match_reasons: string[];
  evidence_id: string | null;
  is_demo: boolean;
  /** Only on fuzzy searches: weighted edit distance to the query. */
  plate_distance?: number;
}

export interface SearchResponse extends ScopeInfo {
  total: number;
  limit: number;
  offset: number;
  mode: "pseudonym" | "fuzzy" | "partial" | null;
  since: string | null;
  until: string | null;
  results: Observation[];
}

export interface Prediction {
  camera_id: string;
  probability: number | null;
  status: string;
  window_start: string | null;
  window_end: string | null;
  from_observation_id: number | null;
}

export interface Vehicle {
  id: number;
  code: string;
  plate_text: string | null;
  plate_display: string | null;
  plate_confidence: number | null;
  vehicle_class: string | null;
  vehicle_color: string | null;
  first_seen_at: string | null;
  last_seen_at: string | null;
  first_camera_id: string | null;
  last_camera_id: string | null;
  observation_count: number;
  camera_count: number;
  total_distance_m: number;
  prediction: Prediction | null;
  is_demo: boolean;
}

export interface VehicleDetailResponse extends Vehicle {
  observations: Observation[];
  cameras: string[];
}

export interface VehiclesResponse extends ScopeInfo {
  total: number;
  results: Vehicle[];
}

export interface TrajectoryPoint {
  id: number;
  seq: number;
  journey_index: number;
  observation_id: number;
  camera_id: string;
  ts: string;
  latitude: number;
  longitude: number;
  from_camera_id: string | null;
  segment_travel_s: number | null;
  segment_distance_m: number | null;
  segment_speed_kmh: number | null;
  link_score: number | null;
  confidence_level: string | null;
  explanation: string | null;
  prediction_status: string | null;
  camera_name: string | null;
  plate_text: string | null;
  speed_kmh: number | null;
  vehicle_class: string | null;
  vehicle_color: string | null;
  evidence_id: string | null;
  match_reasons: string[];
}

export interface TrajectorySegment {
  from_seq: number;
  to_seq: number;
  from_camera_id: string;
  to_camera_id: string;
  start: string | null;
  end: string | null;
  travel_s: number | null;
  distance_m: number | null;
  speed_kmh: number | null;
  confidence_level: string | null;
  link_score: number | null;
  via: string[];
  geometry: number[][];
  unobserved_cameras: string[];
}

export interface TrajectoryResponse {
  vehicle: Vehicle;
  journeys: number[];
  points: TrajectoryPoint[];
  segments: TrajectorySegment[];
  start: string | null;
  end: string | null;
  geometry_source: string;
  legend: Record<string, string>;
}

export interface PredictionResponse {
  vehicle_code: string;
  prediction: Prediction | null;
  vehicle_history: Record<string, number>;
  network_accuracy_24h: { confirmed: number; deviated: number; accuracy: number | null };
}

export interface PlateReadRow {
  ts: string | null;
  raw_text: string;
  normalized_text: string;
  confidence: number;
  char_confidences: number[] | null;
  is_valid_format: boolean;
  corrections: { position: number; from: string; to: string; reason: string }[] | null;
  plate_bbox: number[] | null;
}

export interface MatchConflict {
  vehicle_id: number;
  vehicle_code: string;
  observation_id: number;
  camera_id: string;
  observed_at: string;
  dt_s: number;
  min_travel_s: number | null;
  distance_m: number | null;
  reason: string;
}

export interface MatchComponents {
  scores?: Record<string, number | null>;
  weights?: Record<string, number>;
  fused?: number;
  path?: { cameras: string[]; distance_m: number; min_travel_s: number; typical_travel_s: number; hops: number } | null;
  dt_s?: number;
  runner_up?: number | null;
  considered?: number;
  new_journey?: boolean;
  conflicts?: MatchConflict[];
}

export interface EvidenceFile {
  sha256: string;
  bytes?: number;
  width?: number;
  height?: number;
}

export interface EvidenceMeta {
  id: string;
  observation_id: number | null;
  camera_id: string | null;
  ts: string | null;
  files: Record<string, EvidenceFile>;
  bbox: number[] | null;
  plate_bbox: number[] | null;
  ocr_text: string | null;
  ocr_confidence: number | null;
  manifest_sha256: string | null;
  encrypted: boolean;
  urls?: Record<string, string>;
}

export interface EvidenceVerify {
  evidence_id: string;
  manifest_sha256: string | null;
  valid: boolean;
  manifest_valid: boolean | null;
  files: Record<string, { ok: boolean; expected?: string | null; actual?: string | null; error?: string }>;
}

export interface ObservationDetailResponse extends Observation {
  match_components: MatchComponents;
  color_rgb: number[] | null;
  bbox: number[] | null;
  plate_reads: PlateReadRow[];
  track: { frames: number; started_at: string | null; ended_at: string | null; path: number[][] | null } | null;
  previous: Observation | null;
  evidence: EvidenceMeta | null;
  evidence_access: boolean;
}

export interface CameraLite {
  id: string;
  name: string;
  is_demo: boolean;
}

/** Evidence image roles in capture order (backend app/evidence/store.py ROLES). */
export const EVIDENCE_ROLES = ["vehicle", "plate", "before", "detection", "after"] as const;
