import { useCallback, useEffect, useRef, useState } from "react";
import { paths } from "../api.ts";
import { read, write } from "../logic/appearance.ts";
import { SIM_SOUND_KEY, isSimSounds, newSimSounds, type SimSoundAction } from "../logic/simSounds.ts";
import { readEventPayload, useEventSource } from "./useEventSource.ts";

export interface SimSoundsState {
  /** Play the sim's sounds in this browser (remembered per browser). */
  on: boolean;
  setOn: (on: boolean) => void;
}

/**
 * Sim mode (#219): play what the sim daemon would have played on the robot's
 * speaker. Browsers start audio only after a tap or key press on the page, so
 * the first one unlocks it; tapping an expression or the power button is
 * enough. Files are fetched and decoded whole, which Safari needs (it won't
 * stream audio from a server that doesn't answer range requests).
 */
export function useSimSounds(active: boolean): SimSoundsState {
  const [on, setOnState] = useState(() => read(SIM_SOUND_KEY) !== "off");
  const context = useRef<AudioContext | null>(null);
  const playing = useRef(new Set<AudioBufferSourceNode>());
  // Bumped by a stop, so a sound still downloading doesn't start after it.
  const generation = useRef(0);
  // The newest event this stream has acted on; null until its first payload.
  const last = useRef<number | null>(null);
  const listening = active && on;

  const audio = useCallback((): AudioContext | null => {
    if (context.current) return context.current;
    const Context = window.AudioContext ?? (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext;
    if (!Context) return null;
    context.current = new Context();
    return context.current;
  }, []);

  const stopAll = useCallback(() => {
    generation.current += 1;
    for (const source of playing.current) {
      try {
        source.stop();
      } catch {
        // Already ended.
      }
    }
    playing.current.clear();
  }, []);

  const play = useCallback(
    async (token: string) => {
      const ctx = audio();
      if (!ctx || ctx.state !== "running") return;
      const started = generation.current;
      const response = await fetch(paths.simSound(token), { credentials: "same-origin" });
      if (!response.ok) return;
      const buffer = await ctx.decodeAudioData(await response.arrayBuffer());
      if (generation.current !== started) return;
      // Like the robot's speaker: a new sound cuts off the one playing.
      for (const previous of playing.current) {
        try {
          previous.stop();
        } catch {
          // Already ended.
        }
      }
      playing.current.clear();
      const source = ctx.createBufferSource();
      source.buffer = buffer;
      source.connect(ctx.destination);
      source.onended = () => playing.current.delete(source);
      playing.current.add(source);
      source.start();
    },
    [audio],
  );

  // The first tap or key press unlocks audio for the page.
  useEffect(() => {
    if (!listening) return;
    const unlock = () => void audio()?.resume().catch(() => undefined);
    document.addEventListener("pointerdown", unlock, true);
    document.addEventListener("keydown", unlock, true);
    return () => {
      document.removeEventListener("pointerdown", unlock, true);
      document.removeEventListener("keydown", unlock, true);
    };
  }, [listening, audio]);

  // A new stream starts from its first payload; leaving sim mode, or turning
  // sound off, silences what is playing.
  useEffect(() => {
    last.current = null;
    if (!listening) stopAll();
  }, [listening, stopAll]);

  useEffect(
    () => () => {
      void context.current?.close().catch(() => undefined);
      context.current = null;
    },
    [],
  );

  // `listening` follows page visibility too: hiding the tab stops the sound
  // playing in it, as the stream closes.
  useEventSource(listening ? paths.simSoundsStream : null, {
    // A dropped stream reconnects by itself; its first payload after that
    // is a new starting point, so a long drop doesn't replay a burst.
    error: () => {
      last.current = null;
    },
    "sim-sounds": (event) => {
      const payload = readEventPayload(event);
      if (!isSimSounds(payload)) return;
      const next = newSimSounds(last.current, payload);
      last.current = next.last;
      next.actions.forEach((action: SimSoundAction) => {
        if (action.kind === "stop") stopAll();
        else void play(action.token).catch(() => undefined);
      });
    },
  });

  const setOn = useCallback(
    (next: boolean) => {
      setOnState(next);
      write(SIM_SOUND_KEY, next ? null : "off");
      // This click is a user gesture: unlock audio now.
      if (next) void audio()?.resume().catch(() => undefined);
    },
    [audio],
  );

  return { on, setOn };
}
