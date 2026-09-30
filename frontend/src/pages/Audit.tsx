/**
 * Audit log: who searched for which vehicle, viewed evidence, acted on alerts or changed
 * configuration. GET /api/audit with kind/action/username/resource_type/since/until + paging.
 */
import { useState, type FormEvent } from "react";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { fmtNum, fmtTime, fromLocalInput } from "../lib/format";
import { Badge, PageHeader, Panel, QueryState } from "../components/ui";
import { JsonToggle } from "../components/admin/common";

interface AuditRow {
  id: number;
  ts: string;
  user_id: number | null;
  username: string | null;
  role: string | null;
  action: string;
  resource_type: string | null;
  resource_id: string | null;
  details: unknown;
  ip: string | null;
  success: boolean;
}

type Kind = "" | "search" | "alert" | "evidence" | "auth" | "admin";
/** Server-side action groups (the `kind` parameter of GET /api/audit). */
const KINDS: { id: Kind; label: string; hint: string }[] = [
  { id: "", label: "All", hint: "Every audited action" },
  { id: "search", label: "Search history", hint: "Vehicle searches, trajectory / observation views, OD drill-downs, retrospective watchlist searches" },
  { id: "alert", label: "Alert actions", hint: "Alert acknowledge / resolve and other alert.* actions" },
  { id: "evidence", label: "Evidence access", hint: "Evidence view / download / verify" },
  { id: "auth", label: "Authentication", hint: "Logins, failed logins, password changes" },
  { id: "admin", label: "Admin changes", hint: "Users, cameras, settings, topology, watchlist, retention, dead letters, demo" },
];

interface Filters {
  action: string;
  username: string;
  resource_type: string;
  since: string;
  until: string;
}
const EMPTY: Filters = { action: "", username: "", resource_type: "", since: "", until: "" };

