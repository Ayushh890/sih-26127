/**
 * Register a camera (POST /api/cameras) with a real connection test
 * (POST /api/cameras/test-connection) and ONVIF discovery/probe (POST /api/onvif/discover,
 * POST /api/onvif/probe) to fill in the RTSP URI. Passwords are write-only.
 */
import { useState, type FormEvent } from "react";
import { api, errorText } from "../../lib/api";
import { fmtNum } from "../../lib/format";
import { Badge, Field, Modal } from "../ui";
import { CAMERA_TYPES, COMPASS, SOURCE_HINT, SOURCE_TYPES_CREATE, num, type Camera } from "./model";

export interface TestResult {
  ok: boolean;
  error?: string;
  permanent?: boolean;
  resolution?: string;
  native_fps?: number | null;
  latency_ms?: number;
  uri?: string;
}

interface OnvifDevice {
  endpoint: string;
  xaddrs: string[];
  host: string | null;
  name: string | null;
  hardware: string | null;
  location: string | null;
  responder?: string;
}
interface OnvifDiscoverResult {
  devices: OnvifDevice[];
  timeout_s: number;
  errors: string[];
  note?: string;
}
interface OnvifProfile {
  token: string;
  name: string;
  encoding: string | null;
  resolution: string | null;
  fps: number | null;
  rtsp_uri: string | null;
  error?: string;
}
interface OnvifProbeResult {
  xaddr: string;
  device: Record<string, string | null>;
  profiles: OnvifProfile[];
}

export function TestResultView({ r }: { r: TestResult }) {
  return (
    <div className={`rounded border px-2 py-1 text-[12px] ${r.ok ? "border-emerald-600/50 bg-emerald-950/40 text-emerald-200" : "border-rose-700/60 bg-rose-950/40 text-rose-200"}`} role="status">
      <div className="flex items-center gap-1.5">
        <Badge tone={r.ok ? "green" : "red"}>{r.ok ? "Connected" : "Failed"}</Badge>
        {r.latency_ms != null && <span className="mono">{fmtNum(r.latency_ms, 0)} ms</span>}
        {r.resolution && <span className="mono">{r.resolution}</span>}
        {r.native_fps != null && <span className="mono">{fmtNum(r.native_fps, 1)} fps native</span>}
      </div>
      {r.error && (
        <div className="mt-0.5">
          {r.error}
          {r.permanent ? " (permanent — check the URI / credentials)" : ""}
        </div>
      )}
    </div>
  );
}

const initial = {
  id: "",
  name: "",
  source_type: "rtsp",
  source_uri: "",
  username: "",
  password: "",
  location: "",
  latitude: "",
  longitude: "",
  lane_count: "2",
  direction: "N",
  road_name: "",
  zone: "",
  camera_type: "ANPR",
  resolution: "",
  enabled: true,
  processing_fps: "",
  confidence_threshold: "",
  frame_skip: "",
  max_queue_size: "",
  transport: "",
  loop: false,
};

