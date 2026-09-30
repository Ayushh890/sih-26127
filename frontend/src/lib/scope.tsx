/**
 * Global data scope selector. "auto" lets the backend decide (live if any live camera
 * exists, otherwise demo); the chosen scope is sent as `?scope=` on scope-aware endpoints
 * and every response echoes back what was actually used plus a synthetic-data notice.
 */
import { createContext, useContext, useState, type ReactNode } from "react";

export type Scope = "auto" | "live" | "demo" | "all";
const KEY = "nirnay.scope";

interface ScopeState {
  scope: Scope;
  setScope: (s: Scope) => void;
  /** Value for the `scope` query parameter (undefined = server default). */
  param: "live" | "demo" | "all" | undefined;
}

const Ctx = createContext<ScopeState | null>(null);

export function ScopeProvider({ children }: { children: ReactNode }) {
  const [scope, set] = useState<Scope>(() => {
    const v = localStorage.getItem(KEY);
    return v === "live" || v === "demo" || v === "all" ? v : "auto";
  });
  const setScope = (s: Scope) => {
    localStorage.setItem(KEY, s);
    set(s);
  };
  return <Ctx.Provider value={{ scope, setScope, param: scope === "auto" ? undefined : scope }}>{children}</Ctx.Provider>;
}

export function useScope(): ScopeState {
  const v = useContext(Ctx);
  if (!v) throw new Error("useScope outside ScopeProvider");
  return v;
}
