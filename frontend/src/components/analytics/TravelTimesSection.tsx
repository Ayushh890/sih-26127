/** Segment travel times (median of linked vehicles) against their baselines, with anomaly flags. */
import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { fmtDuration, fmtNum } from "../../lib/format";
import { Badge, Empty, Panel, QueryState, SyntheticBanner } from "../ui";
import { WindowNote, type Envelope } from "./common";

interface Segment {
  from: string;
  to: string;
  median_s: number;
  baseline_s: number | null;
  baseline_source: string | null;
  ratio: number | null;
  samples: number;
  anomalous: boolean;
  explanation: string;
}
interface TravelTimes extends Envelope {
  anomaly_ratio: number;
  min_samples_for_alert: number;
  segments: Segment[];
}

/** window_s accepted by the endpoint: 60 … 86400 s */
export const TT_WINDOWS = [
  { v: 900, label: "Last 15 min" },
  { v: 3600, label: "Last 1 h" },
  { v: 21600, label: "Last 6 h" },
  { v: 86400, label: "Last 24 h" },
];

export function TravelTimesSection({ scope, defaultWindow }: { scope?: string; defaultWindow: number }) {
  const [windowS, setWindowS] = useState<number | null>(null);
  const w = windowS ?? defaultWindow;
  const q = useQuery({
    queryKey: ["analytics", "travel-times", w, scope],
    queryFn: () => api.get<TravelTimes>("/api/analytics/travel-times", { window_s: w, scope }),
  });
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <label className="block">
          <span className="label">Window (ending now)</span>
          <select className="input w-auto" value={w} onChange={(e) => setWindowS(Number(e.target.value))}>
            {TT_WINDOWS.map((o) => (
              <option key={o.v} value={o.v}>
                {o.label}
              </option>
            ))}
            {!TT_WINDOWS.some((o) => o.v === w) && <option value={w}>{fmtDuration(w)}</option>}
          </select>
        </label>
        <WindowNote env={q.data} />
      </div>
      <QueryState q={q}>
        {(t) => (
          <>
            <SyntheticBanner info={t} />
            <div className="text-[11px] text-slate-500">
              A segment is anomalous when its median travel time is ≥ <span className="font-mono text-slate-300">{t.anomaly_ratio}×</span> its baseline. The
              TRAVEL_TIME_ANOMALY rule additionally needs ≥ <span className="font-mono text-slate-300">{t.min_samples_for_alert}</span> linked vehicles before it
              raises an alert; segments with fewer samples are shown but flagged as low-evidence.
            </div>
            {!t.has_data || t.segments.length === 0 ? (
              <Panel title="Travel times">
                <Empty />
              </Panel>
            ) : (
              <Panel title="Segments" bodyClass="p-0 overflow-x-auto">
                <table className="w-full min-w-[900px] text-[12px]">
                  <thead>
                    <tr>
                      <th className="th">Segment</th>
                      <th className="th text-right">Median</th>
                      <th className="th text-right">Baseline</th>
                      <th className="th">Ratio</th>
                      <th className="th text-right">Samples</th>
                      <th className="th">State</th>
                      <th className="th">Explanation</th>
                    </tr>
                  </thead>
                  <tbody>
                    {[...t.segments]
                      .sort((a, b) => (b.ratio ?? 0) - (a.ratio ?? 0))
                      .map((s) => {
                        const lowEvidence = s.samples < t.min_samples_for_alert;
                        const pct = s.ratio == null ? 0 : Math.min(100, (s.ratio / (t.anomaly_ratio * 1.5)) * 100);
                        const thr = (1 / 1.5) * 100;
                        return (
                          <tr key={`${s.from}-${s.to}`} className={s.anomalous ? "bg-rose-500/5" : undefined}>
                            <td className="td mono whitespace-nowrap">
                              <Link className="text-cyan-300 hover:underline" to={`/cameras/${s.from}`}>
                                {s.from}
                              </Link>{" "}
                              →{" "}
                              <Link className="text-cyan-300 hover:underline" to={`/cameras/${s.to}`}>
                                {s.to}
                              </Link>
                            </td>
                            <td className="td text-right mono">{fmtDuration(s.median_s)}</td>
                            <td className="td text-right mono">
                              {fmtDuration(s.baseline_s)}
                              {s.baseline_source && <div className="text-[10px] text-slate-500">{s.baseline_source}</div>}
                            </td>
                            <td className="td w-48">
                              <div className="flex items-center gap-1.5">
                                <span className="w-10 text-right font-mono">{s.ratio == null ? "—" : `${fmtNum(s.ratio, 2)}×`}</span>
                                <div className="relative h-1.5 flex-1 rounded bg-ink-700" title={`threshold ${t.anomaly_ratio}×`}>
                                  <div className="h-1.5 rounded" style={{ width: `${pct}%`, background: s.anomalous ? "#fb7185" : "#22d3ee" }} />
                                  <div className="absolute -top-0.5 h-2.5 w-px bg-slate-300" style={{ left: `${thr}%` }} />
                                </div>
                              </div>
                            </td>
                            <td className="td text-right mono">{s.samples}</td>
                            <td className="td">
                              <div className="flex flex-wrap gap-1">
                                {s.anomalous ? <Badge tone="red">Anomalous</Badge> : <Badge tone="green">Normal</Badge>}
                                {lowEvidence && (
                                  <Badge tone="slate" title={`Fewer than ${t.min_samples_for_alert} samples: not enough evidence to raise an alert`}>
                                    Low evidence
                                  </Badge>
                                )}
                              </div>
                            </td>
                            <td className="td text-[11px] text-slate-400">{s.explanation}</td>
                          </tr>
                        );
                      })}
                  </tbody>
                </table>
              </Panel>
            )}
          </>
        )}
      </QueryState>
    </div>
  );
}