export function AddCameraModal({ onClose, onCreated }: { onClose: () => void; onCreated: (c: Camera) => void }) {
  const [f, setF] = useState(initial);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [test, setTest] = useState<TestResult | null>(null);
  const [testing, setTesting] = useState(false);
  const [testErr, setTestErr] = useState<string | null>(null);
  const set = <K extends keyof typeof initial>(k: K, v: (typeof initial)[K]) => setF((p) => ({ ...p, [k]: v }));

  const runTest = async () => {
    setTesting(true);
    setTest(null);
    setTestErr(null);
    try {
      setTest(
        await api.post<TestResult>("/api/cameras/test-connection", {
          source_type: f.source_type,
          source_uri: f.source_uri.trim(),
          username: f.username || undefined,
          password: f.password || undefined,
        }),
      );
    } catch (e) {
      setTestErr(errorText(e));
    } finally {
      setTesting(false);
    }
  };

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setErr(null);
    const lat = num(f.latitude);
    const lon = num(f.longitude);
    if (lat == null || lon == null) {
      setErr("Latitude and longitude are required (decimal degrees).");
      return;
    }
    const processing: Record<string, unknown> = {};
    const pf = num(f.processing_fps);
    const ct = num(f.confidence_threshold);
    const fs = num(f.frame_skip);
    const mq = num(f.max_queue_size);
    if (pf != null) processing.processing_fps = pf;
    if (ct != null) processing.confidence_threshold = ct;
    if (fs != null) processing.frame_skip = fs;
    if (mq != null) processing.max_queue_size = mq;
    const so: Record<string, unknown> = {};
    if (f.source_type === "rtsp" && f.transport) so.transport = f.transport;
    if (f.source_type === "file" && f.loop) so.loop = true;
    if (Object.keys(so).length) processing.source_options = so;
    const body: Record<string, unknown> = {
      id: f.id.trim(),
      name: f.name.trim(),
      source_type: f.source_type,
      source_uri: f.source_uri.trim(),
      username: f.username || undefined,
      password: f.password || undefined,
      location: f.location || undefined,
      latitude: lat,
      longitude: lon,
      lane_count: num(f.lane_count),
      direction: f.direction,
      road_name: f.road_name || undefined,
      zone: f.zone || undefined,
      camera_type: f.camera_type,
      resolution: f.resolution || undefined,
      enabled: f.enabled,
      processing: Object.keys(processing).length ? processing : undefined,
    };
    setBusy(true);
    try {
      onCreated(await api.post<Camera>("/api/cameras", body));
    } catch (e2) {
      setErr(errorText(e2));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal title="Add camera" onClose={onClose} wide>
      <form onSubmit={submit} className="grid gap-3 lg:grid-cols-[1fr_320px]">
        <div className="space-y-3">
          <fieldset className="grid grid-cols-2 gap-2 md:grid-cols-4">
            <Field label="Camera ID" hint="letters, digits, - _ (2–32)">
              <input className="input mono" required value={f.id} onChange={(e) => set("id", e.target.value)} placeholder="CAM-07" pattern="[A-Za-z0-9][A-Za-z0-9_\-]{1,31}" />
            </Field>
            <div className="col-span-1 md:col-span-2">
              <Field label="Name">
                <input className="input" required maxLength={128} value={f.name} onChange={(e) => set("name", e.target.value)} />
              </Field>
            </div>
            <Field label="Type">
              <select className="input" value={f.camera_type} onChange={(e) => set("camera_type", e.target.value)}>
                {CAMERA_TYPES.map((t) => (
                  <option key={t}>{t}</option>
                ))}
              </select>
            </Field>
          </fieldset>

          <fieldset className="space-y-2 rounded border border-ink-700 p-2">
            <legend className="px-1 text-[11px] font-semibold uppercase tracking-wide text-slate-400">Video source</legend>
            <div className="grid grid-cols-4 gap-2">
              <Field label="Source type">
                <select
                  className="input"
                  value={f.source_type}
                  onChange={(e) => {
                    set("source_type", e.target.value);
                    setTest(null);
                  }}
                >
                  {SOURCE_TYPES_CREATE.map((t) => (
                    <option key={t} value={t}>
                      {t === "http" ? "http (MJPEG / HTTP)" : t}
                    </option>
                  ))}
                </select>
              </Field>
              <div className="col-span-3">
                <Field label="Source URI" hint={SOURCE_HINT[f.source_type]}>
                  <input className="input mono" required maxLength={2048} value={f.source_uri} onChange={(e) => set("source_uri", e.target.value)} />
                </Field>
              </div>
            </div>
            {(f.source_type === "rtsp" || f.source_type === "http") && (
              <div className="grid grid-cols-3 gap-2">
                <Field label="Username">
                  <input className="input" autoComplete="off" maxLength={128} value={f.username} onChange={(e) => set("username", e.target.value)} />
                </Field>
                <Field label="Password" hint="write-only; stored encrypted">
                  <input className="input" type="password" autoComplete="new-password" maxLength={256} value={f.password} onChange={(e) => set("password", e.target.value)} />
                </Field>
                {f.source_type === "rtsp" && (
                  <Field label="RTSP transport">
                    <select className="input" value={f.transport} onChange={(e) => set("transport", e.target.value)}>
                      <option value="">default</option>
                      <option value="tcp">tcp</option>
                      <option value="udp">udp</option>
                    </select>
                  </Field>
                )}
              </div>
            )}
            {f.source_type === "file" && (
              <label className="flex items-center gap-2 text-[12px] text-slate-300">
                <input type="checkbox" checked={f.loop} onChange={(e) => set("loop", e.target.checked)} /> Loop the video file
              </label>
            )}
            <div className="flex items-center gap-2">
              <button type="button" className="btn" disabled={testing || !f.source_uri.trim()} onClick={runTest}>
                {testing && <span className="h-2.5 w-2.5 animate-spin rounded-full border border-slate-400 border-t-transparent" />}
                Test connection
              </button>
              <span className="text-[11px] text-slate-500">Opens the source on the server and grabs one frame.</span>
            </div>
            {testErr && <div className="text-[12px] text-rose-300">{testErr}</div>}
            {test && <TestResultView r={test} />}
          </fieldset>

          <fieldset className="grid grid-cols-2 gap-2 rounded border border-ink-700 p-2 md:grid-cols-4">
            <legend className="px-1 text-[11px] font-semibold uppercase tracking-wide text-slate-400">Location</legend>
            <div className="col-span-2">
              <Field label="Location">
                <input className="input" maxLength={255} value={f.location} onChange={(e) => set("location", e.target.value)} />
              </Field>
            </div>
            <Field label="Latitude">
              <input className="input mono" required type="number" step="any" min={-90} max={90} value={f.latitude} onChange={(e) => set("latitude", e.target.value)} />
            </Field>
            <Field label="Longitude">
              <input className="input mono" required type="number" step="any" min={-180} max={180} value={f.longitude} onChange={(e) => set("longitude", e.target.value)} />
            </Field>
            <Field label="Road name">
              <input className="input" maxLength={128} value={f.road_name} onChange={(e) => set("road_name", e.target.value)} />
            </Field>
            <Field label="Zone">
              <input className="input" maxLength={64} value={f.zone} onChange={(e) => set("zone", e.target.value)} />
            </Field>
            <Field label="Lanes">
              <input className="input mono" type="number" min={1} max={12} value={f.lane_count} onChange={(e) => set("lane_count", e.target.value)} />
            </Field>
            <Field label="Direction">
              <select className="input" value={f.direction} onChange={(e) => set("direction", e.target.value)}>
                {COMPASS.map((d) => (
                  <option key={d}>{d}</option>
                ))}
              </select>
            </Field>
          </fieldset>

          <fieldset className="grid grid-cols-2 gap-2 rounded border border-ink-700 p-2 md:grid-cols-5">
            <legend className="px-1 text-[11px] font-semibold uppercase tracking-wide text-slate-400">Processing (blank = server default)</legend>
            <Field label="Resolution" hint="e.g. 1280x720">
              <input className="input mono" pattern="\d{3,4}x\d{3,4}" value={f.resolution} onChange={(e) => set("resolution", e.target.value)} />
            </Field>
            <Field label="Processing fps" hint="0.5–30">
              <input className="input mono" type="number" step="0.5" min={0.5} max={30} value={f.processing_fps} onChange={(e) => set("processing_fps", e.target.value)} />
            </Field>
            <Field label="Confidence" hint="0.05–0.95">
              <input className="input mono" type="number" step="0.01" min={0.05} max={0.95} value={f.confidence_threshold} onChange={(e) => set("confidence_threshold", e.target.value)} />
            </Field>
            <Field label="Frame skip" hint="0–30">
              <input className="input mono" type="number" min={0} max={30} value={f.frame_skip} onChange={(e) => set("frame_skip", e.target.value)} />
            </Field>
            <Field label="Max queue" hint="1–64">
              <input className="input mono" type="number" min={1} max={64} value={f.max_queue_size} onChange={(e) => set("max_queue_size", e.target.value)} />
            </Field>
          </fieldset>

          {err && (
            <div className="rounded border border-rose-700/60 bg-rose-950/40 p-2 text-[12px] text-rose-200" role="alert">
              {err}
            </div>
          )}
          <div className="flex items-center gap-2">
            <label className="flex items-center gap-2 text-[12px] text-slate-300">
              <input type="checkbox" checked={f.enabled} onChange={(e) => set("enabled", e.target.checked)} /> Start processing immediately
            </label>
            <div className="flex-1" />
            <button type="button" className="btn" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className="btn btn-primary" disabled={busy}>
              {busy && <span className="h-2.5 w-2.5 animate-spin rounded-full border border-slate-200 border-t-transparent" />}
              Create camera
            </button>
          </div>
        </div>

        <OnvifPanel
          username={f.username}
          password={f.password}
          onUse={(uri, resolution, name) => {
            setF((p) => ({ ...p, source_type: "rtsp", source_uri: uri, resolution: resolution && /^\d{3,4}x\d{3,4}$/.test(resolution) ? resolution : p.resolution, name: p.name || name || "" }));
            setTest(null);
          }}
        />
      </form>
    </Modal>
  );
}

