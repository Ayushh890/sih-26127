/**
 * Origin–destination matrix (first → last camera of each journey). Small cells are
 * suppressed by the backend. Drilling into a cell lists individual journeys and is only
 * offered to users with analytics:od_individual (the backend audits every drill-down).
 */
import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { P, useAuth } from "../../lib/auth";
import { fmtDuration, fmtNum, fmtTime } from "../../lib/format";
import { Badge, Empty, ErrorBox, Loading, Modal, Panel, QueryState, SyntheticBanner } from "../ui";
import { WindowNote, winQuery, type Envelope, type Win } from "./common";

interface Cell {
  origin: string;
  destination: string;
  count: number;
  median_travel_s: number;
}
interface Od extends Envelope {
  cells: Cell[];
  suppressed_journeys: number;
  min_count: number;
  cameras: string[];
  total_journeys: number;
  camera_names: Record<string, string | null>;
  drilldown_allowed: boolean;
}
interface Journey {
  global_vehicle_id: number;
  journey_index: number;
  start: string;
  end: string;
  cameras: string[];
  vehicle_code: string | null;
  plate_text: string | null;
}
interface OdVehicles extends Envelope {
  origin: string;
  destination: string;
  journeys: Journey[];
}

export function OdSection({ win, scope }: { win: Win; scope?: string }) {
  const { has } = useAuth();
  const [cell, setCell] = useState<{ o: string; d: string; since?: string; until?: string } | null>(null);
  const q = useQuery({
    queryKey: ["analytics", "od", win, scope],
    queryFn: () => api.get<Od>("/api/analytics/od-matrix", { ...winQuery(win), scope }),
  });
  return (
    <QueryState q={q}>
      {(m) => {
        const canDrill = m.drilldown_allowed && has(P.OD_INDIVIDUAL);
        const byKey = new Map(m.cells.map((c) => [`${c.origin}|${c.destination}`, c]));
        const max = Math.max(1, ...m.cells.map((c) => c.count));
        const origins = m.cameras.filter((c) => m.cells.some((x) => x.origin === c));
        const dests = m.cameras.filter((c) => m.cells.some((x) => x.destination === c));
        return (
          <div className="space-y-2">
            <SyntheticBanner info={m} />
            <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-slate-400">
              <span>
                <span className="font-mono text-slate-200">{fmtNum(m.total_journeys)}</span> multi-camera journeys ·{" "}
                <span className="font-mono text-slate-200">{fmtNum(m.suppressed_journeys)}</span> in cells below the privacy minimum of{" "}
                <span className="font-mono text-slate-200">{m.min_count}</span> (suppressed)
                {canDrill ? " · click a cell to list its journeys (audited)" : " · individual journeys are not available to your role"}
              </span>
              <WindowNote env={m} />
            </div>
            {!m.has_data || m.cells.length === 0 ? (
              <Panel title="Origin–destination matrix">
                <Empty />
              </Panel>
            ) : (
              <Panel title="Origin–destination matrix" subtitle="rows: origin (first camera) · columns: destination (last camera) · cell: journeys / median travel time" bodyClass="p-2 overflow-x-auto">
                <table className="border-separate border-spacing-0.5 text-[11px]">
                  <thead>
                    <tr>
                      <th className="px-1 py-1 text-left text-slate-500">O \ D</th>
                      {dests.map((d) => (
                        <th key={d} className="px-1 py-1 font-mono text-slate-400" title={m.camera_names[d] ?? d}>
                          {d}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {origins.map((o) => (
                      <tr key={o}>
                        <th className="px-1 py-1 text-left font-mono text-slate-400" title={m.camera_names[o] ?? o}>
                          {o}
                        </th>
                        {dests.map((d) => {
                          const c = byKey.get(`${o}|${d}`);
                          if (!c)
                            return (
                              <td key={d} className="h-11 w-20 rounded bg-ink-850 text-center text-slate-600" title={o === d ? "same camera" : "no journeys or suppressed"}>
                                ·
                              </td>
                            );
                          const a = 0.12 + 0.78 * (c.count / max);
                          const content = (
                            <>
                              <div className="font-mono text-[13px] font-semibold">{c.count}</div>
                              <div className="font-mono text-[10px] opacity-80">{fmtDuration(c.median_travel_s)}</div>
                            </>
                          );
                          const style = { background: `rgba(34, 211, 238, ${a.toFixed(2)})`, color: a > 0.55 ? "#082f49" : "#e2e8f0" };
                          const tip = `${o} ${m.camera_names[o] ?? ""} → ${d} ${m.camera_names[d] ?? ""}: ${c.count} journeys, median ${fmtDuration(c.median_travel_s)}`;
                          return (
                            <td key={d} className="h-11 w-20 rounded p-0 text-center" style={style} title={tip}>
                              {canDrill ? (
                                <button
                                  type="button"
                                  className="h-full w-full rounded hover:outline hover:outline-2 hover:outline-slate-200"
                                  aria-label={`Journeys ${o} to ${d}`}
                                  onClick={() => setCell({ o, d, since: m.since, until: m.until })}
                                >
                                  {content}
                                </button>
                              ) : (
                                content
                              )}
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
                <div className="mt-1.5 flex items-center gap-1.5 text-[11px] text-slate-500">
                  <span>fewer</span>
                  <span className="h-2 w-24 rounded" style={{ background: "linear-gradient(90deg, rgba(34,211,238,0.12), rgba(34,211,238,0.9))" }} />
                  <span>more journeys (max {max})</span>
                </div>
              </Panel>
            )}
            {cell && canDrill && <OdDrill cell={cell} win={win} scope={scope} names={m.camera_names} onClose={() => setCell(null)} />}
          </div>
        );
      }}
    </QueryState>
  );
}

function OdDrill({
  cell,
  win,
  scope,
  names,
  onClose,
}: {
  cell: { o: string; d: string; since?: string; until?: string };
  win: Win;
  scope?: string;
  names: Record<string, string | null>;
  onClose: () => void;
}) {
  const q = useQuery({
    queryKey: ["analytics", "od-vehicles", cell, scope],
    queryFn: () =>
      api.get<OdVehicles>("/api/analytics/od-matrix/vehicles", {
        origin: cell.o,
        destination: cell.d,
        period: win.period,
        since: cell.since,
        until: cell.until,
        scope,
      }),
    staleTime: 0,
  });
  return (
    <Modal title={`Journeys ${cell.o} → ${cell.d}`} onClose={onClose} wide>
      <div className="mb-1.5 flex flex-wrap items-center gap-2 text-[11px] text-slate-400">
        <Badge tone="orange">Individual data · audited</Badge>
        <span>
          {names[cell.o] ?? cell.o} → {names[cell.d] ?? cell.d}
        </span>
        <WindowNote env={q.data} />
      </div>
      {q.isLoading ? (
        <Loading />
      ) : q.error ? (
        <ErrorBox error={q.error} onRetry={() => q.refetch()} />
      ) : !q.data || !q.data.has_data || q.data.journeys.length === 0 ? (
        <Empty />
      ) : (
        <div className="max-h-[60vh] overflow-auto">
          <SyntheticBanner info={q.data} />
          <table className="mt-1 w-full text-[12px]">
            <thead>
              <tr>
                <th className="th">Vehicle</th>
                <th className="th">Plate</th>
                <th className="th">Start</th>
                <th className="th">End</th>
                <th className="th text-right">Duration</th>
                <th className="th">Cameras</th>
              </tr>
            </thead>
            <tbody>
              {q.data.journeys.map((j) => (
                <tr key={`${j.global_vehicle_id}-${j.journey_index}`}>
                  <td className="td">
                    {j.vehicle_code ? (
                      <Link className="mono text-cyan-300 hover:underline" to={`/vehicles/${j.vehicle_code}`}>
                        {j.vehicle_code}
                      </Link>
                    ) : (
                      <span className="mono">id {j.global_vehicle_id}</span>
                    )}
                  </td>
                  <td className="td mono">{j.plate_text ?? <span className="text-slate-500">unread</span>}</td>
                  <td className="td mono whitespace-nowrap">{fmtTime(j.start, true)}</td>
                  <td className="td mono whitespace-nowrap">{fmtTime(j.end, true)}</td>
                  <td className="td text-right mono">{fmtDuration((new Date(j.end).getTime() - new Date(j.start).getTime()) / 1000)}</td>
                  <td className="td mono text-[11px]">{j.cameras.join(" → ")}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Modal>
  );
}
