/**
 * Aggregate traffic analytics. Every number is computed by the backend from persisted
 * data; empty periods show "No data available for this period.". Aggregates never list
 * individual vehicles — only the audited OD drill-down does, for authorised users.
 */
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";
import { P, useAuth } from "../lib/auth";
import { useScope } from "../lib/scope";
import { fromLocalInput, toLocalInput } from "../lib/format";
import { PageHeader, Panel, Tabs } from "../components/ui";
import { PERIODS, PERIOD_SECONDS, type Period, type Win } from "../components/analytics/common";
import { SummarySection } from "../components/analytics/SummarySection";
import { TimeseriesSection } from "../components/analytics/TimeseriesSection";
import { CongestionSection } from "../components/analytics/CongestionSection";
import { HotspotsSection } from "../components/analytics/HotspotsSection";
import { OdSection } from "../components/analytics/OdSection";
import { TravelTimesSection } from "../components/analytics/TravelTimesSection";
import { IncidentSection } from "../components/analytics/IncidentSection";

const TABS = [
  { id: "summary", label: "Summary" },
  { id: "timeseries", label: "Time series" },
  { id: "congestion", label: "Congestion" },
  { id: "hotspots", label: "Hotspots" },
  { id: "od", label: "OD matrix" },
  { id: "travel", label: "Travel times" },
  { id: "incident", label: "Incident impact" },
] as const;
type TabId = (typeof TABS)[number]["id"];

/** Tabs driven by the period selector (the others use their own window). */
const PERIOD_TABS: TabId[] = ["summary", "timeseries", "hotspots", "od"];
const CAMERA_TABS: TabId[] = ["summary", "timeseries"];

export default function Analytics() {
  const [sp, setSp] = useSearchParams();
  const { has } = useAuth();
  const { param: scope } = useScope();

  const tab = (TABS.some((t) => t.id === sp.get("tab")) ? sp.get("tab") : "summary") as TabId;
  const period = (PERIODS as readonly string[]).includes(sp.get("period") ?? "") ? (sp.get("period") as Period) : "1h";
  const since = sp.get("since") ?? undefined;
  const until = sp.get("until") ?? undefined;
  const cameraId = sp.get("camera") ?? undefined;
  const custom = !!since;
  const win: Win = { period, since, until };

  const [draftSince, setDraftSince] = useState(since ? toLocalInput(new Date(since)) : "");
  const [draftUntil, setDraftUntil] = useState(until ? toLocalInput(new Date(until)) : "");
  const [showCustom, setShowCustom] = useState(custom);

  const set = (patch: Record<string, string | undefined>) => {
    const next = new URLSearchParams(sp);
    for (const [k, v] of Object.entries(patch)) {
      if (v === undefined || v === "") next.delete(k);
      else next.set(k, v);
    }
    setSp(next, { replace: true });
  };

  const cams = useQuery({
    queryKey: ["cameras", "lite", scope],
    queryFn: () => api.get<{ cameras: { id: string; name: string }[] }>("/api/cameras", { scope }),
    enabled: has(P.CAMERAS_READ),
    staleTime: 60_000,
  });

  const usesPeriod = PERIOD_TABS.includes(tab);
  const usesCamera = CAMERA_TABS.includes(tab);
  const ttDefault = Math.min(86400, PERIOD_SECONDS[period]);

  return (
    <div className="space-y-2">
      <PageHeader title="Traffic analytics" subtitle="Computed from persisted detections, tracks and journeys. Aggregates only — no individual vehicles." />

      <Panel bodyClass="p-2">
        <div className="flex flex-wrap items-end gap-3">
          <div className={usesPeriod ? "" : "opacity-50"} title={usesPeriod ? undefined : "This section uses its own window"}>
            <span className="label">Period</span>
            <div className="flex gap-0.5" role="group" aria-label="Period">
              {PERIODS.map((p) => (
                <button
                  key={p}
                  type="button"
                  aria-pressed={!custom && period === p}
                  className={`btn px-2 ${!custom && period === p ? "btn-primary" : ""}`}
                  onClick={() => {
                    setShowCustom(false);
                    set({ period: p, since: undefined, until: undefined });
                  }}
                >
                  {p}
                </button>
              ))}
              <button type="button" aria-pressed={custom} className={`btn px-2 ${custom ? "btn-primary" : ""}`} onClick={() => setShowCustom((v) => !v)}>
                Custom…
              </button>
            </div>
          </div>
          {showCustom && (
            <form
              className="flex flex-wrap items-end gap-1.5"
              onSubmit={(e) => {
                e.preventDefault();
                const s = fromLocalInput(draftSince);
                if (!s) return;
                set({ since: s, until: fromLocalInput(draftUntil) });
              }}
            >
              <label className="block">
                <span className="label">Since</span>
                <input type="datetime-local" className="input w-auto" required value={draftSince} onChange={(e) => setDraftSince(e.target.value)} aria-label="Since" />
              </label>
              <label className="block">
                <span className="label">Until (empty = now)</span>
                <input type="datetime-local" className="input w-auto" value={draftUntil} onChange={(e) => setDraftUntil(e.target.value)} aria-label="Until" />
              </label>
              <button type="submit" className="btn btn-primary" disabled={!draftSince}>
                Apply
              </button>
              <span className="text-[11px] text-slate-500">max 92 days</span>
            </form>
          )}
          {usesCamera && (
            <label className="block">
              <span className="label">Camera</span>
              {cams.data ? (
                <select className="input w-auto" value={cameraId ?? ""} onChange={(e) => set({ camera: e.target.value })} aria-label="Camera">
                  <option value="">All cameras in scope</option>
                  {cams.data.cameras.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.id} · {c.name}
                    </option>
                  ))}
                </select>
              ) : (
                <input className="input mono w-32" placeholder="all" defaultValue={cameraId ?? ""} onBlur={(e) => set({ camera: e.target.value.trim() })} aria-label="Camera" />
              )}
            </label>
          )}
        </div>
      </Panel>

      <Tabs tabs={TABS as unknown as { id: TabId; label: string }[]} value={tab} onChange={(t) => set({ tab: t === "summary" ? undefined : t })} />

      <div>
        {tab === "summary" && <SummarySection win={win} scope={scope} cameraId={cameraId} />}
        {tab === "timeseries" && <TimeseriesSection win={win} scope={scope} cameraId={cameraId} />}
        {tab === "congestion" && <CongestionSection scope={scope} />}
        {tab === "hotspots" && <HotspotsSection win={win} scope={scope} />}
        {tab === "od" && <OdSection win={win} scope={scope} />}
        {tab === "travel" && <TravelTimesSection key={ttDefault} scope={scope} defaultWindow={ttDefault} />}
        {tab === "incident" && <IncidentSection />}
      </div>
    </div>
  );
}