function OnvifPanel({ username, password, onUse }: { username: string; password: string; onUse: (uri: string, resolution: string | null, name: string | null) => void }) {
  const [disc, setDisc] = useState<OnvifDiscoverResult | null>(null);
  const [discBusy, setDiscBusy] = useState(false);
  const [discErr, setDiscErr] = useState<string | null>(null);
  const [xaddr, setXaddr] = useState("");
  const [devName, setDevName] = useState<string | null>(null);
  const [probe, setProbe] = useState<OnvifProbeResult | null>(null);
  const [probeBusy, setProbeBusy] = useState(false);
  const [probeErr, setProbeErr] = useState<string | null>(null);

  const discover = async () => {
    setDiscBusy(true);
    setDiscErr(null);
    try {
      setDisc(await api.post<OnvifDiscoverResult>("/api/onvif/discover", { timeout_s: 3 }));
    } catch (e) {
      setDiscErr(errorText(e));
    } finally {
      setDiscBusy(false);
    }
  };
  const runProbe = async () => {
    setProbeBusy(true);
    setProbeErr(null);
    setProbe(null);
    try {
      setProbe(await api.post<OnvifProbeResult>("/api/onvif/probe", { xaddr: xaddr.trim(), username: username || undefined, password: password || undefined }));
    } catch (e) {
      setProbeErr(errorText(e));
    } finally {
      setProbeBusy(false);
    }
  };

  return (
    <aside className="space-y-2 rounded border border-ink-700 bg-ink-850 p-2">
      <div className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">ONVIF</div>
      <button type="button" className="btn w-full justify-center" onClick={discover} disabled={discBusy}>
        {discBusy && <span className="h-2.5 w-2.5 animate-spin rounded-full border border-slate-400 border-t-transparent" />}
        Discover cameras on the network
      </button>
      {discErr && <div className="text-[12px] text-rose-300">{discErr}</div>}
      {disc && (
        <div className="space-y-1 text-[12px]">
          {disc.note && <div className="text-[11px] text-slate-500">{disc.note}</div>}
          {disc.errors.map((e, i) => (
            <div key={i} className="text-[11px] text-amber-300">
              {e}
            </div>
          ))}
          {disc.devices.length === 0 ? (
            <div className="text-slate-500">No ONVIF devices answered within {disc.timeout_s}s.</div>
          ) : (
            <ul className="max-h-40 space-y-1 overflow-y-auto">
              {disc.devices.map((d) => (
                <li key={d.endpoint || d.xaddrs[0]}>
                  <button
                    type="button"
                    className={`w-full rounded border px-1.5 py-1 text-left ${xaddr === d.xaddrs[0] ? "border-cyan-600 bg-cyan-500/10" : "border-ink-700 hover:border-ink-600"}`}
                    onClick={() => {
                      setXaddr(d.xaddrs[0]);
                      setDevName(d.name);
                      setProbe(null);
                    }}
                  >
                    <div className="text-slate-200">{d.name ?? d.host ?? d.endpoint}</div>
                    <div className="mono text-[11px] text-slate-500">
                      {d.host}
                      {d.hardware ? ` · ${d.hardware}` : ""}
                    </div>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      <Field label="Device service URL (xaddr)" hint="uses the username/password entered in the form">
        <input className="input mono" value={xaddr} onChange={(e) => setXaddr(e.target.value)} placeholder="http://192.168.1.64/onvif/device_service" />
      </Field>
      <button type="button" className="btn w-full justify-center" onClick={runProbe} disabled={probeBusy || !/^https?:\/\/.{4,}/.test(xaddr.trim())}>
        {probeBusy && <span className="h-2.5 w-2.5 animate-spin rounded-full border border-slate-400 border-t-transparent" />}
        Probe device profiles
      </button>
      {probeErr && <div className="text-[12px] text-rose-300">{probeErr}</div>}
      {probe && (
        <div className="space-y-1 text-[12px]">
          <div className="text-[11px] text-slate-400">
            {[probe.device.Manufacturer, probe.device.Model, probe.device.FirmwareVersion].filter(Boolean).join(" · ") || "device information unavailable"}
          </div>
          {probe.profiles.length === 0 ? (
            <div className="text-slate-500">The device reported no media profiles.</div>
          ) : (
            probe.profiles.map((p) => (
              <div key={p.token} className="rounded border border-ink-700 p-1.5">
                <div className="flex items-center gap-1.5">
                  <span className="text-slate-200">{p.name || p.token}</span>
                  <span className="mono text-[11px] text-slate-500">
                    {[p.encoding, p.resolution, p.fps ? `${p.fps} fps` : null].filter(Boolean).join(" · ")}
                  </span>
                </div>
                {p.rtsp_uri ? (
                  <div className="mt-0.5 flex items-center gap-1">
                    <span className="mono min-w-0 flex-1 truncate text-[11px] text-slate-400" title={p.rtsp_uri}>
                      {p.rtsp_uri}
                    </span>
                    <button type="button" className="btn !px-1.5 !py-0" onClick={() => onUse(p.rtsp_uri!, p.resolution, devName)}>
                      Use
                    </button>
                  </div>
                ) : (
                  <div className="text-[11px] text-rose-300">{p.error ?? "no stream URI"}</div>
                )}
              </div>
            ))
          )}
        </div>
      )}
    </aside>
  );
}
