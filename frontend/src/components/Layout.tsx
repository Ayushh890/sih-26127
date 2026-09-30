import { useEffect, useState } from "react";
import { NavLink, Outlet } from "react-router-dom";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../lib/api";
import { P, useAuth } from "../lib/auth";
import { useScope, type Scope } from "../lib/scope";
import { stream, useConnState, useLiveEvent } from "../lib/ws";
import { Dot } from "./ui";

interface NavItem {
  to: string;
  label: string;
  perm?: string;
  icon: string;
}

const NAV: { group: string; items: NavItem[] }[] = [
  {
    group: "Operations",
    items: [
      { to: "/", label: "Command Center", icon: "◎", perm: P.CAMERAS_READ },
      { to: "/cameras", label: "Cameras", icon: "▣", perm: P.CAMERAS_READ },
      { to: "/local-camera", label: "Local Camera", icon: "◉", perm: P.CAMERAS_CONTROL },
      { to: "/alerts", label: "Alerts", icon: "△", perm: P.ALERTS_READ },
      { to: "/vehicles/search", label: "Vehicle Search", icon: "⌕", perm: P.VEHICLES_SEARCH },
      { to: "/watchlist", label: "Watchlist", icon: "☰", perm: P.WATCHLIST_READ },
    ],
  },
  {
    group: "Intelligence",
    items: [
      { to: "/analytics", label: "Analytics", icon: "▤", perm: P.ANALYTICS_READ },
      { to: "/corridor", label: "Emergency Corridor", icon: "✚", perm: P.CORRIDOR },
      { to: "/topology", label: "Road Topology", icon: "⋔", perm: P.CAMERAS_READ },
    ],
  },
  {
    group: "Administration",
    items: [
      { to: "/system", label: "System Health", icon: "♥", perm: P.SYSTEM_READ },
      { to: "/settings", label: "Settings", icon: "⚙", perm: P.SYSTEM_READ },
      { to: "/audit", label: "Audit Log", icon: "✎", perm: P.AUDIT_READ },
      { to: "/users", label: "Users", icon: "☺", perm: P.USERS_MANAGE },
    ],
  },
];

const CONN: Record<string, { color: string; label: string }> = {
  open: { color: "#34d399", label: "LIVE" },
  connecting: { color: "#38bdf8", label: "CONNECTING" },
  closed: { color: "#fb923c", label: "RECONNECTING" },
  unauthorized: { color: "#fb7185", label: "UNAUTHORIZED" },
  idle: { color: "#64748b", label: "IDLE" },
};

function Clock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(t);
  }, []);
  return <span className="font-mono text-[12px] text-slate-300">{now.toLocaleTimeString(undefined, { hour12: false })}</span>;
}

