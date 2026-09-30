import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import type { ReactNode } from "react";
import { AuthProvider } from "../lib/auth";
import { ScopeProvider } from "../lib/scope";
import { stream, type LiveEvent } from "../lib/ws";
import Cameras from "../pages/Cameras";
import CameraDetail, { buildCameraPatch } from "../pages/CameraDetail";
import {
  applyStatus,
  diffObject,
  filterCameras,
  inScope,
  numList,
  partialPath,
  progress,
  pushDetection,
  upsertOpenAlert,
  type Alert,
  type AlertList,
  type Camera,
  type Detection,
} from "../components/cameras/model";

/* ------------------------------------------------------------------ fixtures */

function cam(over: Partial<Camera> = {}): Camera {
  return {
    id: "CAM01",
    name: "MG Road North",
    location: "MG Road",
    latitude: 12.97,
    longitude: 77.59,
    source_type: "rtsp",
    source_uri: "rtsp://10.0.0.5/stream1",
    has_credentials: true,
    username_hint: "ad***",
    resolution: "1280x720",
    lane_count: 3,
    direction: "N",
    road_name: "MG Road",
    zone: "Central",
    camera_type: "ANPR",
    enabled: true,
    status: "ONLINE",
    status_message: null,
    last_seen_at: null,
    processing: { processing_fps: 5, confidence_threshold: 0.35, frame_skip: 0, max_queue_size: 8 },
    calibration: { reference_width: 1280, vanishing_point: [0.5, 0.2] },
    is_demo: false,
    created_at: null,
    updated_at: null,
    runtime: null,
    ...over,
  } as Camera;
}

const ME = (perms: string[]) => ({ id: 1, username: "op", full_name: null, role: "operator", permissions: perms, can_view_raw_plates: false });
const ALL_PERMS = ["cameras:read", "cameras:write", "cameras:control", "stream:view"];

type Call = { method: string; url: string; body: unknown };
let calls: Call[] = [];
type Handler = (method: string, url: string, body: unknown) => { status?: number; body: unknown } | undefined;

function mockFetch(handler: Handler, perms = ALL_PERMS) {
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      calls.push({ method, url: input, body });
      const r = input === "/api/auth/me" ? { body: ME(perms) } : handler(method, input, body);
      const status = r?.status ?? (r ? 200 : 404);
      const payload = r ? r.body : { detail: `unmocked ${method} ${input}` };
      return { ok: status < 400, status, text: async () => JSON.stringify(payload) } as Response;
    }),
  );
}

