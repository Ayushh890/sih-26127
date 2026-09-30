/**
 * Runtime settings: one panel per backend section with a typed form generated from the
 * section's JSON (numbers, booleans, strings, lists, nested objects). Only changed keys are
 * sent (PATCH /api/settings/{section} {value}); "Reset to defaults" = DELETE.
 * Also hosts "Change my password" (every signed-in user reaches this page).
 */
import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, errorText } from "../lib/api";
import { P, useAuth } from "../lib/auth";
import { fmtTime } from "../lib/format";
import { ActionButton, Badge, PageHeader, Panel, QueryState } from "../components/ui";
import { ChangePassword } from "../components/admin/ChangePassword";
import { prettyKey, Toggle } from "../components/admin/common";

type Json = null | boolean | number | string | Json[] | { [k: string]: Json };
type Obj = { [k: string]: Json };

interface SettingsResponse {
  sections: Record<string, Obj>;
  customised: Record<string, { updated_by: string | null; updated_at: string | null }>;
}

/** Short operator-facing description of each known section (labels only; values come from the API). */
const SECTION_HELP: Record<string, string> = {
  identity: "Cross-camera identity fusion: signal weights, link thresholds, confidence levels and time/route plausibility.",
  ocr: "Plate OCR acceptance: confidence threshold and multi-frame voting.",
  congestion: "Congestion scoring: measurement window, baseline, level thresholds and metric weights.",
  alerts: "Alert rules per type: enable/disable, severity, thresholds and cooldowns.",
  retention: "How long observations, evidence and health data are kept before retention deletes them.",
  privacy: "Plate pseudonymisation per role and suppression of small origin–destination cells.",
};

/** String settings the backend restricts to a fixed set (mirrors backend validation / alert severities). */
function enumFor(section: string, path: string[]): string[] | null {
  const key = path[path.length - 1];
  if (key === "severity") return ["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"];
  if (section === "identity" && path.join(".") === "unconfirmed_plate_max_level") return ["HIGH", "MEDIUM", "LOW"];
  return null;
}

