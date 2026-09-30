/**
 * User-managed watchlist. Nothing is pre-listed: an entry exists only when an authorised
 * user adds it with a reason; the WATCHLIST_MATCH rule then raises alerts for sightings.
 * Every change and every retrospective search is audited by the backend.
 */
import { useState } from "react";
import { Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, errorText } from "../lib/api";
import { P, useAuth } from "../lib/auth";
import { fmtAgo, fmtNum, fmtPct, fmtTime, fromLocalInput, severityTone, toLocalInput, type Tone } from "../lib/format";
import { ActionButton, Badge, Empty, ErrorBox, Field, KV, Loading, Modal, PageHeader, Panel, QueryState } from "../components/ui";
import type { AlertRules } from "../components/alerts/types";

const PRIORITIES = ["LOW", "MEDIUM", "HIGH", "CRITICAL"] as const;

interface Entry {
  id: number;
  plate: string;
  description: string | null;
  reason: string;
  priority: string;
  match_mode: "exact" | "fuzzy";
  active: boolean;
  expires_at: string | null;
  created_by: string | null;
  created_at: string | null;
  last_match_at: string | null;
  match_count: number;
}

interface Sighting {
  id: number;
  camera_id: string;
  camera_name: string | null;
  observed_at: string | null;
  plate_text: string | null;
  plate_display: string | null;
  plate_confidence: number | null;
  vehicle_class: string | null;
  vehicle_color: string | null;
  vehicle_code: string | null;
  match_confidence_level: string | null;
  evidence_id: string | null;
  is_demo: boolean;
  plate_distance: number;
}

interface Retro {
  entry: Entry;
  days: number;
  results: Sighting[];
}

function isExpired(e: Entry): boolean {
  return !!e.expires_at && new Date(e.expires_at).getTime() < Date.now();
}

function stateOf(e: Entry): { label: string; tone: Tone } {
  if (!e.active) return { label: "Inactive", tone: "slate" };
  if (isExpired(e)) return { label: "Expired", tone: "orange" };
  return { label: "Active", tone: "green" };
}

