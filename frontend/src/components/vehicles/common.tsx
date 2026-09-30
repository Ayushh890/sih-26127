/** Small shared pieces for the vehicle / observation screens. */
import { useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { mediaUrl } from "../../lib/api";
import { confidenceTone, fmtPct } from "../../lib/format";
import { Badge } from "../ui";

export function SyntheticBadge({ show }: { show?: boolean | null }) {
  if (!show) return null;
  return (
    <Badge tone="violet" title="Synthetic demo data">
      Synthetic
    </Badge>
  );
}

/** Plate exactly as returned by the backend (may be a PSN-… pseudonym). */
export function PlateText({ display, text, className = "" }: { display?: string | null; text?: string | null; className?: string }) {
  const v = display ?? text;
  if (!v) return <span className="text-[11px] italic text-slate-500">unreadable</span>;
  const pseudo = v.startsWith("PSN-");
  return (
    <span className={`mono whitespace-nowrap ${pseudo ? "text-violet-300" : "text-slate-100"} ${className}`} title={pseudo ? "Pseudonymised for your role" : undefined}>
      {v}
    </span>
  );
}

export function MatchBadge({ level, score }: { level?: string | null; score?: number | null }) {
  if (!level && score == null) return <span className="text-slate-500">—</span>;
  return (
    <span className="inline-flex items-center gap-1.5">
      {level && <Badge tone={confidenceTone(level)}>{level}</Badge>}
      {score != null && <span className="mono text-slate-300">{score.toFixed(3)}</span>}
    </span>
  );
}

/** A 0..1 value as a thin horizontal bar. */
export function Bar({ value, color = "#22d3ee", width = 80 }: { value?: number | null; color?: string; width?: number }) {
  const v = value == null || Number.isNaN(value) ? null : Math.max(0, Math.min(1, value));
  return (
    <span className="inline-block h-1.5 rounded-sm bg-ink-700 align-middle" style={{ width }} title={v == null ? "n/a" : fmtPct(v, 1)}>
      {v != null && <span className="block h-full rounded-sm" style={{ width: `${v * 100}%`, background: color }} />}
    </span>
  );
}

/** Evidence image served with ?token=; shows the real failure (e.g. removed by retention) instead of a broken icon. */
export function EvidenceImg({ evidenceId, role, className = "", alt }: { evidenceId: string; role: string; className?: string; alt?: string }) {
  const [failed, setFailed] = useState(false);
  if (failed)
    return (
      <div className={`flex items-center justify-center rounded border border-ink-700 bg-ink-950 p-2 text-center text-[11px] text-slate-500 ${className}`}>
        {role} image unavailable
      </div>
    );
  return (
    <img
      src={mediaUrl(`/api/evidence/${evidenceId}/${role}.jpg`)}
      alt={alt ?? `${role} evidence`}
      loading="lazy"
      className={`rounded border border-ink-700 bg-ink-950 object-contain ${className}`}
      onError={() => setFailed(true)}
    />
  );
}

export function VehicleLink({ code }: { code?: string | null }) {
  if (!code) return <span className="text-slate-500">—</span>;
  return (
    <Link to={`/vehicles/${code}`} className="mono text-cyan-300 hover:underline">
      {code}
    </Link>
  );
}

export function ObsLink({ id, children }: { id?: number | null; children?: ReactNode }) {
  if (id == null) return <span className="text-slate-500">—</span>;
  return (
    <Link to={`/observations/${id}`} className="mono text-cyan-300 hover:underline">
      {children ?? `#${id}`}
    </Link>
  );
}

/** Match reasons, first line visible with the rest expandable (never hidden entirely). */
export function Reasons({ reasons, fallback }: { reasons?: string[] | null; fallback?: string | null }) {
  const list = (reasons ?? []).filter(Boolean);
  if (!list.length && fallback) list.push(fallback);
  if (!list.length) return <span className="text-slate-500">—</span>;
  if (list.length === 1) return <span className="text-[11px] text-slate-300">{list[0]}</span>;
  return (
    <details className="text-[11px] text-slate-300">
      <summary className="cursor-pointer marker:text-cyan-500">
        {list[0]} <span className="text-slate-500">(+{list.length - 1})</span>
      </summary>
      <ul className="mt-0.5 space-y-0.5 pl-3">
        {list.slice(1).map((r, i) => (
          <li key={i}>› {r}</li>
        ))}
      </ul>
    </details>
  );
}

/** Pager for offset/limit endpoints that report `total`. */
export function Pager({ total, limit, offset, onOffset }: { total: number; limit: number; offset: number; onOffset: (o: number) => void }) {
  const end = Math.min(total, offset + limit);
  return (
    <div className="flex items-center gap-2 text-[12px] text-slate-400">
      <span className="mono">
        {total === 0 ? 0 : offset + 1}–{end} of {total}
      </span>
      <button className="btn" disabled={offset <= 0} onClick={() => onOffset(Math.max(0, offset - limit))}>
        ‹ Prev
      </button>
      <button className="btn" disabled={end >= total} onClick={() => onOffset(offset + limit)}>
        Next ›
      </button>
    </div>
  );
}
