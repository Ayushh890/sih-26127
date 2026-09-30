/**
 * City network map drawn entirely from backend data — no internet tile server.
 * Roads and camera links come from /api/topology (offline road network); cameras,
 * trajectory paths and moving markers are supplied by the page.
 */
import { useEffect, useRef } from "react";
import L from "leaflet";
import { useQuery } from "@tanstack/react-query";
import { api } from "../lib/api";

export interface TopologyCamera {
  id: string;
  lat: number;
  lon: number;
  name: string;
  road_name: string | null;
  is_demo: boolean;
  direction: string | null;
}
export interface TopologyEdge {
  id: number;
  from_camera_id: string;
  to_camera_id: string;
  distance_m: number;
  min_travel_s: number;
  typical_travel_s: number;
  road_name: string | null;
  allowed: boolean;
  road_id: number | null;
  has_geometry: boolean;
}
export interface Topology {
  edges: TopologyEdge[];
  roads: { id: number; name: string; geojson: GeoJSON.LineString; speed_limit_kmh: number | null; lanes: number | null; is_demo: boolean }[];
  geojson: GeoJSON.FeatureCollection<GeoJSON.LineString, { from: string; to: string; allowed: boolean; road_name: string | null; distance_m: number }>;
  cameras: TopologyCamera[];
}

export function useTopology() {
  return useQuery({ queryKey: ["topology"], queryFn: () => api.get<Topology>("/api/topology"), staleTime: 60_000 });
}

export interface MapCamera {
  id: string;
  lat: number;
  lon: number;
  color: string;
  label?: string;
  tooltip?: string;
  /** Ring highlight (e.g. predicted next camera). */
  ring?: string;
  pulse?: boolean;
}
export interface MapPath {
  id: string;
  coords: [number, number][];
  color: string;
  weight?: number;
  dash?: string;
  tooltip?: string;
}
export interface MapPoint {
  id: string;
  lat: number;
  lon: number;
  color: string;
  label?: string;
  radius?: number;
}

interface Props {
  cameras: MapCamera[];
  paths?: MapPath[];
  points?: MapPoint[];
  selectedId?: string | null;
  onCameraClick?: (id: string) => void;
  showLinks?: boolean;
  className?: string;
  /** Change to re-fit the view to the given coordinates. */
  fitTo?: [number, number][] | null;
}

