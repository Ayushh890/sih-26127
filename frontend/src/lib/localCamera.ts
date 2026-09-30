/**
 * Browser Local Camera: capture the laptop webcam here and upload JPEG frames to the backend
 * over /ws/cameras/:id/ingest, where the same stream worker as for RTSP processes them.
 *
 * FrameUplink is the upload socket. Every frame is acknowledged by the server; at most
 * `maxInFlight` frames are unacknowledged at a time, so a slow link or a busy backend slows
 * the upload instead of queueing frames (the page skips captures while it cannot send). The
 * counters come from the server's acks — "received" is what the backend actually accepted.
 */
import { useSyncExternalStore } from "react";
import { tokenStore } from "./api";
import type { CameraRuntime } from "../components/cameras/model";

export type UplinkState = "idle" | "connecting" | "streaming" | "reconnecting" | "failed";

export interface UplinkSnapshot {
  state: UplinkState;
  /** frames handed to the socket */
  sent: number;
  /** frames the backend accepted into the camera's input (from its acks) */
  received: number;
  /** frames the backend refused (not a JPEG / too large) */
  rejected: number;
  inFlight: number;
  maxInFlight: number;
  /** worker state reported by the backend (null: no worker is processing this camera yet) */
  backend: CameraRuntime | null;
  backendAt: number | null;
  error: string | null;
}

export interface UplinkSocket {
  readyState: number;
  binaryType: string;
  onopen: ((ev: unknown) => void) | null;
  onclose: ((ev: { code: number; reason?: string }) => void) | null;
  onmessage: ((ev: { data: unknown }) => void) | null;
  onerror: ((ev: unknown) => void) | null;
  send(data: Blob | ArrayBuffer | string): void;
  close(code?: number): void;
}

/** Close codes after which reconnecting cannot help (see app/api/routes/local_camera.py). */
const FATAL: Record<number, string> = {
  4400: "This camera is not a browser Local Camera.",
  4401: "Session expired — sign in again.",
  4403: "Your role cannot stream into cameras (needs cameras:control).",
  4404: "Camera not found — it may have been deleted.",
  4409: "Another browser tab or device took over this camera.",
};

const INITIAL: UplinkSnapshot = {
  state: "idle",
  sent: 0,
  received: 0,
  rejected: 0,
  inFlight: 0,
  maxInFlight: 2,
  backend: null,
  backendAt: null,
  error: null,
};

export class FrameUplink {
  private ws: UplinkSocket | null = null;
  private listeners = new Set<() => void>();
  private timer: ReturnType<typeof setTimeout> | null = null;
  private retry = 0;
  private wanted = false;
  private cameraId = "";
  snap: UplinkSnapshot = INITIAL;

  constructor(
    private makeSocket: (url: string) => UplinkSocket = (url) => new WebSocket(url) as unknown as UplinkSocket,
    private urlFor: (cameraId: string, token: string) => string = defaultUrl,
  ) {}

  start(cameraId: string): void {
    this.stop();
    this.cameraId = cameraId;
    this.wanted = true;
    this.snap = { ...INITIAL };
    this.connect();
  }

  stop(): void {
    this.wanted = false;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.teardown(1000);
    if (this.snap.state !== "failed") this.set({ state: "idle", inFlight: 0 });
  }

  /** True when a frame may be sent now (socket open and the in-flight window not full). */
  canSend(): boolean {
    return this.snap.state === "streaming" && this.snap.inFlight < this.snap.maxInFlight;
  }

  send(frame: Blob | ArrayBuffer): boolean {
    if (!this.ws || !this.canSend()) return false;
    try {
      this.ws.send(frame);
    } catch {
      return false;
    }
    this.set({ sent: this.snap.sent + 1, inFlight: this.snap.inFlight + 1 });
    return true;
  }

  subscribe = (fn: () => void): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  getSnapshot = (): UplinkSnapshot => this.snap;

  private set(patch: Partial<UplinkSnapshot>): void {
    this.snap = { ...this.snap, ...patch };
    this.listeners.forEach((l) => l());
  }

