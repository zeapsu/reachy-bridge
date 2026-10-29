// Sim mode sounds (#219): the sim daemon has no speaker, so the browser plays what it would have.

/** One event from the controller's `sim-sounds` stream. */
export type SimSoundEvent =
  | { seq: number; kind: "play"; token: string; name: string }
  | { seq: number; kind: "stop" };

/** The stream's payload: the latest few events and the newest `seq`. */
export interface SimSounds {
  seq: number;
  events: SimSoundEvent[];
}

/** What to do, in order. */
export type SimSoundAction = { kind: "play"; token: string } | { kind: "stop" };

export const SIM_SOUND_KEY = "reachy.simSound";

/**
 * The events in `sounds` this page hasn't acted on yet. The first payload a
 * page sees only sets where it starts (`last === null`), so opening a tab
 * doesn't replay what played before. A `seq` that went back means a new sim
 * daemon: everything it reports is new.
 */
export function newSimSounds(last: number | null, sounds: SimSounds): { last: number; actions: SimSoundAction[] } {
  if (last === null) return { last: sounds.seq, actions: [] };
  const from = sounds.seq < last ? 0 : last;
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
  return { last: sounds.seq, actions };
}

/** Whether the stream payload has the shape the controller sends. */
export function isSimSounds(value: unknown): value is SimSounds {
  if (!value || typeof value !== "object") return false;
  const { seq, events } = value as { seq?: unknown; events?: unknown };
  return typeof seq === "number" && Array.isArray(events);
}
