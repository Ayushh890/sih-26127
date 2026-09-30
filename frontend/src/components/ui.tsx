/** Shared building blocks of the operations console (dense, dark, explicit states). */
import { useState, type ReactNode } from "react";
import { errorText } from "../lib/api";
import { TONE_CLASS, type Tone } from "../lib/format";

export function Badge({ tone = "slate", children, title, className = "" }: { tone?: Tone; children: ReactNode; title?: string; className?: string }) {
  return (
    <span title={title} className={`inline-flex items-center gap-1 whitespace-nowrap rounded border px-1.5 py-px text-[11px] font-semibold uppercase tracking-wide ${TONE_CLASS[tone]} ${className}`}>
      {children}
    </span>
  );
}

export function Dot({ color, pulse = false }: { color: string; pulse?: boolean }) {
  return (
    <span className="relative inline-flex h-2 w-2">
      {pulse && <span className="absolute inline-flex h-full w-full animate-ping rounded-full opacity-60" style={{ background: color }} />}
      <span className="relative inline-flex h-2 w-2 rounded-full" style={{ background: color }} />
    </span>
  );
}

export function Panel({
  title,
  actions,
  children,
  className = "",
  bodyClass = "p-2",
  subtitle,
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  bodyClass?: string;
  subtitle?: ReactNode;
}) {
  return (
    <section className={`flex min-h-0 flex-col rounded-md border border-ink-700 bg-ink-900/80 ${className}`}>
      {(title || actions) && (
        <header className="flex items-center justify-between gap-2 border-b border-ink-700 px-2.5 py-1.5">
          <div className="min-w-0">
            <h2 className="truncate text-[12px] font-semibold uppercase tracking-wider text-slate-300">{title}</h2>
            {subtitle && <div className="truncate text-[11px] text-slate-500">{subtitle}</div>}
          </div>
          {actions && <div className="flex shrink-0 items-center gap-1.5">{actions}</div>}
        </header>
      )}
      <div className={`min-h-0 flex-1 ${bodyClass}`}>{children}</div>
    </section>
  );
}

export function Stat({ label, value, hint, tone }: { label: string; value: ReactNode; hint?: ReactNode; tone?: string }) {
  return (
    <div className="rounded-md border border-ink-700 bg-ink-900/80 px-3 py-2">
      <div className="text-[11px] font-medium uppercase tracking-wide text-slate-400">{label}</div>
      <div className="mt-0.5 font-mono text-xl font-semibold" style={tone ? { color: tone } : undefined}>
        {value}
      </div>
      {hint && <div className="mt-0.5 truncate text-[11px] text-slate-500">{hint}</div>}
    </div>
  );
}

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 p-3 text-slate-400" role="status">
      <span className="h-3 w-3 animate-spin rounded-full border-2 border-slate-500 border-t-cyan-400" />
      {label}
    </div>
  );
}

