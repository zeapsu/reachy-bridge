// Sim mode sounds (#219): the sim daemon has no speaker, so the browser plays what it would have.

/** One event from the controller's `sim-sounds` stream. */
export type SimSoundEvent =
  | { seq: number; kind: "play"; token: string; name: string }
  | { seq: number; kind: "stop" };

/** The stream's payload: the latest few events and the newest `seq`; `boot` changes with each sim daemon. */
export interface SimSounds {
  boot: string;
  seq: number;
  events: SimSoundEvent[];
}

/** Where a page is in the sound log: which daemon, and the newest event it acted on. */
export interface SimSoundCursor {
  boot: string;
  seq: number;
}

/** What to do, in order. */
export type SimSoundAction = { kind: "play"; token: string } | { kind: "stop" };

export const SIM_SOUND_KEY = "reachy.simSound";

/**
 * The events in `sounds` this page hasn't acted on yet. The first payload a
 * page sees only sets where it starts (`last === null`), so opening a tab
 * doesn't replay what played before. A new `boot` means a new sim daemon,
 * whose count started over: everything it reports is new.
 */
export function newSimSounds(
  last: SimSoundCursor | null,
  sounds: SimSounds,
): { last: SimSoundCursor; actions: SimSoundAction[] } {
  const next = { boot: sounds.boot, seq: sounds.seq };
  if (last === null) return { last: next, actions: [] };
  const from = sounds.boot !== last.boot ? 0 : last.seq;
  const actions: SimSoundAction[] = [];
  for (const event of [...sounds.events].sort((a, b) => a.seq - b.seq)) {
    if (event.seq <= from) continue;
    if (event.kind === "stop") {
      // A stop cancels what came before it, played or still queued.
      actions.length = 0;
      actions.push({ kind: "stop" });
    } else {
      actions.push({ kind: "play", token: event.token });
    }
  }
  return { last: next, actions };
}

/** Whether the stream payload has the shape the controller sends. */
export function isSimSounds(value: unknown): value is SimSounds {
  if (!value || typeof value !== "object") return false;
  const { boot, seq, events } = value as { boot?: unknown; seq?: unknown; events?: unknown };
  return typeof boot === "string" && typeof seq === "number" && Array.isArray(events);
}
