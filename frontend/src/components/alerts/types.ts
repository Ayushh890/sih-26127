/** Shapes of the alert endpoints (backend app/services/alerts.py alert_dict, routes/alerts.py). */

export interface ScopeInfo {
  scope?: string;
  synthetic?: boolean;
  notice?: string | null;
}

export type AlertStatus = "NEW" | "ACKNOWLEDGED" | "RESOLVED";

export interface Alert {
  id: number;
  code: string;
  type: string;
  severity: string;
  status: AlertStatus | string;
  created_at: string | null;
  updated_at: string | null;
  camera_id: string | null;
  global_vehicle_id: number | null;
  observation_id: number | null;
  title: string;
  reason: string;
  details: Record<string, unknown> | null;
  confidence: number | null;
  evidence_id: string | null;
  occurrences: number;
  acknowledged_by: string | null;
  acknowledged_at: string | null;
  resolved_by: string | null;
  resolved_at: string | null;
  resolution_note: string | null;
  is_demo: boolean;
}

export interface AlertList extends ScopeInfo {
  total: number;
  counts_7d: Record<string, number>;
  results: Alert[];
}

export interface AuditEntry {
  id: number;
  ts: string;
  user_id: number | null;
  username: string | null;
  role: string | null;
  action: string;
  resource_type: string | null;
  resource_id: string | null;
  details: Record<string, unknown> | null;
  ip: string | null;
  success: boolean;
}

export interface EvidenceMeta {
  id: string;
  observation_id: number | null;
  camera_id: string | null;
  ts: string | null;
  files: Record<string, { sha256?: string; bytes?: number; width?: number; height?: number }>;
  bbox: number[] | null;
  plate_bbox: number[] | null;
  ocr_text: string | null;
  ocr_confidence: number | null;
  manifest_sha256: string | null;
  encrypted: boolean;
}

export interface AlertDetailResp extends Alert {
  vehicle_code: string | null;
  evidence: EvidenceMeta | null;
  history: AuditEntry[];
}

export interface AlertRules {
  rules: Record<string, Record<string, unknown>>;
  note: string;
}

export const ALERT_STATUSES = ["OPEN", "NEW", "ACKNOWLEDGED", "RESOLVED"] as const;
export const SEVERITIES = ["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"] as const;

/** Actions the backend accepts for an alert in a given status (see services/alerts.transition). */
export function allowedActions(status: string): ("acknowledge" | "resolve" | "reopen")[] {
  switch (status) {
    case "NEW":
      return ["acknowledge", "resolve"];
    case "ACKNOWLEDGED":
      return ["resolve"];
    case "RESOLVED":
      return ["reopen"];
    default:
      return [];
  }
}

export function reopenedCount(a: Pick<Alert, "details">): number {
  const r = a.details?.["reopened"];
  return typeof r === "number" ? r : 0;
}
