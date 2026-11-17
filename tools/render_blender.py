"""Photoreal renders of the Honey Badger with Blender + Cycles.

1. Export one STL per material from the case model:
     for g in body lid amber lettering metal gold black blue glass screen led paper bumper; do
       openscad --backend=manifold -D 'part="showcase"' -D "group=\"$g\"" -o stl/$g.stl case/honey_badger_case.scad
     done
2. Render:
     blender -b -P tools/render_blender.py -- --stl stl --out case/renders/photo --scene front|rear|top|camp|all
        [--samples 192] [--res 1800x1200]

Everything here is procedural (no downloaded textures or HDRIs), so it runs offline.
"""
import argparse
import math
import os
import sys

import bpy
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
ap = argparse.ArgumentParser()
ap.add_argument("--stl", required=True)
ap.add_argument("--out", required=True)
ap.add_argument("--scene", default="all")
ap.add_argument("--samples", type=int, default=192)
ap.add_argument("--res", default="1800x1200")
ap.add_argument("--phone", default="", help="PNG for the phone screen in the camp scene (a launcher screenshot)")
args = ap.parse_args(argv)
RES = tuple(int(v) for v in args.res.split("x"))
MM = 0.001
CENTER = Vector((102 * MM, 84 * MM, 42 * MM))   # middle of the case


# ───────────────────────── helpers ─────────────────────────
def lin(hexcode):
    h = hexcode.lstrip("#")
    c = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    return tuple(((v + 0.055) / 1.055) ** 2.4 if v > 0.04045 else v / 12.92 for v in c) + (1.0,)


def reset():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    sc = bpy.context.scene
    sc.render.engine = "CYCLES"
    prefs = bpy.context.preferences.addons["cycles"].preferences
    for dev in ("METAL", "OPTIX", "CUDA", "HIP", "ONEAPI"):
        try:
            prefs.compute_device_type = dev
            prefs.get_devices()
            if any(d.type == dev for d in prefs.devices):
                for d in prefs.devices:
                    d.use = True
                sc.cycles.device = "GPU"
                print("render device:", dev)
                break
        except Exception:
            continue
    sc.cycles.samples = args.samples
    sc.cycles.use_denoising = True
    sc.render.resolution_x, sc.render.resolution_y = RES
    sc.render.film_transparent = False
    for vt, look in (("Khronos PBR Neutral", "None"), ("AgX", "AgX - Punchy")):    # keep the brand yellow true
        try:
            sc.view_settings.view_transform = vt
            sc.view_settings.look = look
            break
        except Exception:
            continue
    sc.unit_settings.system = "METRIC"
    return sc


def material(name, color=None, rough=0.4, metal=0.0, emit=None, emit_strength=0.0, coat=0.0, layer_lines=False, sheen=0.0):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    p = nt.nodes["Principled BSDF"]
    if color:
        p.inputs["Base Color"].default_value = lin(color)
    p.inputs["Roughness"].default_value = rough
    p.inputs["Metallic"].default_value = metal
    for key in ("Coat Weight", "Clearcoat"):
        if key in p.inputs:
            p.inputs[key].default_value = coat
            break
    if sheen and "Sheen Weight" in p.inputs:
        p.inputs["Sheen Weight"].default_value = sheen
    if emit:
        key = "Emission Color" if "Emission Color" in p.inputs else "Emission"
        p.inputs[key].default_value = lin(emit)
        p.inputs["Emission Strength"].default_value = emit_strength
    if layer_lines:
        # 0.2 mm FDM layer lines along Z, plus a little noise so it doesn't look machined
        tc = nt.nodes.new("ShaderNodeTexCoord")
        wave = nt.nodes.new("ShaderNodeTexWave")
        wave.bands_direction = "Z"
        wave.inputs["Scale"].default_value = 2 * math.pi / (20 * 0.0002)
        wave.inputs["Distortion"].default_value = 0.4
        noise = nt.nodes.new("ShaderNodeTexNoise")
        noise.inputs["Scale"].default_value = 900
        mix = nt.nodes.new("ShaderNodeMath")
        mix.operation = "MULTIPLY_ADD"
        mix.inputs[2].default_value = 0.0
        bump = nt.nodes.new("ShaderNodeBump")
        bump.inputs["Strength"].default_value = 0.18
        bump.inputs["Distance"].default_value = 0.00004
        nt.links.new(tc.outputs["Object"], wave.inputs["Vector"])
        nt.links.new(tc.outputs["Object"], noise.inputs["Vector"])
        nt.links.new(wave.outputs["Fac"], mix.inputs[0])
        nt.links.new(noise.outputs["Fac"], mix.inputs[1])
        nt.links.new(mix.outputs[0], bump.inputs["Height"])
        nt.links.new(bump.outputs["Normal"], p.inputs["Normal"])
    return m


