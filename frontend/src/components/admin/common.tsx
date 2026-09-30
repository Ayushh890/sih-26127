/** Small helpers shared by the administration / planning pages (topology, corridor, system…). */
import { useState } from "react";
import type { TopologyCamera } from "../NetworkMap";

/** Camera picker fed by the topology graph (only cameras that exist in the road graph). */
export function CameraSelect({
  cameras,
  value,
  onChange,
  placeholder = "Select camera…",
  id,
  disabled,
  required,
}: {
  cameras: TopologyCamera[];
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  id?: string;
  disabled?: boolean;
  required?: boolean;
}) {
  return (
    <select id={id} className="input" value={value} onChange={(e) => onChange(e.target.value)} disabled={disabled} required={required}>
      <option value="">{placeholder}</option>
      {cameras.map((c) => (
        <option key={c.id} value={c.id}>
          {c.id} — {c.name}
          {c.is_demo ? " (synthetic)" : ""}
        </option>
      ))}
    </select>
  );
}

/** Compact collapsible JSON viewer for audit details, event details etc. */
export function JsonToggle({ value, label = "details" }: { value: unknown; label?: string }) {
  const [open, setOpen] = useState(false);
  const empty = value == null || (typeof value === "object" && Object.keys(value as object).length === 0);
  if (empty) return <span className="text-slate-600">—</span>;
  return (
    <div className="min-w-0">
      <button type="button" className="text-[11px] text-cyan-400 hover:text-cyan-300" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        {open ? "▾" : "▸"} {label}
      </button>
      {open && <pre className="mono mt-1 max-h-64 max-w-xl overflow-auto whitespace-pre-wrap break-all rounded bg-ink-950 p-1.5 text-[11px] text-slate-300">{JSON.stringify(value, null, 2)}</pre>}
    </div>
  );
}

/** Human label for a snake_case settings / status key. */
export function prettyKey(k: string): string {
  return k.replace(/_/g, " ");
}

/** Render a scalar status value (booleans as yes/no, objects as compact JSON). */
export function scalar(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(2);
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

export function Toggle({ checked, onChange, disabled, label }: { checked: boolean; onChange: (v: boolean) => void; disabled?: boolean; label?: string }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={checked}
      aria-label={label}
      disabled={disabled}
      onClick={() => onChange(!checked)}
      className={`relative inline-flex h-4 w-7 shrink-0 items-center rounded-full border transition disabled:cursor-not-allowed disabled:opacity-50 ${
        checked ? "border-cyan-500 bg-cyan-700" : "border-ink-600 bg-ink-800"
      }`}
    >
      <span className={`inline-block h-3 w-3 rounded-full bg-slate-100 transition ${checked ? "translate-x-3" : "translate-x-0.5"}`} />
    </button>
  );
}
