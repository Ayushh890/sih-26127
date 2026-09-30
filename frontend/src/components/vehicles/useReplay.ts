/** Animation clock for journey replay (drives the pure math in lib/replay.ts). */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { positionAt, realToVirtual, type ReplayState, type ReplayTimeline } from "../../lib/replay";

export interface ReplayControl {
  v: number;
  playing: boolean;
  state: ReplayState | null;
  play: () => void;
  pause: () => void;
  toggle: () => void;
  seek: (v: number) => void;
  /** Stop and go back to the start of the (next) timeline. */
  reset: () => void;
}

/** `speed` = virtual seconds per wall-clock second. */
export function useReplay(tl: ReplayTimeline | null, speed: number): ReplayControl {
  const [v, setV] = useState(0);
  const [playing, setPlaying] = useState(false);
  const vRef = useRef(0);
  const speedRef = useRef(speed);
  speedRef.current = speed;
  const realRef = useRef<number | null>(null);
  const prevTl = useRef(tl);

  // When the timeline changes (live refetch, gap compression toggled) keep the same *real*
  // instant (derived-state update during render, before realRef is overwritten below).
  let cur = v;
  if (tl !== prevTl.current) {
    prevTl.current = tl;
    cur = tl && realRef.current != null ? realToVirtual(tl, realRef.current) : 0;
    vRef.current = cur;
    if (cur !== v) setV(cur);
  }

  useEffect(() => {
    if (!playing || !tl) return;
    let raf = 0;
    let last = performance.now();
    let lastEmit = 0;
    const step = (now: number) => {
      const dt = Math.max(0, (now - last) / 1000);
      last = now;
      let nv = vRef.current + dt * speedRef.current;
      if (nv >= tl.duration) {
        nv = tl.duration;
        vRef.current = nv;
        setV(nv);
        setPlaying(false);
        return;
      }
      vRef.current = nv;
      if (now - lastEmit > 50) {
        lastEmit = now;
        setV(nv);
      }
      raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [playing, tl]);

  const state = useMemo(() => (tl ? positionAt(tl, cur) : null), [tl, cur]);
  realRef.current = state?.realMs ?? null;

  const seek = useCallback(
    (x: number) => {
      const nv = Math.max(0, Math.min(x, tl?.duration ?? 0));
      vRef.current = nv;
      setV(nv);
    },
    [tl],
  );
  const play = useCallback(() => {
    if (!tl || tl.duration <= 0) return;
    if (vRef.current >= tl.duration) {
      vRef.current = 0;
      setV(0);
    }
    setPlaying(true);
  }, [tl]);
  const pause = useCallback(() => setPlaying(false), []);
  const reset = useCallback(() => {
    setPlaying(false);
    realRef.current = null;
    vRef.current = 0;
    setV(0);
  }, []);
  const toggle = useCallback(() => (playing ? pause() : play()), [playing, play, pause]);
  return { v: cur, playing, state, play, pause, toggle, seek, reset };
}
