import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { buildTimeline, pointAlong, positionAt, realToVirtual, stopTime, type ReplayPointInput, type ReplaySegmentInput } from "../lib/replay";
import { PlateText, Reasons } from "../components/vehicles/common";

const T0 = Date.parse("2026-09-30T12:00:00Z");
const iso = (s: number) => new Date(T0 + s * 1000).toISOString();

// Near the equator so cos(lat) ≈ 1 and planar distances are easy to reason about.
const A: [number, number] = [0, 0];
const B: [number, number] = [0, 0.02];
const C: [number, number] = [0.02, 0.02];

function pt(seq: number, cam: string, s: number, c: [number, number], journey = 0): ReplayPointInput {
  return { seq, journey_index: journey, ts: iso(s), latitude: c[0], longitude: c[1], camera_id: cam };
}

/** A→B along an L-shaped road (detour north then east then back south), B→C straight. */
const points = [pt(0, "CAM-A", 0, A), pt(1, "CAM-B", 100, B), pt(2, "CAM-C", 300, C)];
const segments: ReplaySegmentInput[] = [
  { from_seq: 0, to_seq: 1, geometry: [[0, 0], [0.01, 0], [0.01, 0.02], [0, 0.02]] },
  { from_seq: 1, to_seq: 2, geometry: [[0, 0.02], [0.02, 0.02]] },
];

const close = (a: [number, number] | null, b: [number, number]) => {
  expect(a).not.toBeNull();
  expect(a![0]).toBeCloseTo(b[0], 6);
  expect(a![1]).toBeCloseTo(b[1], 6);
};

describe("replay timeline A→B→C", () => {
  const tl = buildTimeline(points, segments);

  it("builds stops and road legs with real-time durations", () => {
    expect(tl.stops.map((s) => s.cameraId)).toEqual(["CAM-A", "CAM-B", "CAM-C"]);
    expect(tl.legs).toHaveLength(2);
    expect(tl.legs.every((l) => l.kind === "road")).toBe(true);
    expect(tl.duration).toBe(300);
    expect(tl.realDuration).toBe(300);
    expect(tl.startMs).toBe(T0);
    expect(tl.endMs).toBe(T0 + 300_000);
  });

  it("starts at A and is 'at stop' there", () => {
    const s = positionAt(tl, 0);
    close(s.coord, A);
    expect(s.atStop).toBe(0);
    expect(s.realMs).toBe(T0);
  });

  it("follows the road geometry time-proportionally (not the straight line)", () => {
    // 25% of the A→B time = 25% of the 0.04-long L path = first corner [0.01, 0]
    close(positionAt(tl, 25).coord, [0.01, 0]);
    // halfway through A→B = middle of the long middle leg [0.01, 0.01]
    const mid = positionAt(tl, 50);
    close(mid.coord, [0.01, 0.01]);
    expect(mid.kind).toBe("road");
    expect(mid.legIndex).toBe(0);
    expect(mid.stopIndex).toBe(0);
    expect(mid.atStop).toBeNull();
    expect(mid.frac).toBeCloseTo(0.5);
    expect(mid.realMs).toBe(T0 + 50_000);
  });

  it("arrives at B exactly at B's timestamp and continues to C", () => {
    const atB = positionAt(tl, 100);
    close(atB.coord, B);
    expect(atB.atStop).toBe(1);
    expect(atB.stopIndex).toBe(1);
    // B→C lasts 200 s; at t=200 we are halfway
    const s = positionAt(tl, 200);
    close(s.coord, [0.01, 0.02]);
    expect(s.legIndex).toBe(1);
    expect(s.realMs).toBe(T0 + 200_000);
  });

  it("ends (and clamps) at C", () => {
    for (const v of [300, 1e6]) {
      const s = positionAt(tl, v);
      close(s.coord, C);
      expect(s.atStop).toBe(2);
      expect(s.realMs).toBe(T0 + 300_000);
    }
    close(positionAt(tl, -5).coord, A);
  });

  it("maps stops and real instants back to virtual time", () => {
    expect(stopTime(tl, 0)).toBe(0);
    expect(stopTime(tl, 1)).toBe(100);
    expect(stopTime(tl, 2)).toBe(300);
    expect(realToVirtual(tl, T0 + 150_000)).toBeCloseTo(150);
    expect(realToVirtual(tl, T0 - 1)).toBe(0);
    expect(realToVirtual(tl, T0 + 999_999)).toBe(300);
  });

  it("ignores input order (sorts by timestamp)", () => {
    const shuffled = buildTimeline([points[2], points[0], points[1]], segments);
    expect(shuffled.stops.map((s) => s.cameraId)).toEqual(["CAM-A", "CAM-B", "CAM-C"]);
    close(positionAt(shuffled, 50).coord, [0.01, 0.01]);
  });
});

