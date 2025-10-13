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
    if len(vertex_groups) != 1:
        return None
    for v in obj.data.vertices:
        if len(v.groups) == 0:
            return None
        max_veight = max(g.weight for g in v.groups)
        if max_veight < 0.997:
            return None
    return vertex_groups[0].name


def can_be_force_skinned(obj):
    armature = get_armature(obj)
    if armature is not None:
        bone_names = {b.name for b in armature.data.bones}
    else:
        bone_names = set()
    vertex_groups = get_weighted_vertex_groups(obj)
    return get_single_bone_name(obj, vertex_groups, bone_names) is not None or len(vertex_groups) == 0


def iter_animatable():
    yield from bpy.data.objects
    for i in bpy.data.materials:
        yield i
        if (node_tree := getattr(i, 'node_tree', None)) is not None:
            yield node_tree


def get_armature(mesh_obj):
    if mesh_obj.parent is not None and mesh_obj.parent.type == 'ARMATURE':
        return mesh_obj.parent
    for m in mesh_obj.modifiers:
        if m.type == 'ARMATURE':
            return m.object


def ensure_channelbag_exists(action, slot):
    try:
        layer = action.layers[0]
    except IndexError:
        layer = action.layers.new("Layer")

    try:
        strip = layer.strips[0]
    except IndexError:
        strip = layer.strips.new(type='KEYFRAME')

    return strip.channelbag(slot, ensure=True)


def setup_uv_offset(mat, location_x, location_y):
    links = mat.node_tree.links
    node_uv = mat.node_tree.nodes.new('ShaderNodeTexCoord')
    node_uv.location = location_x, location_y

    node_uv_offset_pre = mat.node_tree.nodes.new('ShaderNodeMapping')
    node_uv_offset_pre.inputs[1].default_value = -0.5, -0.5, 0
    node_uv_offset_pre.location = location_x + 200, location_y
    links.new(node_uv.outputs[2], node_uv_offset_pre.inputs['Vector'])

    node_uv_offset = mat.node_tree.nodes.new('ShaderNodeMapping')
    node_uv_offset.label = 'UV offset'
    node_uv_offset.name = 'Mapping'
    node_uv_offset.location = location_x + 400, location_y
    links.new(node_uv_offset_pre.outputs[0], node_uv_offset.inputs['Vector'])

    node_uv_offset_post = mat.node_tree.nodes.new('ShaderNodeMapping')
    node_uv_offset_post.inputs[1].default_value = 0.5, 0.5, 0
    node_uv_offset_post.location = location_x + 600, location_y
    links.new(node_uv_offset.outputs[0], node_uv_offset_post.inputs['Vector'])

    return node_uv_offset_post.outputs[0]


def get_uv_offset_node(material):
    candidates = [
        node for node in material.node_tree.nodes
        if node.bl_idname == 'ShaderNodeMapping'
        and node.label == 'UV offset'
    ]
    return candidates[0] if candidates else None