def badger_materials():
    return {
        "body": material("PETG yellow", "#F2B705", rough=0.32, coat=0.15, layer_lines=True),
        "lid": material("PETG black", "#161616", rough=0.55, coat=0.0),
        "bumper": material("TPU black", "#121212", rough=0.72, sheen=0.3),
        "amber": material("Label amber", "#E8A317", rough=0.35, coat=0.4),
        "lettering": material("Label cream", "#ECE6D6", rough=0.35, coat=0.4),
        "metal": material("Steel", "#C4C7CB", rough=0.25, metal=1.0),
        "gold": material("Gold plating", "#D9B24C", rough=0.22, metal=1.0),
        "black": material("Black plastic", "#141414", rough=0.45),
        "blue": material("USB blue", "#1F5FD1", rough=0.4),
        "glass": material("OLED glass", "#050505", rough=0.04, coat=1.0),
        "screen": material("OLED pixels", "#000000", emit="#CFE3FF", emit_strength=6.0),
        "led": material("Status LED", "#FFB347", rough=0.1, emit="#FF9A2E", emit_strength=12.0),
        "paper": material("Sticker", "#F4F4F0", rough=0.55),
    }


def import_badger(at=None, rot_z=0.0):
    mats = badger_materials()
    parent = bpy.data.objects.new("HoneyBadger", None)
    bpy.context.collection.objects.link(parent)
    for g, m in mats.items():
        path = os.path.join(args.stl, g + ".stl")
        if not os.path.exists(path):
            continue
        bpy.ops.object.select_all(action="DESELECT")
        bpy.ops.wm.stl_import(filepath=path, global_scale=MM, forward_axis="Y", up_axis="Z")
        ob = bpy.context.selected_objects[0]
        ob.name = "badger_" + g
        ob.data.materials.clear()
        ob.data.materials.append(m)
        bpy.context.view_layer.objects.active = ob
        try:
            bpy.ops.object.shade_auto_smooth(angle=math.radians(35))
        except Exception:
            bpy.ops.object.shade_smooth()
        ob.parent = parent
    parent.location = at if at is not None else Vector((0, 0, 0))
    parent.rotation_euler[2] = rot_z
    return parent


def box(name, size, loc, mat, bevel=0.0):
    bpy.ops.mesh.primitive_cube_add(size=1, location=loc)
    ob = bpy.context.active_object
    ob.name = name
    ob.scale = size
    bpy.ops.object.transform_apply(scale=True)
    if bevel:
        mod = ob.modifiers.new("bevel", "BEVEL")
        mod.width = bevel
        mod.segments = 3
    ob.data.materials.append(mat)
    return ob


def area_light(name, loc, target, power, size, color=(1, 1, 1)):
    bpy.ops.object.light_add(type="AREA", location=loc)
    lt = bpy.context.active_object
    lt.name = name
    lt.data.energy = power
    lt.data.size = size
    lt.data.color = color
    look_at(lt, target)
    return lt


def look_at(ob, target):
    d = Vector(target) - ob.location
    ob.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()


def camera(loc, target, lens=70, fstop=None, focus=None):
    bpy.ops.object.camera_add(location=loc)
    cam = bpy.context.active_object
    cam.data.lens = lens
    cam.data.clip_start = 0.01
    look_at(cam, target)
    if fstop:
        cam.data.dof.use_dof = True
        cam.data.dof.aperture_fstop = fstop
        cam.data.dof.focus_distance = focus or (Vector(target) - Vector(loc)).length
    bpy.context.scene.camera = cam
    return cam


