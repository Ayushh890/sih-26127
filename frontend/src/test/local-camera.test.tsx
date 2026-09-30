import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { AuthProvider } from "../lib/auth";
import { FrameUplink, captureSupport, describeMediaError, type UplinkSocket } from "../lib/localCamera";
import LocalCamera from "../pages/LocalCamera";
import type { Camera } from "../components/cameras/model";

/* ------------------------------------------------------------------ fakes */

class FakeSocket implements UplinkSocket {
  static all: FakeSocket[] = [];
  readyState = 1;
  binaryType = "blob";
  onopen: UplinkSocket["onopen"] = null;
  onclose: UplinkSocket["onclose"] = null;
  onmessage: UplinkSocket["onmessage"] = null;
  onerror: UplinkSocket["onerror"] = null;
  sent: unknown[] = [];
  closedWith: number | undefined;
  constructor(public url: string) {
    FakeSocket.all.push(this);
  }
  send(data: Blob | ArrayBuffer | string) {
    this.sent.push(data);
  }
  close(code?: number) {
    this.closedWith = code;
  }
  /** Server → browser message. */
  msg(type: string, data: Record<string, unknown> = {}) {
    act(() => this.onmessage?.({ data: JSON.stringify({ type, data }) }));
  }
  /** Server closes the socket. */
  drop(code: number) {
    act(() => this.onclose?.({ code }));
  }
}

const newUplink = () => new FrameUplink((url) => new FakeSocket(url));
const last = () => FakeSocket.all[FakeSocket.all.length - 1];

function browserCam(over: Partial<Camera> = {}): Camera {
  return {
    id: "LOCAL-01",
    name: "Laptop webcam",
    location: null,
    latitude: 26.85,
    longitude: 80.95,
    source_type: "browser",
    source_uri: "browser://local",
    has_credentials: false,
    username_hint: null,
    resolution: null,
    lane_count: null,
    direction: null,
    road_name: null,
    zone: null,
    camera_type: "OVERVIEW",
    enabled: false,
    status: "OFFLINE",
    status_message: null,
    last_seen_at: null,
    processing: {},
    calibration: {},
    is_demo: false,
    created_at: null,
    updated_at: null,
    runtime: null,
    ...over,
  } as Camera;
}

const OPERATOR = ["cameras:read", "cameras:control", "stream:view"];
type Call = { method: string; url: string; body: unknown };
let calls: Call[] = [];

function mockFetch(cameras: Camera[], perms = OPERATOR) {
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      calls.push({ method, url, body });
      let payload: unknown = { detail: `unmocked ${method} ${url}` };
      let status = 404;
      if (url === "/api/auth/me") [status, payload] = [200, { id: 1, username: "op", full_name: null, role: "operator", permissions: perms }];
      else if (method === "GET" && url.startsWith("/api/cameras?")) [status, payload] = [200, { scope: "all", cameras }];
      else if (method === "POST" && /\/api\/cameras\/[^/]+\/(start|stop)$/.test(url)) [status, payload] = [200, { ...cameras[0], enabled: url.endsWith("start") }];
      else if (method === "POST" && url === "/api/cameras") [status, payload] = [201, browserCam({ id: (body as { id: string }).id })];
      return { ok: status < 400, status, text: async () => JSON.stringify(payload) } as Response;
    }),
  );
}

