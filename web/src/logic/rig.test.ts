import { describe, expect, test } from "bun:test";
import { Vector3 } from "three";
import fixture from "./rig.fixture.json";
import { partMatrices, RIG } from "./rig.ts";

// rig.fixture.json: a pose the sim daemon reached (its head_pose, head_joints,
// body_yaw and antennas), with where MuJoCo puts the head and the rod tips at
// that pose and at neutral. Regenerate it with tools/model/README.md.
const v = (a: number[]) => new Vector3(a[0], a[1], a[2]);
const MM = 0.001;

describe("3D rig", () => {
  test("neutral leaves every part where it was baked", () => {
    const parts = partMatrices({
      head_joints: [0, ...RIG.horns.map((h) => h.neutral)],
      antennas_position: [RIG.antennas.right.neutral, RIG.antennas.left.neutral],
    });
    for (const [name, m] of Object.entries(parts)) {
      const p = new Vector3(0.01, -0.02, 0.15);
      expect(p.clone().applyMatrix4(m).distanceTo(p), name).toBeLessThan(1e-9);
    }
  });

  test("matches MuJoCo at a real daemon pose", () => {
    const parts = partMatrices(fixture.state);
    const head = v(fixture.neutral.head).applyMatrix4(parts.head);
    expect(head.distanceTo(v(fixture.posed.head))).toBeLessThan(0.5 * MM);
    fixture.neutral.rods.forEach((start, i) => {
      const moved = v(start).applyMatrix4(parts[`rod${i + 1}`]);
      expect(moved.distanceTo(v(fixture.posed.rods[i])), `rod${i + 1}`).toBeLessThan(0.5 * MM);
    });
  });

  test("each rod's far end meets the head", () => {
    const parts = partMatrices(fixture.state);
    RIG.rods.forEach((rod, i) => {
      const end = v(rod.end).applyMatrix4(parts[`rod${i + 1}`]);
      const onHead = v(rod.end).applyMatrix4(parts.head);
      // Rods are rigid in the model, so the horn angle closes the gap; allow 1 mm.
      expect(end.distanceTo(onHead), `rod${i + 1}`).toBeLessThan(1 * MM);
    });
  });
});