export function NetworkMap({ cameras, paths = [], points = [], selectedId, onCameraClick, showLinks = true, className = "", fitTo }: Props) {
  const el = useRef<HTMLDivElement>(null);
  const map = useRef<L.Map | null>(null);
  const layers = useRef<{ base: L.LayerGroup; cams: L.LayerGroup; paths: L.LayerGroup; points: L.LayerGroup } | null>(null);
  const fitted = useRef(false);
  const clickRef = useRef(onCameraClick);
  clickRef.current = onCameraClick;
  const topo = useTopology();

  useEffect(() => {
    if (!el.current || map.current) return;
    const m = L.map(el.current, { zoomControl: true, attributionControl: false, preferCanvas: false, zoomSnap: 0.25 });
    m.setView([26.85, 80.95], 13);
    map.current = m;
    layers.current = {
      base: L.layerGroup().addTo(m),
      paths: L.layerGroup().addTo(m),
      cams: L.layerGroup().addTo(m),
      points: L.layerGroup().addTo(m),
    };
    const ro = new ResizeObserver(() => m.invalidateSize());
    ro.observe(el.current);
    return () => {
      ro.disconnect();
      m.remove();
      map.current = null;
      layers.current = null;
      fitted.current = false;
    };
  }, []);

  // Offline road network + camera links.
  useEffect(() => {
    const lg = layers.current;
    const t = topo.data;
    if (!lg || !t) return;
    lg.base.clearLayers();
    for (const r of t.roads) {
      if (!r.geojson?.coordinates?.length) continue;
      const ll = r.geojson.coordinates.map(([lon, lat]) => [lat, lon] as [number, number]);
      L.polyline(ll, { color: "#1e293b", weight: 9, opacity: 1, interactive: false }).addTo(lg.base);
      L.polyline(ll, { color: "#334155", weight: 5, opacity: 1 }).bindTooltip(r.name, { sticky: true, className: "nirnay-tip" }).addTo(lg.base);
    }
    if (showLinks) {
      for (const f of t.geojson.features) {
        const ll = f.geometry.coordinates.map(([lon, lat]) => [lat, lon] as [number, number]);
        L.polyline(ll, { color: f.properties.allowed ? "#0e7490" : "#9f1239", weight: 1.5, opacity: 0.8, dashArray: f.properties.allowed ? "4 6" : "2 4" })
          .bindTooltip(
            `${f.properties.from} → ${f.properties.to}${f.properties.road_name ? ` · ${f.properties.road_name}` : ""} · ${Math.round(f.properties.distance_m)} m${f.properties.allowed ? "" : " · NOT ALLOWED"}`,
            { sticky: true, className: "nirnay-tip" },
          )
          .addTo(lg.base);
      }
    }
  }, [topo.data, showLinks]);

  // Initial fit to cameras.
  useEffect(() => {
    const m = map.current;
    if (!m || fitted.current) return;
    const pts = cameras.map((c) => [c.lat, c.lon] as [number, number]).filter(([a, b]) => Number.isFinite(a) && Number.isFinite(b));
    if (!pts.length) return;
    m.fitBounds(L.latLngBounds(pts).pad(0.15));
    fitted.current = true;
  }, [cameras]);

  useEffect(() => {
    const m = map.current;
    if (!m || !fitTo || fitTo.length === 0) return;
    m.fitBounds(L.latLngBounds(fitTo).pad(0.2), { maxZoom: 16 });
  }, [fitTo]);

  useEffect(() => {
    const lg = layers.current;
    if (!lg) return;
    lg.cams.clearLayers();
    for (const c of cameras) {
      if (!Number.isFinite(c.lat) || !Number.isFinite(c.lon)) continue;
      const sel = c.id === selectedId;
      if (c.ring) L.circleMarker([c.lat, c.lon], { radius: 15, color: c.ring, weight: 2, dashArray: "3 3", fill: false, interactive: false }).addTo(lg.cams);
      const mk = L.circleMarker([c.lat, c.lon], {
        radius: sel ? 10 : 8,
        color: sel ? "#e2e8f0" : "#0f172a",
        weight: sel ? 3 : 2,
        fillColor: c.color,
        fillOpacity: 0.95,
        className: c.pulse ? "nirnay-pulse" : undefined,
      });
      mk.bindTooltip(c.tooltip ?? c.label ?? c.id, { direction: "top", offset: [0, -8], className: "nirnay-tip" });
      mk.on("click", () => clickRef.current?.(c.id));
      mk.addTo(lg.cams);
      L.marker([c.lat, c.lon], {
        interactive: false,
        icon: L.divIcon({ className: "nirnay-cam-label", html: `<span>${escapeHtml(c.label ?? c.id)}</span>`, iconSize: [0, 0], iconAnchor: [-10, 18] }),
      }).addTo(lg.cams);
    }
  }, [cameras, selectedId]);

  useEffect(() => {
    const lg = layers.current;
    if (!lg) return;
    lg.paths.clearLayers();
    for (const p of paths) {
      if (p.coords.length < 2) continue;
      const pl = L.polyline(p.coords, { color: p.color, weight: p.weight ?? 4, opacity: 0.9, dashArray: p.dash, lineCap: "round" });
      if (p.tooltip) pl.bindTooltip(p.tooltip, { sticky: true, className: "nirnay-tip" });
      pl.addTo(lg.paths);
    }
  }, [paths]);

  useEffect(() => {
    const lg = layers.current;
    if (!lg) return;
    lg.points.clearLayers();
    for (const p of points) {
      const mk = L.circleMarker([p.lat, p.lon], { radius: p.radius ?? 6, color: "#f8fafc", weight: 2, fillColor: p.color, fillOpacity: 1 });
      if (p.label) mk.bindTooltip(p.label, { permanent: true, direction: "right", offset: [8, 0], className: "nirnay-tip" });
      mk.addTo(lg.points);
    }
  }, [points]);

  return (
    <div className={`relative ${className}`}>
      <div ref={el} className="absolute inset-0 rounded bg-ink-950" data-testid="network-map" />
      {topo.error ? <div className="absolute left-2 top-2 z-[500] rounded bg-rose-950/80 px-2 py-1 text-[11px] text-rose-200">Road network unavailable</div> : null}
    </div>
  );
}

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]!);
}

/** Resolve a road-following polyline between two adjacent cameras from topology (falls back to a straight line). */
export function edgePath(t: Topology | undefined, from: string, to: string, fallback: [number, number][]): [number, number][] {
  const f = t?.geojson.features.find((x) => x.properties.from === from && x.properties.to === to);
  if (!f) return fallback;
  return f.geometry.coordinates.map(([lon, lat]) => [lat, lon] as [number, number]);
}

/** Interpolate a position along a polyline at fraction 0..1 (by length). */
export function along(coords: [number, number][], frac: number): [number, number] {
  if (coords.length === 0) return [0, 0];
  if (coords.length === 1 || frac <= 0) return coords[0];
  if (frac >= 1) return coords[coords.length - 1];
  const seg: number[] = [];
  let total = 0;
  for (let i = 1; i < coords.length; i++) {
    const d = Math.hypot(coords[i][0] - coords[i - 1][0], coords[i][1] - coords[i - 1][1]);
    seg.push(d);
    total += d;
  }
  let target = total * frac;
  for (let i = 0; i < seg.length; i++) {
    if (target <= seg[i]) {
      const r = seg[i] ? target / seg[i] : 0;
      return [coords[i][0] + (coords[i + 1][0] - coords[i][0]) * r, coords[i][1] + (coords[i + 1][1] - coords[i][1]) * r];
    }
    target -= seg[i];
  }
  return coords[coords.length - 1];
}