def world_color(rgb, strength):
    w = bpy.data.worlds.new("World")
    bpy.context.scene.world = w
    w.use_nodes = True
    bg = w.node_tree.nodes["Background"]
    bg.inputs["Color"].default_value = rgb + (1,)
    bg.inputs["Strength"].default_value = strength


def render(name):
    os.makedirs(args.out, exist_ok=True)
    sc = bpy.context.scene
    sc.render.filepath = os.path.join(args.out, name + ".png")
    sc.render.image_settings.file_format = "PNG"
    bpy.ops.render.render(write_still=True)
    print("wrote", sc.render.filepath)


# ───────────────────────── studio ─────────────────────────
def studio(view):
    reset()
    import_badger()
    # seamless sweep: floor curving up into a back wall
    bpy.ops.mesh.primitive_plane_add(size=1)
    sweep = bpy.context.active_object
    sweep.name = "sweep"
    me = sweep.data
    import bmesh
    bm = bmesh.new()
    pts = []
    for i in range(41):                                    # profile in y–z: flat floor then a 0.35 m radius cove
        t = i / 40
        if t < 0.5:
            pts.append((-2.0 + t * 2 * 2.0, 0.0))
        else:
            a = (t - 0.5) * 2 * (math.pi / 2)
            pts.append((0.0 + 0.35 * math.sin(a), 0.35 - 0.35 * math.cos(a)))
    pts.append((0.35, 2.0))
    verts = []
    for x in (-2.0, 2.0):
        verts.append([bm.verts.new((x, 0.6 + y, z - 0.004)) for y, z in pts])
    for i in range(len(pts) - 1):
        bm.faces.new((verts[0][i], verts[1][i], verts[1][i + 1], verts[0][i + 1]))
    bm.to_mesh(me)
    bm.free()
    bpy.ops.object.shade_smooth()
    sweep.data.materials.append(material("Backdrop", "#E9E7E3", rough=0.7))
    world_color((0.9, 0.9, 0.92), 0.03)
    c = CENTER
    if view == "front":
        camera(c + Vector((0.30, -0.60, 0.31)), c + Vector((0.006, 0, -0.012)), lens=70)
        area_light("key", c + Vector((-0.45, -0.35, 0.55)), c, 13, 0.7)
        area_light("fill", c + Vector((0.55, -0.25, 0.2)), c, 4, 0.9)
        area_light("rim", c + Vector((0.2, 0.55, 0.45)), c, 9, 0.5)
    elif view == "rear":
        camera(c + Vector((-0.32, 0.60, 0.30)), c + Vector((0.0, 0, -0.012)), lens=70)
        area_light("key", c + Vector((0.45, 0.4, 0.55)), c, 13, 0.7)
        area_light("fill", c + Vector((-0.55, 0.2, 0.2)), c, 4, 0.9)
        area_light("rim", c + Vector((-0.2, -0.55, 0.45)), c, 9, 0.5)
    elif view == "top":
        camera(c + Vector((0.07, -0.30, 0.70)), c + Vector((0, 0.008, 0)), lens=60)
        area_light("key", c + Vector((-0.4, -0.3, 0.7)), c, 15, 0.8)
        area_light("fill", c + Vector((0.5, 0.1, 0.4)), c, 4, 1.0)
    render("photo_" + view)


