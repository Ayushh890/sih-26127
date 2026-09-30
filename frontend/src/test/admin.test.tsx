import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import type { ReactNode } from "react";
import { AuthProvider } from "../lib/auth";
import SettingsPage from "../pages/Settings";
import Users from "../pages/Users";
import Audit from "../pages/Audit";
import Topology from "../pages/Topology";

// Leaflet needs a real layout engine; the map itself is not under test here.
vi.mock("../components/NetworkMap", async (orig) => ({
  ...(await orig<typeof import("../components/NetworkMap")>()),
  NetworkMap: () => <div data-testid="network-map" />,
}));

const ME = {
  id: 1,
  username: "admin",
  full_name: "Admin",
  role: "admin",
  permissions: ["settings:write", "system:read", "users:manage", "audit:read"],
  can_view_raw_plates: true,
};

const SETTINGS = {
  sections: {
    congestion: { window_s: 300, thresholds: { moderate: 30, heavy: 55, severe: 75 } },
    alerts: { WRONG_WAY: { enabled: true, severity: "HIGH", cooldown_s: 60 } },
  },
  customised: {},
};

type Call = { method: string; url: string; body: unknown };
let calls: Call[] = [];
type Handler = (method: string, url: string, body: unknown) => { status?: number; body: unknown } | undefined;

function mockFetch(handler: Handler) {
  calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      calls.push({ method, url: input, body });
      const r = input === "/api/auth/me" ? { body: ME } : handler(method, input, body);
      const status = r?.status ?? (r ? 200 : 404);
      const payload = r ? r.body : { detail: `unmocked ${method} ${input}` };
      return { ok: status < 400, status, text: async () => JSON.stringify(payload) } as Response;
    }),
  );
}

function wrap(ui: ReactNode) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <AuthProvider>
        <MemoryRouter>{ui}</MemoryRouter>
      </AuthProvider>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  sessionStorage.setItem("nirnay.token", "test-token");
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  sessionStorage.clear();
});

describe("Settings page", () => {
  it("PATCHes only the changed keys as {value: {...}}", async () => {
    mockFetch((method, url, body) => {
      if (method === "GET" && url === "/api/settings") return { body: SETTINGS };
      if (method === "PATCH" && url === "/api/settings/congestion")
        return { body: { section: "congestion", value: { ...SETTINGS.sections.congestion, ...(body as { value: object }).value } } };
      return undefined;
    });
    wrap(<SettingsPage />);
    await screen.findByText(/Changes apply to running services/);
    const input = (await waitFor(() => {
      const el = document.getElementById("set-congestion-window_s");
      if (!el) throw new Error("not yet");
      return el;
    })) as HTMLInputElement;
    expect(input.value).toBe("300");
    fireEvent.change(input, { target: { value: "600" } });
    const nested = document.getElementById("set-congestion-thresholds-heavy") as HTMLInputElement;
    fireEvent.change(nested, { target: { value: "60" } });

    const panel = document.getElementById("settings-congestion")!;
    fireEvent.click(within(panel).getByRole("button", { name: "Save" }));

    await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
    const patch = calls.find((c) => c.method === "PATCH")!;
    expect(patch.url).toBe("/api/settings/congestion");
    expect(patch.body).toEqual({ value: { window_s: 600, thresholds: { heavy: 60 } } });
    await within(panel).findByText(/Saved window_s, thresholds/);
  });

  it("blocks negative numbers client-side and shows server validation errors", async () => {
    mockFetch((method, url) => {
      if (method === "GET" && url === "/api/settings") return { body: SETTINGS };
      if (method === "PATCH") return { status: 422, body: { detail: "alerts.WRONG_WAY.cooldown_s: expected a number" } };
      return undefined;
    });
    wrap(<SettingsPage />);
    await screen.findByText(/Changes apply to running services/);
    const panel = await waitFor(() => document.getElementById("settings-alerts")!);
    const cooldown = document.getElementById("set-alerts-WRONG_WAY-cooldown_s") as HTMLInputElement;
    fireEvent.change(cooldown, { target: { value: "-5" } });
    expect(within(panel).getByText("Must not be negative")).toBeInTheDocument();
    expect(within(panel).getByRole("button", { name: "Save" })).toBeDisabled();

    fireEvent.change(cooldown, { target: { value: "90" } });
    fireEvent.click(within(panel).getByRole("button", { name: "Save" }));
    expect(await within(panel).findByRole("alert")).toHaveTextContent("expected a number");
  });
});