function wrap(ui: ReactNode, path = "/") {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AuthProvider>
        <ScopeProvider>
          <MemoryRouter initialEntries={[path]}>{ui}</MemoryRouter>
        </ScopeProvider>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

/** Deliver a WebSocket event to subscribed hooks without opening a socket. */
function emit(ev: LiveEvent) {
  const handlers = (stream as unknown as { handlers: Map<string, Set<(e: LiveEvent) => void>> }).handlers;
  act(() => {
    handlers.get(ev.type)?.forEach((h) => h(ev));
  });
}

beforeEach(() => {
  sessionStorage.setItem("nirnay.token", "test-token");
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  sessionStorage.clear();
  localStorage.clear();
});

/* ------------------------------------------------------------------ pure helpers */

describe("camera model helpers", () => {
  it("inScope filters demo/live items by the resolved scope", () => {
    expect(inScope("live", true)).toBe(false);
    expect(inScope("live", false)).toBe(true);
    expect(inScope("demo", true)).toBe(true);
    expect(inScope("demo", false)).toBe(false);
    expect(inScope("all", true)).toBe(true);
    expect(inScope(undefined, false)).toBe(true);
  });

  it("filterCameras combines text, status, zone and source filters", () => {
    const list = [cam(), cam({ id: "CAM02", name: "Ring Rd", status: "OFFLINE", zone: "East", road_name: "Ring Road", source_type: "file" })];
    expect(filterCameras(list, { text: "ring", status: "", zone: "", source: "" }).map((c) => c.id)).toEqual(["CAM02"]);
    expect(filterCameras(list, { text: "", status: "ONLINE", zone: "", source: "" }).map((c) => c.id)).toEqual(["CAM01"]);
    expect(filterCameras(list, { text: "", status: "", zone: "East", source: "file" }).map((c) => c.id)).toEqual(["CAM02"]);
    expect(filterCameras(list, { text: "cam0", status: "", zone: "", source: "" })).toHaveLength(2);
  });

  it("pushDetection prepends, de-duplicates by observation and bounds the feed", () => {
    const d = (id: number) => ({ observation_id: id }) as Detection;
    let feed: Detection[] = [];
    for (let i = 1; i <= 70; i++) feed = pushDetection(feed, d(i));
    expect(feed).toHaveLength(60);
    expect(feed[0].observation_id).toBe(70);
    feed = pushDetection(feed, d(65));
    expect(feed).toHaveLength(60);
    expect(feed.filter((x) => x.observation_id === 65)).toHaveLength(1);
    expect(feed[0].observation_id).toBe(65);
  });

  it("upsertOpenAlert inserts, updates in place and drops resolved alerts", () => {
    const a = (id: number, status = "OPEN") => ({ id, code: `ALT-${id}`, status }) as unknown as Alert;
    let l: AlertList = { results: [a(1)], total: 1 } as AlertList;
    l = upsertOpenAlert(l, a(2), 10);
    expect(l.results.map((x) => x.id)).toEqual([2, 1]);
    expect(l.total).toBe(2);
    l = upsertOpenAlert(l, a(1, "ACKNOWLEDGED"), 10);
    expect(l.results.find((x) => x.id === 1)?.status).toBe("ACKNOWLEDGED");
    expect(l.total).toBe(2);
    l = upsertOpenAlert(l, a(2, "RESOLVED"), 10);
    expect(l.results.map((x) => x.id)).toEqual([1]);
    expect(l.total).toBe(1);
  });

  it("applyStatus copies status/message/ts from camera_status_changed", () => {
    const c = applyStatus(cam(), { camera_id: "CAM01", old_status: "ONLINE", status: "OFFLINE", message: "stream lost", ts: "2026-09-30T10:00:00Z", is_demo: false, intentional: false });
    expect(c.status).toBe("OFFLINE");
    expect(c.status_message).toBe("stream lost");
  });

  it("partialPath returns a length-proportional prefix of a polyline", () => {
    const line: [number, number][] = [
      [0, 0],
      [0, 1],
      [0, 3],
    ];
    expect(partialPath(line, 0)).toEqual([[0, 0]]);
    expect(partialPath(line, 1)).toEqual(line);
    expect(partialPath(line, 0.5)).toEqual([
      [0, 0],
      [0, 1],
      [0, 1.5],
    ]);
    expect(progress(1000, 3000, 2500)).toBe(0.5);
    expect(progress(1000, 3000, 9000)).toBe(1);
  });

  it("diffObject and numList produce minimal, validated PATCH values", () => {
    expect(diffObject({ a: 1, b: "x", c: [1, 2] }, { a: 1, b: "y", c: [1, 2], d: undefined })).toEqual({ b: "y" });
    expect(numList("")).toBeUndefined();
    expect(numList("0.1, 0.5,0.9")).toEqual([0.1, 0.5, 0.9]);
    expect(numList("0.1, x")).toBeNull();
  });

  it("buildCameraPatch sends only changed fields and never echoes credentials", () => {
    const c = cam();
    const base = {
      name: c.name,
      location: "MG Road",
      latitude: "12.97",
      longitude: "77.59",
      road_name: "MG Road",
      zone: "Central",
      lane_count: "3",
      direction: "N",
      camera_type: "ANPR",
      resolution: "1280x720",
      source_type: "rtsp",
      source_uri: c.source_uri,
      username: "",
      password: "",
      clear_credentials: false,
      processing_fps: "5",
      confidence_threshold: "0.35",
      frame_skip: "0",
      max_queue_size: "8",
      reference_width: "1280",
      focal_px: "",
      camera_height_m: "",
      speed_limit_kmh: "",
      flow_toward_camera: false,
      one_way: false,
      vanishing_point: "0.5, 0.2",
      lane_boundaries: "",
    };
    expect(buildCameraPatch(c, base)).toEqual({});
    expect(buildCameraPatch(c, { ...base, name: "New", processing_fps: "8", lane_boundaries: "0.1,0.5,0.9" })).toEqual({
      name: "New",
      processing: { processing_fps: 8 },
      calibration: { lane_boundaries: [0.1, 0.5, 0.9] },
    });
    expect(buildCameraPatch(c, { ...base, password: "s3cret" })).toEqual({ password: "s3cret" });
    expect(buildCameraPatch(c, { ...base, one_way: true })).toEqual({ calibration: { one_way: true } });
    expect(() => buildCameraPatch(c, { ...base, vanishing_point: "0.5" })).toThrow(/Vanishing point/);
    // demo cameras: the source cannot be changed server-side, so it is never sent
    const demo = cam({ is_demo: true, source_type: "demo", source_uri: "demo://CAM01" });
    expect(buildCameraPatch(demo, { ...base, source_type: "demo", source_uri: "rtsp://x", password: "p" })).toEqual({});
  });
});

/* ------------------------------------------------------------------ Cameras page */

describe("Cameras page", () => {
  const LIST = {
    scope: "live",
    synthetic: false,
    notice: null,
    cameras: [cam(), cam({ id: "CAM02", name: "Ring Road East", status: "OFFLINE", status_message: "connection refused", zone: "East", has_credentials: false })],
  };
  const RUNTIME = { ts: 1, cameras: { CAM01: { input_fps: 25, processing_fps: 2, target_processing_fps: 5, queue_depth: 3, max_queue_size: 8, latency_ms: 140, frames_dropped: 7 } } };

  it("renders cameras with live runtime, filters, and updates on camera_status_changed", async () => {
    mockFetch((method, url) => {
      if (method === "GET" && url === "/api/cameras") return { body: LIST };
      if (method === "GET" && url === "/api/cameras/runtime") return { body: RUNTIME };
      return undefined;
    });
    wrap(<Cameras />);
    expect(await screen.findByText("Ring Road East")).toBeInTheDocument();
    expect(screen.getByText("MG Road North")).toBeInTheDocument();
    expect(screen.getByText("connection refused")).toBeInTheDocument();
    // runtime metrics from /api/cameras/runtime
    expect(await screen.findByText("140 ms")).toBeInTheDocument();
    expect(screen.getByText("3/8")).toBeInTheDocument();
    // credentials are never shown, only the hint
    expect(screen.getByText(/credentials set \(ad\*\*\*\)/)).toBeInTheDocument();
    // links to the detail page
    expect(screen.getByRole("link", { name: "CAM01" })).toHaveAttribute("href", "/cameras/CAM01");
    // thumbnails via the authenticated media URL
    const img = screen.getByAltText("CAM01 snapshot") as HTMLImageElement;
    expect(img.src).toContain("/api/cameras/CAM01/snapshot.jpg");
    expect(img.src).toContain("token=test-token");

    fireEvent.change(screen.getByLabelText("Status filter"), { target: { value: "OFFLINE" } });
    expect(screen.queryByText("MG Road North")).not.toBeInTheDocument();
    expect(screen.getByText("Ring Road East")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(screen.getByText("MG Road North")).toBeInTheDocument();

    emit({ type: "camera_status_changed", data: { camera_id: "CAM01", old_status: "ONLINE", status: "DEGRADED", message: "low fps", ts: "2026-09-30T10:00:00Z", is_demo: false, intentional: false } });
    expect(await screen.findByText("low fps")).toBeInTheDocument();
  });

  it("hides Add camera and thumbnails without write / stream permissions", async () => {
    mockFetch((_method, url) => {
      if (url === "/api/cameras") return { body: LIST };
      if (url === "/api/cameras/runtime") return { body: RUNTIME };
      return undefined;
    }, ["cameras:read"]);
    wrap(<Cameras />);
    await screen.findByText("Ring Road East");
    expect(screen.queryByRole("button", { name: /Add camera/ })).not.toBeInTheDocument();
    expect(screen.queryByAltText("CAM01 snapshot")).not.toBeInTheDocument();
  });

  it("shows the API error text", async () => {
    mockFetch((_m, url) => (url === "/api/cameras" ? { status: 403, body: { detail: "Missing permission: cameras:read" } } : { body: RUNTIME }));
    wrap(<Cameras />);
    expect(await screen.findByText(/Missing permission: cameras:read/)).toBeInTheDocument();
  });

  it("Add camera: tests the connection and creates the camera", async () => {
    mockFetch((method, url, body) => {
      if (url === "/api/cameras" && method === "GET") return { body: LIST };
      if (url === "/api/cameras/runtime") return { body: RUNTIME };
      if (url === "/api/cameras/test-connection") return { body: { ok: true, resolution: "1920x1080", native_fps: 25, latency_ms: 310, uri: (body as { source_uri: string }).source_uri } };
      if (url === "/api/cameras" && method === "POST") return { status: 201, body: cam({ id: (body as { id: string }).id }) };
      return { body: {} };
    });
    wrap(
      <Routes>
        <Route path="/" element={<Cameras />} />
        <Route path="/cameras/:id" element={<div>detail page</div>} />
      </Routes>,
    );
    await screen.findByText("Ring Road East");
    fireEvent.click(screen.getByRole("button", { name: /Add camera/ }));
    const byLabel = (re: RegExp) => screen.getByLabelText(re) as HTMLInputElement;
    fireEvent.change(byLabel(/^Camera ID/), { target: { value: "CAM09" } });
    fireEvent.change(byLabel(/^Name/), { target: { value: "Junction 9" } });
    fireEvent.change(byLabel(/^Source URI/), { target: { value: "rtsp://10.0.0.9/live" } });
    fireEvent.change(byLabel(/^Latitude/), { target: { value: "12.9" } });
    fireEvent.change(byLabel(/^Longitude/), { target: { value: "77.6" } });
    expect(byLabel(/^Password/).type).toBe("password");

    fireEvent.click(screen.getByRole("button", { name: /Test connection/ }));
    expect(await screen.findByText(/1920x1080/)).toBeInTheDocument();
    expect(calls.find((c) => c.url === "/api/cameras/test-connection")?.body).toMatchObject({ source_type: "rtsp", source_uri: "rtsp://10.0.0.9/live" });

    fireEvent.click(screen.getByRole("button", { name: /^Create camera/ }));
    expect(await screen.findByText("detail page")).toBeInTheDocument();
    const post = calls.find((c) => c.method === "POST" && c.url === "/api/cameras");
    expect(post?.body).toMatchObject({ id: "CAM09", name: "Junction 9", source_type: "rtsp", source_uri: "rtsp://10.0.0.9/live", latitude: 12.9, longitude: 77.6 });
  });
});

/* ------------------------------------------------------------------ Camera detail */

describe("CameraDetail page", () => {
  const detailHandler: Handler = (method, url, body) => {
    if (method === "GET" && url === "/api/cameras/CAM01") return { body: cam({ runtime: { input_fps: 25, faults: { blur: 42 } } as Camera["runtime"] }) };
    if (url.startsWith("/api/cameras/CAM01/health?"))
      return {
        body: {
          camera_id: "CAM01",
          status: "ONLINE",
          status_message: null,
          window_s: 3600,
          samples: 12,
          enabled: true,
          score: 72,
          grade: "FAIR",
          factors: [{ factor: "fps", value: 2, impact: -18, text: "Processing below target fps" }],
          recommendations: ["Lower processing_fps or add a worker"],
          runtime: null,
        },
      };
    if (method === "POST" && url === "/api/cameras/CAM01/simulate-fault") return { body: { ok: true, camera_id: "CAM01", ...(body as object), note: "applied to the frame source" } };
    if (method === "POST" && url.startsWith("/api/cameras/CAM01/simulate-disconnect")) return { body: { ok: true } };
    if (url.startsWith("/api/cameras/CAM01/logs")) return { body: [] };
    return undefined;
  };

  it("shows stream, runtime faults, health explanation; injects faults; updates live", async () => {
    mockFetch(detailHandler);
    wrap(
      <Routes>
        <Route path="/cameras/:id" element={<CameraDetail />} />
      </Routes>,
      "/cameras/CAM01",
    );
    expect(await screen.findByText(/CAM01 · MG Road North/)).toBeInTheDocument();
    const img = (await screen.findByAltText("CAM01 live preview")) as HTMLImageElement;
    expect(img.src).toContain("/api/cameras/CAM01/stream.mjpg");
    expect(img.src).toContain("overlay=1");
    fireEvent.click(screen.getByLabelText(/Overlay/));
    expect(((await screen.findByAltText("CAM01 live preview")) as HTMLImageElement).src).toContain("overlay=0");
    // graceful stream error
    fireEvent.error(screen.getByAltText("CAM01 live preview"));
    expect(await screen.findByText("No live video")).toBeInTheDocument();

    expect(screen.getByText(/^blur \S+/)).toBeInTheDocument(); // active-fault badge with remaining time
    expect(await screen.findByText("Processing below target fps")).toBeInTheDocument();
    expect(screen.getByText("FAIR")).toBeInTheDocument();
    expect(screen.getByText("Lower processing_fps or add a worker")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Inject fault/ }));
    expect(await screen.findByText(/applied to the frame source/)).toBeInTheDocument();
    expect(calls.find((c) => c.url === "/api/cameras/CAM01/simulate-fault")?.body).toEqual({ kind: "offline", seconds: 30 });
    fireEvent.click(screen.getByRole("button", { name: /Simulate disconnect/ }));
    await waitFor(() => expect(calls.some((c) => c.url === "/api/cameras/CAM01/simulate-disconnect?seconds=30")).toBe(true));

    emit({ type: "camera_status_changed", data: { camera_id: "CAM01", old_status: "ONLINE", status: "OFFLINE", message: "fault: offline", ts: "2026-09-30T10:00:00Z", is_demo: false, intentional: true } });
    expect(await screen.findByText("fault: offline")).toBeInTheDocument();
  });

  it("hides control, fault injection and video for a read-only role", async () => {
    mockFetch(detailHandler, ["cameras:read"]);
    wrap(
      <Routes>
        <Route path="/cameras/:id" element={<CameraDetail />} />
      </Routes>,
      "/cameras/CAM01",
    );
    await screen.findByText(/CAM01 · MG Road North/);
    expect(screen.queryByRole("button", { name: /Restart/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Delete/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Inject fault/ })).not.toBeInTheDocument();
    expect(screen.queryByAltText("CAM01 live preview")).not.toBeInTheDocument();
    expect(screen.getByText(/cannot view live video/)).toBeInTheDocument();
  });
});
