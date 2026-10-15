// Random-walk motion engine. The pet "walks" by moving its own
// transparent BrowserWindow horizontally across the work area; the
// sprite plays walkLeft / walkRight while it travels, then stands
// (idle/mood) during a dwell pause before picking a new target.
//
// The renderer owns the loop (timing + animation state) and reaches
// the window geometry through the preload IPC bridge. We use a
// setTimeout loop rather than requestAnimationFrame because rAF is
// throttled hard when the window isn't focused — a desktop pet should
// keep wandering while you work in another app.

import { useEffect, useRef, useState } from "react";

export type WalkAnim = "walkLeft" | "walkRight" | null;

const SPEED_PX_PER_SEC = 55; // leisurely wander
const TICK_MS = 1000 / 30; // 30fps is plenty for a slow walk
const MIN_DWELL_MS = 2500;
const MAX_DWELL_MS = 8000;
const SHORT_DWELL_MS = 1200; // when a target was too close to bother walking
const MIN_TRAVEL_PX = 32; // ignore targets nearer than this
const ARRIVE_EPS_PX = 0.5;
const MAX_DT_SEC = 0.1; // clamp so a long pause doesn't teleport the pet

function randBetween(lo: number, hi: number): number {
  return lo + Math.random() * (hi - lo);
}

type Segment = {
  curX: number;
  y: number;
  targetX: number;
};

/**
 * Drive the pet window's random walk. Returns the current walk
 * animation ("walkLeft" | "walkRight") while travelling, or null when
 * standing still — the caller composes this with mood/action anims.
 *
 * @param enabled  randomWalk toggle is on AND there's an active pet.
 * @param paused   hold position (cursor is over the pet, or an action
 *                 animation is playing) — the pet stands and waits.
 */
export function useRandomWalk(opts: {
  enabled: boolean;
  paused: boolean;
}): WalkAnim {
  const { enabled, paused } = opts;
  const [walkAnim, setWalkAnim] = useState<WalkAnim>(null);

  // Mutable loop state kept in refs so the single long-lived effect
  // reads live values without re-subscribing every render.
  const pausedRef = useRef(paused);
  pausedRef.current = paused;
  const segRef = useRef<Segment | null>(null);
  const dwellUntilRef = useRef(0);
  const lastTsRef = useRef(0);

  useEffect(() => {
    const bridge = typeof window !== "undefined" ? window.akimDeskPet : undefined;
    const geom = bridge?.window;
    const scr = bridge?.screen;
    // Outside Electron (plain browser dev) or on an old preload without
    // the geometry bridge: no-op, pet just stands.
    if (!enabled || !geom?.getBounds || !geom?.setPosition || !scr?.getWorkArea) {
      setWalkAnim(null);
      return;
    }

    let cancelled = false;
    let timer: number | null = null;
    // Reset transient state on (re)start so a previous run's half-finished
    // segment doesn't leak in.
    segRef.current = null;
    dwellUntilRef.current = 0;
    lastTsRef.current = 0;

    const schedule = () => {
      timer = window.setTimeout(() => void tick(), TICK_MS);
    };

    const stand = () => {
      segRef.current = null;
      setWalkAnim(null);
    };

    const tick = async () => {
      if (cancelled) return;
      const now = performance.now();
      const dt = lastTsRef.current
        ? Math.min(MAX_DT_SEC, (now - lastTsRef.current) / 1000)
        : 0;
      lastTsRef.current = now;

      // Cursor over the pet / action playing → freeze and (re)arm dwell so
      // it doesn't immediately dart off the instant the cursor leaves.
      if (pausedRef.current) {
        stand();
        dwellUntilRef.current = now + randBetween(MIN_DWELL_MS, MAX_DWELL_MS);
        schedule();
        return;
      }

      let seg = segRef.current;
      if (!seg) {
        if (now < dwellUntilRef.current) {
          setWalkAnim(null);
          schedule();
          return;
        }
        // Read live geometry once per segment — cheaper than per tick, and
        // it picks up the window's real position if the user dragged it.
        const bounds = await geom.getBounds();
        const area = await scr.getWorkArea();
        if (cancelled) return;
        if (!bounds || !area) {
          dwellUntilRef.current = now + SHORT_DWELL_MS;
          schedule();
          return;
        }
        const minX = area.x;
        const maxX = area.x + area.width - bounds.width;
        if (maxX <= minX) {
          // Window wider than the work area — nowhere to walk.
          dwellUntilRef.current = now + MAX_DWELL_MS;
          schedule();
          return;
        }
        const targetX = Math.round(randBetween(minX, maxX));
        if (Math.abs(targetX - bounds.x) < MIN_TRAVEL_PX) {
          dwellUntilRef.current = now + SHORT_DWELL_MS;
          schedule();
          return;
        }
        seg = { curX: bounds.x, y: bounds.y, targetX };
        segRef.current = seg;
      }

      const dx = seg.targetX - seg.curX;
      const dir = dx < 0 ? -1 : 1;
      const stepMax = SPEED_PX_PER_SEC * dt;
      if (Math.abs(dx) <= Math.max(stepMax, ARRIVE_EPS_PX)) {
        // Arrived — snap, stand, dwell.
        await geom.setPosition(seg.targetX, seg.y);
        if (cancelled) return;
        stand();
        dwellUntilRef.current = now + randBetween(MIN_DWELL_MS, MAX_DWELL_MS);
        schedule();
        return;
      }
      seg.curX += dir * stepMax;
      await geom.setPosition(Math.round(seg.curX), seg.y);
      if (cancelled) return;
      setWalkAnim(dir < 0 ? "walkLeft" : "walkRight");
      schedule();
    };

    schedule();

    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
      segRef.current = null;
      setWalkAnim(null);
    };
  }, [enabled]);

  return walkAnim;
}
