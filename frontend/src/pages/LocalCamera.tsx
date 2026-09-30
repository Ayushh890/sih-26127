/**
 * Local Camera (demo input): stream this computer's webcam into the backend pipeline.
 *
 * The browser captures the webcam (getUserMedia), encodes frames as JPEG and uploads them to
 * /ws/cameras/:id/ingest (lib/localCamera.ts). The backend runs the same worker as for CCTV
 * (detection, tracking, plates, OCR, re-identification) on a camera whose source_type is
 * "browser". Start enables that camera, Stop disables it. Everything shown under "Backend"
 * is the worker's own measured state — nothing is estimated in the browser.
 *
 * Data: GET /api/cameras?scope=all, POST /api/cameras (register), POST /api/cameras/:id/start|stop,
 * WS /ws/cameras/:id/ingest, GET /api/cameras/:id/stream.mjpg (annotated backend output).
 */
import { useEffect, useMemo, useRef, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, errorText, mediaUrl } from "../lib/api";
import { P, useAuth } from "../lib/auth";
import { fmtNum, statusTone } from "../lib/format";
import {
  FrameUplink,
  RESOLUTIONS,
  SEND_FPS,
  captureSupport,
  describeMediaError,
  useUplink,
  type UplinkSnapshot,
} from "../lib/localCamera";
import type { Camera, CameraList } from "../components/cameras/model";
import { Badge, Empty, ErrorBox, Field, KV, Loading, PageHeader, Panel, Stat } from "../components/ui";

const JPEG_QUALITY = 0.7;

export interface LocalCameraDeps {
  uplink: FrameUplink;
  getUserMedia: (c: MediaStreamConstraints) => Promise<MediaStream>;
  support: () => string | null;
}

const defaultDeps = (): LocalCameraDeps => ({
  uplink: new FrameUplink(),
  getUserMedia: (c) => navigator.mediaDevices.getUserMedia(c),
  support: () => captureSupport(),
});

const CONN_LABEL: Record<string, { label: string; tone: "green" | "cyan" | "orange" | "red" | "slate" }> = {
  idle: { label: "Not streaming", tone: "slate" },
  connecting: { label: "Connecting", tone: "cyan" },
  streaming: { label: "Connected", tone: "green" },
  reconnecting: { label: "Reconnecting", tone: "orange" },
  failed: { label: "Failed", tone: "red" },
};