# ───────────────────────── camping table ─────────────────────────
def solar_panel(origin, yaw, tilt):
    """A generic folding 40 W panel: two cell boards in a fabric surround, propped on a kickstand."""
    fabric = material("Panel fabric", "#2B2D2F", rough=0.92, sheen=0.5)
    cells = bpy.data.materials.new("Solar cells")
    cells.use_nodes = True
    nt = cells.node_tree
    p = nt.nodes["Principled BSDF"]
    p.inputs["Roughness"].default_value = 0.12
    for key in ("Coat Weight", "Clearcoat"):
        if key in p.inputs:
            p.inputs[key].default_value = 1.0
            break
    brick = nt.nodes.new("ShaderNodeTexBrick")
    brick.offset = 0.0
    brick.inputs["Color1"].default_value = lin("#0E1A3A")
    brick.inputs["Color2"].default_value = lin("#13244D")
    brick.inputs["Mortar"].default_value = lin("#9AA3AE")
    brick.inputs["Scale"].default_value = 1.0
    brick.inputs["Mortar Size"].default_value = 0.0012
    brick.inputs["Brick Width"].default_value = 0.052
    brick.inputs["Row Height"].default_value = 0.052
    tc = nt.nodes.new("ShaderNodeTexCoord")
    cmap = nt.nodes.new("ShaderNodeMapping")                    # the leaves stand in x–z: turn the brick grid to face front
    cmap.inputs["Rotation"].default_value = (math.radians(90), 0, 0)
    nt.links.new(tc.outputs["Object"], cmap.inputs["Vector"])
    nt.links.new(cmap.outputs["Vector"], brick.inputs["Vector"])
    nt.links.new(brick.outputs["Color"], p.inputs["Base Color"])
    finger = nt.nodes.new("ShaderNodeTexWave")                 # thin silver fingers across each cell
    finger.bands_direction = "X"
    finger.inputs["Scale"].default_value = 2 * math.pi / (20 * 0.0025)
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].position = 0.92
    nt.links.new(cmap.outputs["Vector"], finger.inputs["Vector"])
    nt.links.new(finger.outputs["Fac"], ramp.inputs["Fac"])
    mixc = nt.nodes.new("ShaderNodeMixRGB")
    mixc.inputs["Color2"].default_value = lin("#6E7782")
    nt.links.new(ramp.outputs["Color"], mixc.inputs["Fac"])
    nt.links.new(brick.outputs["Color"], mixc.inputs["Color1"])
    nt.links.new(mixc.outputs["Color"], p.inputs["Base Color"])

    root = bpy.data.objects.new("SolarPanel", None)
    bpy.context.collection.objects.link(root)
    W, H, T = 0.36, 0.42, 0.008                                   # each leaf
    hinge = box("hinge", (0.014, T * 0.8, H - 0.02), (0, 0, H / 2), fabric)      # fabric hinge joining the leaves
    hinge.parent = root
    for i, x in enumerate((-W / 2 - 0.003, W / 2 + 0.003)):
        leaf = box(f"leaf{i}", (W, T, H), (x, 0, H / 2), fabric, bevel=0.004)
        leaf.parent = root
        cell = box(f"cells{i}", (W - 0.03, 0.002, H - 0.05), (x, -T / 2 - 0.0005, H / 2 + 0.006), cells)
        cell.parent = root
    jb = box("junction", (0.07, 0.025, 0.05), (W / 2 + 0.003, T / 2 + 0.013, 0.07), material("Junction box", "#1B1B1B", rough=0.5), bevel=0.004)
    jb.parent = root
    stand = box("kickstand", (0.3, 0.004, 0.3), (0, 0.11, 0.13), fabric)
    stand.rotation_euler[0] = math.radians(-42)
    stand.parent = root
    root.rotation_euler = (math.radians(-tilt), 0, yaw)
    root.location = origin
    bpy.context.view_layer.update()
    return root, jb


def cable(points, radius=0.0028):
    cu = bpy.data.curves.new("cable", "CURVE")
    cu.dimensions = "3D"
    cu.bevel_depth = radius
    cu.bevel_resolution = 4
    sp = cu.splines.new("BEZIER")
    sp.bezier_points.add(len(points) - 1)
    for bp, pt in zip(sp.bezier_points, points, strict=True):
        bp.co = pt
        bp.handle_left_type = bp.handle_right_type = "AUTO"
    ob = bpy.data.objects.new("cable", cu)
    bpy.context.collection.objects.link(ob)
    ob.data.materials.append(material("Cable", "#121212", rough=0.55))
    return ob


