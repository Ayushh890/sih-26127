/** Explainable congestion score per camera for the current analysis window (live-refreshed). */
import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { useConnState, useLiveEvent } from "../../lib/ws";
import { fmtAgo, fmtDuration, fmtNum, fmtPct, levelTone, scoreHex } from "../../lib/format";
import { Badge, Empty, Explanation, KV, Panel, QueryState, SyntheticBanner } from "../ui";
import { ComponentBars, type CongestionComponent } from "./ComponentBars";
import { WindowNote, type Envelope, type Metrics } from "./common";

export interface CongestionCamera {
  camera_id: string;
  camera_name: string;
  latitude: number | null;
  longitude: number | null;
  window_s: number;
  status: string;
  is_demo: boolean;
  score: number | null;
  level: string;
  components: Record<string, CongestionComponent>;
  weights?: Record<string, number>;
  explanation: string[];
  metrics: Metrics | null;
}
interface Congestion extends Envelope {
  cameras: CongestionCamera[];
  thresholds: Record<string, number>;
  weights: Record<string, number>;
  levels: string[];
}

export function CongestionSection({ scope }: { scope?: string }) {
  const qc = useQueryClient();
  const conn = useConnState();
  const [liveAt, setLiveAt] = useState<number | null>(null);
  const key = ["analytics", "congestion", scope];
  const q = useQuery({ queryKey: key, queryFn: () => api.get<Congestion>("/api/analytics/congestion", { scope }) });
  useLiveEvent("analytics_updated", () => {
    setLiveAt(Date.now());
    void qc.invalidateQueries({ queryKey: key });
  });

  return (
    <QueryState q={q}>
      {(c) => {
        const scored = c.cameras.filter((x) => x.score != null).sort((a, b) => (b.score ?? 0) - (a.score ?? 0));
        const unscored = c.cameras.filter((x) => x.score == null);
        return (
          <div className="space-y-2">
            <SyntheticBanner info={c} />
            <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-slate-400">
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="label mb-0">Levels</span>
                <Thresholds levels={c.levels} thresholds={c.thresholds} />
                <span className="ml-2 label mb-0">Weights</span>
                <span className="font-mono">
                  {Object.entries(c.weights)
                    .map(([k, v]) => `${k} ${v}`)
                    .join(" · ")}
                </span>
              </div>
              <div className="flex items-center gap-2">
                <WindowNote env={c} />
                <Badge tone={conn === "open" ? "green" : "orange"} title="Refreshed on every analytics_updated event">
                  {conn === "open" ? (liveAt ? `live · ${fmtAgo(liveAt)}` : "live") : `not live (${conn})`}
                </Badge>
              </div>
            </div>
            <div className="text-[11px] text-slate-500">
              Score = weighted mean of the components that had data in the window (missing components are dropped and weights renormalised). Each bar shows
              the component's normalised value, its weight and the points it adds to the score.
            </div>
            {!c.has_data ? (
              <Panel title="Congestion">
                <Empty />
                {unscored.length > 0 && (
                  <ul className="border-t border-ink-700 px-2 py-1.5 text-[11px] text-slate-500">
                    {unscored.map((u) => (
                      <li key={u.camera_id}>
                        <span className="font-mono">{u.camera_id}</span> {u.camera_name}: {u.explanation.join("; ")}
                      </li>
                    ))}
                  </ul>
                )}
              </Panel>
            ) : (
              <>
                <div className="grid gap-2 md:grid-cols-2 xl:grid-cols-3">
                  {scored.map((cam) => (
                    <CameraCard key={cam.camera_id} cam={cam} />
                  ))}
                </div>
                {unscored.length > 0 && (
                  <Panel title="Cameras without data in this window">
                    <ul className="text-[12px] text-slate-400">
                      {unscored.map((u) => (
                        <li key={u.camera_id}>
                          <Link className="font-mono text-cyan-300 hover:underline" to={`/cameras/${u.camera_id}`}>
                            {u.camera_id}
                          </Link>{" "}
                          {u.camera_name} — {u.explanation.join("; ")}
                        </li>
                      ))}
                    </ul>
                  </Panel>
                )}
              </>
            )}
          </div>
        );
      }}
    </QueryState>
  );
}

function Thresholds({ levels, thresholds }: { levels: string[]; thresholds: Record<string, number> }) {
  return (
    <span className="inline-flex flex-wrap items-center gap-1">
      {levels.map((l, i) => {
        const lo = i === 0 ? 0 : thresholds[l.toLowerCase()];
        const next = levels[i + 1];
        const hi = next ? thresholds[next.toLowerCase()] : 100;
        return (
          <Badge key={l} tone={levelTone(l)} title={`${l}: score ${lo ?? "?"}–${hi ?? "?"}`}>
            {l} <span className="font-mono">{lo ?? "?"}–{hi ?? "?"}</span>
          </Badge>
        );
      })}
    </span>
  );
}

function CameraCard({ cam }: { cam: CongestionCamera }) {
  return (
    <Panel
      title={
        <span className="flex items-center gap-1.5">
          <Link className="font-mono text-cyan-300 hover:underline" to={`/cameras/${cam.camera_id}`}>
            {cam.camera_id}
          </Link>
          <span className="truncate normal-case tracking-normal text-slate-300">{cam.camera_name}</span>
        </span>
      }
      subtitle={`camera ${cam.status} · window ${fmtDuration(cam.window_s)}`}
      actions={
        <>
          {cam.is_demo && <Badge tone="violet">Synthetic</Badge>}
          <Badge tone={levelTone(cam.level)}>{cam.level}</Badge>
        </>
      }
    >
      <div className="flex items-start gap-3">
        <div className="w-20 shrink-0 text-center">
          <div className="font-mono text-3xl font-semibold" style={{ color: scoreHex(cam.score) }}>
            {fmtNum(cam.score, 0)}
          </div>
          <div className="text-[11px] text-slate-500">/ 100</div>
        </div>
        <div className="min-w-0 flex-1">
          <ComponentBars components={cam.components} weights={cam.weights} />
        </div>
      </div>
      <div className="mt-2 border-t border-ink-700 pt-1.5">
        <Explanation lines={cam.explanation} />
      </div>
      {cam.metrics && (
        <div className="mt-1.5 border-t border-ink-700 pt-1.5">
          <KV
            rows={[
              ["Vehicles", `${fmtNum(cam.metrics.vehicles)} (${fmtNum(cam.metrics.rate_per_min, 2)}/min)`],
              ["Avg speed", fmtNum(cam.metrics.avg_speed_kmh, 1, "km/h")],
              ["Occupancy / queue", `${fmtPct(cam.metrics.occupancy, 1)} / ${fmtNum(cam.metrics.queue, 2)}`],
              ["Data coverage", `${fmtDuration(cam.metrics.covered_s)} of ${fmtDuration(cam.window_s)} (${cam.metrics.buckets} buckets)`],
            ]}
          />
        </div>
      )}
    </Panel>
  );
}
