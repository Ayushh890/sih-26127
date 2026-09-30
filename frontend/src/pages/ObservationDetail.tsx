/**
 * Sighting detail: every attribute the pipeline recorded, the plate-read votes, the identity
 * match breakdown (fused score, per-cue components, reasons, conflicts) and the evidence package
 * with SHA-256 integrity verification.
 */
import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api, errorText } from "../lib/api";
import { P, useAuth } from "../lib/auth";
import { fmtDuration, fmtKm, fmtNum, fmtPct, fmtTime, TONE_HEX } from "../lib/format";
import { Badge, Empty, ErrorBox, Explanation, KV, Loading, Modal, PageHeader, Panel, SyntheticBanner } from "../components/ui";
import {
  EVIDENCE_ROLES,
  type EvidenceMeta,
  type EvidenceVerify,
  type MatchComponents,
  type ObservationDetailResponse,
  type PlateReadRow,
} from "../components/vehicles/types";
import { Bar, EvidenceImg, MatchBadge, ObsLink, PlateText, SyntheticBadge, VehicleLink } from "../components/vehicles/common";

function bboxText(b?: number[] | null): string {
  if (!b || !b.length) return "—";
  return `[${b.map((x) => (Number.isInteger(x) ? x : x.toFixed(1))).join(", ")}]`;
}

export default function ObservationDetail() {
  const { id = "" } = useParams();
  const { has } = useAuth();
  const q = useQuery({
    queryKey: ["observation", id],
    queryFn: ({ signal }) => api.get<ObservationDetailResponse>(`/api/observations/${encodeURIComponent(id)}`, undefined, signal),
  });
  if (q.isLoading) return <Loading label="Loading sighting…" />;
  if (q.error) return <ErrorBox error={q.error} onRetry={() => q.refetch()} />;
  const o = q.data;
  if (!o) return <Empty>Sighting not found.</Empty>;

  return (
    <div className="flex flex-col gap-2">
      <PageHeader
        title={`Sighting #${o.id}`}
        subtitle={
          <span className="flex flex-wrap items-center gap-2">
            <span className="mono">{o.camera_id}</span>
            {o.camera_name && <span>{o.camera_name}</span>}
            <span className="mono">{fmtTime(o.observed_at, true)}</span>
            <PlateText display={o.plate_display} text={o.plate_text} />
            <SyntheticBadge show={o.is_demo} />
          </span>
        }
        actions={
          <>
            {o.vehicle_code && (
              <Link className="btn" to={`/vehicles/${o.vehicle_code}`}>
                Vehicle {o.vehicle_code} ›
              </Link>
            )}
            <Link className="btn" to="/vehicles/search">
              ‹ Search
            </Link>
          </>
        }
      />
      {o.is_demo && <SyntheticBanner info={{ synthetic: true, notice: "This sighting was produced by the synthetic demo traffic scenario." }} />}

      <div className="grid gap-2 lg:grid-cols-2 2xl:grid-cols-3">
        <SightingPanel o={o} />
        <PlatePanel o={o} />
        <MatchPanel o={o} />
      </div>

      <EvidenceSection o={o} canRead={has(P.EVIDENCE_READ)} />
    </div>
  );
}

