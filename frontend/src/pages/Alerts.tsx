/**
 * Alert queue. Alerts are raised only by the explicit rules listed at /api/alerts/rules;
 * the list is filtered server-side (status / severity / type / camera / since), paged, and
 * kept current from the alert_created / alert_updated WebSocket events.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { P, useAuth } from "../lib/auth";
import { useScope } from "../lib/scope";
import { useConnState, useLiveEvent } from "../lib/ws";
import { alertStatusTone, fmtAgo, fmtPct, fmtTime, fromLocalInput, severityTone, toLocalInput } from "../lib/format";
import { Badge, Empty, ErrorBox, Loading, PageHeader, Panel, SyntheticBanner } from "../components/ui";
import { AlertRowActions } from "../components/alerts/AlertActions";
import { ALERT_STATUSES, SEVERITIES, reopenedCount, type Alert, type AlertList, type AlertRules } from "../components/alerts/types";

const PAGE_SIZES = [25, 50, 100, 200];

interface CameraLite {
  id: string;
  name: string;
}

function matchesFilters(a: Alert, f: { status?: string; severity?: string; type?: string; camera_id?: string; since?: string }, scope?: string): boolean {
  if (f.status === "OPEN" && a.status === "RESOLVED") return false;
  if (f.status && f.status !== "OPEN" && a.status !== f.status) return false;
  if (f.severity && a.severity !== f.severity) return false;
  if (f.type && a.type !== f.type.toUpperCase()) return false;
  if (f.camera_id && a.camera_id !== f.camera_id) return false;
  if (f.since && a.created_at && new Date(a.created_at) < new Date(f.since)) return false;
  if (scope === "live" && a.is_demo) return false;
  if (scope === "demo" && !a.is_demo) return false;
  return true;
}

export default function Alerts() {
  const [sp, setSp] = useSearchParams();
  const { has } = useAuth();
  const { param: scope } = useScope();
  const qc = useQueryClient();
  const conn = useConnState();

  const filters = {
    status: sp.get("status") ?? undefined,
    severity: sp.get("severity") ?? undefined,
    type: sp.get("type") ?? undefined,
    camera_id: sp.get("camera_id") ?? sp.get("camera") ?? undefined,
    since: sp.get("since") ?? undefined,
  };
  const limit = Number(sp.get("limit")) || 50;
  const offset = Math.max(0, Number(sp.get("offset")) || 0);

  const setParam = (patch: Record<string, string | undefined>) => {
    const next = new URLSearchParams(sp);
    for (const [k, v] of Object.entries(patch)) {
      if (v === undefined || v === "") next.delete(k);
      else next.set(k, v);
    }
    if (!("offset" in patch)) next.delete("offset");
    if ("camera_id" in patch) next.delete("camera");
    setSp(next, { replace: true });
  };

  const key = ["alerts", "list", filters, limit, offset, scope] as const;
  const q = useQuery({
    queryKey: key,
    queryFn: () => api.get<AlertList>("/api/alerts", { ...filters, scope, limit, offset }),
    placeholderData: (prev) => prev,
  });
  const rules = useQuery({ queryKey: ["alert-rules"], queryFn: () => api.get<AlertRules>("/api/alerts/rules"), staleTime: 60_000 });
  const cams = useQuery({
    queryKey: ["cameras", "lite", scope],
    queryFn: () => api.get<{ cameras: CameraLite[] }>("/api/cameras", { scope }),
    enabled: has(P.CAMERAS_READ),
    staleTime: 60_000,
  });

  // Brief highlight of rows touched by live events.
  const [flash, setFlash] = useState<Record<string, number>>({});
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);
  useEffect(() => () => timers.current.forEach(clearTimeout), []);
  const highlight = useCallback((code: string) => {
    const stamp = Date.now();
    setFlash((f) => ({ ...f, [code]: stamp }));
    timers.current.push(
      setTimeout(() => setFlash((f) => {
        if (f[code] !== stamp) return f;
        const { [code]: _drop, ...rest } = f;
        void _drop;
        return rest;
      }), 4000),
    );
  }, []);

  useLiveEvent(["alert_created", "alert_updated"], (ev) => {
    const a = ev.data as unknown as Alert;
    if (!a || typeof a.code !== "string") return;
    qc.setQueryData<AlertList>(key, (old) => {
      if (!old) return old;
      const idx = old.results.findIndex((r) => r.code === a.code);
      const fits = matchesFilters(a, filters, old.scope);
      if (idx >= 0) {
        const results = [...old.results];
        if (fits) results[idx] = a;
        else results.splice(idx, 1);
        return { ...old, results };
      }
      if (ev.type === "alert_created" && fits && offset === 0) {
        return { ...old, total: old.total + 1, results: [a, ...old.results].slice(0, limit) };
      }
      return old;
    });
    highlight(a.code);
    // Re-sync totals / counts from the server.
    void qc.invalidateQueries({ queryKey: ["alerts", "list"] });
    void qc.invalidateQueries({ queryKey: ["alert", a.code] });
  });

  const onActed = (a: Alert) => {
    highlight(a.code);
    qc.setQueryData<AlertList>(key, (old) => (old ? { ...old, results: old.results.map((r) => (r.code === a.code ? a : r)) } : old));
    void qc.invalidateQueries({ queryKey: ["alerts", "list"] });
    void qc.invalidateQueries({ queryKey: ["alert", a.code] });
  };

  const typeOptions = useMemo(() => {
    const s = new Set(Object.keys(rules.data?.rules ?? {}));
    q.data?.results.forEach((a) => s.add(a.type));
    if (filters.type) s.add(filters.type.toUpperCase());
    return [...s].sort();
  }, [rules.data, q.data, filters.type]);

  const data = q.data;
  const total = data?.total ?? 0;

  return (
    <div className="space-y-2">
      <PageHeader
        title="Alerts"
        subtitle={rules.data?.note ?? "Alerts are raised only by explicit, configurable rules."}
        actions={
          <>
            <Badge tone={conn === "open" ? "green" : "orange"} title="WebSocket connection for live alert updates">
              {conn === "open" ? "Live" : `Not live (${conn})`}
            </Badge>
            <button className="btn" onClick={() => q.refetch()} disabled={q.isFetching}>
              {q.isFetching ? "Refreshing…" : "Refresh"}
            </button>
          </>
        }
      />
      <SyntheticBanner info={data} />

      {data && (
        <div className="flex flex-wrap items-center gap-1.5 text-[12px] text-slate-400">
          <span className="label mb-0">Last 7 days</span>
          {Object.keys(data.counts_7d).length === 0 && <span>no alerts</span>}
          {Object.entries(data.counts_7d).map(([s, n]) => (
            <button key={s} className="inline-flex" onClick={() => setParam({ status: s })} title={`Show ${s} alerts`}>
              <Badge tone={alertStatusTone(s)}>
                {s} <span className="font-mono">{n}</span>
              </Badge>
            </button>
          ))}
        </div>
      )}

      <Panel bodyClass="p-2">
        <div className="grid grid-cols-2 gap-2 md:grid-cols-6">
          <label className="block">
            <span className="label">Status</span>
            <select aria-label="Status" className="input" value={filters.status ?? ""} onChange={(e) => setParam({ status: e.target.value })}>
              <option value="">All</option>
              {ALERT_STATUSES.map((s) => (
                <option key={s} value={s}>
                  {s === "OPEN" ? "OPEN (not resolved)" : s}
                </option>
              ))}
            </select>
          </label>
          <label className="block">
            <span className="label">Severity</span>
            <select aria-label="Severity" className="input" value={filters.severity ?? ""} onChange={(e) => setParam({ severity: e.target.value })}>
              <option value="">All</option>
              {SEVERITIES.map((s) => (
                <option key={s} value={s}>
                  {s}
                </option>
              ))}
            </select>
          </label>
          <label className="block">
            <span className="label">Type</span>
            <select aria-label="Type" className="input" value={filters.type?.toUpperCase() ?? ""} onChange={(e) => setParam({ type: e.target.value })}>
              <option value="">All</option>
              {typeOptions.map((t) => (
                <option key={t} value={t}>
                  {t}
                </option>
              ))}
            </select>
          </label>
          <label className="block">
            <span className="label">Camera</span>
            {cams.data ? (
              <select aria-label="Camera" className="input" value={filters.camera_id ?? ""} onChange={(e) => setParam({ camera_id: e.target.value })}>
                <option value="">All</option>
                {filters.camera_id && !cams.data.cameras.some((c) => c.id === filters.camera_id) && <option value={filters.camera_id}>{filters.camera_id}</option>}
                {cams.data.cameras.map((c) => (
                  <option key={c.id} value={c.id}>
                    {c.id} · {c.name}
                  </option>
                ))}
              </select>
            ) : (
              <input
                aria-label="Camera"
                className="input"
                placeholder="CAM-01"
                defaultValue={filters.camera_id ?? ""}
                onBlur={(e) => setParam({ camera_id: e.target.value.trim() })}
              />
            )}
          </label>
          <label className="block">
            <span className="label">Created since</span>
            <input
              type="datetime-local"
              className="input"
              value={filters.since ? toLocalInput(new Date(filters.since)) : ""}
              onChange={(e) => setParam({ since: fromLocalInput(e.target.value) })}
            />
          </label>
          <div className="flex items-end gap-1.5">
            <label className="block flex-1">
              <span className="label">Page size</span>
              <select className="input" value={limit} onChange={(e) => setParam({ limit: e.target.value })}>
                {PAGE_SIZES.map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </select>
            </label>
            <button className="btn" onClick={() => setSp(new URLSearchParams(), { replace: true })}>
              Clear
            </button>
          </div>
        </div>
      </Panel>

      <Panel
        title={`Alerts${data ? ` · ${total}` : ""}`}
        bodyClass="p-0 overflow-x-auto"
        actions={
          data && total > 0 ? (
            <div className="flex items-center gap-1.5 text-[12px] text-slate-400">
              <span className="font-mono">
                {offset + 1}–{Math.min(offset + limit, total)} of {total}
              </span>
              <button className="btn" disabled={offset === 0} onClick={() => setParam({ offset: String(Math.max(0, offset - limit)) })}>
                ‹ Prev
              </button>
              <button className="btn" disabled={offset + limit >= total} onClick={() => setParam({ offset: String(offset + limit) })}>
                Next ›
              </button>
            </div>
          ) : null
        }
      >
        {q.isLoading ? (
          <Loading />
        ) : q.error ? (
          <ErrorBox error={q.error} onRetry={() => q.refetch()} />
        ) : !data || data.results.length === 0 ? (
          <Empty>No alerts match these filters.</Empty>
        ) : (
          <table className="w-full min-w-[1100px] text-[12px]">
            <thead>
              <tr>
                <th className="th">Code</th>
                <th className="th">Created</th>
                <th className="th">Type</th>
                <th className="th">Severity</th>
                <th className="th">Status</th>
                <th className="th">Camera</th>
                <th className="th">Title / reason</th>
                <th className="th text-right">Occ.</th>
                <th className="th text-right">Conf.</th>
                {has(P.ALERTS_ACT) && <th className="th">Actions</th>}
              </tr>
            </thead>
            <tbody>
              {data.results.map((a) => {
                const reopened = reopenedCount(a);
                return (
                  <tr key={a.code} data-testid={`alert-row-${a.code}`} className={`transition-colors duration-700 ${flash[a.code] ? "bg-cyan-500/15" : "hover:bg-ink-800/60"}`}>
                    <td className="td mono whitespace-nowrap">
                      <Link className="text-cyan-300 hover:underline" to={`/alerts/${a.code}`}>
                        {a.code}
                      </Link>
                      {a.is_demo && (
                        <Badge tone="violet" className="ml-1">
                          Synthetic
                        </Badge>
                      )}
                    </td>
                    <td className="td whitespace-nowrap" title={fmtTime(a.created_at, true)}>
                      <div className="mono">{fmtTime(a.created_at)}</div>
                      <div className="text-[11px] text-slate-500">{fmtAgo(a.created_at)}</div>
                    </td>
                    <td className="td mono whitespace-nowrap">{a.type}</td>
                    <td className="td">
                      <Badge tone={severityTone(a.severity)}>{a.severity}</Badge>
                    </td>
                    <td className="td">
                      <Badge tone={alertStatusTone(a.status)}>{a.status}</Badge>
                    </td>
                    <td className="td mono whitespace-nowrap">
                      {a.camera_id ? (
                        <Link className="text-cyan-300 hover:underline" to={`/cameras/${a.camera_id}`}>
                          {a.camera_id}
                        </Link>
                      ) : (
                        "—"
                      )}
                    </td>
                    <td className="td max-w-[420px]">
                      <div className="truncate font-medium text-slate-100" title={a.title}>
                        {a.title}
                      </div>
                      <div className="truncate text-[11px] text-slate-400" title={a.reason}>
                        {a.reason}
                      </div>
                    </td>
                    <td className="td text-right mono whitespace-nowrap">
                      ×{a.occurrences}
                      {reopened > 0 && (
                        <div className="text-[11px] text-orange-300" title="Condition recurred after auto-resolution and the alert was re-opened">
                          reopened {reopened}×
                        </div>
                      )}
                    </td>
                    <td className="td text-right mono">{a.confidence == null ? "—" : fmtPct(a.confidence)}</td>
                    {has(P.ALERTS_ACT) && (
                      <td className="td">
                        <AlertRowActions alert={a} onDone={onActed} />
                      </td>
                    )}
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </Panel>
    </div>
  );
}
