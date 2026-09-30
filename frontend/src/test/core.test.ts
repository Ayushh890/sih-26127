import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api, errorText, mediaUrl, qs, tokenStore } from "../lib/api";
import { EventStream, type SocketLike } from "../lib/ws";
import { confidenceTone, fmtDuration, fmtKm, levelTone, predictionTone, scoreHex, TONE_HEX } from "../lib/format";

class FakeSocket implements SocketLike {
  static all: FakeSocket[] = [];
  readyState = 0;
  sent: string[] = [];
  closed = false;
  onopen: ((ev: unknown) => void) | null = null;
  onclose: ((ev: { code: number }) => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onerror: ((ev: unknown) => void) | null = null;
  constructor(public url: string) {
    FakeSocket.all.push(this);
  }
  send(d: string) {
    this.sent.push(d);
  }
  close() {
    this.closed = true;
  }
  open() {
    this.readyState = 1;
    this.onopen?.({});
  }
  emit(obj: unknown) {
    this.onmessage?.({ data: typeof obj === "string" ? obj : JSON.stringify(obj) });
  }
  drop(code = 1006) {
    this.onclose?.({ code });
  }
}

describe("EventStream", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    FakeSocket.all = [];
    tokenStore.set("tok-123");
  });
  afterEach(() => {
    vi.useRealTimers();
    tokenStore.clear();
  });

  const make = () => new EventStream((u) => new FakeSocket(u), (t) => `ws://test/ws/events?token=${t}`);

  it("connects with the token and dispatches typed events to subscribers", () => {
    const s = make();
    const matched = vi.fn();
    const any = vi.fn();
    s.on("vehicle_matched", matched);
    s.on("*", any);
    s.start();
    expect(s.state).toBe("connecting");
    const ws = FakeSocket.all[0];
    expect(ws.url).toBe("ws://test/ws/events?token=tok-123");
    ws.open();
    expect(s.state).toBe("open");
    ws.emit({ type: "vehicle_matched", ts: "2026-01-01T00:00:00Z", data: { vehicle_code: "VEH-000001" } });
    ws.emit({ type: "alert_created", data: { code: "ALR-1" } });
    ws.emit({ type: "pong" });
    ws.emit("not json");
    expect(matched).toHaveBeenCalledTimes(1);
    expect(matched.mock.calls[0][0].data.vehicle_code).toBe("VEH-000001");
    expect(any).toHaveBeenCalledTimes(2);
    expect(s.received).toBe(2);
    s.stop();
  });

  it("reconnects with exponential backoff and stops on unauthorized close", () => {
    const s = make();
    s.start();
    FakeSocket.all[0].open();
    FakeSocket.all[0].drop();
    expect(s.state).toBe("closed");
    vi.advanceTimersByTime(999);
    expect(FakeSocket.all).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeSocket.all).toHaveLength(2);
    FakeSocket.all[1].drop(); // failed before opening: next delay doubles
    vi.advanceTimersByTime(1999);
    expect(FakeSocket.all).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(FakeSocket.all).toHaveLength(3);
    FakeSocket.all[2].drop(4401);
    expect(s.state).toBe("unauthorized");
    vi.advanceTimersByTime(60000);
    expect(FakeSocket.all).toHaveLength(3);
  });

  it("sends keep-alive pings and unsubscribes cleanly", () => {
    const s = make();
    const fn = vi.fn();
    const off = s.on("trajectory_updated", fn);
    s.start();
    const ws = FakeSocket.all[0];
    ws.open();
    vi.advanceTimersByTime(25000);
    expect(ws.sent).toContain(JSON.stringify({ type: "ping" }));
    off();
    ws.emit({ type: "trajectory_updated", data: {} });
    expect(fn).not.toHaveBeenCalled();
    s.stop();
    expect(ws.closed).toBe(true);
    expect(s.state).toBe("idle");
  });

  it("does not connect without a token", () => {
    tokenStore.clear();
    const s = make();
    s.start();
    expect(FakeSocket.all).toHaveLength(0);
    expect(s.state).toBe("unauthorized");
  });
});

describe("api client", () => {
  afterEach(() => {
    vi.unstubAllGlobals();
    tokenStore.clear();
  });

  it("builds query strings skipping empty values and repeating arrays", () => {
    expect(qs({ a: 1, b: "", c: undefined, d: null, e: ["x", "y"], f: false })).toBe("?a=1&e=x&e=y&f=false");
    expect(qs({})).toBe("");
  });

  it("sends the bearer token and JSON body", async () => {
    tokenStore.set("abc");
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(api.post("/api/x", { v: 1 }, { scope: "demo" })).resolves.toEqual({ ok: true });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/api/x?scope=demo");
    expect(init.method).toBe("POST");
    expect(init.headers.Authorization).toBe("Bearer abc");
    expect(init.body).toBe(JSON.stringify({ v: 1 }));
  });

  it("surfaces FastAPI validation errors and permission errors readably", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(new Response(JSON.stringify({ detail: [{ loc: ["body", "processing_fps"], msg: "must be > 0" }] }), { status: 422 })),
    );
    const err = await api.get("/api/y").catch((e) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect(err.status).toBe(422);
    expect(err.message).toBe("processing_fps: must be > 0");
    expect(errorText(new ApiError(403, "missing permission plates:view_raw"))).toBe("Not permitted: missing permission plates:view_raw");
  });

  it("adds the token to media URLs", () => {
    tokenStore.set("t o");
    expect(mediaUrl("/api/cameras/CAM-01/stream.mjpg", { overlay: 1 })).toBe("/api/cameras/CAM-01/stream.mjpg?overlay=1&token=t+o");
  });
});

describe("format helpers", () => {
  it("formats durations and distances", () => {
    expect(fmtDuration(42)).toBe("42s");
    expect(fmtDuration(125)).toBe("2m 5s");
    expect(fmtDuration(3720)).toBe("1h 2m");
    expect(fmtDuration(null)).toBe("—");
    expect(fmtKm(850)).toBe("850 m");
    expect(fmtKm(8090)).toBe("8.09 km");
  });

  it("maps levels, confidence and predictions to tones", () => {
    expect(levelTone("SEVERE")).toBe("red");
    expect(levelTone("NO_DATA")).toBe("slate");
    expect(confidenceTone("HIGH")).toBe("green");
    expect(confidenceTone("NEW")).toBe("blue");
    expect(predictionTone("CONFIRMED")).toBe("green");
    expect(predictionTone("DEVIATED")).toBe("orange");
    expect(scoreHex(80)).toBe(TONE_HEX.red);
    expect(scoreHex(10)).toBe(TONE_HEX.green);
    expect(scoreHex(null)).toBe(TONE_HEX.slate);
  });
});