export default function LocalCamera({ deps }: { deps?: Partial<LocalCameraDeps> }) {
  const [d] = useState<LocalCameraDeps>(() => ({ ...defaultDeps(), ...deps }));
  const { has } = useAuth();
  const qc = useQueryClient();
  const snap = useUplink(d.uplink);

  const list = useQuery({
    queryKey: ["cameras", "list", "all"],
    queryFn: () => api.get<CameraList>("/api/cameras", { scope: "all" }),
    refetchInterval: 10_000,
  });
  const all = useMemo(() => list.data?.cameras ?? [], [list.data]);
  const browserCams = useMemo(() => all.filter((c) => c.source_type === "browser"), [all]);

  const [picked, setPicked] = useState<string>("");
  const [res, setRes] = useState<string>("640x480");
  const [fps, setFps] = useState<number>(5);
  const [phase, setPhase] = useState<"idle" | "starting" | "live">("idle");
  const [error, setError] = useState<string | null>(null);
  const [local, setLocal] = useState({ uploadFps: 0, skipped: 0 });
  const [registering, setRegistering] = useState(false);

  const videoRef = useRef<HTMLVideoElement | null>(null);
  const mediaRef = useRef<MediaStream | null>(null);
  const loopRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const liveIdRef = useRef<string | null>(null);
  const skippedRef = useRef(0);

  const camId = picked && browserCams.some((c) => c.id === picked) ? picked : (browserCams[0]?.id ?? "");
  const cam = browserCams.find((c) => c.id === camId);

  /** Stop capturing and uploading (does not touch the camera's enabled flag). */
  const stopCapture = () => {
    if (loopRef.current) clearInterval(loopRef.current);
    loopRef.current = null;
    mediaRef.current?.getTracks().forEach((t) => t.stop());
    mediaRef.current = null;
    if (videoRef.current) videoRef.current.srcObject = null;
    d.uplink.stop();
    liveIdRef.current = null;
    setPhase("idle");
  };

  const disable = async (id: string) => {
    try {
      await api.post(`/api/cameras/${encodeURIComponent(id)}/stop`);
    } catch (e) {
      setError(`Capture stopped, but disabling ${id} failed: ${errorText(e)}`);
    }
    await qc.invalidateQueries({ queryKey: ["cameras"] });
  };

  const stop = async (reason?: string) => {
    const id = liveIdRef.current;
    stopCapture();
    if (reason) setError(reason);
    if (id) await disable(id);
  };

  const tick = () => {
    const v = videoRef.current;
    if (!v || v.readyState < 2 || !v.videoWidth) return;
    if (!d.uplink.canSend()) {
      if (d.uplink.snap.state === "streaming") skippedRef.current += 1; // backend has not acknowledged yet
      return;
    }
    const canvas = document.createElement("canvas");
    canvas.width = v.videoWidth;
    canvas.height = v.videoHeight;
    canvas.getContext("2d")?.drawImage(v, 0, 0);
    canvas.toBlob((blob) => blob && d.uplink.send(blob), "image/jpeg", JPEG_QUALITY);
  };

  const start = async () => {
    if (!cam) return;
    setError(null);
    const unsupported = d.support();
    if (unsupported) {
      setError(unsupported);
      return;
    }
    const r = RESOLUTIONS.find((x) => x.id === res) ?? RESOLUTIONS[1];
    setPhase("starting");
    let media: MediaStream;
    try {
      media = await d.getUserMedia({ video: { width: { ideal: r.w }, height: { ideal: r.h } }, audio: false });
    } catch (e) {
      setError(describeMediaError(e));
      setPhase("idle");
      return;
    }
    mediaRef.current = media;
    media.getVideoTracks().forEach((t) => t.addEventListener("ended", () => void stop("The camera was disconnected or turned off.")));
    const v = videoRef.current;
    if (v) {
      v.srcObject = media;
      await Promise.resolve()
        .then(() => v.play())
        .catch(() => undefined); // autoplay refusal only affects the preview, not the upload
    }
    // Upload first, then enable the camera: its worker finds frames waiting instead of reporting OFFLINE.
    liveIdRef.current = cam.id;
    skippedRef.current = 0;
    d.uplink.start(cam.id);
    loopRef.current = setInterval(tick, 1000 / fps);
    setPhase("live");
    try {
      if (!cam.enabled) await api.post(`/api/cameras/${encodeURIComponent(cam.id)}/start`);
      await qc.invalidateQueries({ queryKey: ["cameras"] });
    } catch (e) {
      stopCapture();
      setError(`Could not enable ${cam.id}: ${errorText(e)}`);
    }
  };

  // Uplink gave up (takeover, permissions, camera deleted): stop capturing. The camera stays
  // enabled — after a takeover another tab is streaming into it.
  const failed = snap.state === "failed" && phase === "live";
  useEffect(() => {
    if (failed) stopCapture();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [failed]);

  // Local counters, once a second: upload rate from the backend's own acknowledgements.
  useEffect(() => {
    if (phase !== "live") return;
    let last = d.uplink.snap.received;
    const t = setInterval(() => {
      const now = d.uplink.snap.received;
      setLocal({ uploadFps: Math.max(0, now - last), skipped: skippedRef.current });
      last = now;
    }, 1000);
    return () => clearInterval(t);
  }, [phase, d.uplink]);

  // Leaving the page ends the session like Stop does.
  useEffect(
    () => () => {
      const id = liveIdRef.current;
      stopCapture();
      if (id) void api.post(`/api/cameras/${encodeURIComponent(id)}/stop`).catch(() => undefined);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  const busy = phase !== "idle";
  const conn = phase === "idle" && snap.state !== "failed" ? CONN_LABEL.idle : CONN_LABEL[snap.state];
  const shownError = error ?? (snap.state === "failed" || snap.state === "reconnecting" ? snap.error : null);

  return (
    <div className="flex flex-col gap-2">
      <PageHeader
        title="Local Camera"
        subtitle={
          <span className="flex flex-wrap items-center gap-1.5">
            <Badge tone="yellow">Local camera demo</Badge>
            <span>This computer's webcam, streamed from this browser into the same detection pipeline as CCTV. It is not a CCTV feed.</span>
          </span>
        }
      />

      {list.isLoading ? (
        <Loading />
      ) : list.error ? (
        <ErrorBox error={list.error} onRetry={() => list.refetch()} />
      ) : (
        <>
          <Panel
            title="Session"
            actions={
              has(P.CAMERAS_WRITE) && !busy && !registering ? (
                <button className="btn" onClick={() => setRegistering(true)}>
                  ＋ Register local camera
                </button>
              ) : undefined
            }
          >
            {registering || (!browserCams.length && has(P.CAMERAS_WRITE)) ? (
              <RegisterForm
                cameras={all}
                onCancel={browserCams.length ? () => setRegistering(false) : undefined}
                onCreated={async (c) => {
                  await qc.invalidateQueries({ queryKey: ["cameras"] });
                  setPicked(c.id);
                  setRegistering(false);
                }}
              />
            ) : !browserCams.length ? (
              <Empty>No local camera is registered. Ask an administrator to register one (source type "browser").</Empty>
            ) : (
              <div className="flex flex-wrap items-end gap-2">
                <Field label="Camera">
                  <select className="input" value={camId} disabled={busy} onChange={(e) => setPicked(e.target.value)} aria-label="Camera">
                    {browserCams.map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.id} · {c.name}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="Resolution">
                  <select className="input" value={res} disabled={busy} onChange={(e) => setRes(e.target.value)} aria-label="Resolution">
                    {RESOLUTIONS.map((r) => (
                      <option key={r.id} value={r.id}>
                        {r.id}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="Send fps">
                  <select className="input" value={fps} disabled={busy} onChange={(e) => setFps(Number(e.target.value))} aria-label="Send fps">
                    {SEND_FPS.map((f) => (
                      <option key={f} value={f}>
                        {f}
                      </option>
                    ))}
                  </select>
                </Field>
                {busy ? (
                  <button className="btn btn-danger" onClick={() => void stop()} disabled={phase === "starting"}>
                    ■ Stop camera
                  </button>
                ) : (
                  <button className="btn btn-primary" onClick={() => void start()}>
                    ▶ Start camera
                  </button>
                )}
                {cam && (
                  <Link to={`/cameras/${encodeURIComponent(cam.id)}`} className="self-center text-[12px] text-cyan-400 hover:underline">
                    Camera details →
                  </Link>
                )}
              </div>
            )}
            {shownError && (
              <div role="alert" className="mt-2 rounded border border-rose-700/60 bg-rose-950/40 px-2 py-1 text-[12px] text-rose-200">
                {shownError}
              </div>
            )}
          </Panel>

          <div className="grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-6">
            <Stat label="Connection" value={<Badge tone={conn.tone}>{conn.label}</Badge>} hint={snap.state === "streaming" ? `window ${snap.inFlight}/${snap.maxInFlight} frames` : undefined} />
            <Stat label="Frames sent" value={fmtNum(snap.sent)} hint={`skipped ${local.skipped} (waiting for backend)`} />
            <Stat label="Received by backend" value={fmtNum(snap.received)} hint={`rejected ${snap.rejected} · ${local.uploadFps} fps`} />
            <Stat label="Backend input fps" value={fmtNum(snap.backend?.input_fps, 1)} hint="measured by the worker" />
            <Stat label="Processing fps" value={fmtNum(snap.backend?.processing_fps, 1)} hint={`target ${fmtNum(snap.backend?.target_processing_fps, 1)}`} />
            <Stat label="Active tracks" value={fmtNum(snap.backend?.active_tracks)} hint={`processed ${fmtNum(snap.backend?.frames_processed)}`} />
          </div>

          <div className="grid gap-2 xl:grid-cols-12">
            <Panel title="Camera preview (this browser)" className="xl:col-span-6">
              <div className="relative flex aspect-video items-center justify-center overflow-hidden rounded border border-ink-700 bg-black">
                <video ref={videoRef} muted playsInline className={`h-full w-full object-contain ${busy ? "" : "hidden"}`} aria-label="Local camera preview" />
                {!busy && <span className="absolute text-[12px] text-slate-500">Camera off — press Start camera.</span>}
              </div>
            </Panel>
            <Panel title="Backend output (detections)" className="xl:col-span-6">
              {!has(P.STREAM_VIEW) ? (
                <Empty>Your role cannot view live video ({P.STREAM_VIEW}).</Empty>
              ) : phase === "live" && cam ? (
                <div className="relative flex aspect-video items-center justify-center overflow-hidden rounded border border-ink-700 bg-black">
                  <img
                    src={mediaUrl(`/api/cameras/${encodeURIComponent(cam.id)}/stream.mjpg`, { overlay: 1, fps: 6 })}
                    alt={`${cam.id} processed by the backend`}
                    className="h-full w-full object-contain"
                  />
                </div>
              ) : (
                <Empty>The annotated output of the backend worker appears here while streaming.</Empty>
              )}
            </Panel>
          </div>

          <Panel title="Backend processing status">
            <BackendStatus snap={snap} live={phase === "live"} cam={cam} />
          </Panel>
        </>
      )}
    </div>
  );
}

function BackendStatus({ snap, live, cam }: { snap: UplinkSnapshot; live: boolean; cam?: Camera }) {
  const b = snap.backend;
  if (!live) {
    return <Empty>{cam ? `${cam.id} is ${cam.enabled ? "enabled" : "disabled"} · status ${cam.status}.` : "No camera selected."} Start the camera to see the worker's state.</Empty>;
  }
  if (!b) {
    return (
      <Empty>
        {snap.received
          ? "Frames are reaching the backend, but no worker has reported state for this camera yet (it starts within a few seconds; if it does not, check that the stream workers are running)."
          : "Waiting for the first frames to reach the backend…"}
      </Empty>
    );
  }
  return (
    <KV
      rows={[
        ["Status", <Badge tone={statusTone(b.status)}>{b.status ?? "—"}</Badge>],
        ["Message", b.message || "—"],
        ["Resolution", b.resolution ?? "—"],
        ["Frames in / processed / dropped", `${fmtNum(b.frames_in)} / ${fmtNum(b.frames_processed)} / ${fmtNum(b.frames_dropped)}`],
        ["Latency", `${fmtNum(b.latency_ms)} ms (inference ${fmtNum(b.inference_ms)} ms)`],
        ["Reported", snap.backendAt ? new Date(snap.backendAt).toLocaleTimeString(undefined, { hour12: false }) : "—"],
      ]}
    />
  );
}

function RegisterForm({ cameras, onCreated, onCancel }: { cameras: Camera[]; onCreated: (c: Camera) => void | Promise<void>; onCancel?: () => void }) {
  // Place it near the existing network by default; the operator can change the coordinates.
  const near = cameras[0];
  const nextId = () => {
    for (let i = 1; i < 100; i++) {
      const id = `LOCAL-${String(i).padStart(2, "0")}`;
      if (!cameras.some((c) => c.id === id)) return id;
    }
    return "LOCAL-99";
  };
  const [f, setF] = useState(() => ({
    id: nextId(),
    name: "Laptop webcam",
    latitude: near ? String(near.latitude) : "",
    longitude: near ? String(near.longitude) : "",
  }));
  const [err, setErr] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setErr(null);
    const lat = Number(f.latitude);
    const lon = Number(f.longitude);
    if (f.latitude.trim() === "" || f.longitude.trim() === "" || !Number.isFinite(lat) || !Number.isFinite(lon)) {
      setErr("Latitude and longitude are required.");
      return;
    }
    setSaving(true);
    try {
      const c = await api.post<Camera>("/api/cameras", {
        id: f.id.trim().toUpperCase(),
        name: f.name.trim(),
        latitude: lat,
        longitude: lon,
        source_type: "browser",
        source_uri: "browser://local",
        camera_type: "OVERVIEW",
        enabled: false, // enabled by Start, so it is not reported offline before anyone streams
      });
      await onCreated(c);
    } catch (e2) {
      setErr(errorText(e2));
    } finally {
      setSaving(false);
    }
  };
  const set = (k: keyof typeof f) => (e: { target: { value: string } }) => setF((p) => ({ ...p, [k]: e.target.value }));
  return (
    <form onSubmit={submit} className="space-y-2">
      <div className="text-[12px] text-slate-400">
        Register a camera whose frames come from this page (source type <span className="mono">browser</span>). It appears on the map and in analytics like any
        other camera while it streams.
      </div>
      <div className="grid gap-2 sm:grid-cols-4">
        <Field label="Camera ID">
          <input className="input mono" value={f.id} onChange={set("id")} required aria-label="Camera ID" />
        </Field>
        <Field label="Name">
          <input className="input" value={f.name} onChange={set("name")} required aria-label="Name" />
        </Field>
        <Field label="Latitude">
          <input className="input mono" value={f.latitude} onChange={set("latitude")} aria-label="Latitude" />
        </Field>
        <Field label="Longitude">
          <input className="input mono" value={f.longitude} onChange={set("longitude")} aria-label="Longitude" />
        </Field>
      </div>
      <div className="flex items-center gap-2">
        <button type="submit" className="btn btn-primary" disabled={saving}>
          Register camera
        </button>
        {onCancel && (
          <button type="button" className="btn" onClick={onCancel}>
            Cancel
          </button>
        )}
        {err && <span className="text-[12px] text-rose-300">{err}</span>}
      </div>
    </form>
  );
}