def wood():
    m = bpy.data.materials.new("Table wood")
    m.use_nodes = True
    nt = m.node_tree
    p = nt.nodes["Principled BSDF"]
    p.inputs["Roughness"].default_value = 0.5
    tc = nt.nodes.new("ShaderNodeTexCoord")
    mapping = nt.nodes.new("ShaderNodeMapping")
    mapping.inputs["Scale"].default_value = (1.2, 7.0, 1.0)      # stretch grain along x
    wave = nt.nodes.new("ShaderNodeTexWave")
    wave.wave_type = "RINGS"
    wave.inputs["Scale"].default_value = 3.5
    wave.inputs["Distortion"].default_value = 4.0
    wave.inputs["Detail"].default_value = 4
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].color = lin("#7A5030")
    ramp.color_ramp.elements[1].color = lin("#A27348")
    nt.links.new(tc.outputs["Object"], mapping.inputs["Vector"])
    nt.links.new(mapping.outputs["Vector"], wave.inputs["Vector"])
    nt.links.new(wave.outputs["Fac"], ramp.inputs["Fac"])
    nt.links.new(ramp.outputs["Color"], p.inputs["Base Color"])
    bump = nt.nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.03
    nt.links.new(wave.outputs["Fac"], bump.inputs["Height"])
    nt.links.new(bump.outputs["Normal"], p.inputs["Normal"])
    return m


def ground():
    m = bpy.data.materials.new("Meadow")
    m.use_nodes = True
    nt = m.node_tree
    p = nt.nodes["Principled BSDF"]
    p.inputs["Roughness"].default_value = 0.95
    noise = nt.nodes.new("ShaderNodeTexNoise")
    noise.inputs["Scale"].default_value = 6.0
    noise.inputs["Detail"].default_value = 10
    ramp = nt.nodes.new("ShaderNodeValToRGB")
    ramp.color_ramp.elements[0].color = lin("#3E5A22")
    ramp.color_ramp.elements[1].color = lin("#8A8A4A")
    nt.links.new(noise.outputs["Fac"], ramp.inputs["Fac"])
    nt.links.new(ramp.outputs["Color"], p.inputs["Base Color"])
    fine = nt.nodes.new("ShaderNodeTexNoise")
    fine.inputs["Scale"].default_value = 400
    bump = nt.nodes.new("ShaderNodeBump")
    bump.inputs["Strength"].default_value = 0.6
    nt.links.new(fine.outputs["Fac"], bump.inputs["Height"])
    nt.links.new(bump.outputs["Normal"], p.inputs["Normal"])
    return m


def sky(sun_elev, sun_rot):
    w = bpy.data.worlds.new("Sky")
    bpy.context.scene.world = w
    w.use_nodes = True
    nt = w.node_tree
    bg = nt.nodes["Background"]
    try:
        s = nt.nodes.new("ShaderNodeTexSky")
        for t in ("NISHITA", "MULTIPLE_SCATTERING", "SINGLE_SCATTERING", "HOSEK_WILKIE"):
            try:
                s.sky_type = t
                break
            except Exception:
                continue
        for attr, val in (("sun_elevation", math.radians(sun_elev)), ("sun_rotation", sun_rot), ("sun_disc", False), ("altitude", 300)):
            if hasattr(s, attr):
                setattr(s, attr, val)
        nt.links.new(s.outputs["Color"], bg.inputs["Color"])
        bg.inputs["Strength"].default_value = 0.35
    except Exception:
        bg.inputs["Color"].default_value = (0.45, 0.62, 0.9, 1)
        bg.inputs["Strength"].default_value = 0.8


def tree(loc, h):
    trunk = material("Bark", "#4A3423", rough=0.9)
    leaves = material("Leaves", "#2F4A1E", rough=0.85)
    bpy.ops.mesh.primitive_cylinder_add(radius=0.12 * h / 6, depth=h * 0.45, location=(loc[0], loc[1], h * 0.22))
    bpy.context.active_object.data.materials.append(trunk)
    for i, (z, r) in enumerate(((0.45, 0.32), (0.62, 0.26), (0.78, 0.18))):
        bpy.ops.mesh.primitive_cone_add(radius1=r * h / 2, depth=h * 0.42, location=(loc[0], loc[1], h * z))
        ob = bpy.context.active_object
        ob.data.materials.append(leaves)
        d = ob.modifiers.new("d", "DISPLACE")
        tex = bpy.data.textures.new(f"t{i}{loc[0]}", "CLOUDS")
        tex.noise_scale = 0.4
        d.texture = tex
        d.strength = 0.25


