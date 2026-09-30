/**
 * One alert with everything needed to judge it: the reason, the exact rule configuration
 * that produced it, the raw rule inputs (details), linked camera / vehicle / observation,
 * evidence images, the lifecycle and the audited operator history.
 */
import { useState, type ReactNode } from "react";
import { Link, useParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, mediaUrl } from "../lib/api";
import { P, useAuth } from "../lib/auth";
import { useLiveEvent } from "../lib/ws";
import { alertStatusTone, fmtAgo, fmtPct, fmtTime, levelTone, severityTone } from "../lib/format";
import { ActionButton, Badge, Empty, Explanation, KV, PageHeader, Panel, QueryState } from "../components/ui";
import { AlertActionPanel } from "../components/alerts/AlertActions";
import { reopenedCount, type Alert, type AlertDetailResp, type EvidenceMeta } from "../components/alerts/types";
import { ComponentBars, type CongestionComponent } from "../components/analytics/ComponentBars";

const EVIDENCE_ROLES = ["vehicle", "plate", "before", "detection", "after"] as const;
const HANDLED = new Set(["rule", "first_reason", "explanation", "components", "weights", "metrics", "reopened", "last_auto_resolution"]);

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function Val({ k, v }: { k: string; v: unknown }): ReactNode {
  if (v == null || v === "") return <span className="text-slate-500">—</span>;
  if (typeof v === "string" || typeof v === "number") {
    const s = String(v);
    if (k === "vehicle_code") return <Link className="mono text-cyan-300 hover:underline" to={`/vehicles/${encodeURIComponent(s)}`}>{s}</Link>;
    if (k === "camera_id" || k === "from" || k === "to") return <Link className="mono text-cyan-300 hover:underline" to={`/cameras/${encodeURIComponent(s)}`}>{s}</Link>;
    if (k === "observation_id") return <Link className="mono text-cyan-300 hover:underline" to={`/observations/${encodeURIComponent(s)}`}>#{s}</Link>;
    if (k.endsWith("_at") && typeof v === "string") return <span className="mono">{fmtTime(v, true)}</span>;
    return <span className="mono">{s}</span>;
  }
  if (typeof v === "boolean") return <span className="mono">{v ? "true" : "false"}</span>;
  if (Array.isArray(v)) {
    if (v.every((x) => x == null || ["string", "number", "boolean"].includes(typeof x))) return <span className="mono">{v.map((x) => String(x)).join(", ") || "—"}</span>;
    return <pre className="mono whitespace-pre-wrap text-[11px] text-slate-300">{JSON.stringify(v, null, 1)}</pre>;
  }
  if (isPlainObject(v)) {
    return (
      <div className="rounded border border-ink-700 bg-ink-850/60 p-1.5">
        <KV rows={Object.entries(v).map(([kk, vv]) => [kk, <Val k={kk} v={vv} />])} />
      </div>
    );
  }
  return <span className="mono">{String(v)}</span>;
}

function EvidencePanel({ ev, alert }: { ev: EvidenceMeta | null; alert: Alert }) {
  const { has } = useAuth();
  const [verify, setVerify] = useState<{ valid: boolean; [k: string]: unknown } | null>(null);
  if (!alert.evidence_id) return <Empty>No evidence package is attached to this alert.</Empty>;
  if (!has(P.EVIDENCE_READ)) return <Empty>Evidence exists for this alert; viewing it requires the evidence:read permission.</Empty>;
  if (!ev) return <Empty>Evidence record {alert.evidence_id} was not found (it may have been removed by retention).</Empty>;
  const roles = EVIDENCE_ROLES.filter((r) => ev.files?.[r]);
  return (
    <div className="space-y-2">
      <div className="grid grid-cols-2 gap-2 lg:grid-cols-3">
        {roles.map((r) => (
          <EvidenceImage key={r} id={ev.id} role={r} sha={ev.files[r]?.sha256} />
        ))}
        {!roles.length && <Empty>The evidence package has no image files.</Empty>}
      </div>
      <KV
        rows={[
          ["Evidence id", <span className="mono">{ev.id}</span>],
          ["Captured", fmtTime(ev.ts, true)],
          ["Camera", ev.camera_id ? <Val k="camera_id" v={ev.camera_id} /> : "—"],
          ["Observation", ev.observation_id ? <Val k="observation_id" v={ev.observation_id} /> : "—"],
          ["OCR text", <span className="mono">{ev.ocr_text ?? "—"}</span>],
          ["OCR confidence", ev.ocr_confidence == null ? "—" : fmtPct(ev.ocr_confidence, 1)],
          ["Manifest SHA-256", <span className="mono break-all text-[11px]">{ev.manifest_sha256 ?? "—"}</span>],
          ["Encrypted at rest", ev.encrypted ? "yes" : "no"],
        ]}
      />
      <div className="flex flex-wrap items-center gap-2">
        <ActionButton onClick={async () => setVerify(await api.post<{ valid: boolean }>(`/api/evidence/${ev.id}/verify`))}>Verify integrity</ActionButton>
        {verify && (
          <Badge tone={verify.valid ? "green" : "red"} title={JSON.stringify(verify)}>
            {verify.valid ? "Hashes match manifest" : "Integrity check FAILED"}
          </Badge>
        )}
      </div>
    </div>
  );
}