function SightingPanel({ o }: { o: ObservationDetailResponse }) {
  const dwell =
    o.first_seen_at && o.last_seen_at ? (new Date(o.last_seen_at).getTime() - new Date(o.first_seen_at).getTime()) / 1000 : null;
  const rgb = o.color_rgb && o.color_rgb.length >= 3 ? `rgb(${o.color_rgb[0]}, ${o.color_rgb[1]}, ${o.color_rgb[2]})` : null;
  return (
    <Panel title="Sighting">
      <KV
        rows={[
          [
            "Camera",
            <span>
              <Link to={`/cameras/${o.camera_id}`} className="mono text-cyan-300 hover:underline">
                {o.camera_id}
              </Link>{" "}
              {o.camera_name}
            </span>,
          ],
          ["Observed at", <span className="mono">{fmtTime(o.observed_at, true)}</span>],
          [
            "In view",
            <span className="mono">
              {fmtTime(o.first_seen_at)} → {fmtTime(o.last_seen_at)} ({fmtDuration(dwell)})
            </span>,
          ],
          [
            "Class",
            <span>
              {o.vehicle_class ?? "—"} <span className="text-slate-500">({fmtPct(o.class_confidence)} conf.)</span>
            </span>,
          ],
          [
            "Colour",
            <span className="inline-flex items-center gap-1.5">
              {rgb && <span className="inline-block h-3 w-3 rounded-sm border border-ink-600" style={{ background: rgb }} title={`measured ${rgb}`} />}
              {o.vehicle_color ?? "—"}
              {rgb && <span className="mono text-[11px] text-slate-500">{rgb}</span>}
            </span>,
          ],
          [
            "Speed",
            <span className="mono">
              {fmtNum(o.speed_kmh, 1, "km/h")}
              {o.speed_kmh != null && o.speed_is_estimate && <span className="ml-1 text-[11px] text-slate-500">(estimate from calibration)</span>}
            </span>,
          ],
          ["Direction", `${o.direction ?? "—"}${o.heading_deg != null ? ` (${o.heading_deg.toFixed(0)}°)` : ""}`],
          ["Motion", o.motion ?? "—"],
          ["Lane", o.lane ?? "—"],
          ["BBox (px)", <span className="mono text-[11px]">{bboxText(o.bbox)}</span>],
          [
            "Track",
            o.track ? (
              <span className="mono text-[11px]">
                {o.track.frames} frames · {fmtTime(o.track.started_at)} → {fmtTime(o.track.ended_at)}
                {o.track.path ? ` · ${o.track.path.length} path samples` : ""}
              </span>
            ) : (
              "—"
            ),
          ],
          ["Source", o.is_demo ? <Badge tone="violet">Synthetic</Badge> : <Badge tone="green">Live</Badge>],
        ]}
      />
    </Panel>
  );
}

