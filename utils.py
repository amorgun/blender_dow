import hashlib
import math

import bpy
import mathutils


def console_get():
    for area in bpy.context.screen.areas:
        if area.type == 'CONSOLE':
            for space in area.spaces:
                if space.type == 'CONSOLE':
                    for region in area.regions:
                        if region.type == 'WINDOW':
                            return area, space, region
    return None, None, None


def console_write(text):
    area, space, region = console_get()
    if area is None:
        return

    context_override = bpy.context.copy()
    context_override.update({
        "space": space,
        "area": area,
        "region": region,
    })
    with bpy.context.temp_override(**context_override):
        for line in text.split("\n"):
            bpy.ops.console.scrollback_append(text=line, type='OUTPUT')


print = lambda x: console_write(str(x))

def print_rot_loc_matrix(intro: str, data):
    import mathutils

    loc = mathutils.Vector([0,0,0])
    if isinstance(data, mathutils.Quaternion):
        rot = data
    else:
        loc, rot, _ = data.decompose()
    rot = rot.to_euler()
    print(f'{intro}:\tRX={math.degrees(rot.x):> z6.1f}\tRY={math.degrees(rot.y):> z6.1f}\tRZ={math.degrees(rot.z):> z6.1f}\tX={loc.x: 1.3f}\tY={loc.y: 1.3f}\tZ={loc.z: 1.3f}')


def flip_image_y(image):
    import itertools

    width, height = image.size
    row_size = width * image.channels
    pixels_orig = image.pixels[:]
    pixels = (pixels_orig[(height - row - 1) * row_size: (height - row) * row_size] for row in range(height))
    pixels = itertools.chain.from_iterable(pixels)
    pixels = list(pixels)
    image.pixels[:] = pixels
    return image


def get_hash(s: str) -> str:
    return hashlib.md5(bytes(s, 'utf8')).hexdigest()


def add_driver(obj, obj_prop_path: str, target_id: str, target_data_path: str, fallback_value, index: int = -1):
    if index != -1:
        drivers = [obj.driver_add(obj_prop_path, index).driver]
    else:
        drivers = [d.driver for d in obj.driver_add(obj_prop_path, index)]
    for driver in drivers:
        var = driver.variables.new()
        driver.type = 'SUM'
        var.targets[0].id = target_id
        # TODO set var.targets[0] type to armature
        var.targets[0].data_path = target_data_path
        var.targets[0].use_fallback_value = True
        var.targets[0].fallback_value = fallback_value
    return drivers


def get_weighted_vertex_groups(obj):
    used_groups = {g.group for v in obj.data.vertices for g in v.groups if g.weight > 0.001}
    return [v for v in obj.vertex_groups if v.index in used_groups]


def get_single_bone_name(obj, vertex_groups, vertex_group_whitelist) -> str:
    if obj.parent_type == 'BONE' and obj.parent_bone != '':
        return obj.parent_bone
    vertex_groups = [v for v in vertex_groups if v.name in vertex_group_whitelist]
    if len(vertex_groups) > 1:
        return None
    for v in obj.data.vertices:
        if len(v.groups) == 0:
            return None
        max_veight = max(g.weight for g in v.groups)
        if max_veight < 0.997:
            return None
    return vertex_groups[0].name


def can_be_force_skinned(obj):
    for m in obj.modifiers:
        if m.type == 'ARMATURE' and m.object is not None:
            bone_names = {b.name for b in m.object.data.bones}
            break
    else:
        bone_names = set()
    vertex_groups = get_weighted_vertex_groups(obj)
    return get_single_bone_name(obj, vertex_groups, bone_names) is not None or len(vertex_groups) == 0
