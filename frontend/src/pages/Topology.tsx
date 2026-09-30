/**
 * Road topology: the directed camera graph used for travel-time plausibility, rejection of
 * physically impossible identity matches, next-camera prediction and trajectory geometry.
 */
import { useMemo, useState, type FormEvent } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { api, errorText } from "../lib/api";
import { P, useAuth } from "../lib/auth";
import { fmtDuration, fmtKm, TONE_HEX } from "../lib/format";
import { edgePath, NetworkMap, useTopology, type MapCamera, type MapPath, type Topology as TopologyT, type TopologyEdge } from "../components/NetworkMap";
import { ActionButton, Badge, Empty, ErrorBox, Explanation, Field, KV, Loading, Modal, PageHeader, Panel } from "../components/ui";
import { CameraSelect } from "../components/admin/common";

type Edge = TopologyEdge & { direction?: string | null };

interface PathResult {
  reachable: boolean;
  from?: string;
  to?: string;
  explanation?: string;
  cameras?: string[];
  distance_m?: number;
  min_travel_s?: number;
  typical_travel_s?: number;
  hops?: number;
  edges?: { from: string; to: string; road_name: string | null; distance_m: number; min_travel_s: number }[];
}

const COMPASS = ["", "N", "NE", "E", "SE", "S", "SW", "W", "NW"];