function EvidenceImage({ id, role, sha }: { id: string; role: string; sha?: string }) {
  const [failed, setFailed] = useState(false);
  return (
    <figure className="rounded border border-ink-700 bg-ink-950 p-1">
      {failed ? (
        <div className="flex h-24 items-center justify-center text-[11px] text-slate-500">Image unavailable</div>
      ) : (
        <img className="max-h-56 w-full object-contain" alt={`${role} evidence`} src={mediaUrl(`/api/evidence/${id}/${role}.jpg`)} onError={() => setFailed(true)} />
      )}
      <figcaption className="mt-0.5 flex justify-between gap-1 text-[11px] text-slate-400">
        <span className="uppercase">{role}</span>
        {sha && <span className="truncate font-mono text-slate-600" title={`SHA-256 ${sha}`}>{sha.slice(0, 12)}…</span>}
      </figcaption>
    </figure>
  );
}

export default function AlertDetail() {
  const { ref = "" } = useParams();
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["alert", ref], queryFn: () => api.get<AlertDetailResp>(`/api/alerts/${encodeURIComponent(ref)}`) });

  useLiveEvent("alert_updated", (ev) => {
    const a = ev.data as unknown as Alert;
    if (a && (a.code === q.data?.code || a.code === ref.toUpperCase() || String(a.id) === ref)) void qc.invalidateQueries({ queryKey: ["alert", ref] });
  });

  const onDone = (a: Alert) => {
    qc.setQueryData<AlertDetailResp>(["alert", ref], (old) => (old ? { ...old, ...a } : old));
    void qc.invalidateQueries({ queryKey: ["alert", ref] });
    void qc.invalidateQueries({ queryKey: ["alerts", "list"] });
  };

  return (
    <div className="space-y-2">
      <div className="text-[12px]">
        <Link className="text-cyan-300 hover:underline" to="/alerts">
          ← Alerts
        </Link>
      </div>
      <QueryState q={q}>
        {(a) => {
          const d = (a.details ?? {}) as Record<string, unknown>;
          const rule = isPlainObject(d.rule) ? d.rule : null;
          const firstReason = typeof d.first_reason === "string" ? d.first_reason : null;
          const explanation = Array.isArray(d.explanation) ? (d.explanation as string[]) : null;
          const components = isPlainObject(d.components) ? (d.components as unknown as Record<string, CongestionComponent>) : null;
          const reopened = reopenedCount(a);
          const rest = Object.entries(d).filter(([k]) => !HANDLED.has(k));
          return (
            <>
              <PageHeader
                title={`${a.code} · ${a.title}`}
                subtitle={
                  <span className="flex flex-wrap items-center gap-1.5">
                    <Badge tone={severityTone(a.severity)}>{a.severity}</Badge>
                    <Badge tone={alertStatusTone(a.status)}>{a.status}</Badge>
                    <Badge tone="cyan">{a.type}</Badge>
                    {a.is_demo && <Badge tone="violet" title="Raised from the synthetic demo traffic scenario">Synthetic</Badge>}
                    <span>
                      raised {fmtTime(a.created_at, true)} ({fmtAgo(a.created_at)}) · last update {fmtTime(a.updated_at, true)}
                    </span>
                  </span>
                }
              />
              <div className="grid gap-2 xl:grid-cols-3">
                <div className="space-y-2 xl:col-span-2">
                  <Panel title="Why this alert was raised">
                    <div className="space-y-2">
                      <p className="text-[13px] text-slate-100">{a.reason}</p>
                      {firstReason && firstReason !== a.reason && (
                        <div className="text-[12px] text-slate-400">
                          <span className="label">First occurrence reason</span>
                          {firstReason}
                        </div>
                      )}
                      <KV
                        rows={[
                          ["Confidence", a.confidence == null ? <span className="text-slate-500">not applicable for this rule</span> : fmtPct(a.confidence, 1)],
                          ["Occurrences", <span className="mono">{a.occurrences}</span>],
                          ...(reopened > 0
                            ? ([["Re-opened", <span className="text-orange-300">{reopened}× after automatic resolution (condition recurred within the rule cooldown)</span>]] as [ReactNode, ReactNode][])
                            : []),
                          ...(typeof d.last_auto_resolution === "string" ? ([["Last auto-resolution", d.last_auto_resolution]] as [ReactNode, ReactNode][]) : []),
                        ]}
                      />
                      {explanation && <Explanation lines={explanation} />}
                    </div>
                  </Panel>

                  {components && Object.keys(components).length > 0 && (
                    <Panel
                      title="Congestion components at the time of the alert"
                      subtitle={typeof d.score === "number" ? `score ${d.score}/100` : undefined}
                      actions={typeof d.level === "string" ? <Badge tone={levelTone(d.level)}>{d.level}</Badge> : undefined}
                    >
                      <ComponentBars components={components} weights={isPlainObject(d.weights) ? (d.weights as Record<string, number>) : undefined} />
                      {isPlainObject(d.metrics) && (
                        <div className="mt-2">
                          <span className="label">Window metrics</span>
                          <Val k="metrics" v={d.metrics} />
                        </div>
                      )}
                    </Panel>
                  )}

                  <Panel title="Rule inputs (details)">
                    {rest.length ? <KV rows={rest.map(([k, v]) => [k, <Val k={k} v={v} />])} /> : <Empty>The rule recorded no additional inputs.</Empty>}
                  </Panel>

                  <Panel title="Evidence">
                    <EvidencePanel ev={a.evidence} alert={a} />
                  </Panel>
                </div>

                <div className="space-y-2">
                  <Panel title="Actions">
                    <AlertActionPanel alert={a} onDone={onDone} />
                  </Panel>

                  <Panel title="Rule that produced this alert" subtitle="Configuration snapshot stored with the alert">
                    {rule ? (
                      <>
                        <KV rows={Object.entries(rule).map(([k, v]) => [k, <Val k={k} v={v} />])} />
                        <div className="mt-1.5 text-[11px] text-slate-500">
                          Alerts are raised only by explicit rules; current rule settings are under{" "}
                          <Link className="text-cyan-300 hover:underline" to="/settings">
                            Settings
                          </Link>
                          .
                        </div>
                      </>
                    ) : (
                      <Empty>No rule snapshot was stored with this alert.</Empty>
                    )}
                  </Panel>

                  <Panel title="Linked records">
                    <KV
                      rows={[
                        ["Camera", a.camera_id ? <Val k="camera_id" v={a.camera_id} /> : "—"],
                        ["Vehicle", a.vehicle_code ? <Val k="vehicle_code" v={a.vehicle_code} /> : a.global_vehicle_id ? <span className="mono">id {a.global_vehicle_id}</span> : "—"],
                        ["Observation", a.observation_id ? <Val k="observation_id" v={a.observation_id} /> : "—"],
                        ["Evidence", a.evidence_id ? <span className="mono break-all text-[11px]">{a.evidence_id}</span> : "—"],
                      ]}
                    />
                  </Panel>

                  <Panel title="Lifecycle">
                    <KV
                      rows={[
                        ["Created", fmtTime(a.created_at, true)],
                        ["Acknowledged", a.acknowledged_at ? `${fmtTime(a.acknowledged_at, true)} by ${a.acknowledged_by ?? "—"}` : "—"],
                        ["Resolved", a.resolved_at ? `${fmtTime(a.resolved_at, true)} by ${a.resolved_by ?? "—"}` : "—"],
                        ["Resolution note", a.resolution_note || "—"],
                      ]}
                    />
                  </Panel>

                  <Panel title="Audit history" bodyClass="p-0">
                    {a.history.length === 0 ? (
                      <Empty>No operator actions recorded. Automatic (system) transitions appear in the lifecycle above.</Empty>
                    ) : (
                      <table className="w-full text-[12px]">
                        <thead>
                          <tr>
                            <th className="th">Time</th>
                            <th className="th">User</th>
                            <th className="th">Action</th>
                            <th className="th">Note</th>
                          </tr>
                        </thead>
                        <tbody>
                          {a.history.map((h) => {
                            const det = h.details ?? {};
                            return (
                              <tr key={h.id}>
                                <td className="td mono whitespace-nowrap" title={h.ip ? `from ${h.ip}` : undefined}>
                                  {fmtTime(h.ts, true)}
                                </td>
                                <td className="td">
                                  {h.username ?? "—"}
                                  {h.role && <div className="text-[11px] text-slate-500">{h.role}</div>}
                                </td>
                                <td className="td">
                                  <span className="mono">{h.action}</span>
                                  {typeof det.from === "string" && (
                                    <div className="text-[11px] text-slate-400">
                                      {String(det.from)} → {String(det.to ?? "")}
                                    </div>
                                  )}
                                  {!h.success && <Badge tone="red">failed</Badge>}
                                </td>
                                <td className="td text-slate-300">{typeof det.note === "string" && det.note ? det.note : "—"}</td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    )}
                  </Panel>
                </div>
              </div>
            </>
          );
        }}
      </QueryState>
    </div>
  );
}