describe("gap compression", () => {
  const tl = buildTimeline(points, segments, { compressGapsS: 60 });

  it("caps each interval but keeps the readout in real time", () => {
    expect(tl.duration).toBe(120); // 100→60, 200→60
    expect(tl.realDuration).toBe(300);
    expect(stopTime(tl, 1)).toBe(60);
    const s = positionAt(tl, 90); // halfway through the compressed B→C leg
    close(s.coord, [0.01, 0.02]);
    expect(s.realMs).toBe(T0 + 200_000);
    expect(realToVirtual(tl, T0 + 200_000)).toBeCloseTo(90);
  });

  it("leaves short intervals untouched", () => {
    const t2 = buildTimeline(points, segments, { compressGapsS: 1000 });
    expect(t2.duration).toBe(300);
  });
});

describe("unknown route and journey breaks", () => {
  it("uses the resolver when a segment has no geometry, else a gap that holds position", () => {
    const pts = [pt(0, "CAM-A", 0, A), pt(1, "CAM-B", 100, B)];
    const viaResolver = buildTimeline(pts, [], { resolvePath: () => [A, [0, 0.01], B] });
    expect(viaResolver.legs[0].kind).toBe("road");
    close(positionAt(viaResolver, 50).coord, [0, 0.01]);

    const noRoute = buildTimeline(pts, [], { resolvePath: () => null });
    expect(noRoute.legs[0].kind).toBe("gap");
    const s = positionAt(noRoute, 50);
    expect(s.kind).toBe("gap");
    close(s.coord, A); // not observed: do not invent a path
    close(positionAt(noRoute, 100).coord, B);
  });

  it("does not draw a road between journeys even if a path could be resolved", () => {
    const pts = [pt(0, "CAM-A", 0, A, 0), pt(1, "CAM-B", 100, B, 0), pt(2, "CAM-C", 4000, C, 1)];
    const tl = buildTimeline(pts, segments.slice(0, 1), { resolvePath: () => [B, C] });
    expect(tl.legs.map((l) => l.kind)).toEqual(["road", "gap"]);
    const s = positionAt(tl, 2000);
    close(s.coord, B);
    expect(s.kind).toBe("gap");
    expect(tl.duration).toBe(4000);
    expect(buildTimeline(pts, segments.slice(0, 1), { compressGapsS: 60 }).duration).toBe(120);
  });

  it("anchors geometry to the stops when the polyline does not start/end on them", () => {
    const tl = buildTimeline([pt(0, "CAM-A", 0, A), pt(1, "CAM-B", 10, B)], [{ from_seq: 0, to_seq: 1, geometry: [[0, 0.005], [0, 0.015]] }]);
    expect(tl.legs[0].coords[0]).toEqual(A);
    expect(tl.legs[0].coords[tl.legs[0].coords.length - 1]).toEqual(B);
  });

  it("handles a single sighting, empty input and identical timestamps", () => {
    const one = buildTimeline([pt(0, "CAM-A", 0, A)]);
    expect(one.duration).toBe(0);
    close(positionAt(one, 10).coord, A);
    expect(positionAt(buildTimeline([]), 0).coord).toBeNull();
    const same = buildTimeline([pt(0, "CAM-A", 0, A), pt(1, "CAM-B", 0, B), pt(2, "CAM-C", 10, C)], segments);
    const s = positionAt(same, 0);
    expect(Number.isFinite(s.coord![0])).toBe(true);
    close(positionAt(same, 10).coord, C);
  });
});

describe("pointAlong", () => {
  it("interpolates by length", () => {
    const coords: [number, number][] = [[0, 0], [0, 1], [0, 3]];
    const cum = [0, 1, 3];
    close(pointAlong(coords, cum, 0.5), [0, 1.5]);
    close(pointAlong(coords, cum, 0), [0, 0]);
    close(pointAlong(coords, cum, 1), [0, 3]);
  });
});

describe("vehicle UI pieces", () => {
  it("shows pseudonymised plates exactly as returned", () => {
    render(<PlateText display="PSN-563160A835" text="PSN-563160A835" />);
    const el = screen.getByText("PSN-563160A835");
    expect(el).toHaveAttribute("title", "Pseudonymised for your role");
  });

  it("marks unreadable plates instead of hiding them", () => {
    render(<PlateText display={null} text={null} />);
    expect(screen.getByText("unreadable")).toBeInTheDocument();
  });

  it("never hides match reasons (first visible, rest expandable)", () => {
    render(<Reasons reasons={["plate matches exactly", "travel time 110s vs expected 144s"]} />);
    expect(screen.getByText(/plate matches exactly/)).toBeInTheDocument();
    expect(screen.getByText(/travel time 110s/)).toBeInTheDocument();
  });

  it("falls back to the explanation string", () => {
    render(<Reasons reasons={[]} fallback="first sighting of a journey" />);
    expect(screen.getByText("first sighting of a journey")).toBeInTheDocument();
  });
});
