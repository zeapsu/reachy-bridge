import { describe, expect, test } from "bun:test";
import { carouselMoves, moveLabel } from "./moves.ts";

describe("moves", () => {
  test("labels read like words, keeping variant numbers past the first", () => {
    expect(moveLabel("loving1")).toBe("Loving");
    expect(moveLabel("proud2")).toBe("Proud 2");
    expect(moveLabel("yes_sad1")).toBe("Yes sad");
    expect(moveLabel("mini-deep-sleep")).toBe("Mini deep sleep");
    expect(moveLabel("waiting")).toBe("Waiting");
  });
  test("featured first, then the map's order, unknown moves last with a fallback", () => {
    const moves = carouselMoves(["waiting", "scared1", "sad1", "loving1"], "emotions");
    expect(moves.map((m) => m.name)).toEqual(["loving1", "sad1", "scared1", "waiting"]);
    expect(moves[0].emoji).toBe("🥰");
    expect(moves[3].emoji).toBe("🙂");
        expect(carouselMoves(["new_dance"], "dances")[0].emoji).toBe("🎵");
  });
});