function isObj(v: Json | undefined): v is Obj {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** Keys of `next` that differ from `base`, recursively (the minimal PATCH body). */
function diff(base: Obj, next: Obj): Obj {
  const out: Obj = {};
  for (const [k, v] of Object.entries(next)) {
    const b = base[k];
    if (isObj(v) && isObj(b)) {
      const d = diff(b, v);
      if (Object.keys(d).length) out[k] = d;
    } else if (JSON.stringify(v) !== JSON.stringify(b)) out[k] = v;
  }
  return out;
}

function setIn(o: Obj, path: string[], v: Json): Obj {
  const [h, ...rest] = path;
  return { ...o, [h]: rest.length ? setIn(isObj(o[h]) ? (o[h] as Obj) : {}, rest, v) : v };
}

export default function SettingsPage() {
  const q = useQuery({ queryKey: ["settings"], queryFn: () => api.get<SettingsResponse>("/api/settings") });
  const { has } = useAuth();
  const canWrite = has(P.SETTINGS_WRITE);

  return (
    <div className="flex flex-col gap-2">
      <PageHeader
        title="Settings"
        subtitle={canWrite ? "Changes apply to running services within seconds and are audited." : "Read-only — your role lacks settings:write."}
        actions={
          q.data && (
            <div className="flex flex-wrap gap-1">
              {Object.keys(q.data.sections).map((s) => (
                <a key={s} href={`#settings-${s}`} className="btn btn-ghost !py-0.5">
                  {s}
                  {q.data!.customised[s] && <span className="text-amber-300">●</span>}
                </a>
              ))}
            </div>
          )
        }
      />
      <ChangePassword />
      <QueryState q={q} isEmpty={(d) => Object.keys(d.sections).length === 0} empty="The server returned no settings sections.">
        {(d) => (
          <>
            {Object.entries(d.sections).map(([name, value]) => (
              <SectionPanel key={name} name={name} value={value} meta={d.customised[name]} canWrite={canWrite} />
            ))}
          </>
        )}
      </QueryState>
    </div>
  );
}

function SectionPanel({ name, value, meta, canWrite }: { name: string; value: Obj; meta?: { updated_by: string | null; updated_at: string | null }; canWrite: boolean }) {
  const qc = useQueryClient();
  const [draft, setDraft] = useState<Obj>(value);
  const [base, setBase] = useState<Obj>(value);
  const [version, setVersion] = useState(0);
  const [invalid, setInvalid] = useState<Record<string, boolean>>({});
  const [err, setErr] = useState<string | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Server value changed (another save/reset/refetch) and we have no local edits → adopt it.
  if (JSON.stringify(value) !== JSON.stringify(base) && Object.keys(diff(base, draft)).length === 0) {
    setBase(value);
    setDraft(value);
    setVersion((v) => v + 1);
  }

  const patch = diff(base, draft);
  const dirty = Object.keys(patch).length > 0;
  const hasInvalid = Object.values(invalid).some(Boolean);

  function adopt(v: Obj) {
    setBase(v);
    setDraft(v);
    setInvalid({});
    setVersion((x) => x + 1);
  }

  async function save() {
    setBusy(true);
    setErr(null);
    setSaved(null);
    try {
      const r = await api.patch<{ section: string; value: Obj }>(`/api/settings/${name}`, { value: patch });
      adopt(r.value);
      setSaved(`Saved ${Object.keys(patch).join(", ")}.`);
      await qc.invalidateQueries({ queryKey: ["settings"] });
    } catch (e) {
      setErr(errorText(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div id={`settings-${name}`} className="scroll-mt-2">
      <Panel
        title={
          <span className="inline-flex items-center gap-2">
            {name}
            {meta ? <Badge tone="yellow">customised</Badge> : <Badge tone="slate">defaults</Badge>}
            {dirty && <Badge tone="cyan">unsaved</Badge>}
          </span>
        }
        subtitle={
          <>
            {SECTION_HELP[name] ?? ""}
            {meta && (
              <span className="text-amber-200/80">
                {SECTION_HELP[name] ? " · " : ""}last changed by {meta.updated_by ?? "unknown"} at {fmtTime(meta.updated_at, true)}
              </span>
            )}
          </>
        }
        actions={
          canWrite && (
            <>
              <button className="btn btn-ghost" disabled={!dirty || busy} onClick={() => adopt(base)}>
                Discard edits
              </button>
              <button className="btn btn-primary" disabled={!dirty || hasInvalid || busy} onClick={save}>
                {busy ? "Saving…" : "Save"}
              </button>
              {meta && (
                <ActionButton
                  className="btn btn-danger"
                  confirm={`Reset "${name}" to built-in defaults? All customised values in this section are removed.`}
                  onClick={async () => {
                    setErr(null);
                    const r = await api.del<{ section: string; value: Obj }>(`/api/settings/${name}`);
                    adopt(r.value);
                    setSaved("Reset to defaults.");
                    await qc.invalidateQueries({ queryKey: ["settings"] });
                  }}
                >
                  Reset to defaults
                </ActionButton>
              )}
            </>
          )
        }
      >
        {err && (
          <div className="mb-2 rounded border border-rose-700/60 bg-rose-950/40 px-2 py-1 text-[12px] text-rose-200" role="alert">
            Server rejected the change: {err}
          </div>
        )}
        {saved && !dirty && (
          <div className="mb-2 text-[12px] text-emerald-300" role="status">
            {saved}
          </div>
        )}
        <ObjectEditor
          key={version}
          section={name}
          path={[]}
          value={draft}
          base={base}
          disabled={!canWrite}
          onChange={(p, v) => setDraft((d) => setIn(d, p, v))}
          onValidity={(p, ok) => setInvalid((m) => ({ ...m, [p.join(".")]: !ok }))}
        />
      </Panel>
    </div>
  );
}

interface EditorProps {
  section: string;
  path: string[];
  disabled: boolean;
  onChange: (path: string[], v: Json) => void;
  onValidity: (path: string[], ok: boolean) => void;
}

function ObjectEditor({ value, base, ...p }: EditorProps & { value: Obj; base: Obj }) {
  const entries = Object.entries(value);
  const leaves = entries.filter(([, v]) => !isObj(v));
  const nested = entries.filter(([, v]) => isObj(v));
  return (
    <div className="space-y-2">
      {leaves.length > 0 && (
        <div className="grid grid-cols-1 gap-x-3 gap-y-2 sm:grid-cols-2 lg:grid-cols-4">
          {leaves.map(([k, v]) => (
            <LeafField key={k} {...p} path={[...p.path, k]} value={v} changed={JSON.stringify(v) !== JSON.stringify(base[k])} />
          ))}
        </div>
      )}
      {nested.length > 0 && (
        <div className={`grid grid-cols-1 gap-2 ${p.path.length === 0 ? "md:grid-cols-2 xl:grid-cols-3" : ""}`}>
          {nested.map(([k, v]) => (
            <fieldset key={k} className="rounded border border-ink-700 bg-ink-850/60 p-2">
              <legend className="px-1 text-[11px] font-semibold uppercase tracking-wide text-cyan-300">{prettyKey(k)}</legend>
              <ObjectEditor {...p} path={[...p.path, k]} value={v as Obj} base={isObj(base[k]) ? (base[k] as Obj) : {}} />
            </fieldset>
          ))}
        </div>
      )}
    </div>
  );
}

function LeafField({ section, path, value, disabled, onChange, onValidity, changed }: EditorProps & { value: Json; changed: boolean }) {
  const key = path[path.length - 1];
  const id = `set-${section}-${path.join("-")}`;
  const label = (
    <label htmlFor={id} className="label flex items-center gap-1">
      {prettyKey(key)}
      {changed && <span className="text-cyan-300" title="edited">●</span>}
    </label>
  );
  if (typeof value === "boolean") {
    return (
      <div>
        {label}
        <div className="flex h-[26px] items-center gap-2">
          <Toggle checked={value} disabled={disabled} label={prettyKey(key)} onChange={(v) => onChange(path, v)} />
          <span className="text-[12px] text-slate-400">{value ? "enabled" : "disabled"}</span>
        </div>
      </div>
    );
  }
  if (typeof value === "number") {
    return (
      <div>
        {label}
        <NumberInput id={id} value={value} disabled={disabled} onChange={(v) => onChange(path, v)} onValidity={(ok) => onValidity(path, ok)} />
      </div>
    );
  }
  if (Array.isArray(value)) {
    return (
      <div>
        {label}
        <ListInput id={id} value={value} disabled={disabled} onChange={(v) => onChange(path, v)} />
      </div>
    );
  }
  const options = enumFor(section, path);
  if (options) {
    const s = String(value ?? "");
    return (
      <div>
        {label}
        <select id={id} className="input" value={s} disabled={disabled} onChange={(e) => onChange(path, e.target.value)}>
          {(options.includes(s) ? options : [s, ...options]).map((o) => (
            <option key={o}>{o}</option>
          ))}
        </select>
      </div>
    );
  }
  return (
    <div>
      {label}
      <input id={id} className="input" maxLength={256} value={String(value ?? "")} disabled={disabled} onChange={(e) => onChange(path, e.target.value)} />
    </div>
  );
}

function NumberInput({ id, value, disabled, onChange, onValidity }: { id: string; value: number; disabled: boolean; onChange: (v: number) => void; onValidity: (ok: boolean) => void }) {
  const [text, setText] = useState(String(value));
  const [bad, setBad] = useState<string | null>(null);
  return (
    <>
      <input
        id={id}
        className={`input mono ${bad ? "!border-rose-500" : ""}`}
        type="number"
        step="any"
        min={0}
        value={text}
        disabled={disabled}
        onChange={(e) => {
          const t = e.target.value;
          setText(t);
          const n = Number(t);
          const problem = t.trim() === "" || !Number.isFinite(n) ? "Enter a number" : n < 0 ? "Must not be negative" : null;
          setBad(problem);
          onValidity(!problem);
          if (!problem) onChange(n);
        }}
      />
      {bad && <span className="mt-0.5 block text-[11px] text-rose-300">{bad}</span>}
    </>
  );
}

function ListInput({ id, value, disabled, onChange }: { id: string; value: Json[]; disabled: boolean; onChange: (v: Json[]) => void }) {
  const numeric = value.length > 0 && value.every((x) => typeof x === "number");
  const [text, setText] = useState(value.join(", "));
  return (
    <>
      <input
        id={id}
        className="input"
        value={text}
        disabled={disabled}
        onChange={(e) => {
          setText(e.target.value);
          const items = e.target.value
            .split(",")
            .map((s) => s.trim())
            .filter(Boolean);
          onChange(numeric ? items.map(Number).filter(Number.isFinite) : items);
        }}
      />
      <span className="mt-0.5 block text-[11px] text-slate-500">Comma-separated list</span>
    </>
  );
}
