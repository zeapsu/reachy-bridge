# 3D model

The dashboard's 3D view (`web/src/components/robotView.ts`) draws Reachy Mini
from two committed files:

- `web/src/assets/reachy_mini.glb`: one mesh per moving part (`base`, `body`,
  `horn1`..`horn6`, `rod1`..`rod6`, `head`, `antenna_right`, `antenna_left`),
  baked in world coordinates at the neutral pose, plus `head_glass`, the
  camera eyes' lenses, which the view draws as glass. It is meshopt
  compressed (EXT_meshopt_compression); `GLTFLoader` decodes it with three's
  `MeshoptDecoder`.
- `web/src/assets/reachy_mini.rig.json`: what the browser needs to move them:
  the head pivot, the horn and antenna hinges, and the rod ends.

`web/src/logic/rig.ts` turns the daemon's state (`head_pose`, `head_joints`,
`body_yaw`, `antennas_position`) into one matrix per part, and
`rig.test.ts` checks it against MuJoCo at a real daemon pose.

The meshes come from the `reachy-mini` package (Apache-2.0, Pollen Robotics
and Hugging Face), from the MuJoCo model at
`reachy_mini/descriptions/reachy_mini/mjcf/scene.xml`, at the version
`uv.lock` pins.

## Regenerating

Rebuild the assets when the locked `reachy-mini` changes its model. From the
repository root, after `uv sync`:

```bash
uv run --with trimesh --with scipy python tools/model/export.py /tmp/reachy-model
```

```bash
/Applications/Blender.app/Contents/MacOS/Blender -b --python tools/model/decimate.py -- /tmp/reachy-model/reachy_mini_full.glb /tmp/reachy-model/reachy_mini.glb
```

```bash
bunx @gltf-transform/cli@4.2.1 meshopt /tmp/reachy-model/reachy_mini.glb web/src/assets/reachy_mini.glb
```

```bash
cp /tmp/reachy-model/reachy_mini.rig.json web/src/assets/
```

`decimate.py` needs Blender (tested with 5.1); it cuts the model from about
1M faces to about 260k (12 MB), and meshopt compression brings that to 1.8 MB.
It draws at a steady 60 fps on an M4 Mac at desktop and phone sizes.

Then refresh the test fixture: start a sim daemon on port 8200
(`tools/sim/README.md`), move it to an off-centre pose with a `goto`, and run

```bash
uv run python tools/model/fixture.py http://127.0.0.1:8200 web/src/assets/reachy_mini.rig.json web/src/logic/rig.fixture.json
```

and `bun test src/logic/rig.test.ts` in `web/`.
