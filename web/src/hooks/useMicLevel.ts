import { type RefObject, useEffect, useState } from "react";
import { MIC_SILENT_MS, meterFill, meterStep, rms } from "../logic/voice.ts";

/**
 * Show `track`'s live level on `el` as the CSS variable `--level` (0..1),
 * once per frame without re-rendering React. The audio goes to an analyser
 * only, never to the speakers.
 *
 * Returns true once the track has sent nothing for MIC_SILENT_MS since it
 * started: likely the wrong or a muted input. Any sound clears it for good,
 * so a working mic in a quiet room isn't flagged. Switching to another
 * silent input keeps it set, so the picker doesn't blink away mid-choice.
 */
export function useMicLevel(track: MediaStreamTrack | null, el: RefObject<HTMLElement | null>): boolean {
  const [silent, setSilent] = useState(false);
  useEffect(() => {
    const node = el.current;
    if (!track) setSilent(false);
    if (!track || !node || typeof AudioContext === "undefined") return;
    const ctx = new AudioContext();
    const source = ctx.createMediaStreamSource(new MediaStream([track]));
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 1024;
    source.connect(analyser);
    // Safari may start it suspended; a page that's capturing may resume it.
    if (ctx.state === "suspended") void ctx.resume().catch(() => {});
    const samples = new Float32Array(analyser.fftSize);
    let shown = 0;
    let heard = false;
    const began = performance.now();
    let last = began;
    let frame = 0;
    const tick = (now: number) => {
      analyser.getFloatTimeDomainData(samples);
      const fill = meterFill(rms(samples));
      shown = meterStep(shown, fill, (now - last) / 1000);
      last = now;
      node.style.setProperty("--level", shown.toFixed(3));
      if (!heard && fill > 0) {
        heard = true;
        setSilent(false);
      } else if (!heard && now - began > MIC_SILENT_MS) setSilent(true);
      frame = requestAnimationFrame(tick);
    };
    frame = requestAnimationFrame(tick);
    return () => {
      cancelAnimationFrame(frame);
      node.style.setProperty("--level", "0");
      source.disconnect();
      void ctx.close().catch(() => {});
    };
  }, [track, el]);
  return silent;
}