  private connect(): void {
    const token = tokenStore.get();
    if (!token) {
      this.wanted = false;
      this.set({ state: "failed", error: FATAL[4401] });
      return;
    }
    this.set({ state: this.retry ? "reconnecting" : "connecting", inFlight: 0 });
    const ws = this.makeSocket(this.urlFor(this.cameraId, token));
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    ws.onmessage = (m) => {
      let msg: { type?: string; data?: Record<string, unknown> };
      try {
        msg = JSON.parse(String(m.data));
      } catch {
        return;
      }
      const d = msg.data ?? {};
      if (msg.type === "hello") {
        this.retry = 0;
        this.set({ state: "streaming", error: null, inFlight: 0, maxInFlight: Number(d.max_in_flight) || 2 });
      } else if (msg.type === "ack") {
        const patch: Partial<UplinkSnapshot> = {
          inFlight: Math.max(0, this.snap.inFlight - 1),
          received: Number(d.received ?? this.snap.received),
          rejected: Number(d.rejected ?? this.snap.rejected),
        };
        if (d.accepted === false && typeof d.reason === "string") patch.error = d.reason;
        if ("backend" in d) Object.assign(patch, { backend: (d.backend as CameraRuntime | null) ?? null, backendAt: Date.now() });
        this.set(patch);
      } else if (msg.type === "pong") {
        this.set({ backend: (d.backend as CameraRuntime | null) ?? null, backendAt: Date.now() });
      } else if (msg.type === "error" && typeof d.detail === "string") {
        this.set({ error: d.detail });
      }
    };
    ws.onclose = (e) => {
      this.ws = null;
      if (!this.wanted) return;
      const fatal = FATAL[e.code];
      if (fatal) {
        this.wanted = false;
        this.set({ state: "failed", error: fatal, inFlight: 0 });
        return;
      }
      const delay = Math.min(10000, 1000 * 2 ** this.retry++);
      this.set({ state: "reconnecting", inFlight: 0, error: `Connection to the backend lost — retrying in ${Math.round(delay / 1000)} s.` });
      this.timer = setTimeout(() => this.connect(), delay);
    };
    ws.onerror = () => {
      /* onclose follows and decides whether to reconnect */
    };
  }

  private teardown(code: number): void {
    const ws = this.ws;
    this.ws = null;
    if (!ws) return;
    ws.onclose = null;
    ws.onmessage = null;
    ws.onopen = null;
    try {
      ws.close(code);
    } catch {
      /* already closed */
    }
  }
}

function defaultUrl(cameraId: string, token: string): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/ws/cameras/${encodeURIComponent(cameraId)}/ingest?token=${encodeURIComponent(token)}`;
}

export function useUplink(u: FrameUplink): UplinkSnapshot {
  return useSyncExternalStore(u.subscribe, u.getSnapshot, u.getSnapshot);
}

/* --------------------------------------------------------------------------- capture */

/** Why the browser cannot capture at all (null: it can try). */
export function captureSupport(env: { isSecureContext?: boolean; mediaDevices?: { getUserMedia?: unknown } } = {
  isSecureContext: globalThis.isSecureContext,
  mediaDevices: globalThis.navigator?.mediaDevices,
}): string | null {
  if (env.isSecureContext === false) {
    return "Camera access needs a secure page: open the console over https:// or at http://localhost (not a LAN IP over plain http).";
  }
  if (!env.mediaDevices || typeof env.mediaDevices.getUserMedia !== "function") {
    return "This browser does not support camera capture (navigator.mediaDevices.getUserMedia). Use a current Chrome, Edge or Firefox.";
  }
  return null;
}

/** Operator-facing explanation of a getUserMedia failure. */
export function describeMediaError(e: unknown): string {
  const name = e && typeof e === "object" && "name" in e ? String((e as { name: unknown }).name) : "";
  switch (name) {
    case "NotAllowedError":
    case "PermissionDeniedError":
      return "Camera permission was denied. Allow camera access for this site in the browser's address bar, then press Start again.";
    case "NotFoundError":
    case "DevicesNotFoundError":
      return "No camera was found on this computer. Connect a webcam and press Start again.";
    case "NotReadableError":
    case "TrackStartError":
      return "The camera is in use by another application (Zoom, Teams, another tab…) or the OS blocked it. Close that app and try again.";
    case "OverconstrainedError":
      return "The camera does not support the selected resolution. Pick a lower resolution and try again.";
    case "SecurityError":
      return "The browser blocked camera access on this page (insecure origin or a permissions policy).";
    case "AbortError":
      return "The camera could not be started (the device was interrupted). Try again.";
    default:
      return `Could not start the camera: ${e instanceof Error ? e.message : String(e)}`;
  }
}

export const RESOLUTIONS = [
  { id: "320x240", w: 320, h: 240 },
  { id: "640x480", w: 640, h: 480 },
  { id: "1280x720", w: 1280, h: 720 },
] as const;

export const SEND_FPS = [2, 5, 10, 15] as const;