function PlatePanel({ o }: { o: ObservationDetailResponse }) {
  return (
    <Panel title="Plate">
      <div className="mb-2 flex flex-wrap items-center gap-2">
        <PlateText display={o.plate_display} text={o.plate_text} className="text-[15px]" />
        {o.plate_valid === true && <Badge tone="green">valid format</Badge>}
        {o.plate_valid === false && <Badge tone="orange">invalid format</Badge>}
      </div>
      <KV
        rows={[
          ["Normalised", <span className="mono">{o.plate_text ?? "—"}</span>],
          ["Raw (best read)", <span className="mono">{o.plate_raw ?? "—"}</span>],
          [
            "Confidence",
            <span className="inline-flex items-center gap-1.5">
              <Bar value={o.plate_confidence} />
              <span className="mono">{fmtPct(o.plate_confidence, 1)}</span>
            </span>,
          ],
          ["Votes", <span className="mono">{o.plate_votes ?? "—"} vote(s)</span>],
        ]}
      />
      <div className="label mt-2">Individual OCR reads ({o.plate_reads.length})</div>
      {o.plate_reads.length === 0 ? (
        <div className="text-[12px] text-slate-500">No per-frame plate reads stored for this sighting.</div>
      ) : (
        <div className="max-h-64 overflow-auto">
          <table className="w-full text-[11px]">
            <thead>
              <tr>
                <th className="th">Time</th>
                <th className="th">Raw → normalised</th>
                <th className="th">Conf.</th>
                <th className="th">Per-char</th>
                <th className="th">Corrections</th>
              </tr>
            </thead>
            <tbody>
              {o.plate_reads.map((r, i) => (
                <PlateReadTr key={i} r={r} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Panel>
  );
}

function PlateReadTr({ r }: { r: PlateReadRow }) {
  return (
    <tr>
      <td className="td mono whitespace-nowrap">{fmtTime(r.ts)}</td>
      <td className="td mono whitespace-nowrap">
        {r.raw_text}
        {r.normalized_text && r.normalized_text !== r.raw_text && <span className="text-slate-400"> → {r.normalized_text}</span>}
        {!r.is_valid_format && (
          <Badge tone="orange" className="ml-1">
            invalid
          </Badge>
        )}
      </td>
      <td className="td mono">{fmtPct(r.confidence, 1)}</td>
      <td className="td">
        <span className="flex h-4 items-end gap-px" title={(r.char_confidences ?? []).map((c) => c.toFixed(2)).join(" ")}>
          {(r.char_confidences ?? []).map((c, j) => (
            <span key={j} className="w-1 rounded-sm" style={{ height: `${Math.max(10, c * 100)}%`, background: c >= 0.9 ? TONE_HEX.green : c >= 0.7 ? TONE_HEX.yellow : TONE_HEX.red }} />
          ))}
        </span>
      </td>
      <td className="td">
        {r.corrections && r.corrections.length ? (
          r.corrections.map((c, j) => (
            <div key={j} className="mono whitespace-nowrap text-amber-200">
              pos {c.position}: {c.from}→{c.to} <span className="text-slate-500">{c.reason}</span>
            </div>
          ))
        ) : (
          <span className="text-slate-500">—</span>
        )}
      </td>
    </tr>
  );
}

const COMPONENT_LABEL: Record<string, string> = {
  plate: "Plate",
  time: "Travel time",
  route: "Route",
  appearance: "Appearance",
  attributes: "Class / colour",
};

function MatchPanel({ o }: { o: ObservationDetailResponse }) {
  const mc: MatchComponents = o.match_components ?? {};
  const scores = mc.scores ?? {};
  const weights = mc.weights ?? {};
  const keys = Array.from(new Set([...Object.keys(weights), ...Object.keys(scores).filter((k) => k in COMPONENT_LABEL)]));
  const conflicts = mc.conflicts ?? [];
  return (
    <Panel title="Identity match">
      <KV
        rows={[
          ["Global vehicle", <VehicleLink code={o.vehicle_code} />],
          ["Match", <MatchBadge level={o.match_confidence_level} score={o.match_score} />],
          ["Previous sighting", o.previous ? <PreviousLink o={o} /> : <span className="text-slate-500">none (first sighting of this vehicle)</span>],
          ["Candidates considered", <span className="mono">{mc.considered ?? "—"}</span>],
          ["Runner-up score", <span className="mono">{mc.runner_up != null ? mc.runner_up.toFixed(4) : "—"}</span>],
          ["New journey", mc.new_journey == null ? "—" : mc.new_journey ? "yes" : "no"],
          ["Time since previous", <span className="mono">{fmtDuration(mc.dt_s)}</span>],
          [
            "Road path",
            mc.path ? (
              <span className="mono text-[11px]">
                {mc.path.cameras.join(" → ")} · {fmtKm(mc.path.distance_m)} · min {fmtDuration(mc.path.min_travel_s)} · typical {fmtDuration(mc.path.typical_travel_s)} · {mc.path.hops} hop(s)
              </span>
            ) : (
              "—"
            ),
          ],
        ]}
      />
      {keys.length > 0 && (
        <div className="mt-2">
          <div className="label">
            Score components {mc.fused != null && <span className="normal-case text-slate-300">· fused {mc.fused.toFixed(4)}</span>}
          </div>
          <table className="w-full text-[11px]">
            <thead>
              <tr>
                <th className="th">Cue</th>
                <th className="th">Score</th>
                <th className="th">Weight</th>
                <th className="th">Contribution</th>
              </tr>
            </thead>
            <tbody>
              {keys.map((k) => {
                const s = scores[k];
                const w = weights[k];
                return (
                  <tr key={k}>
                    <td className="td">{COMPONENT_LABEL[k] ?? k}</td>
                    <td className="td whitespace-nowrap">
                      <Bar value={s} color={s == null ? undefined : s >= 0.8 ? TONE_HEX.green : s >= 0.5 ? TONE_HEX.yellow : TONE_HEX.orange} />{" "}
                      <span className="mono">{s != null ? s.toFixed(3) : "n/a"}</span>
                      {k === "appearance" && scores.appearance_cosine != null && (
                        <div className="mono text-slate-500">cosine {scores.appearance_cosine.toFixed(4)}</div>
                      )}
                    </td>
                    <td className="td mono">{w != null ? w.toFixed(2) : "—"}</td>
                    <td className="td mono">{s != null && w != null ? (s * w).toFixed(3) : "—"}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      <div className="mt-2">
        <div className="label">Reasons</div>
        {o.match_reasons.length ? <Explanation lines={o.match_reasons} /> : <div className="text-[12px] text-slate-500">No match explanation recorded.</div>}
      </div>
      {conflicts.length > 0 && (
        <div className="mt-2 rounded border border-rose-700/60 bg-rose-950/30 p-1.5">
          <div className="label !text-rose-300">Same-plate conflicts (possible cloned plate)</div>
          {conflicts.map((c, i) => (
            <div key={i} className="text-[11px] text-rose-200">
              <VehicleLink code={c.vehicle_code} /> at <span className="mono">{c.camera_id}</span> {fmtTime(c.observed_at)} (<ObsLink id={c.observation_id} />) · Δt{" "}
              {fmtDuration(c.dt_s)} vs min {fmtDuration(c.min_travel_s)} over {fmtKm(c.distance_m)} — {c.reason}
            </div>
          ))}
        </div>
      )}
    </Panel>
  );
}

function PreviousLink({ o }: { o: ObservationDetailResponse }) {
  const p = o.previous!;
  return (
    <span className="text-[12px]">
      <ObsLink id={p.id} /> <span className="mono">{p.camera_id}</span> {fmtTime(p.observed_at)} <PlateText display={p.plate_display} text={p.plate_text} />
    </span>
  );
}

// ---------------------------------------------------------------------------------------------
// Evidence
// ---------------------------------------------------------------------------------------------

function EvidenceSection({ o, canRead }: { o: ObservationDetailResponse; canRead: boolean }) {
  if (!canRead || !o.evidence_access) {
    return (
      <Panel title="Evidence">
        <Empty>Your role does not have access to evidence ({P.EVIDENCE_READ}).</Empty>
      </Panel>
    );
  }
  if (!o.evidence_id) {
    return (
      <Panel title="Evidence">
        <Empty>No evidence package was stored for this sighting.</Empty>
      </Panel>
    );
  }
  return <EvidencePanel id={o.evidence_id} />;
}

function EvidencePanel({ id }: { id: string }) {
  const q = useQuery({ queryKey: ["evidence", id], queryFn: ({ signal }) => api.get<EvidenceMeta>(`/api/evidence/${encodeURIComponent(id)}`, undefined, signal) });
  const verify = useMutation({ mutationFn: () => api.post<EvidenceVerify>(`/api/evidence/${encodeURIComponent(id)}/verify`) });
  const [zoom, setZoom] = useState<string | null>(null);
  const v = verify.data;
  return (
    <Panel
      title="Evidence package"
      subtitle={<span className="mono">{id}</span>}
      actions={
        <button className="btn btn-primary" onClick={() => verify.mutate()} disabled={verify.isPending || !q.data}>
          {verify.isPending && <span className="h-2.5 w-2.5 animate-spin rounded-full border border-slate-300 border-t-transparent" />}
          Verify integrity
        </button>
      }
    >
      {q.isLoading ? (
        <Loading />
      ) : q.error ? (
        <ErrorBox error={q.error} onRetry={() => q.refetch()} />
      ) : q.data ? (
        <div className="flex flex-col gap-2">
          {verify.error ? <div className="rounded border border-rose-700/60 bg-rose-950/40 px-2 py-1 text-[12px] text-rose-200">Verification failed: {errorText(verify.error)}</div> : null}
          {v && (
            <div className={`rounded border px-2 py-1 text-[12px] ${v.valid ? "border-emerald-600/50 bg-emerald-950/30 text-emerald-200" : "border-rose-700/60 bg-rose-950/40 text-rose-200"}`}>
              <span className="font-semibold">{v.valid ? "Integrity verified" : "Integrity check FAILED"}</span> — files re-hashed with SHA-256; manifest{" "}
              {v.manifest_valid == null ? "not checked (no manifest hash)" : v.manifest_valid ? "matches" : "does NOT match"}.
            </div>
          )}
          <div className="grid grid-cols-2 gap-2 md:grid-cols-3 xl:grid-cols-5">
            {EVIDENCE_ROLES.filter((r) => q.data!.files[r]).map((role) => {
              const f = q.data!.files[role];
              const res = v?.files[role];
              return (
                <figure key={role} className="flex flex-col gap-1 rounded border border-ink-700 bg-ink-850 p-1.5">
                  <button type="button" onClick={() => setZoom(role)} title="Enlarge" className="block">
                    <EvidenceImg evidenceId={id} role={role} className={`w-full ${role === "plate" ? "h-24" : "h-36"}`} />
                  </button>
                  <figcaption className="text-[11px]">
                    <div className="flex items-center justify-between">
                      <span className="font-semibold uppercase tracking-wide text-slate-300">{role}</span>
                      {res && (
                        <Badge tone={res.ok ? "green" : "red"} title={res.error ?? (res.ok ? "hash matches" : `expected ${res.expected}, got ${res.actual}`)}>
                          {res.ok ? "ok" : res.error ?? "mismatch"}
                        </Badge>
                      )}
                    </div>
                    <div className="text-slate-500">
                      {f.width && f.height ? `${f.width}×${f.height}` : ""} {f.bytes != null ? `· ${fmtNum(f.bytes / 1024, 1)} KB` : ""}
                    </div>
                    <div className="mono break-all text-[10px] text-slate-400" title="SHA-256">
                      {f.sha256}
                    </div>
                    {res && !res.ok && res.actual && <div className="mono break-all text-[10px] text-rose-300">actual {res.actual}</div>}
                  </figcaption>
                </figure>
              );
            })}
          </div>
          {Object.keys(q.data.files).length === 0 && <Empty>The evidence package contains no files.</Empty>}
          <KV
            rows={[
              ["Captured", <span className="mono">{fmtTime(q.data.ts, true)}</span>],
              ["Camera", <span className="mono">{q.data.camera_id ?? "—"}</span>],
              [
                "OCR",
                <span className="inline-flex items-center gap-2">
                  <PlateText text={q.data.ocr_text} /> <span className="mono text-slate-400">{fmtPct(q.data.ocr_confidence, 1)}</span>
                </span>,
              ],
              ["Vehicle bbox", <span className="mono text-[11px]">{bboxText(q.data.bbox)}</span>],
              ["Plate bbox", <span className="mono text-[11px]">{bboxText(q.data.plate_bbox)}</span>],
              ["Manifest SHA-256", <span className="mono break-all text-[11px]">{q.data.manifest_sha256 ?? "—"}</span>],
              ["Encrypted at rest", q.data.encrypted ? "yes" : "no"],
            ]}
          />
          {zoom && (
            <Modal title={`Evidence · ${zoom}`} onClose={() => setZoom(null)} wide>
              <EvidenceImg evidenceId={id} role={zoom} className="mx-auto max-h-[75vh] w-auto" />
              <div className="mono mt-1 break-all text-[11px] text-slate-400">SHA-256 {q.data.files[zoom]?.sha256}</div>
            </Modal>
          )}
        </div>
      ) : null}
    </Panel>
  );
}

