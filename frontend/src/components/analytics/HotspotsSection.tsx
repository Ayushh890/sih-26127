/** Cameras ranked by period congestion and alert load, on the network map and as a table. */
import { useMemo, useState } from "react";
import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../lib/api";
import { fmtNum, fmtPct, levelTone, scoreHex } from "../../lib/format";
import { Badge, Empty, Panel, QueryState, SyntheticBanner } from "../ui";
import { NetworkMap, type MapCamera } from "../NetworkMap";
import { WindowNote, winQuery, type Envelope, type Win } from "./common";

interface Hotspot {
  camera_id: string;
  camera_name: string;
  latitude: number | null;
  longitude: number | null;
  score: number;
  level: string;
  peak_queue_score: number;
  alerts: number;
  vehicles: number;
  avg_speed_kmh: number | null;
  queue: number;
  occupancy: number;
}
interface Hotspots extends Envelope {
  hotspots: Hotspot[];
}

export function HotspotsSection({ win, scope }: { win: Win; scope?: string }) {
  const [sel, setSel] = useState<string | null>(null);
  const q = useQuery({
    queryKey: ["analytics", "hotspots", win, scope],
    queryFn: () => api.get<Hotspots>("/api/analytics/hotspots", { ...winQuery(win), scope }),
  });
  const cams = useMemo<MapCamera[]>(
    () =>
      (q.data?.hotspots ?? [])
        .filter((h) => h.latitude != null && h.longitude != null)
        .map((h) => ({
          id: h.camera_id,
          lat: h.latitude as number,
          lon: h.longitude as number,
          color: scoreHex(h.score),
          label: `${h.camera_id} ${fmtNum(h.score, 0)}`,
          tooltip: `${h.camera_name}: ${h.level} ${fmtNum(h.score, 1)}/100 · ${h.alerts} alert(s)`,
          pulse: h.level === "SEVERE",
        })),
    [q.data],
  );
  return (
    <QueryState q={q}>
      {(d) => (
        <div className="space-y-2">
          <SyntheticBanner info={d} />
          <div className="flex justify-between gap-2 text-[11px] text-slate-500">
            <span>Period score = congestion components averaged over the whole period (speed, occupancy, queue). Peak = worst 1-bucket queue score.</span>
            <WindowNote env={d} />
          </div>
          {!d.has_data || d.hotspots.length === 0 ? (
            <Panel title="Hotspots">
              <Empty />
            </Panel>
          ) : (
            <div className="grid gap-2 xl:grid-cols-5">
              <Panel title="Hotspot map" className="xl:col-span-2" bodyClass="p-0">
                <NetworkMap cameras={cams} selectedId={sel} onCameraClick={setSel} className="h-[360px]" />
              </Panel>
              <Panel title="Ranking" className="xl:col-span-3" bodyClass="p-0 overflow-x-auto">
                <table className="w-full text-[12px]">
                  <thead>
                    <tr>
                      <th className="th">#</th>
                      <th className="th">Camera</th>
                      <th className="th">Level</th>
                      <th className="th text-right">Score</th>
                      <th className="th text-right">Peak queue</th>
                      <th className="th text-right">Alerts</th>
                      <th className="th text-right">Vehicles</th>
                      <th className="th text-right">Avg km/h</th>
                      <th className="th text-right">Queue</th>
                      <th className="th text-right">Occupancy</th>
                    </tr>
                  </thead>
                  <tbody>
                    {d.hotspots.map((h, i) => (
                      <tr key={h.camera_id} className={sel === h.camera_id ? "bg-cyan-500/10" : "hover:bg-ink-800/60"} onMouseEnter={() => setSel(h.camera_id)}>
                        <td className="td mono text-slate-500">{i + 1}</td>
                        <td className="td">
                          <Link className="mono text-cyan-300 hover:underline" to={`/cameras/${h.camera_id}`}>
                            {h.camera_id}
                          </Link>
                          <div className="text-[11px] text-slate-500">{h.camera_name}</div>
                        </td>
                        <td className="td">
                          <Badge tone={levelTone(h.level)}>{h.level}</Badge>
                        </td>
                        <td className="td text-right">
                          <span className="font-mono font-semibold" style={{ color: scoreHex(h.score) }}>
                            {fmtNum(h.score, 1)}
                          </span>
                        </td>
                        <td className="td text-right mono">{fmtNum(h.peak_queue_score, 1)}</td>
                        <td className="td text-right mono">
                          {h.alerts > 0 ? (
                            <Link className="text-cyan-300 hover:underline" to={`/alerts?camera_id=${encodeURIComponent(h.camera_id)}`}>
                              {h.alerts}
                            </Link>
                          ) : (
                            0
                          )}
                        </td>
                        <td className="td text-right mono">{fmtNum(h.vehicles)}</td>
                        <td className="td text-right mono">{fmtNum(h.avg_speed_kmh, 1)}</td>
                        <td className="td text-right mono">{fmtNum(h.queue, 2)}</td>
                        <td className="td text-right mono">{fmtPct(h.occupancy, 1)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Panel>
            </div>
          )}
        </div>
      )}
    </QueryState>
  );
}