def camp():
    reset()
    sc = bpy.context.scene
    sc.cycles.samples = max(args.samples, 160)
    sky(32, math.radians(200))
    bpy.ops.object.light_add(type="SUN", location=(0, 0, 5))
    sun = bpy.context.active_object
    sun.data.energy = 3.2
    sun.data.angle = math.radians(1.2)
    sun.data.color = (1.0, 0.93, 0.82)
    sun.rotation_euler = (math.radians(58), 0, math.radians(200))
    # meadow + trees far behind
    bpy.ops.mesh.primitive_plane_add(size=80, location=(0, 0, 0))
    bpy.context.active_object.data.materials.append(ground())
    for x, y, h in ((-3.2, 7.5, 7), (1.5, 9.0, 9), (4.8, 6.5, 6.5), (-6.5, 11, 10), (7.5, 12, 11), (-1.0, 14, 12), (10, 8, 8)):
        tree((x, y, 0), h)
    # folding camp table
    TOP = 0.72
    w = wood()
    box("tabletop", (1.2, 0.68, 0.025), (0, 0.1, TOP - 0.0125), w, bevel=0.004)
    alu = material("Aluminium", "#B8BCC0", rough=0.3, metal=1.0)
    for x in (-0.56, 0.56):
        for y in (-0.2, 0.4):
            bpy.ops.mesh.primitive_cylinder_add(radius=0.012, depth=TOP - 0.025, location=(x, y, (TOP - 0.025) / 2))
            bpy.context.active_object.data.materials.append(alu)
    # the Badger, front three-quarter to the camera
    b = import_badger(at=Vector((-0.24, -0.06, TOP + 0.004)), rot_z=math.radians(22))
    # panel on the table behind and to the right, turned toward the sun
    panel, jb = solar_panel(Vector((0.30, 0.34, TOP)), math.radians(-12), 26)
    bpy.context.view_layer.update()
    jb_pos = jb.matrix_world.translation
    # GX16 plug on the Badger's rear and a cable to the panel's junction box
    gx = b.matrix_world @ Vector((44 * MM, 168 * MM + 0.011, 21 * MM))
    bpy.ops.mesh.primitive_cylinder_add(radius=0.0105, depth=0.03, location=gx + Vector((0, 0.0, 0)))
    plug = bpy.context.active_object
    plug.rotation_euler = (math.radians(90), 0, math.radians(18))
    plug.data.materials.append(material("Plug", "#A9ADB1", rough=0.3, metal=1.0))
    tail = b.matrix_world @ Vector((44 * MM, 168 * MM + 0.04, 21 * MM))
    cable([tail, tail + Vector((0.05, 0.08, -0.016)), Vector((jb_pos.x - 0.1, jb_pos.y + 0.02, TOP + 0.004)),
           Vector((jb_pos.x - 0.02, jb_pos.y + 0.03, TOP + 0.01)), jb_pos + Vector((0, 0.01, -0.02))])
    # enamel mug and a phone showing the library
    bpy.ops.mesh.primitive_cylinder_add(radius=0.042, depth=0.09, location=(0.40, -0.16, TOP + 0.045))
    mug = bpy.context.active_object
    mug.data.materials.append(material("Enamel", "#2E5E7E", rough=0.25, coat=0.6))
    bpy.ops.mesh.primitive_torus_add(major_radius=0.026, minor_radius=0.006, location=(0.445, -0.16, TOP + 0.05), rotation=(math.radians(90), 0, 0))
    bpy.context.active_object.data.materials.append(material("Enamel", "#2E5E7E", rough=0.25, coat=0.6))
    PX, PY, PR = 0.0, -0.17, math.radians(18)
    phone = box("phone", (0.075, 0.155, 0.008), (PX, PY, TOP + 0.004), material("Phone", "#1A1A1C", rough=0.3, metal=0.5), bevel=0.006)
    phone.rotation_euler[2] = PR
    bpy.ops.mesh.primitive_plane_add(size=1, location=(0, 0, 0))
    scr = bpy.context.active_object
    scr.scale = (0.068, 0.146, 1)
    scr.parent = phone                       # rides on the phone, just above its glass
    scr.location = (0, 0, 0.0042)
    sm = material("Phone screen", "#050505", rough=0.08, coat=1.0)
    if args.phone and os.path.exists(args.phone):
        nt = sm.node_tree
        img = nt.nodes.new("ShaderNodeTexImage")
        img.image = bpy.data.images.load(args.phone)
        pr = nt.nodes["Principled BSDF"]
        nt.links.new(img.outputs["Color"], pr.inputs["Emission Color" if "Emission Color" in pr.inputs else "Emission"])
        pr.inputs["Emission Strength"].default_value = 0.55
    scr.data.materials.append(sm)
    cam_at = Vector((0.30, -1.10, TOP + 0.44))
    camera(cam_at, Vector((0.05, 0.10, TOP + 0.10)), lens=50, fstop=4.0, focus=(cam_at - Vector((-0.17, -0.0, TOP + 0.05))).length)
    render("photo_camp")


