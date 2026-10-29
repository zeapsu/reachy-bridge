import { describe, expect, test } from "bun:test";
import { isSimSounds, newSimSounds, type SimSounds } from "./simSounds.ts";

const play = (seq: number, token = `t${seq}`) => ({ seq, kind: "play" as const, token, name: `${token}.wav` });
const stop = (seq: number) => ({ seq, kind: "stop" as const });
const sounds = (seq: number, ...events: SimSounds["events"]): SimSounds => ({ seq, events });

describe("newSimSounds", () => {
  test("the first payload only sets where the page starts", () => {
    expect(newSimSounds(null, sounds(2, play(1), play(2)))).toEqual({ last: 2, actions: [] });
  });

  test("plays only what is newer, in order", () => {
    expect(newSimSounds(1, sounds(3, play(3), play(1), play(2))).actions).toEqual([
      { kind: "play", token: "t2" },
      { kind: "play", token: "t3" },
    ]);
  });

  test("a stop cancels the sounds before it", () => {
    expect(newSimSounds(0, sounds(3, play(1), stop(2), play(3))).actions).toEqual([
      { kind: "stop" },
      { kind: "play", token: "t3" },
    ]);
  });

  test("a restarted sim daemon starts its count again", () => {
    expect(newSimSounds(40, sounds(1, play(1))).actions).toEqual([{ kind: "play", token: "t1" }]);
  });

  test("an unchanged payload does nothing", () => {
    expect(newSimSounds(2, sounds(2, play(1), play(2)))).toEqual({ last: 2, actions: [] });
  });
});

test("isSimSounds checks the payload's shape", () => {
  expect(isSimSounds({ seq: 1, events: [] })).toBe(true);
  expect(isSimSounds({ seq: "1", events: [] })).toBe(false);
  expect(isSimSounds(null)).toBe(false);
});
