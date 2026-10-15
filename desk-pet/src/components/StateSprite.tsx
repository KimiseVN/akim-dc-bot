// Grid-spritesheet animator for the per-state art model. Each state is a
// uniform cols×rows grid of square cells (one pose per cell, left→right
// top→bottom). A JS ticker advances the frame at the state's fps; we step
// `background-position` rather than use a CSS keyframe so a 2-D grid
// (multiple rows) works. Loops or plays once depending on the state.
//
// The sheet URL is resolved per stage (baby/teen/adult/mystic), so the
// pet's art evolves with its stage automatically.

import { useEffect, useRef, useState } from "react";
import { StateSheetConfig, stateSheetUrl } from "../pet/manifest";

const DEFAULT_DISPLAY_HEIGHT = 240;

export default function StateSprite({
  assetId,
  stage,
  config,
  state,
  flipX = false,
  onAnimationEnd,
}: {
  assetId: string;
  stage: string;
  config: StateSheetConfig;
  state: string;
  flipX?: boolean;
  onAnimationEnd?: () => void;
}): JSX.Element {
  const timing = config.states[state] ?? { fps: 10, loop: true };
  const url = stateSheetUrl(assetId, config, stage, state);
  const size = config.displayHeight ?? DEFAULT_DISPLAY_HEIGHT;
  const [frame, setFrame] = useState(0);
  // Keep the latest onAnimationEnd without restarting the ticker.
  const onEndRef = useRef(onAnimationEnd);
  onEndRef.current = onAnimationEnd;

  // (Re)start the ticker whenever the sheet (state/stage) or timing
  // changes. flipX changes don't restart it — the run cycle keeps going.
  useEffect(() => {
    setFrame(0);
    let f = 0;
    let ended = false;
    const period = 1000 / Math.max(1, timing.fps);
    const id = window.setInterval(() => {
      f += 1;
      if (f >= config.frames) {
        if (timing.loop) {
          f = 0;
        } else {
          window.clearInterval(id);
          setFrame(config.frames - 1);
          if (!ended) {
            ended = true;
            onEndRef.current?.();
          }
          return;
        }
      }
      setFrame(f);
    }, period);
    return () => window.clearInterval(id);
  }, [url, timing.fps, timing.loop, config.frames]);

  const col = frame % config.cols;
  const row = Math.floor(frame / config.cols);

  return (
    <div
      style={{
        width: size,
        height: size,
        backgroundImage: `url(${url})`,
        // Square cells → cell display size == height. Scaling the whole
        // sheet by `size` keeps the stepping math in display pixels.
        backgroundSize: `${config.cols * size}px ${config.rows * size}px`,
        backgroundPosition: `-${col * size}px -${row * size}px`,
        backgroundRepeat: "no-repeat",
        transform: flipX ? "scaleX(-1)" : undefined,
        transformOrigin: "center bottom",
        imageRendering: "auto",
      }}
    />
  );
}
