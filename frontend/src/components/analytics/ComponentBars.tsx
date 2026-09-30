/**
 * Explainable congestion components. The backend score is
 *   100 · Σ(wₖ · valueₖ) / Σ wₖ   over the components that had data,
 * so each bar shows the component value (0–1), its weight and the points it contributes.
 */
import { fmtDuration, fmtNum, scoreHex } from "../../lib/format";

export interface CongestionComponent {
  value: number;
  raw: number | null;
  text: string;
  segments?: { from: string; median_s: number; baseline_s: number | null; baseline_source?: string; samples: number }[];
}

export function ComponentBars({ components, weights }: { components: Record<string, CongestionComponent> | null | undefined; weights?: Record<string, number> | null }) {
  const entries = Object.entries(components ?? {});
  if (!entries.length) return null;
  const w = weights ?? {};
  const totalW = entries.reduce((s, [k]) => s + (w[k] ?? 0), 0);
  const ranked = [...entries].sort((a, b) => (w[b[0]] ?? 0) * b[1].value - (w[a[0]] ?? 0) * a[1].value);
  return (
    <div className="space-y-1.5">
      {ranked.map(([k, c]) => {
        const pts = totalW > 0 && w[k] != null ? (100 * w[k] * c.value) / totalW : null;
        return (
          <div key={k}>
            <div className="flex items-baseline justify-between gap-2 text-[11px]">
              <span className="font-semibold uppercase tracking-wide text-slate-300">{k.replace("_", " ")}</span>
              <span className="font-mono text-slate-400">
                {fmtNum(c.value * 100, 0)}%{w[k] != null && <> · w {fmtNum(w[k], 2)}</>}
                {pts != null && <> · +{fmtNum(pts, 1)} pts</>}
              </span>
            </div>
            <div className="mt-0.5 h-1.5 w-full rounded bg-ink-700" role="meter" aria-label={`${k} component`} aria-valuenow={Math.round(c.value * 100)} aria-valuemin={0} aria-valuemax={100}>
              <div className="h-1.5 rounded" style={{ width: `${Math.min(100, Math.max(0, c.value * 100))}%`, background: scoreHex(c.value * 100) }} />
            </div>
            <div className="mt-0.5 text-[11px] text-slate-400">{c.text}</div>
            {c.segments && c.segments.length > 0 && (
              <ul className="mt-0.5 space-y-px text-[11px] text-slate-500">
                {c.segments.map((s, i) => (
                  <li key={i} className="font-mono">
                    from {s.from}: median {fmtDuration(s.median_s)} vs baseline {fmtDuration(s.baseline_s)}
                    {s.baseline_source ? ` (${s.baseline_source})` : ""} · n={s.samples}
                  </li>
                ))}
              </ul>
            )}
          </div>
        );
      })}
    </div>
  );
}
