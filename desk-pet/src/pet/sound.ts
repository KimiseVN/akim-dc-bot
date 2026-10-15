// Tiny procedural sound engine. We synthesize short blips with the Web
// Audio API instead of bundling audio files — no binary assets, no
// Howler dependency, and nothing to license. Each care action gets a
// small recognisable motif.
//
// The AudioContext is created lazily on first use (which is always
// inside a user-gesture handler — a click on the pet or a menu item —
// so the browser autoplay policy is satisfied).

import { ActionKind } from "../api/petApi";

let _ctx: AudioContext | null = null;

function ctx(): AudioContext | null {
  if (typeof window === "undefined") return null;
  if (_ctx) return _ctx;
  const Ctor =
    window.AudioContext ||
    (window as unknown as { webkitAudioContext?: typeof AudioContext })
      .webkitAudioContext;
  if (!Ctor) return null;
  _ctx = new Ctor();
  return _ctx;
}

type Blip = {
  freq: number;
  startMs: number;
  durMs: number;
  type?: OscillatorType;
  gain?: number;
};

function play(blips: Blip[]): void {
  const ac = ctx();
  if (!ac) return;
  // A context can get suspended (e.g. window hidden) — resume is async
  // but fire-and-forget is fine for a blip.
  if (ac.state === "suspended") void ac.resume();
  const now = ac.currentTime;
  for (const b of blips) {
    const osc = ac.createOscillator();
    const g = ac.createGain();
    osc.type = b.type ?? "sine";
    osc.frequency.value = b.freq;
    const t0 = now + b.startMs / 1000;
    const t1 = t0 + b.durMs / 1000;
    const peak = b.gain ?? 0.12;
    // Short attack + exponential release so blips don't click.
    g.gain.setValueAtTime(0.0001, t0);
    g.gain.exponentialRampToValueAtTime(peak, t0 + 0.012);
    g.gain.exponentialRampToValueAtTime(0.0001, t1);
    osc.connect(g).connect(ac.destination);
    osc.start(t0);
    osc.stop(t1 + 0.02);
  }
}

const MOTIFS: Record<ActionKind, Blip[]> = {
  // munch-munch
  feed: [
    { freq: 320, startMs: 0, durMs: 90, type: "square", gain: 0.08 },
    { freq: 300, startMs: 130, durMs: 90, type: "square", gain: 0.08 },
  ],
  // a downward "glug"
  drink: [
    { freq: 520, startMs: 0, durMs: 110 },
    { freq: 360, startMs: 110, durMs: 140 },
  ],
  // bouncy rising third
  play: [
    { freq: 523, startMs: 0, durMs: 90, type: "triangle" },
    { freq: 659, startMs: 100, durMs: 120, type: "triangle" },
  ],
  // airy shimmer
  clean: [
    { freq: 880, startMs: 0, durMs: 70, gain: 0.07 },
    { freq: 1175, startMs: 70, durMs: 90, gain: 0.07 },
  ],
  // soft low settle
  sleep: [
    { freq: 300, startMs: 0, durMs: 180, type: "sine", gain: 0.09 },
    { freq: 220, startMs: 180, durMs: 260, type: "sine", gain: 0.09 },
  ],
  // punchy three-note "work-out" fanfare
  train: [
    { freq: 392, startMs: 0, durMs: 90, type: "square", gain: 0.08 },
    { freq: 523, startMs: 95, durMs: 90, type: "square", gain: 0.08 },
    { freq: 659, startMs: 190, durMs: 130, type: "square", gain: 0.08 },
  ],
  // gentle two-note chime
  pet: [
    { freq: 660, startMs: 0, durMs: 110, type: "sine" },
    { freq: 990, startMs: 110, durMs: 150, type: "sine" },
  ],
};

/** Play the motif for a care action. No-op when audio is unavailable. */
export function playActionSound(action: ActionKind): void {
  const motif = MOTIFS[action];
  if (motif) play(motif);
}
