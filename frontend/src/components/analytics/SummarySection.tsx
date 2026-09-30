import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router-dom";
import { api } from "../../lib/api";
import { fmtDuration, fmtNum, fmtPct } from "../../lib/format";
import { Empty, Panel, QueryState, Stat, SyntheticBanner } from "../ui";
import { WindowNote, winQuery, type Envelope, type Metrics, type Win } from "./common";

interface Summary extends Envelope {
  totals: Metrics | null;
  observations: number;
  plate_read_rate: number | null;
  by_class: Record<string, number>;
  by_camera: Record<string, Metrics>;
}

export function SummarySection({ win, scope, cameraId }: { win: Win; scope?: string; cameraId?: string }) {
  const q = useQuery({
    queryKey: ["analytics", "summary", win, scope, cameraId],
    queryFn: () => api.get<Summary>("/api/analytics/summary", { ...winQuery(win), scope, camera_id: cameraId }),
  });
  return (
    <QueryState q={q}>
      {(s) => (
        <div className="space-y-2">
          <SyntheticBanner info={s} />
          <div className="flex justify-end">
            <WindowNote env={s} />
          </div>
          {!s.has_data ? (
            <Panel title="Summary">
              <Empty />
            </Panel>
          ) : (
            <>
              <div className="grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-8">
                <Stat label="Vehicles counted" value={fmtNum(s.totals?.vehicles)} hint="new tracks in view" />
                <Stat label="Flow" value={fmtNum(s.totals?.rate_per_min, 2)} hint="vehicles / min" />
                <Stat label="Avg speed" value={fmtNum(s.totals?.avg_speed_kmh, 1)} hint="km/h (estimated)" />
                <Stat label="Occupancy" value={fmtPct(s.totals?.occupancy, 1)} hint="road area covered" />
                <Stat label="Queue" value={fmtNum(s.totals?.queue, 2)} hint="stationary vehicles in view" />
                <Stat label="Density" value={fmtNum(s.totals?.density, 2)} hint={`vehicles in frame · max ${fmtNum(s.totals?.max_in_frame)}`} />
                <Stat label="Observations" value={fmtNum(s.observations)} hint="plate/vehicle sightings" />
                <Stat
                  label="Plate read rate"
                  value={fmtPct(s.plate_read_rate, 1)}
                  hint={s.totals ? `coverage ${fmtDuration(s.totals.covered_s)} in ${s.totals.buckets} buckets` : undefined}
                />
              </div>
              <div className="grid gap-2 lg:grid-cols-3">
                <Panel title="Vehicle class mix" subtitle="Observations by detected class">
                  {Object.keys(s.by_class).length === 0 ? <Empty /> : <ClassBars byClass={s.by_class} />}
                </Panel>
                <Panel title="By camera" className="lg:col-span-2" bodyClass="p-0 overflow-x-auto">
                  {Object.keys(s.by_camera).length === 0 ? (
                    <Empty />
                  ) : (
                    <table className="w-full text-[12px]">
                      <thead>
                        <tr>
                          <th className="th">Camera</th>
                          <th className="th text-right">Vehicles</th>
                          <th className="th text-right">Veh/min</th>
                          <th className="th text-right">Avg km/h</th>
                          <th className="th text-right">Occupancy</th>
                          <th className="th text-right">Queue</th>
                          <th className="th text-right">Density</th>
                          <th className="th text-right">Coverage</th>
                        </tr>
                      </thead>
                      <tbody>
                        {Object.entries(s.by_camera).map(([cid, m]) => (
                          <tr key={cid}>
                            <td className="td mono">
                              <Link className="text-cyan-300 hover:underline" to={`/cameras/${cid}`}>
                                {cid}
                              </Link>
                            </td>
                            <td className="td text-right mono">{fmtNum(m.vehicles)}</td>
                            <td className="td text-right mono">{fmtNum(m.rate_per_min, 2)}</td>
                            <td className="td text-right mono">{fmtNum(m.avg_speed_kmh, 1)}</td>
                            <td className="td text-right mono">{fmtPct(m.occupancy, 1)}</td>
                            <td className="td text-right mono">{fmtNum(m.queue, 2)}</td>
                            <td className="td text-right mono">{fmtNum(m.density, 2)}</td>
                            <td className="td text-right mono">{fmtDuration(m.covered_s)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </Panel>
              </div>
            </>
          )}
        </div>
      )}
    </QueryState>
  );
}

function ClassBars({ byClass }: { byClass: Record<string, number> }) {
  const rows = Object.entries(byClass).sort((a, b) => b[1] - a[1]);
  const total = rows.reduce((s, [, n]) => s + n, 0);
  const max = rows[0]?.[1] ?? 1;
  return (
    <ul className="space-y-1.5">
      {rows.map(([cls, n]) => (
        <li key={cls}>
          <div className="flex justify-between text-[12px]">
            <span className="capitalize text-slate-300">{cls}</span>
            <span className="font-mono text-slate-400">
              {fmtNum(n)} · {fmtPct(total ? n / total : null, 0)}
            </span>
          </div>
          <div className="mt-0.5 h-1.5 rounded bg-ink-700">
            <div className="h-1.5 rounded bg-cyan-400" style={{ width: `${(100 * n) / max}%` }} />
          </div>
        </li>
      ))}
    </ul>
  );
}
