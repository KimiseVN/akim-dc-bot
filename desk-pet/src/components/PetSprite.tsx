// CSS-spritesheet animator. The component renders one frame-sized
// <div> with a `background-image` of the spritesheet and uses a CSS
// keyframe to step the `background-position-x` from 0 to
// -(frameWidth * frames). `animation-timing-function: steps(N)` gives
// the frame-by-frame look without a JS ticker.
//
// We emit a unique keyframe rule per (assetId, animation) pair into
// a <style> element so different pets with different frame counts
// don't fight over the same `@keyframes` name.

import { useEffect, useMemo, useRef } from "react";
import { LoadedAsset, PetAnimationState, SpriteAnimationDefinition } from "../pet/manifest";
import { resolveAnimation } from "../pet/visualState";

const _injectedKeyframes = new Set<string>();

function ensureKeyframes(name: string, totalX: number): void {
  if (_injectedKeyframes.has(name)) return;
  const css = `@keyframes ${name} { from { background-position-x: 0px; } to { background-position-x: -${totalX}px; } }`;
  const style = document.createElement("style");
  style.dataset.petKeyframe = name;
  style.appendChild(document.createTextNode(css));
  document.head.appendChild(style);
  _injectedKeyframes.add(name);
}

export type PetSpriteProps = {
  asset: LoadedAsset | null;
  animation: PetAnimationState;
  scale?: number;
  onAnimationEnd?: () => void;
};

export default function PetSprite({
  asset,
  animation,
  scale = 1,
  onAnimationEnd,
}: PetSpriteProps): JSX.Element | null {
  const elRef = useRef<HTMLDivElement | null>(null);

  // Resolve the manifest's animation row for this state, with fallback.
  const def = useMemo<SpriteAnimationDefinition | null>(() => {
    if (!asset) return null;
    const anims = asset.manifest.animations || {};
    const availableRows = new Set<PetAnimationState>(
      Object.keys(anims) as PetAnimationState[],
    );
    const resolved = resolveAnimation(animation, availableRows);
    return anims[resolved] || null;
  }, [asset, animation]);

  const frameWidth = asset?.manifest.frameWidth ?? 192;
  const frameHeight = asset?.manifest.frameHeight ?? 208;
  const totalX = def ? frameWidth * def.frames : 0;
  const keyframeName = useMemo(
    () => (def ? `pet-anim-${asset?.assetId}-${def.row}-${def.frames}` : ""),
    [asset?.assetId, def],
  );

  useEffect(() => {
    if (def && keyframeName) {
      ensureKeyframes(keyframeName, totalX);
    }
  }, [def, keyframeName, totalX]);

  // Re-fire the CSS animation on every animation change. Without this
  // a same-state re-render would NOT replay; we want eat-after-eat to
  // visibly reset.
  useEffect(() => {
    const el = elRef.current;
    if (!el || !def) return;
    el.style.animation = "none";
    // Force reflow to flush the cancellation
    void el.offsetWidth;
    const iterations =
      def.iterations === "infinite" ? "infinite" : String(def.iterations);
    el.style.animation = `${keyframeName} ${def.durationMs}ms steps(${def.frames}) ${iterations}`;
  }, [def, keyframeName]);

  if (!asset || !def || !asset.spritesheetUrl) {
    return null;
  }

  return (
    <div
      ref={elRef}
      onAnimationEnd={() => {
        if (def.iterations !== "infinite") {
          onAnimationEnd?.();
        }
      }}
      style={{
        width: frameWidth,
        height: frameHeight,
        transform: `scale(${scale})`,
        transformOrigin: "center bottom",
        backgroundImage: `url(${asset.spritesheetUrl})`,
        backgroundPositionY: `-${def.row * frameHeight}px`,
        backgroundRepeat: "no-repeat",
        imageRendering: "auto",
      }}
    />
  );
}
