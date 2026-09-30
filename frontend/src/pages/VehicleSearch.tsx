/**
 * Vehicle search: sightings by plate (exact / partial / fuzzy / pseudonym) and attribute
 * filters, global vehicle identities, and the recent-sightings feed. All filters live in the
 * URL so a result set is linkable; every search is written to the backend audit log.
 */
import { useEffect, useRef, useState, type FormEvent } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { P, useAuth } from "../lib/auth";
import { useScope } from "../lib/scope";
import { useLiveEvent } from "../lib/ws";
import { fmtAgo, fmtKm, fmtNum, fmtPct, fmtTime, fromLocalInput, toLocalInput } from "../lib/format";
import { Badge, Empty, ErrorBox, Field, Loading, PageHeader, Panel, SyntheticBanner, Tabs } from "../components/ui";
import type { CameraLite, Observation, ScopeInfo, SearchResponse, VehiclesResponse } from "../components/vehicles/types";
import { MatchBadge, ObsLink, Pager, PlateText, Reasons, SyntheticBadge, VehicleLink } from "../components/vehicles/common";

type Tab = "search" | "vehicles" | "recent";

/** Query-string keys of GET /api/vehicles/search (backend app/api/routes/vehicles.py). */
const SEARCH_KEYS = [
  "plate",
  "fuzzy",
  "max_distance",
  "camera_id",
  "since",
  "until",
  "vehicle_class",
  "color",
  "min_plate_confidence",
  "match_level",
  "unreadable_plate",
  "limit",
  "offset",
] as const;
type SearchKey = (typeof SEARCH_KEYS)[number];
type Draft = Record<SearchKey, string>;

/** Vocabulary the backend pipeline emits (detector classes; app/ml/preprocessing/image.py colours). Free text is allowed too. */
const CLASS_HINTS = ["car", "motorcycle", "bus", "truck", "bicycle"];
const COLOR_HINTS = ["black", "white", "silver", "grey", "red", "orange", "brown", "yellow", "green", "blue", "purple", "unknown"];

function draftFrom(sp: URLSearchParams): Draft {
  const d = {} as Draft;
  for (const k of SEARCH_KEYS) d[k] = sp.get(k) ?? "";
  return d;
}

function useCameras() {
  const { has } = useAuth();
  const { param } = useScope();
  return useQuery({
    queryKey: ["cameras", param],
    queryFn: () => api.get<{ cameras: CameraLite[] }>("/api/cameras", { scope: param }),
    enabled: has(P.CAMERAS_READ),
    staleTime: 60_000,
  });
}

function CameraSelect({ value, onChange, id }: { value: string; onChange: (v: string) => void; id?: string }) {
  const cams = useCameras();
  if (!cams.data) {
    return <input id={id} className="input" value={value} onChange={(e) => onChange(e.target.value)} placeholder={cams.isLoading ? "loading cameras…" : "CAM-01"} />;
  }
  return (
    <select id={id} className="input" value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">Any camera</option>
      {cams.data.cameras.map((c) => (
        <option key={c.id} value={c.id}>
          {c.id} · {c.name}
          {c.is_demo ? " (synthetic)" : ""}
        </option>
      ))}
    </select>
  );
}

export default function VehicleSearch() {
  const [sp, setSp] = useSearchParams();
  const { has } = useAuth();
  // /api/vehicles and /api/observations require trajectory:read (the search itself needs vehicles:search).
  const canTraj = has(P.TRAJECTORY_READ);
  const requested = (sp.get("tab") as Tab) || "search";
  const tab: Tab = requested !== "search" && !canTraj ? "search" : requested;
  const setTab = (t: Tab) => {
    const n = new URLSearchParams(sp);
    if (t === "search") n.delete("tab");
    else n.set("tab", t);
    setSp(n);
  };
  return (
    <div className="flex flex-col gap-2">
      <PageHeader
        title="Vehicle search"
        subtitle="Find sightings by plate or attributes. Every search is recorded in the audit log (search audit history)."
      />
      <Tabs<Tab>
        value={tab}
        onChange={setTab}
        tabs={[
          { id: "search" as Tab, label: "Sightings search" },
          ...(canTraj
            ? [
                { id: "vehicles" as Tab, label: "Vehicle identities" },
                { id: "recent" as Tab, label: "Recent observations" },
              ]
            : []),
        ]}
      />
      {tab === "search" && <SearchTab sp={sp} setSp={setSp} />}
      {tab === "vehicles" && <VehiclesTab sp={sp} setSp={setSp} />}
      {tab === "recent" && <RecentTab sp={sp} setSp={setSp} />}
    </div>
  );
}