export function Layout() {
  const { user, logout, has } = useAuth();
  const { scope, setScope, param } = useScope();
  const conn = useConnState();
  const qc = useQueryClient();
  const [collapsed, setCollapsed] = useState(false);

  useEffect(() => {
    stream.start();
    return () => stream.stop();
  }, []);

  const openAlerts = useQuery({
    queryKey: ["alerts", "open-count", param],
    queryFn: () => api.get<{ total: number }>("/api/alerts", { status: "NEW", limit: 1, scope: param }),
    enabled: has(P.ALERTS_READ),
    refetchInterval: 60_000,
  });
  useLiveEvent(["alert_created", "alert_updated"], () => qc.invalidateQueries({ queryKey: ["alerts"] }));

  const c = CONN[conn] ?? CONN.idle;
  return (
    <div className="flex h-screen overflow-hidden bg-ink-950 text-slate-200">
      <aside className={`${collapsed ? "w-12" : "w-52"} flex shrink-0 flex-col border-r border-ink-700 bg-ink-900 transition-[width]`}>
        <div className="flex h-11 items-center gap-2 border-b border-ink-700 px-3">
          <button className="text-cyan-400" onClick={() => setCollapsed((v) => !v)} aria-label="Toggle navigation" title="Toggle navigation">
            ◈
          </button>
          {!collapsed && (
            <div className="leading-tight">
              <div className="text-[13px] font-bold tracking-[0.2em] text-slate-100">NIRNAY</div>
              <div className="text-[9px] uppercase tracking-wider text-slate-500">City-wide vehicle intelligence</div>
            </div>
          )}
        </div>
        <nav className="flex-1 overflow-y-auto py-2">
          {NAV.map((g) => {
            const items = g.items.filter((i) => !i.perm || has(i.perm));
            if (!items.length) return null;
            return (
              <div key={g.group} className="mb-2">
                {!collapsed && <div className="px-3 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-wider text-slate-500">{g.group}</div>}
                {items.map((i) => (
                  <NavLink
                    key={i.to}
                    to={i.to}
                    end={i.to === "/"}
                    title={i.label}
                    className={({ isActive }) =>
                      `mx-1.5 flex items-center gap-2 rounded px-2 py-1.5 text-[12.5px] ${isActive ? "bg-cyan-500/10 text-cyan-200" : "text-slate-400 hover:bg-ink-800 hover:text-slate-200"}`
                    }
                  >
                    <span className="w-4 text-center">{i.icon}</span>
                    {!collapsed && <span className="flex-1 truncate">{i.label}</span>}
                    {!collapsed && i.to === "/alerts" && (openAlerts.data?.total ?? 0) > 0 && (
                      <span className="rounded bg-rose-500/20 px-1.5 font-mono text-[10px] text-rose-300">{openAlerts.data!.total}</span>
                    )}
                  </NavLink>
                ))}
              </div>
            );
          })}
        </nav>
        {!collapsed && user && (
          <div className="border-t border-ink-700 p-2 text-[11px]">
            <div className="truncate text-slate-300">{user.full_name || user.username}</div>
            <div className="flex items-center justify-between text-slate-500">
              <span className="uppercase">{user.role}</span>
              <button className="text-slate-400 hover:text-rose-300" onClick={logout}>
                Sign out
              </button>
            </div>
          </div>
        )}
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-11 shrink-0 items-center gap-3 border-b border-ink-700 bg-ink-900/70 px-3">
          <div className="flex items-center gap-1.5" title={`Live event stream: ${c.label}`} data-testid="conn-state">
            <Dot color={c.color} pulse={conn === "open"} />
            <span className="text-[11px] font-semibold tracking-wide" style={{ color: c.color }}>
              {c.label}
            </span>
          </div>
          <div className="h-4 w-px bg-ink-700" />
          <label className="flex items-center gap-1.5 text-[11px] text-slate-400">
            Data scope
            <select className="input !w-auto !py-0.5 text-[12px]" value={scope} onChange={(e) => setScope(e.target.value as Scope)} aria-label="Data scope">
              <option value="auto">Auto</option>
              <option value="live">Live only</option>
              <option value="demo">Demo (synthetic)</option>
              <option value="all">All sources</option>
            </select>
          </label>
          <div className="flex-1" />
          {has(P.ALERTS_READ) && (
            <NavLink to="/alerts?status=NEW" className="flex items-center gap-1.5 text-[12px] text-slate-300 hover:text-white" title="New alerts">
              <span className={(openAlerts.data?.total ?? 0) > 0 ? "text-rose-400" : "text-slate-500"}>△</span>
              <span className="font-mono">{openAlerts.data?.total ?? "—"}</span>
              <span className="text-slate-500">new</span>
            </NavLink>
          )}
          {user && !user.can_view_raw_plates && (
            <span className="rounded border border-violet-500/40 px-1.5 text-[10px] font-semibold uppercase text-violet-300" title="Plate numbers are pseudonymised for your role">
              Privacy mode
            </span>
          )}
          <Clock />
        </header>
        <main className="min-h-0 flex-1 overflow-auto p-3">
          <Outlet />
        </main>
      </div>
    </div>
  );
}