export function ErrorBox({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  return (
    <div className="m-2 rounded border border-rose-700/60 bg-rose-950/40 p-2 text-rose-200" role="alert">
      <div className="font-medium">Request failed</div>
      <div className="text-[12px] text-rose-300">{errorText(error)}</div>
      {onRetry && (
        <button className="btn mt-2" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}

export function Empty({ children = "No data available for this period." }: { children?: ReactNode }) {
  return <div className="p-4 text-center text-[12px] text-slate-500">{children}</div>;
}

/** Renders loading / error / empty states around a react-query result. */
export function QueryState<T>({
  q,
  empty,
  isEmpty,
  children,
}: {
  q: { isLoading: boolean; error: unknown; data: T | undefined; refetch: () => unknown };
  empty?: ReactNode;
  isEmpty?: (d: T) => boolean;
  children: (d: T) => ReactNode;
}) {
  if (q.isLoading) return <Loading />;
  if (q.error) return <ErrorBox error={q.error} onRetry={() => q.refetch()} />;
  if (q.data === undefined) return <Empty>{empty}</Empty>;
  if (isEmpty && isEmpty(q.data)) return <Empty>{empty}</Empty>;
  return <>{children(q.data)}</>;
}

/** A banner shown whenever the displayed data includes synthetic demo data. */
export function SyntheticBanner({ info }: { info?: { synthetic?: boolean; notice?: string | null; scope?: string } | null }) {
  if (!info?.synthetic) return null;
  return (
    <div className="flex items-center gap-2 rounded border border-violet-500/40 bg-violet-950/40 px-2 py-1 text-[12px] text-violet-200">
      <Badge tone="violet">Synthetic</Badge>
      <span>{info.notice ?? "Includes synthetic demo data."}</span>
    </div>
  );
}

export function Explanation({ lines, className = "" }: { lines?: (string | null | undefined)[] | null; className?: string }) {
  const ls = (lines ?? []).filter(Boolean) as string[];
  if (!ls.length) return null;
  return (
    <ul className={`space-y-0.5 text-[12px] text-slate-300 ${className}`}>
      {ls.map((l, i) => (
        <li key={i} className="flex gap-1.5">
          <span className="text-cyan-500">›</span>
          <span>{l}</span>
        </li>
      ))}
    </ul>
  );
}

/** Button that runs an async action, shows progress and reports failures inline. */
export function ActionButton({
  onClick,
  children,
  className = "btn",
  confirm,
  disabled,
  title,
}: {
  onClick: () => Promise<unknown>;
  children: ReactNode;
  className?: string;
  confirm?: string;
  disabled?: boolean;
  title?: string;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  return (
    <span className="inline-flex flex-col">
      <button
        type="button"
        title={title}
        className={className}
        disabled={busy || disabled}
        onClick={async () => {
          if (confirm && !window.confirm(confirm)) return;
          setBusy(true);
          setErr(null);
          try {
            await onClick();
          } catch (e) {
            setErr(errorText(e));
          } finally {
            setBusy(false);
          }
        }}
      >
        {busy && <span className="h-2.5 w-2.5 animate-spin rounded-full border border-slate-400 border-t-transparent" />}
        {children}
      </button>
      {err && <span className="mt-0.5 max-w-xs text-[11px] text-rose-300">{err}</span>}
    </span>
  );
}

export function Field({ label, children, hint }: { label: string; children: ReactNode; hint?: ReactNode }) {
  return (
    <label className="block">
      <span className="label">{label}</span>
      {children}
      {hint && <span className="mt-0.5 block text-[11px] text-slate-500">{hint}</span>}
    </label>
  );
}

export function Modal({ title, onClose, children, wide = false }: { title: ReactNode; onClose: () => void; children: ReactNode; wide?: boolean }) {
  return (
    <div className="fixed inset-0 z-[2000] flex items-start justify-center overflow-y-auto bg-black/60 p-6" onMouseDown={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        className={`w-full ${wide ? "max-w-5xl" : "max-w-xl"} rounded-md border border-ink-600 bg-ink-900 shadow-2xl`}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <header className="flex items-center justify-between border-b border-ink-700 px-3 py-2">
          <h3 className="text-[13px] font-semibold text-slate-100">{title}</h3>
          <button className="btn btn-ghost" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>
        <div className="p-3">{children}</div>
      </div>
    </div>
  );
}

export function Tabs<T extends string>({ tabs, value, onChange }: { tabs: { id: T; label: ReactNode }[]; value: T; onChange: (v: T) => void }) {
  return (
    <div className="flex gap-0.5 border-b border-ink-700" role="tablist">
      {tabs.map((t) => (
        <button
          key={t.id}
          role="tab"
          aria-selected={value === t.id}
          className={`-mb-px border-b-2 px-3 py-1.5 text-[12px] font-medium ${value === t.id ? "border-cyan-400 text-cyan-200" : "border-transparent text-slate-400 hover:text-slate-200"}`}
          onClick={() => onChange(t.id)}
        >
          {t.label}
        </button>
      ))}
    </div>
  );
}

export function KV({ rows }: { rows: [ReactNode, ReactNode][] }) {
  return (
    <dl className="grid grid-cols-[max-content_1fr] gap-x-3 gap-y-0.5 text-[12px]">
      {rows.map(([k, v], i) => (
        <div key={i} className="contents">
          <dt className="text-slate-500">{k}</dt>
          <dd className="min-w-0 break-words text-slate-200">{v ?? "—"}</dd>
        </div>
      ))}
    </dl>
  );
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: ReactNode; actions?: ReactNode }) {
  return (
    <div className="mb-2 flex flex-wrap items-end justify-between gap-2">
      <div>
        <h1 className="text-[15px] font-semibold tracking-wide text-slate-100">{title}</h1>
        {subtitle && <div className="text-[12px] text-slate-400">{subtitle}</div>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-1.5">{actions}</div>}
    </div>
  );
}