# ───────────────────────── multi-angle studio set ─────────────────────────
ANGLES = [  # name, azimuth (0 = straight at the front, + = toward the right side), elevation, distance, lens
    ("front_left", -35, 22, 0.62, 70),
    ("front_right", 35, 22, 0.62, 70),
    ("front", 0, 7, 0.78, 85),
    ("low_hero", -24, 5, 0.50, 50),
    ("top_three_quarter", 28, 52, 0.66, 70),
    ("rear_right", 145, 22, 0.62, 70),
    ("rear", 180, 7, 0.78, 85),
    ("left_side_fan", -90, 16, 0.66, 70),
]


def angles():
    import bmesh
    reset()
    import_badger()
    c = CENTER
    # seamless sweep, built around the case centre so it can turn with the camera
    bpy.ops.mesh.primitive_plane_add(size=1)
    sweep = bpy.context.active_object
    sweep.name = "sweep"
    bm = bmesh.new()
    pts = [(-2.0 + t * 2.6, 0.0) for t in [i / 20 for i in range(21)]]
    pts += [(0.6 + 0.4 * math.sin(a), 0.4 - 0.4 * math.cos(a)) for a in [math.pi / 2 * i / 24 for i in range(1, 25)]]
    pts.append((1.0, 2.5))
    rows = [[bm.verts.new((x, y, z - 0.004)) for y, z in pts] for x in (-2.5, 2.5)]
    for i in range(len(pts) - 1):
        bm.faces.new((rows[0][i], rows[1][i], rows[1][i + 1], rows[0][i + 1]))
    bm.to_mesh(sweep.data)
    bm.free()
    bpy.ops.object.shade_smooth()
    sweep.location = (c.x, c.y, 0)
    sweep.data.materials.append(material("Backdrop", "#E6E5E1", rough=0.75))
    world_color((0.9, 0.9, 0.92), 0.03)
    cam = camera(c + Vector((0, -0.6, 0.2)), c, lens=70)
    lights = []
    for name, az, el, dist, lens in ANGLES:
        for lt in lights:
            bpy.data.objects.remove(lt, do_unlink=True)
        def at(a, e, d):
            a, e = math.radians(a), math.radians(e)
            return c + Vector((d * math.sin(a) * math.cos(e), -d * math.cos(a) * math.cos(e), d * math.sin(e)))
        cam.location = at(az, el, dist)
        cam.data.lens = lens
        look_at(cam, c + Vector((0, 0, -0.012 if el > 10 else 0.0)))
        sweep.rotation_euler[2] = math.radians(az)
        lights = [area_light("key", at(az - 55, 48, 0.75), c, 13, 0.7),
                  area_light("fill", at(az + 65, 14, 0.65), c, 4, 0.9),
                  area_light("rim", at(az + 165, 38, 0.65), c, 9, 0.5)]
        render("angle_" + name)


scenes = ["front", "rear", "top", "camp"] if args.scene == "all" else args.scene.split(",")
for s in scenes:
    if s == "angles":
        angles()
    elif s == "camp":
        camp()
    else:
        studio(s)
