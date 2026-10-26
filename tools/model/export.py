"""Bake Reachy Mini's MuJoCo model into the dashboard's 3D view assets.

Settles the model at the neutral pose (the joint angles the daemon reports with
the head at home), then writes one mesh per moving part in world coordinates
and a rig file with what the browser needs to move them: the horn hinges, the
rod ends, the head pivot and the antenna hinges. The meshes come from the
reachy-mini package (Apache-2.0), at the version uv.lock pins.

    uv run --with trimesh --with scipy python tools/model/export.py OUT_DIR

then decimate.py (Blender) shrinks OUT_DIR/reachy_mini_full.glb for the web.
"""

import json
import os
import sys

import mujoco
import numpy as np
import reachy_mini
import trimesh

# Joint angles the daemon reports at the neutral head pose (head_joints).
NEUTRAL_STEWART = [0.6244, -0.6244, 0.6243, -0.6244, 0.6242, -0.6242]
NEUTRAL_ANTENNAS = {"right_antenna": -0.1746, "left_antenna": 0.1746}
SETTLE_STEPS = 3000


def main(out: str) -> None:
    scene = os.path.join(os.path.dirname(reachy_mini.__file__), "descriptions/reachy_mini/mjcf/scene.xml")
    m = mujoco.MjModel.from_xml_path(scene)
    d = mujoco.MjData(m)

    def body(name: str) -> int:
        return mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_BODY, name)

    def joint(name: str) -> int:
        return mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_JOINT, name)

    for name, angle in NEUTRAL_ANTENNAS.items():
        d.qpos[m.jnt_qposadr[joint(name)]] = angle
    d.ctrl[: 1 + len(NEUTRAL_STEWART)] = [0.0, *NEUTRAL_STEWART]
    # The closed Stewart chains are equality constraints: let the solver close them.
    for _ in range(SETTLE_STEPS):
        mujoco.mj_step(m, d)
        for name, angle in NEUTRAL_ANTENNAS.items():
            d.qpos[m.jnt_qposadr[joint(name)]] = angle
    mujoco.mj_forward(m, d)

    horns = [body("dc15_a01_horn_dummy" + ("" if i == 1 else f"_{i}")) for i in range(1, 7)]
    rods = [body("stewart_link_rod" + ("" if i == 1 else f"_{i}")) for i in range(1, 7)]
    head = body("xl_330")
    part = {body("body_foot_3dprint"): "base", body("body_down_3dprint"): "body", head: "head"}
    part.update({b: f"horn{i}" for i, b in enumerate(horns, 1)})
    part.update({b: f"rod{i}" for i, b in enumerate(rods, 1)})
    part[m.jnt_bodyid[joint("right_antenna")]] = "antenna_right"
    part[m.jnt_bodyid[joint("left_antenna")]] = "antenna_left"
    for b in range(m.nbody):  # unnamed helper bodies under the head
        if b not in part and m.body_parentid[b] == head:
            part[b] = "head"

    meshes: dict[str, list[trimesh.Trimesh]] = {}
    for g in range(m.ngeom):
        if m.geom_type[g] != mujoco.mjtGeom.mjGEOM_MESH or m.geom_group[g] >= 3:
            continue  # group 3 holds collision shapes
        name = part.get(int(m.geom_bodyid[g]))
        if name is None:
            continue
        mid = m.geom_dataid[g]
        v0, nv = m.mesh_vertadr[mid], m.mesh_vertnum[mid]
        f0, nf = m.mesh_faceadr[mid], m.mesh_facenum[mid]
        verts = m.mesh_vert[v0 : v0 + nv] @ d.geom_xmat[g].reshape(3, 3).T + d.geom_xpos[g]
        mesh = trimesh.Trimesh(verts, m.mesh_face[f0 : f0 + nf], process=True)
        rgba = m.mat_rgba[m.geom_matid[g]] if m.geom_matid[g] >= 0 else m.geom_rgba[g]
        colors = np.tile((np.asarray(rgba) * 255).astype(np.uint8), (len(mesh.faces), 1))
        mesh.visual = trimesh.visual.ColorVisuals(mesh, face_colors=colors)
        meshes.setdefault(name, []).append(mesh)

    scene_out = trimesh.Scene()
    for name, parts in meshes.items():
        scene_out.add_geometry(trimesh.util.concatenate(parts), node_name=name, geom_name=name)
    os.makedirs(out, exist_ok=True)
    scene_out.export(os.path.join(out, "reachy_mini_full.glb"))

    def site(name: str) -> list[float]:
        return d.site_xpos[mujoco.mj_name2id(m, mujoco.mjtObj.mjOBJ_SITE, name)].round(6).tolist()

    def hinge(name: str) -> dict:
        j = joint(name)
        return {"anchor": d.xanchor[j].round(6).tolist(), "axis": d.xaxis[j].round(6).tolist(),
                "neutral": round(float(d.qpos[m.jnt_qposadr[j]]), 6)}

    rig = {
        "units": "metres, z up, robot faces +x",
        "head_pivot": site("head"),
        "horns": [hinge(f"stewart_{i}") for i in range(1, 7)],
        # A rod starts at its horn's tip and ends where it meets the head.
        "rods": [{"start": d.xpos[r].round(6).tolist(),
                  "end": site(f"closing_{i}_1") if i < 6 else d.xanchor[joint("passive_7")].round(6).tolist()}
                 for i, r in enumerate(rods, 1)],
        "antennas": {"right": hinge("right_antenna"), "left": hinge("left_antenna")},
    }
    with open(os.path.join(out, "reachy_mini.rig.json"), "w") as f:
        json.dump(rig, f, indent=1)
    print(json.dumps(rig)[:600])
    print({k: sum(len(p.faces) for p in v) for k, v in meshes.items()})


if __name__ == "__main__":
    main(sys.argv[1])