function wrap(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AuthProvider>
        <MemoryRouter>{ui}</MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

function fakeMedia() {
  const track = { stop: vi.fn(), addEventListener: vi.fn() };
  return { track, stream: { getTracks: () => [track], getVideoTracks: () => [track] } as unknown as MediaStream };
}

beforeEach(() => {
  FakeSocket.all = [];
  sessionStorage.setItem("nirnay.token", "test-token");
  vi.spyOn(HTMLMediaElement.prototype, "play").mockResolvedValue(undefined);
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  vi.useRealTimers();
  sessionStorage.clear();
});

/* ------------------------------------------------------------------ capture helpers */

describe("capture errors", () => {
  it("explains every getUserMedia failure the operator can fix", () => {
    const err = (name: string) => Object.assign(new Error("x"), { name });
    expect(describeMediaError(err("NotAllowedError"))).toMatch(/permission was denied/i);
    expect(describeMediaError(err("NotFoundError"))).toMatch(/no camera was found/i);
    expect(describeMediaError(err("NotReadableError"))).toMatch(/in use by another application/i);
    expect(describeMediaError(err("OverconstrainedError"))).toMatch(/lower resolution/i);
    expect(describeMediaError(new Error("boom"))).toMatch(/boom/);
  });

  it("detects insecure pages and browsers without getUserMedia", () => {
    expect(captureSupport({ isSecureContext: false, mediaDevices: { getUserMedia: () => 0 } })).toMatch(/https:\/\/ or at http:\/\/localhost/);
    expect(captureSupport({ isSecureContext: true, mediaDevices: undefined })).toMatch(/does not support camera capture/);
    expect(captureSupport({ isSecureContext: true, mediaDevices: { getUserMedia: () => 0 } })).toBeNull();
  });
});

/* ------------------------------------------------------------------ uplink */

describe("FrameUplink", () => {
  it("authenticates, respects the in-flight window and counts what the backend acknowledged", () => {
    const u = newUplink();
    u.start("LOCAL-01");
    const ws = last();
    expect(ws.url).toContain("/ws/cameras/LOCAL-01/ingest?token=test-token");
    expect(ws.binaryType).toBe("arraybuffer");
    expect(u.canSend()).toBe(false); // not before the server's hello
    ws.msg("hello", { camera_id: "LOCAL-01", max_in_flight: 2 });
    expect(u.snap.state).toBe("streaming");
    const frame = new Blob([new Uint8Array([0xff, 0xd8])]);
    expect(u.send(frame)).toBe(true);
    expect(u.send(frame)).toBe(true);
    expect(u.send(frame)).toBe(false); // window full: the page skips this capture
    expect(u.snap).toMatchObject({ sent: 2, inFlight: 2, received: 0 });
    ws.msg("ack", { received: 1, rejected: 0, accepted: true, backend: { status: "ONLINE", input_fps: 4.8 } });
    expect(u.snap).toMatchObject({ inFlight: 1, received: 1, backend: { status: "ONLINE", input_fps: 4.8 } });
    ws.msg("ack", { received: 1, rejected: 1, accepted: false, reason: "frame must be a JPEG of at most 2 MB" });
    expect(u.snap).toMatchObject({ inFlight: 0, rejected: 1, error: "frame must be a JPEG of at most 2 MB" });
    expect(u.snap.backend?.input_fps).toBe(4.8); // acks without stats keep the last backend state
    u.stop();
    expect(ws.closedWith).toBe(1000);
    expect(u.snap.state).toBe("idle");
  });

  it("reconnects after a dropped connection but not after a takeover", () => {
    vi.useFakeTimers();
    const u = newUplink();
    u.start("LOCAL-01");
    last().msg("hello", {});
    last().drop(1006);
    expect(u.snap.state).toBe("reconnecting");
    act(() => vi.advanceTimersByTime(1000));
    expect(FakeSocket.all).toHaveLength(2);
    last().msg("hello", {});
    expect(u.snap.state).toBe("streaming");
    last().drop(4409);
    expect(u.snap).toMatchObject({ state: "failed", error: expect.stringMatching(/took over/) });
    act(() => vi.advanceTimersByTime(60000));
    expect(FakeSocket.all).toHaveLength(2);
  });
});

/* ------------------------------------------------------------------ page */

describe("Local Camera page", () => {
  it("is labelled as a local camera demo, not CCTV", async () => {
    mockFetch([browserCam()]);
    wrap(<LocalCamera deps={{ uplink: newUplink(), support: () => null }} />);
    expect(await screen.findByText(/local camera demo/i)).toBeTruthy();
    expect(screen.getByText(/it is not a cctv feed/i)).toBeTruthy();
    expect(await screen.findByRole("button", { name: /start camera/i })).toBeTruthy();
  });

  it("reports a denied permission and does not enable the camera", async () => {
    mockFetch([browserCam()]);
    const getUserMedia = vi.fn().mockRejectedValue(Object.assign(new Error("denied"), { name: "NotAllowedError" }));
    wrap(<LocalCamera deps={{ uplink: newUplink(), getUserMedia, support: () => null }} />);
    fireEvent.click(await screen.findByRole("button", { name: /start camera/i }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/permission was denied/i);
    expect(calls.some((c) => c.url.endsWith("/start"))).toBe(false);
    expect(FakeSocket.all).toHaveLength(0);
    expect(screen.getByRole("button", { name: /start camera/i })).toBeTruthy();
  });

  it("refuses to start on an insecure page", async () => {
    mockFetch([browserCam()]);
    const getUserMedia = vi.fn();
    wrap(<LocalCamera deps={{ uplink: newUplink(), getUserMedia, support: () => "Camera access needs a secure page" }} />);
    fireEvent.click(await screen.findByRole("button", { name: /start camera/i }));
    expect((await screen.findByRole("alert")).textContent).toMatch(/secure page/);
    expect(getUserMedia).not.toHaveBeenCalled();
  });

  it("starts the upload, enables the camera, shows backend counters and stops cleanly", async () => {
    mockFetch([browserCam()]);
    const { stream, track } = fakeMedia();
    const getUserMedia = vi.fn().mockResolvedValue(stream);
    wrap(<LocalCamera deps={{ uplink: newUplink(), getUserMedia, support: () => null }} />);
    fireEvent.change(await screen.findByLabelText("Resolution"), { target: { value: "1280x720" } });
    fireEvent.click(screen.getByRole("button", { name: /start camera/i }));

    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url === "/api/cameras/LOCAL-01/start")).toBe(true));
    expect(getUserMedia).toHaveBeenCalledWith({ video: { width: { ideal: 1280 }, height: { ideal: 720 } }, audio: false });
    const ws = last();
    expect(ws.url).toContain("/ws/cameras/LOCAL-01/ingest");
    ws.msg("hello", { camera_id: "LOCAL-01", max_in_flight: 2 });
    expect(await screen.findByText("Connected")).toBeTruthy();
    expect(screen.getByAltText(/processed by the backend/i).getAttribute("src")).toContain("/api/cameras/LOCAL-01/stream.mjpg");
    ws.msg("ack", {
      received: 37,
      rejected: 0,
      accepted: true,
      backend: { status: "ONLINE", message: null, input_fps: 4.9, processing_fps: 2.1, active_tracks: 3, frames_processed: 12, resolution: "1280x720" },
    });
    expect(await screen.findByText("37")).toBeTruthy();
    expect(screen.getByText("4.9")).toBeTruthy();
    expect(screen.getByText("ONLINE")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: /stop camera/i }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url === "/api/cameras/LOCAL-01/stop")).toBe(true));
    expect(track.stop).toHaveBeenCalled();
    expect(ws.closedWith).toBe(1000);
    expect(screen.getByRole("button", { name: /start camera/i })).toBeTruthy();
  });

  it("stops capturing when another tab takes over, leaving the camera enabled", async () => {
    mockFetch([browserCam({ enabled: true })]);
    const { stream, track } = fakeMedia();
    wrap(<LocalCamera deps={{ uplink: newUplink(), getUserMedia: vi.fn().mockResolvedValue(stream), support: () => null }} />);
    fireEvent.click(await screen.findByRole("button", { name: /start camera/i }));
    await waitFor(() => expect(FakeSocket.all).toHaveLength(1));
    last().msg("hello", {});
    last().drop(4409);
    expect((await screen.findByRole("alert")).textContent).toMatch(/took over/);
    expect(track.stop).toHaveBeenCalled();
    expect(calls.some((c) => c.url.endsWith("/start") || c.url.endsWith("/stop"))).toBe(false); // already enabled; not ours to disable
  });

  it("lets a camera admin register a browser camera when none exists", async () => {
    mockFetch([], [...OPERATOR, "cameras:write"]);
    wrap(<LocalCamera deps={{ uplink: newUplink(), support: () => null }} />);
    fireEvent.change(await screen.findByLabelText("Latitude"), { target: { value: "26.85" } });
    fireEvent.change(screen.getByLabelText("Longitude"), { target: { value: "80.95" } });
    fireEvent.click(screen.getByRole("button", { name: /register camera/i }));
    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url === "/api/cameras")).toBe(true));
    expect(calls.find((c) => c.method === "POST" && c.url === "/api/cameras")?.body).toMatchObject({
      id: "LOCAL-01",
      source_type: "browser",
      source_uri: "browser://local",
      enabled: false,
    });
  });

  it("tells operators without cameras:write to ask an administrator", async () => {
    mockFetch([]);
    wrap(<LocalCamera deps={{ uplink: newUplink(), support: () => null }} />);
    expect(await screen.findByText(/ask an administrator/i)).toBeTruthy();
  });
});