describe("Users page", () => {
  it("creates a user via POST /api/users and never renders the password", async () => {
    const list = [{ id: 1, username: "admin", full_name: "Admin", role: "admin", is_active: true, is_demo: true, created_at: null, last_login_at: null }];
    mockFetch((method, url, body) => {
      if (method === "GET" && url === "/api/users") return { body: list };
      if (method === "GET" && url === "/api/users/roles")
        return {
          body: [
            { name: "admin", description: "Full access", permissions: ["users:manage", "system:read"] },
            { name: "viewer", description: "Read only", permissions: ["system:read"] },
          ],
        };
      if (method === "POST" && url === "/api/users") {
        const b = body as { username: string; full_name: string; role: string };
        const u = { id: 9, username: b.username, full_name: b.full_name, role: b.role, is_active: true, is_demo: false, created_at: null, last_login_at: null };
        list.push(u);
        return { status: 201, body: u };
      }
      return undefined;
    });
    wrap(<Users />);
    // role matrix rendered from the API
    expect(await screen.findByText("users:manage")).toBeInTheDocument();
    await screen.findByText("Full access");
    const btn = screen.getByRole("button", { name: "+ New user" });
    await waitFor(() => expect(btn).toBeEnabled());
    fireEvent.click(btn);

    const dialog = await screen.findByRole("dialog");
    fireEvent.change(within(dialog).getByLabelText(/^Username/), { target: { value: "newop" } });
    fireEvent.change(within(dialog).getByLabelText(/^Full name/), { target: { value: "New Operator" } });
    fireEvent.change(within(dialog).getByLabelText(/^Password/), { target: { value: "s3cret-password" } });
    fireEvent.change(within(dialog).getByLabelText(/^Confirm password/), { target: { value: "s3cret-password" } });
    fireEvent.change(within(dialog).getByLabelText(/^Role/), { target: { value: "viewer" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Create user" }));

    await waitFor(() => expect(calls.some((c) => c.method === "POST" && c.url === "/api/users")).toBe(true));
    expect(calls.find((c) => c.method === "POST")!.body).toEqual({ username: "newop", full_name: "New Operator", password: "s3cret-password", role: "viewer" });
    expect(await screen.findByText("newop")).toBeInTheDocument();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(document.body.textContent).not.toContain("s3cret-password");
  });
});

describe("Audit page", () => {
  it("applies the 'Search history' quick filter as kind=search", async () => {
    mockFetch((method, url) => {
      if (method === "GET" && url.startsWith("/api/audit"))
        return {
          body: {
            total: 1,
            results: [
              { id: 1, ts: "2026-09-30T12:00:00Z", user_id: 1, username: "admin", role: "admin", action: "vehicle.search", resource_type: "vehicle", resource_id: "UP32", details: { q: "UP32" }, ip: "127.0.0.1", success: true },
            ],
          },
        };
      return undefined;
    });
    wrap(<Audit />);
    expect(await screen.findByText("vehicle.search")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Search history" }));
    await waitFor(() => expect(calls.some((c) => c.url.startsWith("/api/audit?") && c.url.includes("kind=search"))).toBe(true));
    const last = calls.filter((c) => c.url.startsWith("/api/audit")).pop()!;
    expect(last.url).toContain("limit=100");
    expect(last.url).toContain("offset=0");
  });
});

describe("Topology page", () => {
  const TOPO = {
    cameras: [
      { id: "CAM-01", lat: 26.83, lon: 80.92, name: "One", road_name: null, is_demo: true, direction: null },
      { id: "CAM-02", lat: 26.84, lon: 80.93, name: "Two", road_name: null, is_demo: true, direction: null },
    ],
    edges: [
      { id: 7, from_camera_id: "CAM-01", to_camera_id: "CAM-02", distance_m: 1200, min_travel_s: 50, typical_travel_s: 140, road_name: "Station Road", direction: "NE", allowed: true, road_id: 1, has_geometry: true },
    ],
    roads: [],
    geojson: { type: "FeatureCollection", features: [] },
  };

  async function openEdit() {
    wrap(<Topology />);
    fireEvent.click(await screen.findByRole("button", { name: "Edit" }));
    return screen.findByRole("dialog");
  }

  it("PATCHes direction and replacement road geometry", async () => {
    mockFetch((method, url) => {
      if (method === "GET" && url === "/api/topology") return { body: TOPO };
      if (method === "PATCH" && url === "/api/topology/edges/7") return { body: TOPO.edges[0] };
      return undefined;
    });
    // the Edit button only exists for topology:write
    ME.permissions.push("topology:write", "cameras:read");
    try {
      const dialog = await openEdit();
      fireEvent.change(within(dialog).getByLabelText(/^Travel direction/), { target: { value: "SW" } });
      fireEvent.change(within(dialog).getByLabelText(/^Geometry/), { target: { value: "replace" } });
      const ta = within(dialog).getByLabelText(/^Road geometry/);
      // a single point is rejected client-side
      fireEvent.change(ta, { target: { value: "[[26.83, 80.92]]" } });
      fireEvent.click(within(dialog).getByRole("button", { name: "Save changes" }));
      expect(await within(dialog).findByRole("alert")).toHaveTextContent("at least two");
      expect(calls.some((c) => c.method === "PATCH")).toBe(false);

      fireEvent.change(ta, { target: { value: "[[26.83, 80.92], [26.835, 80.925], [26.84, 80.93]]" } });
      fireEvent.click(within(dialog).getByRole("button", { name: "Save changes" }));
      await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
      const patch = calls.find((c) => c.method === "PATCH")!;
      expect(patch.url).toBe("/api/topology/edges/7");
      expect(patch.body).toEqual({ direction: "SW", path: [[26.83, 80.92], [26.835, 80.925], [26.84, 80.93]] });
    } finally {
      ME.permissions.splice(ME.permissions.indexOf("topology:write"), 2);
    }
  });

  it("clears road geometry with path: []", async () => {
    mockFetch((method, url) => {
      if (method === "GET" && url === "/api/topology") return { body: TOPO };
      if (method === "PATCH") return { body: { ...TOPO.edges[0], has_geometry: false } };
      return undefined;
    });
    ME.permissions.push("topology:write", "cameras:read");
    try {
      const dialog = await openEdit();
      fireEvent.change(within(dialog).getByLabelText(/^Geometry/), { target: { value: "clear" } });
      fireEvent.click(within(dialog).getByRole("button", { name: "Save changes" }));
      await waitFor(() => expect(calls.some((c) => c.method === "PATCH")).toBe(true));
      expect(calls.find((c) => c.method === "PATCH")!.body).toEqual({ path: [] });
    } finally {
      ME.permissions.splice(ME.permissions.indexOf("topology:write"), 2);
    }
  });
});
