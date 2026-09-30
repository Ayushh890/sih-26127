import { lazy, Suspense, type ReactNode } from "react";
import { BrowserRouter, Navigate, Route, Routes, useLocation } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ApiError } from "./lib/api";
import { AuthProvider, P, useAuth } from "./lib/auth";
import { ScopeProvider } from "./lib/scope";
import { Layout } from "./components/Layout";
import { Empty, Loading } from "./components/ui";
import Login from "./pages/Login";

const CommandCenter = lazy(() => import("./pages/CommandCenter"));
const Cameras = lazy(() => import("./pages/Cameras"));
const CameraDetail = lazy(() => import("./pages/CameraDetail"));
const VehicleSearch = lazy(() => import("./pages/VehicleSearch"));
const VehicleDetail = lazy(() => import("./pages/VehicleDetail"));
const ObservationDetail = lazy(() => import("./pages/ObservationDetail"));
const Alerts = lazy(() => import("./pages/Alerts"));
const AlertDetail = lazy(() => import("./pages/AlertDetail"));
const Analytics = lazy(() => import("./pages/Analytics"));
const Watchlist = lazy(() => import("./pages/Watchlist"));
const Topology = lazy(() => import("./pages/Topology"));
const Corridor = lazy(() => import("./pages/Corridor"));
const SystemPage = lazy(() => import("./pages/System"));
const SettingsPage = lazy(() => import("./pages/Settings"));
const Audit = lazy(() => import("./pages/Audit"));
const Users = lazy(() => import("./pages/Users"));

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      refetchOnWindowFocus: false,
      retry: (n, e) => !(e instanceof ApiError && e.status >= 400 && e.status < 500) && n < 2,
    },
  },
});

function RequireAuth({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth();
  const loc = useLocation();
  if (loading) return <Loading label="Restoring session…" />;
  if (!user) return <Navigate to="/login" replace state={{ from: loc.pathname + loc.search }} />;
  return <>{children}</>;
}

function Guard({ perm, children }: { perm: string; children: ReactNode }) {
  const { has } = useAuth();
  if (!has(perm)) return <Empty>Your role does not have access to this page ({perm}).</Empty>;
  return <Suspense fallback={<Loading />}>{children}</Suspense>;
}

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route
        element={
          <RequireAuth>
            <Layout />
          </RequireAuth>
        }
      >
        <Route index element={<Guard perm={P.CAMERAS_READ}><CommandCenter /></Guard>} />
        <Route path="cameras" element={<Guard perm={P.CAMERAS_READ}><Cameras /></Guard>} />
        <Route path="cameras/:id" element={<Guard perm={P.CAMERAS_READ}><CameraDetail /></Guard>} />
        <Route path="vehicles/search" element={<Guard perm={P.VEHICLES_SEARCH}><VehicleSearch /></Guard>} />
        <Route path="vehicles/:ref" element={<Guard perm={P.TRAJECTORY_READ}><VehicleDetail /></Guard>} />
        <Route path="observations/:id" element={<Guard perm={P.TRAJECTORY_READ}><ObservationDetail /></Guard>} />
        <Route path="alerts" element={<Guard perm={P.ALERTS_READ}><Alerts /></Guard>} />
        <Route path="alerts/:ref" element={<Guard perm={P.ALERTS_READ}><AlertDetail /></Guard>} />
        <Route path="analytics" element={<Guard perm={P.ANALYTICS_READ}><Analytics /></Guard>} />
        <Route path="watchlist" element={<Guard perm={P.WATCHLIST_READ}><Watchlist /></Guard>} />
        <Route path="topology" element={<Guard perm={P.CAMERAS_READ}><Topology /></Guard>} />
        <Route path="corridor" element={<Guard perm={P.CORRIDOR}><Corridor /></Guard>} />
        <Route path="system" element={<Guard perm={P.SYSTEM_READ}><SystemPage /></Guard>} />
        <Route path="settings" element={<Guard perm={P.SYSTEM_READ}><SettingsPage /></Guard>} />
        <Route path="audit" element={<Guard perm={P.AUDIT_READ}><Audit /></Guard>} />
        <Route path="users" element={<Guard perm={P.USERS_MANAGE}><Users /></Guard>} />
        <Route path="*" element={<Empty>Page not found.</Empty>} />
      </Route>
    </Routes>
  );
}

export default function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <ScopeProvider>
          <BrowserRouter>
            <AppRoutes />
          </BrowserRouter>
        </ScopeProvider>
      </AuthProvider>
    </QueryClientProvider>
  );
}
