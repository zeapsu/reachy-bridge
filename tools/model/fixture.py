"""Write web/src/logic/rig.fixture.json, the pose rig.test.ts checks against.

Reads the pose a (sim) daemon is holding, then settles the MuJoCo model at
neutral and at that pose and records where the head, the six rods and a point on each antenna end up.
rig.test.ts poses the 3D model with the browser's formulas and expects the
same positions. Move the sim to an off-centre pose first (head tilted and
turned, body yaw and antennas away from zero) so the check means something.

    uv run python tools/model/fixture.py http://127.0.0.1:8200 RIG_JSON OUT_JSON
"""

import json
import os
import sys
import urllib.request

import mujoco
import numpy as np
import reachy_mini

SETTLE_STEPS = 4000
NEUTRAL_ANTENNAS = [-0.1746, 0.1746]
# A point on each antenna, off its hinge axis (local z), in the antenna's frame.
ANTENNA_POINT = [0.0, -0.06, 0.0]


def main(daemon: str, rig_path: str, out: str) -> None:
    with open(rig_path) as f:
        rig = json.load(f)
    url = f"{daemon}/api/state/full?with_head_joints=true&with_body_yaw=true&with_antenna_positions=true&use_pose_matrix=true"
    with urllib.request.urlopen(url) as r:
        state = json.load(r)

    scene = os.path.join(os.path.dirname(reachy_mini.__file__), "descriptions/reachy_mini/mjcf/scene.xml")
    model = mujoco.MjModel.from_xml_path(scene)
    data = mujoco.MjData(model)

    def joint(name: str) -> int:
        return model.jnt_qposadr[mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, name)]

    def body(name: str) -> int:
        return mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_BODY, name)

    def settle(joints: list[float], antennas: list[float]) -> dict:
        mujoco.mj_resetData(model, data)
        for _ in range(SETTLE_STEPS):
            data.ctrl[:7] = joints
            mujoco.mj_step(model, data)
            # Pin the antennas after each step, or their actuators pull them
            # off. The daemon reports them as minus their qpos (see export.py).
            data.qpos[joint("right_antenna")] = -antennas[0]
            data.qpos[joint("left_antenna")] = -antennas[1]
        mujoco.mj_forward(model, data)
        rods = [body("stewart_link_rod" + ("" if i == 1 else f"_{i}")) for i in range(1, 7)]
        tips = [
            (data.xpos[b] + data.xmat[b].reshape(3, 3) @ ANTENNA_POINT).tolist()
            for b in (model.jnt_bodyid[mujoco.mj_name2id(model, mujoco.mjtObj.mjOBJ_JOINT, f"{side}_antenna")] for side in ("right", "left"))
        ]
        return {
            "head": data.xpos[body("xl_330")].tolist(),
            "rods": [data.xpos[r].tolist() for r in rods],
            "antennas": tips,
        }

    neutral = settle([0, *[h["neutral"] for h in rig["horns"]]], NEUTRAL_ANTENNAS)
    posed = settle(state["head_joints"], state["antennas_position"])

    # The daemon's 4x4 head pose as x/y/z and roll/pitch/yaw (R = Rz·Ry·Rx, as the SDK builds it).
    m = np.array(state["head_pose"]["m"]).reshape(4, 4)
    r = m[:3, :3]
    head_pose = {
        "x": float(m[0, 3]),
        "y": float(m[1, 3]),
        "z": float(m[2, 3]),
        "roll": float(np.arctan2(r[2, 1], r[2, 2])),
        "pitch": float(np.arcsin(-r[2, 0])),
        "yaw": float(np.arctan2(r[1, 0], r[0, 0])),
    }
    fixture = {
        "state": {
            "head_pose": head_pose,
            "head_joints": state["head_joints"],
            "body_yaw": state["body_yaw"],
            "antennas_position": state["antennas_position"],
        },
        "neutral": neutral,
        "posed": posed,
    }
    with open(out, "w") as f:
        json.dump(fixture, f, indent=1)
        f.write("\n")


if __name__ == "__main__":
    main(*sys.argv[1:4])
