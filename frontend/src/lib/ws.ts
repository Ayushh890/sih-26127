/**
 * Live event stream client for /ws/events.
 *
 * One shared connection per tab; components subscribe to event types. Reconnects with
 * exponential backoff (1 s → 30 s), keeps the socket alive with pings, and exposes its
 * connection state so the UI can show when it is *not* live instead of silently going stale.
 */
import { useEffect, useRef, useSyncExternalStore } from "react";
import { tokenStore } from "./api";

export type EventType =
  | "vehicle_detected"
  | "plate_read"
  | "vehicle_matched"
  | "trajectory_updated"
  | "alert_created"
  | "alert_updated"
  | "camera_status_changed"
  | "analytics_updated";

export interface LiveEvent<T = Record<string, unknown>> {
  type: EventType;
  ts?: number | string;
  data: T;
}

export type ConnState = "idle" | "connecting" | "open" | "closed" | "unauthorized";

type Handler = (ev: LiveEvent) => void;

export interface SocketLike {
  readyState: number;
  onopen: ((ev: unknown) => void) | null;
  onclose: ((ev: { code: number }) => void) | null;
  onmessage: ((ev: { data: string }) => void) | null;
  onerror: ((ev: unknown) => void) | null;
  send(data: string): void;
  close(code?: number): void;
}

export class EventStream {
  private ws: SocketLike | null = null;
  private handlers = new Map<string, Set<Handler>>();
  private listeners = new Set<() => void>();
  private retry = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private ping: ReturnType<typeof setInterval> | null = null;
  private wanted = false;
  state: ConnState = "idle";
  lastEventAt: number | null = null;
  received = 0;

  constructor(
    private makeSocket: (url: string) => SocketLike = (url) => new WebSocket(url) as unknown as SocketLike,
    private urlFor: (token: string) => string = defaultUrl,
  ) {}

  start(): void {
    this.wanted = true;
    if (!this.ws) this.connect();
  }

  stop(): void {
    this.wanted = false;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.teardown();
    this.setState("idle");
  }

  on(type: EventType | "*", fn: Handler): () => void {
    let set = this.handlers.get(type);
    if (!set) this.handlers.set(type, (set = new Set()));
    set.add(fn);
    return () => set!.delete(fn);
  }

  subscribeState = (fn: () => void): (() => void) => {
    this.listeners.add(fn);
    return () => this.listeners.delete(fn);
  };

  getState = (): ConnState => this.state;

  private setState(s: ConnState): void {
    if (s === this.state) return;
    this.state = s;
    this.listeners.forEach((l) => l());
  }

  private connect(): void {
    const token = tokenStore.get();
    if (!token) {
      this.setState("unauthorized");
      return;
    }
    this.setState("connecting");
    const ws = this.makeSocket(this.urlFor(token));
    this.ws = ws;
    ws.onopen = () => {
      this.retry = 0;
      this.setState("open");
      this.ping = setInterval(() => {
        try {
          ws.send(JSON.stringify({ type: "ping" }));
        } catch {
          /* socket closing */
        }
      }, 25000);
    };
    ws.onmessage = (m) => {
      let ev: LiveEvent;
      try {
        ev = JSON.parse(m.data);
      } catch {
        return;
      }
      if (!ev || typeof ev.type !== "string" || ev.type === ("pong" as EventType) || ev.type === ("hello" as EventType)) return;
      this.lastEventAt = Date.now();
      this.received += 1;
      this.handlers.get(ev.type)?.forEach((h) => h(ev));
      this.handlers.get("*")?.forEach((h) => h(ev));
    };
    ws.onclose = (e) => {
      this.teardown();
      if (e.code === 4401) {
        this.setState("unauthorized");
        return;
      }
      this.setState("closed");
      if (this.wanted) {
        const delay = Math.min(30000, 1000 * 2 ** this.retry++);
        this.timer = setTimeout(() => this.connect(), delay);
      }
    };
    ws.onerror = () => {
      /* onclose follows and schedules the reconnect */
    };
  }

  private teardown(): void {
    if (this.ping) clearInterval(this.ping);
    this.ping = null;
    const ws = this.ws;
    this.ws = null;
    if (ws) {
      ws.onclose = null;
      ws.onmessage = null;
      ws.onopen = null;
      try {
        ws.close();
      } catch {
        /* already closed */
      }
    }
  }
}

function defaultUrl(token: string): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${location.host}/ws/events?token=${encodeURIComponent(token)}`;
}

export const stream = new EventStream();

export function useConnState(): ConnState {
  return useSyncExternalStore(stream.subscribeState, stream.getState, stream.getState);
}

/** Subscribe to live events for the lifetime of the component. The handler may change freely. */
export function useLiveEvent(types: EventType | EventType[] | "*", handler: Handler): void {
  const ref = useRef(handler);
  ref.current = handler;
  const key = Array.isArray(types) ? types.join(",") : types;
  useEffect(() => {
    const list = key.split(",") as (EventType | "*")[];
    const offs = list.map((t) => stream.on(t, (e) => ref.current(e)));
    return () => offs.forEach((o) => o());
  }, [key]);
}
