"""
EduGrid gallery scene: renders 8 frames of the EduGrid grid mark with Blender Cycles.
Run from the repo folder:   blender -b --python edugrid_render_scene.py
Frames are written to static/outputs/frame_0001.png ... frame_0008.png.
Rendered for the demo with Blender 4.0, Cycles CPU, 128 samples, 960x540.
"""
import os
OUT_DIR = os.path.join(os.getcwd(), "static", "outputs")
os.makedirs(OUT_DIR, exist_ok=True)
import bpy, math
scn = bpy.context.scene
bpy.ops.wm.read_factory_settings(use_empty=True)
scn = bpy.context.scene

def mat(name, color, metal=0.0, rough=0.4, emit=None, strength=0.0):
    m = bpy.data.materials.new(name); m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*color, 1)
    b.inputs["Metallic"].default_value = metal
    b.inputs["Roughness"].default_value = rough
    if emit:
        b.inputs["Emission Color"].default_value = (*emit, 1)
        b.inputs["Emission Strength"].default_value = strength
    return m

floor_m = mat("floor", (0.015, 0.02, 0.03), 0.0, 0.25)
cube_m  = mat("cube",  (0.05, 0.12, 0.35), 0.6, 0.3)
dim_m   = mat("dim",   (0.03, 0.06, 0.15), 0.6, 0.35)
core_m  = mat("core",  (0.1, 0.8, 1.0), 0.0, 0.2, emit=(0.13, 0.83, 0.93), strength=6.0)

bpy.ops.mesh.primitive_plane_add(size=40); bpy.context.object.data.materials.append(floor_m)

for i in range(3):
    for j in range(3):
        x, y = (i - 1) * 1.35, (j - 1) * 1.35
        center = (i == 1 and j == 1)
        h = 1.4 if center else (1.0 if (i + j) % 2 else 0.7)
        bpy.ops.mesh.primitive_cube_add(size=1, location=(x, y, h / 2))
        o = bpy.context.object; o.scale = (1.0, 1.0, h)
        bpy.ops.object.transform_apply(scale=True)
        bev = o.modifiers.new("bevel", "BEVEL"); bev.width = 0.08; bev.segments = 4
        bpy.ops.object.shade_smooth()
        o.data.materials.append(core_m if center else (cube_m if (i + j) % 2 else dim_m))

bpy.ops.object.light_add(type='AREA', location=(4, -4, 6)); L = bpy.context.object
L.data.energy = 900; L.data.size = 4; L.data.color = (0.75, 0.85, 1.0)
L.rotation_euler = (math.radians(40), 0, math.radians(45))
bpy.ops.object.light_add(type='AREA', location=(-5, 3, 3)); L2 = bpy.context.object
L2.data.energy = 300; L2.data.size = 3; L2.data.color = (0.4, 0.55, 1.0)
L2.rotation_euler = (math.radians(70), 0, math.radians(-120))

w = bpy.data.worlds.new("w"); scn.world = w; w.use_nodes = True
w.node_tree.nodes["Background"].inputs[0].default_value = (0.004, 0.007, 0.015, 1)

bpy.ops.object.empty_add(location=(0, 0, 0.5)); target = bpy.context.object
bpy.ops.object.camera_add(); cam = bpy.context.object; scn.camera = cam
cam.data.lens = 40
t = cam.constraints.new('TRACK_TO'); t.target = target; t.track_axis = 'TRACK_NEGATIVE_Z'; t.up_axis = 'UP_Y'

scn.render.engine = 'CYCLES'
scn.cycles.device = 'CPU'
scn.cycles.samples = 128
scn.cycles.use_denoising = False   # this Blender build has no OpenImageDenoise
scn.cycles.max_bounces = 6
scn.render.resolution_x, scn.render.resolution_y = 960, 540
scn.render.image_settings.file_format = 'PNG'
scn.view_settings.view_transform = 'AgX' if 'AgX' in [v.identifier for v in scn.view_settings.bl_rna.properties['view_transform'].enum_items] else 'Filmic'

N = 8
for k in range(N):
    a = math.radians(-60 + k * (120 / (N - 1)))
    r = 7.5
    cam.location = (r * math.cos(a), r * math.sin(a), 4.2)
    path = os.path.join(OUT_DIR, f"frame_{k+1:04d}.png")
    if os.path.exists(path):
        continue
    scn.render.filepath = path
    bpy.ops.render.render(write_still=True)
    print("RENDERED", k + 1)