export default function Watchlist() {
  const { has } = useAuth();
  const qc = useQueryClient();
  const [active, setActive] = useState<"" | "true" | "false">("");
  const [editing, setEditing] = useState<Entry | "new" | null>(null);
  const [retro, setRetro] = useState<Entry | null>(null);
  const canWrite = has(P.WATCHLIST_WRITE);
  const canRetro = has(P.WATCHLIST_READ) && has(P.VEHICLES_SEARCH);

  const q = useQuery({
    queryKey: ["watchlist", active],
    queryFn: () => api.get<{ results: Entry[] }>("/api/watchlist", { active: active || undefined }),
  });
  const rules = useQuery({ queryKey: ["alert-rules"], queryFn: () => api.get<AlertRules>("/api/alerts/rules"), enabled: has(P.ALERTS_READ), staleTime: 60_000 });
  const wm = rules.data?.rules?.WATCHLIST_MATCH as { enabled?: boolean; fuzzy_max_distance?: number; cooldown_s?: number } | undefined;

  const refresh = () => qc.invalidateQueries({ queryKey: ["watchlist"] });

  return (
    <div className="space-y-2">
      <PageHeader
        title="Watchlist"
        subtitle="Plates of interest entered by authorised users. Every change is audited."
        actions={
          <>
            <select aria-label="Filter by state" className="input w-auto" value={active} onChange={(e) => setActive(e.target.value as typeof active)}>
              <option value="">All entries</option>
              <option value="true">Active only</option>
              <option value="false">Inactive only</option>
            </select>
            {canWrite && (
              <button className="btn btn-primary" onClick={() => setEditing("new")}>
                + Add plate
              </button>
            )}
          </>
        }
      />

      <div className="rounded border border-sky-700/40 bg-sky-950/30 px-2.5 py-1.5 text-[12px] text-sky-100">
        <div>
          Alerts fire only from explicit, configurable rules. No plate is ever flagged by default — a watchlist alert is raised only when a sighting matches an
          entry on this list, which starts empty and is maintained by authorised users with a mandatory reason.
        </div>
        {wm && (
          <div className="mt-0.5 text-sky-300/80">
            WATCHLIST_MATCH rule: {wm.enabled === false ? "DISABLED" : "enabled"} · fuzzy entries match plates within edit distance{" "}
            <span className="font-mono">{fmtNum(wm.fuzzy_max_distance ?? null, 1)}</span> · cooldown{" "}
            <span className="font-mono">{fmtNum(wm.cooldown_s ?? null)}</span> s. Fuzzy matches are reported with their distance so a possible misread stays visible.
          </div>
        )}
      </div>

      <Panel title={`Entries${q.data ? ` · ${q.data.results.length}` : ""}`} bodyClass="p-0 overflow-x-auto">
        <QueryState q={q} isEmpty={(d) => d.results.length === 0} empty={canWrite ? "The watchlist is empty. Add a plate to start matching." : "The watchlist is empty."}>
          {(d) => (
            <table className="w-full min-w-[1000px] text-[12px]">
              <thead>
                <tr>
                  <th className="th">Plate</th>
                  <th className="th">Priority</th>
                  <th className="th">Match</th>
                  <th className="th">Reason / description</th>
                  <th className="th">State</th>
                  <th className="th">Expires</th>
                  <th className="th">Added</th>
                  <th className="th text-right">Matches</th>
                  <th className="th">Last match</th>
                  <th className="th">Actions</th>
                </tr>
              </thead>
              <tbody>
                {d.results.map((e) => {
                  const st = stateOf(e);
                  return (
                    <tr key={e.id} className="hover:bg-ink-800/60">
                      <td className="td mono whitespace-nowrap font-semibold text-slate-100">{e.plate}</td>
                      <td className="td">
                        <Badge tone={severityTone(e.priority)}>{e.priority}</Badge>
                      </td>
                      <td className="td">
                        <Badge tone={e.match_mode === "fuzzy" ? "yellow" : "blue"}>{e.match_mode}</Badge>
                      </td>
                      <td className="td max-w-[360px]">
                        <div className="text-slate-100">{e.reason}</div>
                        {e.description && <div className="text-[11px] text-slate-400">{e.description}</div>}
                      </td>
                      <td className="td">
                        <Badge tone={st.tone}>{st.label}</Badge>
                      </td>
                      <td className="td whitespace-nowrap">{e.expires_at ? fmtTime(e.expires_at, true) : <span className="text-slate-500">never</span>}</td>
                      <td className="td whitespace-nowrap">
                        {fmtTime(e.created_at, true)}
                        <div className="text-[11px] text-slate-500">by {e.created_by ?? "—"}</div>
                      </td>
                      <td className="td text-right mono">{e.match_count}</td>
                      <td className="td whitespace-nowrap">{e.last_match_at ? fmtAgo(e.last_match_at) : <span className="text-slate-500">never</span>}</td>
                      <td className="td">
                        <div className="flex flex-wrap gap-1">
                          {canRetro && (
                            <button className="btn px-1.5 py-0.5 text-[11px]" onClick={() => setRetro(e)}>
                              Retrospective
                            </button>
                          )}
                          {canWrite && (
                            <>
                              <button className="btn px-1.5 py-0.5 text-[11px]" onClick={() => setEditing(e)}>
                                Edit
                              </button>
                              <ActionButton
                                className="btn px-1.5 py-0.5 text-[11px]"
                                onClick={async () => {
                                  await api.patch(`/api/watchlist/${e.id}`, { active: !e.active });
                                  await refresh();
                                }}
                              >
                                {e.active ? "Deactivate" : "Activate"}
                              </ActionButton>
                              <ActionButton
                                className="btn btn-danger px-1.5 py-0.5 text-[11px]"
                                confirm={`Delete watchlist entry ${e.plate}? This is audited and cannot be undone.`}
                                onClick={async () => {
                                  await api.del(`/api/watchlist/${e.id}`);
                                  await refresh();
                                }}
                              >
                                Delete
                              </ActionButton>
                            </>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}
        </QueryState>
      </Panel>

      {editing && (
        <EntryForm
          entry={editing === "new" ? null : editing}
          onClose={() => setEditing(null)}
          onSaved={async () => {
            setEditing(null);
            await refresh();
          }}
        />
      )}
      {retro && <RetroModal entry={retro} onClose={() => setRetro(null)} />}
    </div>
  );
}

function EntryForm({ entry, onClose, onSaved }: { entry: Entry | null; onClose: () => void; onSaved: () => void }) {
  const [plate, setPlate] = useState(entry?.plate ?? "");
  const [reason, setReason] = useState(entry?.reason ?? "");
  const [description, setDescription] = useState(entry?.description ?? "");
  const [priority, setPriority] = useState(entry?.priority ?? "MEDIUM");
  const [matchMode, setMatchMode] = useState<"exact" | "fuzzy">(entry?.match_mode ?? "exact");
  const [expires, setExpires] = useState(entry?.expires_at ? toLocalInput(new Date(entry.expires_at)) : "");
  const [active, setActive] = useState(entry?.active ?? true);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const submit = async () => {
    setBusy(true);
    setErr(null);
    try {
      const expires_at = expires ? fromLocalInput(expires) : null;
      if (entry) {
        await api.patch(`/api/watchlist/${entry.id}`, { reason, description, priority, match_mode: matchMode, active, expires_at });
      } else {
        await api.post("/api/watchlist", { plate, reason, description, priority, match_mode: matchMode, expires_at });
      }
      onSaved();
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title={entry ? `Edit watchlist entry ${entry.plate}` : "Add plate to watchlist"} onClose={onClose}>
      <form
        className="space-y-2"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        {!entry && (
          <Field label="Plate" hint="4–12 letters/digits; spaces, dashes and dots are removed by the server.">
            <input className="input mono uppercase" required minLength={4} maxLength={16} value={plate} onChange={(e) => setPlate(e.target.value)} autoFocus />
          </Field>
        )}
        <Field label="Reason (required)" hint="Why this plate is being watched — recorded in the audit log.">
          <input className="input" required minLength={3} maxLength={255} value={reason} onChange={(e) => setReason(e.target.value)} />
        </Field>
        <Field label="Description">
          <input className="input" maxLength={255} value={description} onChange={(e) => setDescription(e.target.value)} />
        </Field>
        <div className="grid grid-cols-2 gap-2">
          <Field label="Priority">
            <select className="input" value={priority} onChange={(e) => setPriority(e.target.value)}>
              {PRIORITIES.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Matching" hint={matchMode === "fuzzy" ? "Also matches near-identical reads (possible OCR errors)." : "Only an identical normalised plate matches."}>
            <select className="input" value={matchMode} onChange={(e) => setMatchMode(e.target.value as "exact" | "fuzzy")}>
              <option value="exact">exact</option>
              <option value="fuzzy">fuzzy</option>
            </select>
          </Field>
        </div>
        <Field label="Expires at" hint="Leave empty for no expiry.">
          <div className="flex gap-1.5">
            <input type="datetime-local" className="input" value={expires} onChange={(e) => setExpires(e.target.value)} />
            {expires && (
              <button type="button" className="btn" onClick={() => setExpires("")}>
                Clear
              </button>
            )}
          </div>
        </Field>
        {entry && (
          <label className="flex items-center gap-2 text-[12px] text-slate-300">
            <input type="checkbox" checked={active} onChange={(e) => setActive(e.target.checked)} />
            Active (inactive entries never raise alerts)
          </label>
        )}
        {err && <div className="text-[12px] text-rose-300" role="alert">{err}</div>}
        <div className="flex justify-end gap-1.5">
          <button type="button" className="btn btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="btn btn-primary" disabled={busy}>
            {entry ? "Save changes" : "Add to watchlist"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

function RetroModal({ entry, onClose }: { entry: Entry; onClose: () => void }) {
  const [days, setDays] = useState(7);
  const q = useQuery({
    queryKey: ["watchlist-retro", entry.id, days],
    queryFn: () => api.get<Retro>(`/api/watchlist/${entry.id}/retrospective`, { days }),
    staleTime: 0,
  });
  return (
    <Modal title={`Retrospective search · ${entry.plate}`} onClose={onClose} wide>
      <div className="mb-2 flex flex-wrap items-end justify-between gap-2">
        <KV
          rows={[
            ["Match mode", entry.match_mode],
            ["Reason", entry.reason],
          ]}
        />
        <label className="block">
          <span className="label">Look back</span>
          <select className="input w-auto" value={days} onChange={(e) => setDays(Number(e.target.value))}>
            {[1, 3, 7, 14, 30, 90].map((d) => (
              <option key={d} value={d}>
                {d} day{d > 1 ? "s" : ""}
              </option>
            ))}
          </select>
        </label>
      </div>
      <div className="mb-1 text-[11px] text-slate-500">This search is recorded in the audit log. Results are capped at 200 sightings by the server.</div>
      {q.isLoading ? (
        <Loading />
      ) : q.error ? (
        <ErrorBox error={q.error} onRetry={() => q.refetch()} />
      ) : !q.data || q.data.results.length === 0 ? (
        <Empty>No sightings of this plate in the last {days} day{days > 1 ? "s" : ""}.</Empty>
      ) : (
        <div className="max-h-[60vh] overflow-auto">
          <table className="w-full text-[12px]">
            <thead>
              <tr>
                <th className="th">Seen</th>
                <th className="th">Camera</th>
                <th className="th">Plate read</th>
                <th className="th text-right">Distance</th>
                <th className="th text-right">OCR conf.</th>
                <th className="th">Vehicle</th>
                <th className="th">Observation</th>
              </tr>
            </thead>
            <tbody>
              {q.data.results.map((s) => (
                <tr key={s.id}>
                  <td className="td mono whitespace-nowrap">{fmtTime(s.observed_at, true)}</td>
                  <td className="td">
                    <Link className="mono text-cyan-300 hover:underline" to={`/cameras/${s.camera_id}`}>
                      {s.camera_id}
                    </Link>
                    {s.camera_name && <div className="text-[11px] text-slate-500">{s.camera_name}</div>}
                  </td>
                  <td className="td mono">
                    {s.plate_display ?? s.plate_text ?? "—"}
                    {s.is_demo && (
                      <Badge tone="violet" className="ml-1">
                        Synthetic
                      </Badge>
                    )}
                  </td>
                  <td className="td text-right">
                    <Badge tone={s.plate_distance === 0 ? "green" : "yellow"} title="Edit distance between the read plate and the watchlist plate">
                      {s.plate_distance === 0 ? "exact" : `d=${fmtNum(s.plate_distance, 1)}`}
                    </Badge>
                  </td>
                  <td className="td text-right mono">{fmtPct(s.plate_confidence, 1)}</td>
                  <td className="td">
                    {s.vehicle_code ? (
                      <Link className="mono text-cyan-300 hover:underline" to={`/vehicles/${s.vehicle_code}`}>
                        {s.vehicle_code}
                      </Link>
                    ) : (
                      "—"
                    )}
                    {(s.vehicle_class || s.vehicle_color) && (
                      <div className="text-[11px] text-slate-500">{[s.vehicle_color, s.vehicle_class].filter(Boolean).join(" ")}</div>
                    )}
                  </td>
                  <td className="td">
                    <Link className="mono text-cyan-300 hover:underline" to={`/observations/${s.id}`}>
                      #{s.id}
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Modal>
  );
}
