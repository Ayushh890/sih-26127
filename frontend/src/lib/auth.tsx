import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import { api, ApiError, onUnauthorized, tokenStore } from "./api";

export interface Me {
  id: number;
  username: string;
  full_name: string | null;
  role: string;
  permissions: string[];
  can_view_raw_plates: boolean;
}

interface AuthState {
  user: Me | null;
  loading: boolean;
  login: (username: string, password: string) => Promise<void>;
  logout: () => void;
  has: (perm: string) => boolean;
}

const Ctx = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<Me | null>(null);
  const [loading, setLoading] = useState<boolean>(!!tokenStore.get());

  const logout = useCallback(() => {
    tokenStore.clear();
    setUser(null);
  }, []);

  useEffect(() => {
    onUnauthorized(logout);
    return () => onUnauthorized(null);
  }, [logout]);

  useEffect(() => {
    if (!tokenStore.get()) return;
    api
      .get<Me>("/api/auth/me")
      .then(setUser)
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) tokenStore.clear();
      })
      .finally(() => setLoading(false));
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    const res = await api.post<{ access_token: string }>("/api/auth/login", { username, password });
    tokenStore.set(res.access_token);
    setUser(await api.get<Me>("/api/auth/me"));
  }, []);

  const value = useMemo<AuthState>(
    () => ({ user, loading, login, logout, has: (p: string) => !!user?.permissions.includes(p) }),
    [user, loading, login, logout],
  );
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useAuth(): AuthState {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth outside AuthProvider");
  return v;
}

/** Permission names as defined by the backend (app/auth/permissions.py). */
export const P = {
  ALERTS_READ: "alerts:read",
  ALERTS_ACT: "alerts:act",
  ANALYTICS_READ: "analytics:read",
  OD_INDIVIDUAL: "analytics:od_individual",
  AUDIT_READ: "audit:read",
  CAMERAS_READ: "cameras:read",
  CAMERAS_WRITE: "cameras:write",
  CAMERAS_CONTROL: "cameras:control",
  CORRIDOR: "corridor:use",
  EVIDENCE_READ: "evidence:read",
  PLATES_RAW: "plates:view_raw",
  SETTINGS_WRITE: "settings:write",
  STREAM_VIEW: "stream:view",
  SYSTEM_READ: "system:read",
  TOPOLOGY_WRITE: "topology:write",
  TRAJECTORY_READ: "trajectory:read",
  USERS_MANAGE: "users:manage",
  VEHICLES_SEARCH: "vehicles:search",
  WATCHLIST_READ: "watchlist:read",
  WATCHLIST_WRITE: "watchlist:write",
} as const;