export default function Audit() {
  const [kind, setKind] = useState<Kind>("");
  const [form, setForm] = useState<Filters>(EMPTY);
  const [applied, setApplied] = useState<Filters>(EMPTY);
  const [limit, setLimit] = useState(100);
  const [offset, setOffset] = useState(0);
  const [actions, setActions] = useState<string[]>([]);

  const q = useQuery({
    queryKey: ["audit", kind, applied, limit, offset],
    queryFn: () =>
      api.get<{ total: number; results: AuditRow[] }>("/api/audit", {
        kind,
        action: applied.action.trim(),
        username: applied.username.trim(),
        resource_type: applied.resource_type.trim(),
        since: fromLocalInput(applied.since),
        until: fromLocalInput(applied.until),
        limit,
        offset,
      }),
    placeholderData: keepPreviousData,
  });

  // Remember action names seen so far to offer them as suggestions (the API has no distinct-actions endpoint).
  const seen = q.data?.results.map((r) => r.action) ?? [];
  if (seen.some((a) => !actions.includes(a))) setActions([...new Set([...actions, ...seen])].sort());

  function apply(e: FormEvent) {
    e.preventDefault();
    setApplied(form);
    setOffset(0);
  }

  const total = q.data?.total ?? 0;
  const page = Math.floor(offset / limit) + 1;
  const pages = Math.max(1, Math.ceil(total / limit));

  return (
    <div className="flex h-full flex-col gap-2">
      <PageHeader title="Audit Log" subtitle="Tamper-evident record of searches, evidence access, alert actions and administrative changes" />
      <div className="flex flex-wrap gap-1" role="group" aria-label="Quick filters">
        {KINDS.map((k) => (
          <button
            key={k.id || "all"}
            title={k.hint}
            className={`btn ${kind === k.id ? "btn-primary" : ""}`}
            onClick={() => {
              setKind(k.id);
              setOffset(0);
            }}
          >
            {k.label}
          </button>
        ))}
      </div>
      <form onSubmit={apply} className="grid grid-cols-2 gap-2 rounded-md border border-ink-700 bg-ink-900/80 p-2 md:grid-cols-3 xl:grid-cols-[repeat(5,minmax(0,1fr))_auto]">
        <label>
          <span className="label">Action</span>
          <input className="input mono" list="audit-actions" placeholder="e.g. vehicle.search" value={form.action} onChange={(e) => setForm({ ...form, action: e.target.value })} />
          <datalist id="audit-actions">
            {actions.map((a) => (
              <option key={a} value={a} />
            ))}
          </datalist>
        </label>
        <label>
          <span className="label">Username</span>
          <input className="input" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} />
        </label>
        <label>
          <span className="label">Target type</span>
          <input className="input" placeholder="vehicle, evidence, user…" value={form.resource_type} onChange={(e) => setForm({ ...form, resource_type: e.target.value })} />
        </label>
        <label>
          <span className="label">Since</span>
          <input className="input" type="datetime-local" value={form.since} onChange={(e) => setForm({ ...form, since: e.target.value })} />
        </label>
        <label>
          <span className="label">Until</span>
          <input className="input" type="datetime-local" value={form.until} onChange={(e) => setForm({ ...form, until: e.target.value })} />
        </label>
        <div className="flex items-end gap-1">
          <button className="btn btn-primary">Apply</button>
          <button
            type="button"
            className="btn btn-ghost"
            onClick={() => {
              setForm(EMPTY);
              setApplied(EMPTY);
              setOffset(0);
            }}
          >
            Clear
          </button>
        </div>
      </form>

      <Panel
        className="min-h-0 flex-1"
        bodyClass="overflow-auto p-0"
        title={`${fmtNum(total)} entries`}
        subtitle={KINDS.find((k) => k.id === kind)?.hint}
        actions={
          <div className="flex items-center gap-1 text-[12px] text-slate-400">
            <select className="input !w-20 !py-0.5" value={limit} onChange={(e) => { setLimit(Number(e.target.value)); setOffset(0); }} aria-label="Page size">
              {[25, 50, 100, 250, 500].map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
            <button className="btn !py-0.5" disabled={offset === 0 || q.isFetching} onClick={() => setOffset(Math.max(0, offset - limit))}>
              ‹ Prev
            </button>
            <span className="mono">
              {page}/{pages}
            </span>
            <button className="btn !py-0.5" disabled={offset + limit >= total || q.isFetching} onClick={() => setOffset(offset + limit)}>
              Next ›
            </button>
          </div>
        }
      >
        <QueryState q={q} isEmpty={(d) => d.results.length === 0} empty="No audit entries match these filters.">
          {(d) => (
            <table className="w-full text-[12px]">
              <thead>
                <tr>
                  <th className="th">Time</th>
                  <th className="th">User</th>
                  <th className="th">Role</th>
                  <th className="th">Action</th>
                  <th className="th">Target</th>
                  <th className="th">IP</th>
                  <th className="th">Result</th>
                  <th className="th">Details</th>
                </tr>
              </thead>
              <tbody>
                {d.results.map((r) => (
                  <tr key={r.id} className="hover:bg-ink-800/60">
                    <td className="td mono whitespace-nowrap">{fmtTime(r.ts, true)}</td>
                    <td className="td">{r.username ?? <span className="text-slate-500">anonymous</span>}</td>
                    <td className="td text-slate-400">{r.role ?? "—"}</td>
                    <td className="td">
                      <button
                        className="mono text-cyan-300 hover:underline"
                        title="Filter by this action"
                        onClick={() => {
                          const f = { ...form, action: r.action };
                          setForm(f);
                          setApplied(f);
                          setOffset(0);
                        }}
                      >
                        {r.action}
                      </button>
                    </td>
                    <td className="td mono break-all">
                      {r.resource_type ? <span className="text-slate-400">{r.resource_type}:</span> : null}
                      {r.resource_id ?? (r.resource_type ? "" : "—")}
                    </td>
                    <td className="td mono text-slate-400">{r.ip ?? "—"}</td>
                    <td className="td">{r.success ? <Badge tone="green">ok</Badge> : <Badge tone="red">failed</Badge>}</td>
                    <td className="td">
                      <JsonToggle value={r.details} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </QueryState>
      </Panel>
    </div>
  );
}