interface TabProps {
  sp: URLSearchParams;
  setSp: (n: URLSearchParams) => void;
}

// ---------------------------------------------------------------------------------------------
// Sightings search
// ---------------------------------------------------------------------------------------------

function SearchTab({ sp, setSp }: TabProps) {
  const { user } = useAuth();
  const { param } = useScope();
  const rawOk = !!user?.can_view_raw_plates;
  const [draft, setDraft] = useState<Draft>(() => draftFrom(sp));
  const spKey = sp.toString();
  useEffect(() => setDraft(draftFrom(new URLSearchParams(spKey))), [spKey]);

  const active = SEARCH_KEYS.some((k) => sp.has(k));
  const query: Record<string, string> = {};
  for (const k of SEARCH_KEYS) {
    const v = sp.get(k);
    if (v) query[k] = v;
  }
  const limit = Number(query.limit ?? 50) || 50;
  const offset = Number(query.offset ?? 0) || 0;

  const q = useQuery({
    queryKey: ["vehicle-search", query, param],
    queryFn: ({ signal }) => api.get<SearchResponse>("/api/vehicles/search", { ...query, scope: param }, signal),
    enabled: active,
    staleTime: 30_000,
  });

  const set = (k: SearchKey, v: string) => setDraft((d) => ({ ...d, [k]: v }));

  const apply = (d: Draft, newOffset = 0) => {
    const n = new URLSearchParams();
    const t = sp.get("tab");
    if (t) n.set("tab", t);
    for (const k of SEARCH_KEYS) {
      if (k === "offset" || k === "limit") continue;
      if (k === "max_distance" && d.fuzzy !== "true") continue;
      if (d[k]) n.set(k, d[k]);
    }
    n.set("limit", d.limit || "50");
    if (newOffset) n.set("offset", String(newOffset));
    setSp(n);
  };

  const submit = (e: FormEvent) => {
    e.preventDefault();
    apply(draft);
  };

  const clear = () => {
    const n = new URLSearchParams();
    const t = sp.get("tab");
    if (t) n.set("tab", t);
    setSp(n);
  };

  const isPseudoQuery = draft.plate.trim().toUpperCase().startsWith("PSN-");

  return (
    <div className="flex flex-col gap-2">
      <Panel title="Search criteria">
        <form onSubmit={submit} className="grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-6">
          <div className="col-span-2">
            <Field
              label="Plate"
              hint={
                rawOk
                  ? "Partial match by default (min 2 characters); a PSN-… pseudonym also works."
                  : "Privacy mode: your role can search by pseudonym (PSN-…) only."
              }
            >
              <input
                className="input mono"
                value={draft.plate}
                onChange={(e) => set("plate", e.target.value)}
                placeholder={rawOk ? "UP32GM2024 or PSN-…" : "PSN-…"}
                maxLength={24}
                aria-label="Plate"
              />
            </Field>
          </div>
          <Field label="Fuzzy (OCR errors)" hint={isPseudoQuery ? "Not used for pseudonym search" : "Weighted edit distance"}>
            <div className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={draft.fuzzy === "true"}
                onChange={(e) => set("fuzzy", e.target.checked ? "true" : "")}
                disabled={!rawOk || isPseudoQuery}
                aria-label="Fuzzy"
              />
              <input
                type="number"
                className="input"
                min={0}
                max={4}
                step={0.5}
                value={draft.max_distance}
                placeholder="max dist. (1.5)"
                disabled={draft.fuzzy !== "true"}
                onChange={(e) => set("max_distance", e.target.value)}
                aria-label="Max edit distance"
              />
            </div>
          </Field>
          <Field label="Camera">
            <CameraSelect value={draft.camera_id} onChange={(v) => set("camera_id", v)} />
          </Field>
          <Field label="Since" hint="Default: 7 days before 'until'">
            <input
              type="datetime-local"
              className="input"
              value={draft.since ? toLocalInput(new Date(draft.since)) : ""}
              onChange={(e) => set("since", fromLocalInput(e.target.value) ?? "")}
            />
          </Field>
          <Field label="Until" hint="Default: now">
            <input
              type="datetime-local"
              className="input"
              value={draft.until ? toLocalInput(new Date(draft.until)) : ""}
              onChange={(e) => set("until", fromLocalInput(e.target.value) ?? "")}
            />
          </Field>
          <Field label="Class">
            <input className="input" list="veh-class-hints" value={draft.vehicle_class} onChange={(e) => set("vehicle_class", e.target.value)} placeholder="any" maxLength={16} />
          </Field>
          <Field label="Colour">
            <input className="input" list="veh-color-hints" value={draft.color} onChange={(e) => set("color", e.target.value)} placeholder="any" maxLength={16} />
          </Field>
          <Field label="Min plate confidence">
            <input
              type="number"
              className="input"
              min={0}
              max={1}
              step={0.05}
              value={draft.min_plate_confidence}
              onChange={(e) => set("min_plate_confidence", e.target.value)}
              placeholder="0 – 1"
            />
          </Field>
          <Field label="Match level">
            <select className="input" value={draft.match_level} onChange={(e) => set("match_level", e.target.value)}>
              <option value="">Any</option>
              <option value="HIGH">HIGH</option>
              <option value="MEDIUM">MEDIUM</option>
              <option value="LOW">LOW</option>
              <option value="NEW">NEW (first sighting)</option>
            </select>
          </Field>
          <Field label="Plate readability">
            <select className="input" value={draft.unreadable_plate} onChange={(e) => set("unreadable_plate", e.target.value)}>
              <option value="">Any</option>
              <option value="false">Plate read</option>
              <option value="true">Unreadable plate only</option>
            </select>
          </Field>
          <Field label="Page size">
            <select className="input" value={draft.limit || "50"} onChange={(e) => set("limit", e.target.value)}>
              {["25", "50", "100", "200", "500"].map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </Field>
          <div className="col-span-2 flex items-end gap-1.5">
            <button type="submit" className="btn btn-primary">
              Search
            </button>
            <button type="button" className="btn btn-ghost" onClick={clear}>
              Clear
            </button>
          </div>
          <datalist id="veh-class-hints">
            {CLASS_HINTS.map((c) => (
              <option key={c} value={c} />
            ))}
          </datalist>
          <datalist id="veh-color-hints">
            {COLOR_HINTS.map((c) => (
              <option key={c} value={c} />
            ))}
          </datalist>
        </form>
      </Panel>

      {!active ? (
        <Panel>
          <Empty>Set criteria and press Search. With no criteria the search returns all sightings of the last 7 days.</Empty>
        </Panel>
      ) : q.isLoading ? (
        <Loading label="Searching…" />
      ) : q.error ? (
        <ErrorBox error={q.error} onRetry={() => q.refetch()} />
      ) : q.data ? (
        <SearchResults data={q.data} limit={limit} offset={offset} onOffset={(o) => apply(draftFrom(sp), o)} fetching={q.isFetching} />
      ) : null}
    </div>
  );
}

function SearchResults({ data, limit, offset, onOffset, fetching }: { data: SearchResponse; limit: number; offset: number; onOffset: (o: number) => void; fetching: boolean }) {
  return (
    <div className="flex flex-col gap-2">
      <SyntheticBanner info={data} />
      <Panel
        title={`Sightings (${fmtNum(data.total)})`}
        subtitle={
          <>
            {fmtTime(data.since, true)} → {fmtTime(data.until, true)}
            {data.mode && (
              <>
                {" "}
                · match mode <span className="text-slate-300">{data.mode}</span>
              </>
            )}
            {data.scope && <> · scope {data.scope}</>}
            {fetching && " · refreshing…"}
          </>
        }
        actions={<Pager total={data.total} limit={data.limit ?? limit} offset={data.offset ?? offset} onOffset={onOffset} />}
        bodyClass="p-0"
      >
        {data.results.length === 0 ? <Empty>No sightings match these criteria.</Empty> : <ObservationTable rows={data.results} showDistance={data.mode === "fuzzy"} />}
      </Panel>
    </div>
  );
}

function ObservationTable({ rows, showDistance = false }: { rows: Observation[]; showDistance?: boolean }) {
  return (
    <div className="max-h-[65vh] overflow-auto">
      <table className="w-full text-[12px]">
        <thead>
          <tr>
            <th className="th">Obs</th>
            <th className="th">Time</th>
            <th className="th">Camera</th>
            <th className="th">Plate</th>
            {showDistance && <th className="th">Dist.</th>}
            <th className="th">Plate conf.</th>
            <th className="th">Class / colour</th>
            <th className="th">Speed</th>
            <th className="th">In view</th>
            <th className="th">Vehicle</th>
            <th className="th">Match</th>
            <th className="th w-[28%]">Match reasons</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((o) => (
            <tr key={o.id} className="hover:bg-ink-800/60">
              <td className="td">
                <ObsLink id={o.id} />
              </td>
              <td className="td mono whitespace-nowrap" title={o.observed_at ?? undefined}>
                {fmtTime(o.observed_at, true)}
              </td>
              <td className="td whitespace-nowrap">
                <Link to={`/cameras/${o.camera_id}`} className="mono text-slate-200 hover:text-cyan-300">
                  {o.camera_id}
                </Link>
                {o.camera_name && <div className="text-[11px] text-slate-500">{o.camera_name}</div>}
              </td>
              <td className="td">
                <div className="flex items-center gap-1">
                  <PlateText display={o.plate_display} text={o.plate_text} />
                  <SyntheticBadge show={o.is_demo} />
                </div>
                {o.plate_raw && o.plate_text && o.plate_raw !== o.plate_text && <div className="mono text-[11px] text-slate-500">raw {o.plate_raw}</div>}
              </td>
              {showDistance && <td className="td mono">{o.plate_distance != null ? o.plate_distance.toFixed(2) : "—"}</td>}
              <td className="td mono whitespace-nowrap">
                {fmtPct(o.plate_confidence, 1)}
                {o.plate_valid === false && (
                  <Badge tone="orange" className="ml-1">
                    invalid fmt
                  </Badge>
                )}
                {o.plate_votes != null && <div className="text-[11px] text-slate-500">{o.plate_votes} reads</div>}
              </td>
              <td className="td whitespace-nowrap">
                {o.vehicle_class ?? "—"}
                {o.class_confidence != null && <span className="text-[11px] text-slate-500"> {fmtPct(o.class_confidence)}</span>}
                <div className="text-[11px] text-slate-400">{o.vehicle_color ?? "—"}</div>
              </td>
              <td className="td mono whitespace-nowrap">
                {fmtNum(o.speed_kmh, 1, "km/h")}
                {o.speed_kmh != null && o.speed_is_estimate && <div className="text-[10px] text-slate-500">estimate</div>}
              </td>
              <td className="td mono whitespace-nowrap text-[11px] text-slate-400">
                {fmtTime(o.first_seen_at)}
                <br />
                {fmtTime(o.last_seen_at)}
              </td>
              <td className="td">
                <VehicleLink code={o.vehicle_code} />
              </td>
              <td className="td">
                <MatchBadge level={o.match_confidence_level} score={o.match_score} />
              </td>
              <td className="td">
                <Reasons reasons={o.match_reasons} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
// Global vehicle identities (GET /api/vehicles)
// ---------------------------------------------------------------------------------------------

function VehiclesTab({ sp, setSp }: TabProps) {
  const { param } = useScope();
  const minCams = sp.get("v_min_cameras") ?? "1";
  const since = sp.get("v_since") ?? "";
  const limit = Number(sp.get("v_limit") ?? 50) || 50;
  const offset = Number(sp.get("v_offset") ?? 0) || 0;
  const update = (patch: Record<string, string | null>) => {
    const n = new URLSearchParams(sp);
    for (const [k, v] of Object.entries(patch)) {
      if (v == null || v === "") n.delete(k);
      else n.set(k, v);
    }
    setSp(n);
  };
  const q = useQuery({
    queryKey: ["vehicles", param, minCams, since, limit, offset],
    queryFn: ({ signal }) => api.get<VehiclesResponse>("/api/vehicles", { scope: param, min_cameras: minCams, since, limit, offset }, signal),
  });
  return (
    <div className="flex flex-col gap-2">
      <Panel title="Filters">
        <div className="flex flex-wrap items-end gap-2">
          <div className="w-40">
            <Field label="Min cameras">
              <input type="number" min={1} max={50} className="input" value={minCams} onChange={(e) => update({ v_min_cameras: e.target.value, v_offset: null })} />
            </Field>
          </div>
          <div className="w-56">
            <Field label="Last seen since">
              <input
                type="datetime-local"
                className="input"
                value={since ? toLocalInput(new Date(since)) : ""}
                onChange={(e) => update({ v_since: fromLocalInput(e.target.value) ?? null, v_offset: null })}
              />
            </Field>
          </div>
          <div className="w-28">
            <Field label="Page size">
              <select className="input" value={String(limit)} onChange={(e) => update({ v_limit: e.target.value, v_offset: null })}>
                {["25", "50", "100", "200"].map((n) => (
                  <option key={n}>{n}</option>
                ))}
              </select>
            </Field>
          </div>
        </div>
      </Panel>
      {q.isLoading ? (
        <Loading />
      ) : q.error ? (
        <ErrorBox error={q.error} onRetry={() => q.refetch()} />
      ) : q.data ? (
        <>
          <SyntheticBanner info={q.data} />
          <Panel
            title={`Vehicle identities (${fmtNum(q.data.total)})`}
            subtitle="Global identities linked across cameras, most recently seen first"
            actions={<Pager total={q.data.total} limit={limit} offset={offset} onOffset={(o) => update({ v_offset: o ? String(o) : null })} />}
            bodyClass="p-0"
          >
            {q.data.results.length === 0 ? (
              <Empty />
            ) : (
              <div className="max-h-[65vh] overflow-auto">
                <table className="w-full text-[12px]">
                  <thead>
                    <tr>
                      <th className="th">Vehicle</th>
                      <th className="th">Plate</th>
                      <th className="th">Plate conf.</th>
                      <th className="th">Class / colour</th>
                      <th className="th">First seen</th>
                      <th className="th">Last seen</th>
                      <th className="th">Cameras</th>
                      <th className="th">Sightings</th>
                      <th className="th">Distance</th>
                      <th className="th">Predicted next</th>
                    </tr>
                  </thead>
                  <tbody>
                    {q.data.results.map((v) => (
                      <tr key={v.id} className="hover:bg-ink-800/60">
                        <td className="td">
                          <div className="flex items-center gap-1">
                            <VehicleLink code={v.code} />
                            <SyntheticBadge show={v.is_demo} />
                          </div>
                        </td>
                        <td className="td">
                          <PlateText display={v.plate_display} text={v.plate_text} />
                        </td>
                        <td className="td mono">{fmtPct(v.plate_confidence, 1)}</td>
                        <td className="td">
                          {v.vehicle_class ?? "—"} <span className="text-slate-400">/ {v.vehicle_color ?? "—"}</span>
                        </td>
                        <td className="td mono whitespace-nowrap">
                          {fmtTime(v.first_seen_at, true)}
                          <div className="text-[11px] text-slate-500">{v.first_camera_id}</div>
                        </td>
                        <td className="td mono whitespace-nowrap">
                          {fmtTime(v.last_seen_at, true)}
                          <div className="text-[11px] text-slate-500">
                            {v.last_camera_id} · {fmtAgo(v.last_seen_at)}
                          </div>
                        </td>
                        <td className="td mono">{v.camera_count}</td>
                        <td className="td mono">{v.observation_count}</td>
                        <td className="td mono">{fmtKm(v.total_distance_m)}</td>
                        <td className="td">
                          {v.prediction ? (
                            <span className="mono">
                              {v.prediction.camera_id} <span className="text-slate-400">{fmtPct(v.prediction.probability)}</span>{" "}
                              <span className="text-[11px] text-slate-500">{v.prediction.status}</span>
                            </span>
                          ) : (
                            <span className="text-slate-500">—</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>
        </>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------------------------
// Recent observations feed (GET /api/observations)
// ---------------------------------------------------------------------------------------------

function RecentTab({ sp, setSp }: TabProps) {
  const { param } = useScope();
  const qc = useQueryClient();
  const camera = sp.get("o_camera_id") ?? "";
  const sinceId = sp.get("o_since_id") ?? "";
  const limit = sp.get("o_limit") ?? "50";
  const [sinceDraft, setSinceDraft] = useState(sinceId);
  useEffect(() => setSinceDraft(sinceId), [sinceId]);
  const update = (patch: Record<string, string | null>) => {
    const n = new URLSearchParams(sp);
    for (const [k, v] of Object.entries(patch)) {
      if (v == null || v === "") n.delete(k);
      else n.set(k, v);
    }
    setSp(n);
  };
  const key = ["observations", param, camera, sinceId, limit];
  const q = useQuery({
    queryKey: key,
    queryFn: ({ signal }) => api.get<ScopeInfo & { results: Observation[] }>("/api/observations", { scope: param, camera_id: camera, since_id: sinceId, limit }, signal),
  });
  // New sightings arrive as vehicle_matched events: refresh the feed (at most every 2 s).
  const lastRefresh = useRef(0);
  useLiveEvent("vehicle_matched", () => {
    const now = Date.now();
    if (now - lastRefresh.current < 2000) return;
    lastRefresh.current = now;
    qc.invalidateQueries({ queryKey: ["observations"] });
  });
  return (
    <div className="flex flex-col gap-2">
      <Panel title="Filters">
        <div className="flex flex-wrap items-end gap-2">
          <div className="w-72">
            <Field label="Camera">
              <CameraSelect value={camera} onChange={(v) => update({ o_camera_id: v })} />
            </Field>
          </div>
          <form
            className="flex items-end gap-1"
            onSubmit={(e) => {
              e.preventDefault();
              update({ o_since_id: sinceDraft });
            }}
          >
            <div className="w-44">
              <Field label="Newer than obs. ID">
                <input type="number" min={0} className="input" value={sinceDraft} onChange={(e) => setSinceDraft(e.target.value)} />
              </Field>
            </div>
            <button className="btn" type="submit">
              Apply
            </button>
          </form>
          <div className="w-28">
            <Field label="Limit">
              <select className="input" value={limit} onChange={(e) => update({ o_limit: e.target.value })}>
                {["25", "50", "100", "200"].map((n) => (
                  <option key={n}>{n}</option>
                ))}
              </select>
            </Field>
          </div>
          <button className="btn" onClick={() => q.refetch()} disabled={q.isFetching}>
            {q.isFetching ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </Panel>
      {q.isLoading ? (
        <Loading />
      ) : q.error ? (
        <ErrorBox error={q.error} onRetry={() => q.refetch()} />
      ) : q.data ? (
        <>
          <SyntheticBanner info={q.data} />
          <Panel title={`Recent observations (${q.data.results.length})`} subtitle="Newest first · updates live on new matches" bodyClass="p-0">
            {q.data.results.length === 0 ? <Empty /> : <ObservationTable rows={q.data.results} />}
          </Panel>
        </>
      ) : null}
    </div>
  );
}
