"""Shrink reachy_mini_full.glb for the browser (run inside Blender).

    /Applications/Blender.app/Contents/MacOS/Blender -b --python tools/model/decimate.py -- IN.glb OUT.glb

Welds each part's vertices, collapses it to a fraction of its faces and
exports smooth-shaded GLB with the vertex colours. Blender's collapse keeps the
shells' shape where fast-simplification crumpled them.
"""

import sys

import bpy

src, dst = sys.argv[sys.argv.index("--") + 1 :]
RATIOS = {"body": 0.08, "head": 0.11, "base": 0.15, "antenna_right": 0.12, "antenna_left": 0.12}
DEFAULT_RATIO = 0.08  # horns and rods

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=src)
for o in [o for o in bpy.context.scene.objects if o.type == "MESH"]:
    bpy.context.view_layer.objects.active = o
    bpy.ops.object.select_all(action="DESELECT")
    o.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.mesh.remove_doubles(threshold=0.00002)
    bpy.ops.object.mode_set(mode="OBJECT")
    before = len(o.data.polygons)
    mod = o.modifiers.new("decimate", "DECIMATE")
    mod.decimate_type = "COLLAPSE"
    mod.ratio = RATIOS.get(o.name, DEFAULT_RATIO)
    mod.use_collapse_triangulate = True
    bpy.ops.object.modifier_apply(modifier="decimate")
    for poly in o.data.polygons:
        poly.use_smooth = True
    print(f"{o.name}: {before} -> {len(o.data.polygons)} faces")
bpy.ops.export_scene.gltf(filepath=dst, export_format="GLB", export_apply=True, export_normals=True, export_yup=True)