export default function Topology() {
  const { has } = useAuth();
  const canWrite = has(P.TOPOLOGY_WRITE);
  const topo = useTopology();
  const qc = useQueryClient();
  const refresh = () => qc.invalidateQueries({ queryKey: ["topology"] });

  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [path, setPath] = useState<PathResult | null>(null);
  const [pathErr, setPathErr] = useState<string | null>(null);
  const [pathBusy, setPathBusy] = useState(false);
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<Edge | null>(null);
  const [filter, setFilter] = useState("");

  const t = topo.data;
  const camById = useMemo(() => new Map((t?.cameras ?? []).map((c) => [c.id, c])), [t]);

  async function findPath(e?: FormEvent) {
    e?.preventDefault();
    if (!from || !to) return;
    setPathBusy(true);
    setPathErr(null);
    setPath(null);
    try {
      setPath(await api.get<PathResult>("/api/topology/path", { from, to }));
    } catch (ex) {
      setPathErr(errorText(ex));
    } finally {
      setPathBusy(false);
    }
  }

  const pathCams = path?.reachable ? path.cameras ?? [] : [];
  const mapCams: MapCamera[] = (t?.cameras ?? []).map((c) => {
    const onPath = pathCams.includes(c.id);
    const isEnd = c.id === from || c.id === to;
    return {
      id: c.id,
      lat: c.lat,
      lon: c.lon,
      label: c.id,
      color: onPath ? TONE_HEX.yellow : c.is_demo ? TONE_HEX.violet : TONE_HEX.cyan,
      ring: isEnd ? "#f8fafc" : undefined,
      tooltip: `${c.id} · ${c.name}${c.road_name ? ` · ${c.road_name}` : ""}${c.is_demo ? " · SYNTHETIC" : ""}`,
    };
  });

  const mapPaths: MapPath[] = useMemo(() => {
    if (!t || pathCams.length < 2) return [];
    const coords: [number, number][] = [];
    for (let i = 1; i < pathCams.length; i++) {
      const a = camById.get(pathCams[i - 1]);
      const b = camById.get(pathCams[i]);
      if (!a || !b) continue;
      const seg = edgePath(t, a.id, b.id, [
        [a.lat, a.lon],
        [b.lat, b.lon],
      ]);
      coords.push(...(coords.length ? seg.slice(1) : seg));
    }
    return [{ id: "path", coords, color: TONE_HEX.yellow, weight: 5, tooltip: `${path?.from ?? from} → ${path?.to ?? to}` }];
  }, [t, path]);
  const fitTo = useMemo(() => (mapPaths[0]?.coords.length ? mapPaths[0].coords : null), [mapPaths]);

  function onCameraClick(id: string) {
    if (!from || (from && to)) {
      setFrom(id);
      setTo("");
      setPath(null);
    } else if (id !== from) setTo(id);
  }

  const edges = ((t?.edges ?? []) as Edge[]).filter((e) => {
    if (!filter) return true;
    const f = filter.toLowerCase();
    return [e.from_camera_id, e.to_camera_id, e.road_name ?? ""].some((s) => s.toLowerCase().includes(f));
  });

  return (
    <div className="flex h-full flex-col gap-2">
      <PageHeader
        title="Road Topology"
        subtitle="Directed camera graph: travel-time priors per link and whether a vehicle may travel that link at all"
        actions={
          <>
            <button className="btn" onClick={() => topo.refetch()} disabled={topo.isFetching}>
              {topo.isFetching ? "Refreshing…" : "Refresh"}
            </button>
            {canWrite && (
              <button className="btn btn-primary" onClick={() => setAdding(true)} disabled={!t}>
                + Add edge
              </button>
            )}
          </>
        }
      />
      <div className="rounded border border-cyan-800/50 bg-cyan-950/30 px-2 py-1.5 text-[12px] text-cyan-100">
        The topology graph is what rejects physically impossible identity matches: two sightings can only be linked to the same vehicle if an
        allowed directed path connects the cameras and the elapsed time is not faster than the path's minimum travel time. Deleting or
        disallowing a link changes which matches the identity engine will accept.
      </div>

      {topo.isLoading ? (
        <Loading label="Loading road graph…" />
      ) : topo.error ? (
        <ErrorBox error={topo.error} onRetry={() => topo.refetch()} />
      ) : !t ? (
        <Empty>No topology returned.</Empty>
      ) : (
        <div className="grid min-h-0 flex-1 grid-cols-1 gap-2 xl:grid-cols-[minmax(0,1fr)_380px]">
          <div className="flex min-h-0 flex-col gap-2">
            <Panel
              title="Network map"
              subtitle={`${t.cameras.length} cameras · ${t.edges.length} directed links · ${t.roads.length} road segments — click two cameras to find a path`}
              bodyClass="p-0"
              className="min-h-[340px] flex-[3]"
            >
              <NetworkMap cameras={mapCams} paths={mapPaths} fitTo={fitTo} onCameraClick={onCameraClick} selectedId={from || null} className="h-full min-h-[320px]" />
            </Panel>
            <Panel
              title="Directed edges"
              subtitle="A → B and B → A are separate links; a missing reverse link means one-way"
              className="min-h-[220px] flex-[2]"
              bodyClass="overflow-auto p-0"
              actions={<input className="input !w-48 !py-0.5" placeholder="Filter camera / road…" value={filter} onChange={(e) => setFilter(e.target.value)} />}
            >
              {edges.length === 0 ? (
                <Empty>{t.edges.length ? "No edges match the filter." : "No edges configured — identity matching has no route constraints."}</Empty>
              ) : (
                <table className="w-full text-[12px]">
                  <thead>
                    <tr>
                      <th className="th">From</th>
                      <th className="th">To</th>
                      <th className="th">Road</th>
                      <th className="th">Dir</th>
                      <th className="th text-right">Distance</th>
                      <th className="th text-right">Min time</th>
                      <th className="th text-right">Typical</th>
                      <th className="th">Allowed</th>
                      <th className="th">Geometry</th>
                      {canWrite && <th className="th" />}
                    </tr>
                  </thead>
                  <tbody>
                    {edges.map((e) => (
                      <tr key={e.id} className="hover:bg-ink-800/60">
                        <td className="td mono">{e.from_camera_id}</td>
                        <td className="td mono">{e.to_camera_id}</td>
                        <td className="td">{e.road_name || <span className="text-slate-600">—</span>}</td>
                        <td className="td mono">{e.direction || "—"}</td>
                        <td className="td mono text-right">{fmtKm(e.distance_m)}</td>
                        <td className="td mono text-right">{fmtDuration(e.min_travel_s)}</td>
                        <td className="td mono text-right">{fmtDuration(e.typical_travel_s)}</td>
                        <td className="td">{e.allowed ? <Badge tone="green">allowed</Badge> : <Badge tone="red">blocked</Badge>}</td>
                        <td className="td text-slate-400">{e.has_geometry ? "road-following" : "straight line"}</td>
                        {canWrite && (
                          <td className="td whitespace-nowrap text-right">
                            <span className="inline-flex gap-1">
                              <button className="btn btn-ghost !px-1.5 !py-0" onClick={() => setEditing(e)}>
                                Edit
                              </button>
                              <ActionButton
                                className="btn btn-ghost !px-1.5 !py-0 text-rose-300"
                                confirm={`Delete edge ${e.from_camera_id} → ${e.to_camera_id}? Matches along this link will no longer be accepted.`}
                                onClick={async () => {
                                  await api.del(`/api/topology/edges/${e.id}`);
                                  await refresh();
                                }}
                              >
                                Delete
                              </ActionButton>
                            </span>
                          </td>
                        )}
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </Panel>
          </div>

          <div className="flex min-h-0 flex-col gap-2 overflow-auto">
            <Panel title="Path finder" subtitle="Shortest allowed directed path (GET /api/topology/path)">
              <form onSubmit={findPath} className="space-y-2">
                <Field label="From camera">
                  <CameraSelect cameras={t.cameras} value={from} onChange={(v) => { setFrom(v); setPath(null); }} />
                </Field>
                <Field label="To camera">
                  <CameraSelect cameras={t.cameras} value={to} onChange={(v) => { setTo(v); setPath(null); }} />
                </Field>
                <div className="flex gap-1.5">
                  <button className="btn btn-primary" disabled={!from || !to || from === to || pathBusy}>
                    {pathBusy ? "Finding…" : "Find path"}
                  </button>
                  <button
                    type="button"
                    className="btn"
                    disabled={!from && !to}
                    onClick={() => {
                      setFrom(to);
                      setTo(from);
                      setPath(null);
                    }}
                    title="Swap direction"
                  >
                    ⇄ Swap
                  </button>
                  <button type="button" className="btn btn-ghost" onClick={() => { setFrom(""); setTo(""); setPath(null); setPathErr(null); }}>
                    Clear
                  </button>
                </div>
              </form>
              {pathErr && <div className="mt-2 text-[12px] text-rose-300" role="alert">{pathErr}</div>}
              {path && <PathView p={path} t={t} />}
            </Panel>
            <Panel title="Road segments" subtitle="Offline road network drawn on the map" bodyClass="max-h-64 overflow-auto p-0">
              {t.roads.length === 0 ? (
                <Empty>No road geometry loaded.</Empty>
              ) : (
                <table className="w-full text-[12px]">
                  <thead>
                    <tr>
                      <th className="th">Road</th>
                      <th className="th text-right">Limit</th>
                      <th className="th text-right">Lanes</th>
                    </tr>
                  </thead>
                  <tbody>
                    {t.roads.map((r) => (
                      <tr key={r.id}>
                        <td className="td">
                          {r.name} {r.is_demo && <Badge tone="violet">Synthetic</Badge>}
                        </td>
                        <td className="td mono text-right">{r.speed_limit_kmh != null ? `${r.speed_limit_kmh} km/h` : "—"}</td>
                        <td className="td mono text-right">{r.lanes ?? "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </Panel>
          </div>
        </div>
      )}

      {adding && t && <EdgeCreateModal t={t} onClose={() => setAdding(false)} onDone={refresh} />}
      {editing && <EdgeEditModal edge={editing} onClose={() => setEditing(null)} onDone={refresh} />}
    </div>
  );
}

function PathView({ p, t }: { p: PathResult; t: TopologyT }) {
  if (!p.reachable) {
    return (
      <div className="mt-2 rounded border border-rose-700/60 bg-rose-950/40 p-2 text-[12px] text-rose-200">
        <div className="font-semibold">
          Not reachable: {p.from} → {p.to}
        </div>
        <div className="text-rose-300">{p.explanation}</div>
      </div>
    );
  }
  const name = (id: string) => t.cameras.find((c) => c.id === id)?.name;
  return (
    <div className="mt-2 space-y-2">
      <KV
        rows={[
          ["Route", <span className="mono">{(p.cameras ?? []).join(" → ")}</span>],
          ["Hops", p.hops],
          ["Distance", fmtKm(p.distance_m)],
          ["Minimum travel", fmtDuration(p.min_travel_s)],
          ["Typical travel", fmtDuration(p.typical_travel_s)],
        ]}
      />
      <table className="w-full text-[11px]">
        <thead>
          <tr>
            <th className="th">Link</th>
            <th className="th">Road</th>
            <th className="th text-right">Dist</th>
            <th className="th text-right">Min</th>
          </tr>
        </thead>
        <tbody>
          {(p.edges ?? []).map((e, i) => (
            <tr key={i}>
              <td className="td mono" title={`${name(e.from) ?? ""} → ${name(e.to) ?? ""}`}>
                {e.from} → {e.to}
              </td>
              <td className="td">{e.road_name || "—"}</td>
              <td className="td mono text-right">{fmtKm(e.distance_m)}</td>
              <td className="td mono text-right">{fmtDuration(e.min_travel_s)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <Explanation
        lines={[
          `A match ${p.cameras?.[0]} → ${p.cameras?.[p.cameras.length - 1]} faster than ${fmtDuration(p.min_travel_s)} is physically impossible and is rejected.`,
        ]}
      />
    </div>
  );
}

function num(s: string): number | undefined {
  if (s.trim() === "") return undefined;
  const n = Number(s);
  return Number.isFinite(n) ? n : undefined;
}

const PATH_HELP = "Road geometry must be a JSON array of at least two [lat, lon] pairs, e.g. [[26.83, 80.92], [26.84, 80.93]].";

/** Parse the road-geometry textarea: [[lat, lon], …] with ≥2 valid points. */
function parsePath(text: string): number[][] | null {
  try {
    const p = JSON.parse(text);
    if (!Array.isArray(p) || p.length < 2) return null;
    const ok = p.every(
      (x) => Array.isArray(x) && x.length === 2 && x.every((y) => typeof y === "number" && Number.isFinite(y)) && Math.abs(x[0]) <= 90 && Math.abs(x[1]) <= 180,
    );
    return ok ? p : null;
  } catch {
    return null;
  }
}

function PathInput({ value, onChange, hint }: { value: string; onChange: (v: string) => void; hint: string }) {
  return (
    <Field label="Road geometry" hint={hint}>
      <textarea className="input mono h-16" value={value} onChange={(e) => onChange(e.target.value)} placeholder="[[26.832, 80.923], [26.838, 80.933]]" />
    </Field>
  );
}

function EdgeCreateModal({ t, onClose, onDone }: { t: TopologyT; onClose: () => void; onDone: () => unknown }) {
  const [f, setF] = useState({ from: "", to: "", distance: "", min: "", typical: "", road: "", direction: "", allowed: true, bidirectional: false, path: "" });
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const set = (k: keyof typeof f, v: string | boolean) => setF((o) => ({ ...o, [k]: v }));

  async function submit(e: FormEvent) {
    e.preventDefault();
    setErr(null);
    const distance = num(f.distance);
    if (distance === undefined || distance <= 0) return setErr("Distance must be a positive number of metres.");
    let path: number[][] | undefined;
    if (f.path.trim()) {
      const p = parsePath(f.path);
      if (!p) return setErr(PATH_HELP);
      path = p;
    }
    setBusy(true);
    try {
      await api.post("/api/topology/edges", {
        from_camera_id: f.from,
        to_camera_id: f.to,
        distance_m: distance,
        min_travel_s: num(f.min),
        typical_travel_s: num(f.typical),
        road_name: f.road,
        direction: f.direction,
        allowed: f.allowed,
        bidirectional: f.bidirectional,
        path,
      });
      await onDone();
      onClose();
    } catch (ex) {
      setErr(errorText(ex));
    } finally {
      setBusy(false);
    }
  }

  const d = num(f.distance);
  return (
    <Modal title="Add directed edge" onClose={onClose}>
      <form onSubmit={submit} className="space-y-2">
        <div className="grid grid-cols-2 gap-2">
          <Field label="From camera">
            <CameraSelect cameras={t.cameras} value={f.from} onChange={(v) => set("from", v)} required />
          </Field>
          <Field label="To camera">
            <CameraSelect cameras={t.cameras} value={f.to} onChange={(v) => set("to", v)} required />
          </Field>
          <Field label="Distance (m)">
            <input className="input" type="number" min={1} step="any" value={f.distance} onChange={(e) => set("distance", e.target.value)} required />
          </Field>
          <Field label="Road name">
            <input className="input" maxLength={128} value={f.road} onChange={(e) => set("road", e.target.value)} />
          </Field>
          <Field label="Min travel (s)" hint={d ? `Blank → server default ≈ ${(d / 25).toFixed(1)} s (90 km/h)` : "Blank → distance at 90 km/h"}>
            <input className="input" type="number" min={0.1} step="any" value={f.min} onChange={(e) => set("min", e.target.value)} />
          </Field>
          <Field label="Typical travel (s)" hint={d ? `Blank → server default ≈ ${(d / 8.33).toFixed(1)} s (30 km/h)` : "Blank → distance at 30 km/h"}>
            <input className="input" type="number" min={0.1} step="any" value={f.typical} onChange={(e) => set("typical", e.target.value)} />
          </Field>
          <Field label="Travel direction">
            <select className="input" value={f.direction} onChange={(e) => set("direction", e.target.value)}>
              {COMPASS.map((c) => (
                <option key={c} value={c}>
                  {c || "—"}
                </option>
              ))}
            </select>
          </Field>
          <div className="flex flex-col justify-end gap-1 pb-1 text-[12px]">
            <label className="flex items-center gap-1.5">
              <input type="checkbox" checked={f.allowed} onChange={(e) => set("allowed", e.target.checked)} /> Allowed (vehicles may travel this link)
            </label>
            <label className="flex items-center gap-1.5">
              <input type="checkbox" checked={f.bidirectional} onChange={(e) => set("bidirectional", e.target.checked)} /> Also create the reverse link
            </label>
          </div>
        </div>
        <PathInput
          value={f.path}
          onChange={(v) => set("path", v)}
          hint="Optional JSON [[lat, lon], …] from the From camera to the To camera; reversed automatically for the reverse link. Blank → straight line."
        />
        {err && <div className="text-[12px] text-rose-300" role="alert">{err}</div>}
        <div className="flex justify-end gap-1.5">
          <button type="button" className="btn btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" disabled={busy || !f.from || !f.to || f.from === f.to}>
            {busy ? "Saving…" : "Create edge"}
          </button>
        </div>
      </form>
    </Modal>
  );
}

function EdgeEditModal({ edge, onClose, onDone }: { edge: Edge; onClose: () => void; onDone: () => unknown }) {
  const [f, setF] = useState({
    distance: String(edge.distance_m),
    min: String(edge.min_travel_s),
    typical: String(edge.typical_travel_s),
    road: edge.road_name ?? "",
    direction: edge.direction ?? "",
    allowed: edge.allowed,
    geometry: "keep" as "keep" | "replace" | "clear",
    path: "",
  });
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setErr(null);
    const body: Record<string, unknown> = {};
    const d = num(f.distance);
    const mn = num(f.min);
    const ty = num(f.typical);
    if (d === undefined || mn === undefined || ty === undefined) return setErr("Distance and travel times must be numbers.");
    if (d !== edge.distance_m) body.distance_m = d;
    if (mn !== edge.min_travel_s) body.min_travel_s = mn;
    if (ty !== edge.typical_travel_s) body.typical_travel_s = ty;
    if (f.road !== (edge.road_name ?? "")) body.road_name = f.road;
    if (f.direction !== (edge.direction ?? "")) body.direction = f.direction;
    if (f.allowed !== edge.allowed) body.allowed = f.allowed;
    if (f.geometry === "clear") body.path = [];
    if (f.geometry === "replace") {
      const p = parsePath(f.path);
      if (!p) return setErr(PATH_HELP);
      body.path = p;
    }
    if (Object.keys(body).length === 0) return onClose();
    setBusy(true);
    try {
      await api.patch(`/api/topology/edges/${edge.id}`, body);
      await onDone();
      onClose();
    } catch (ex) {
      setErr(errorText(ex));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={`Edit edge ${edge.from_camera_id} → ${edge.to_camera_id}`} onClose={onClose}>
      <form onSubmit={submit} className="space-y-2">
        <div className="grid grid-cols-2 gap-2">
          <Field label="Distance (m)">
            <input className="input" type="number" min={1} step="any" value={f.distance} onChange={(e) => setF({ ...f, distance: e.target.value })} required />
          </Field>
          <Field label="Road name">
            <input className="input" maxLength={128} value={f.road} onChange={(e) => setF({ ...f, road: e.target.value })} />
          </Field>
          <Field label="Min travel (s)" hint="Faster than this = impossible match">
            <input className="input" type="number" min={0.1} step="any" value={f.min} onChange={(e) => setF({ ...f, min: e.target.value })} required />
          </Field>
          <Field label="Typical travel (s)">
            <input className="input" type="number" min={0.1} step="any" value={f.typical} onChange={(e) => setF({ ...f, typical: e.target.value })} required />
          </Field>
          <Field label="Travel direction">
            <select className="input" value={f.direction} onChange={(e) => setF({ ...f, direction: e.target.value })}>
              {(COMPASS.includes(f.direction) ? COMPASS : [...COMPASS, f.direction]).map((c) => (
                <option key={c} value={c}>
                  {c || "—"}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Geometry" hint={edge.has_geometry ? "Currently road-following" : "Currently a straight line"}>
            <select className="input" value={f.geometry} onChange={(e) => setF({ ...f, geometry: e.target.value as typeof f.geometry })}>
              <option value="keep">Keep current</option>
              <option value="replace">Replace with JSON below</option>
              {edge.has_geometry && <option value="clear">Clear (straight line)</option>}
            </select>
          </Field>
        </div>
        {f.geometry === "replace" && (
          <PathInput value={f.path} onChange={(v) => setF({ ...f, path: v })} hint={`JSON [[lat, lon], …] from ${edge.from_camera_id} to ${edge.to_camera_id}; at least two points.`} />
        )}
        <label className="flex items-center gap-1.5 text-[12px]">
          <input type="checkbox" checked={f.allowed} onChange={(e) => setF({ ...f, allowed: e.target.checked })} /> Allowed — unchecked blocks this direction (e.g. one-way road)
        </label>
        {err && <div className="text-[12px] text-rose-300" role="alert">{err}</div>}
        <div className="flex justify-end gap-1.5">
          <button type="button" className="btn btn-ghost" onClick={onClose}>
            Cancel
          </button>
          <button className="btn btn-primary" disabled={busy}>
            {busy ? "Saving…" : "Save changes"}
          </button>
        </div>
      </form>
    </Modal>
  );
}
