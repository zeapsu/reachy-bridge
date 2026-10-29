import { describe, expect, test } from "bun:test";
import { isSimSounds, newSimSounds, type SimSounds } from "./simSounds.ts";

const play = (seq: number, token = `t${seq}`) => ({ seq, kind: "play" as const, token, name: `${token}.wav` });
const stop = (seq: number) => ({ seq, kind: "stop" as const });
const sounds = (seq: number, ...events: SimSounds["events"]): SimSounds => ({ boot: "a", seq, events });
const at = (seq: number, boot = "a") => ({ boot, seq });

describe("newSimSounds", () => {
  test("the first payload only sets where the page starts", () => {
    expect(newSimSounds(null, sounds(2, play(1), play(2)))).toEqual({ last: at(2), actions: [] });
  });

  test("plays only what is newer, in order", () => {
    expect(newSimSounds(at(1), sounds(3, play(3), play(1), play(2))).actions).toEqual([
      { kind: "play", token: "t2" },
      { kind: "play", token: "t3" },
    ]);
  });

  test("a stop cancels the sounds before it", () => {
    expect(newSimSounds(at(0), sounds(3, play(1), stop(2), play(3))).actions).toEqual([
      { kind: "stop" },
      { kind: "play", token: "t3" },
    ]);
  });

  test("a restarted sim daemon starts its count again, even past the old one", () => {
    expect(newSimSounds(at(1, "old"), sounds(1, play(1))).actions).toEqual([{ kind: "play", token: "t1" }]);
    expect(newSimSounds(at(40, "old"), sounds(41, play(41))).actions).toEqual([{ kind: "play", token: "t41" }]);
  });

  test("an unchanged payload does nothing", () => {
    expect(newSimSounds(at(2), sounds(2, play(1), play(2)))).toEqual({ last: at(2), actions: [] });
  });
});

test("isSimSounds checks the payload's shape", () => {
  expect(isSimSounds({ boot: "a", seq: 1, events: [] })).toBe(true);
  expect(isSimSounds({ seq: 1, events: [] })).toBe(false);
  expect(isSimSounds({ boot: "a", seq: "1", events: [] })).toBe(false);
  expect(isSimSounds(null)).toBe(false);
});
