import enum
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


PROP_SEP = '__'

PROP_ARGS = {
    (bpy.types.Material, 'full_path'): {
        'default': '',
        'subtype': 'FILE_PATH',
        'description': 'Path to where the game will look for this material. Useful for reusing the same material between multiple models',
    },
    (bpy.types.Material, 'internal'): {
        'default': False,
        'description': 'Do not export this material into a separate file and keep it inside the model file',
    },
    (bpy.types.PoseBone, 'stale'): {
        'default': False,
        'description': 'Apply it to each bone you want to disable in an animation.',
    },
    (bpy.types.Object, 'force_invisible'): {
        'default': False,
        'description': 'Force the mesh to be invisible in the current animation. Used for creating vis_ animations to conditionally display meshes, e.g. weapon upgrades and random heads',  # Usage: https://dawnofwar.org.ru/publ/27-1-0-177
    },
    (bpy.types.Object, 'visibility'): {
        'default': 1.0,
        'min': 0,
        'max': 1,
        'description': 'Used for changing the mesh visibility during an animation',
    },
    (bpy.types.Object, 'xref_source'): {
        'default': '',
        'description': "If set don't export the mesh data and instead reference it from the specified models",
    },
    (bpy.types.Object, 'uv_offset'): {
        'default': [0., 0.],
        'description': 'Used for animating moving textures, e.g. tank tracks and chainsword teeth',
    },
    (bpy.types.Object, 'uv_tiling'): {
        'default': [1., 1.],
        'description': "Used for animating UV tiling of the material. I haven't seen any examples of it in the real models",
    },
}


def create_prop_name(prefix: str, hashable: str, max_len: int = 63) -> str:
    hashable = hashable.lower()
    res = f'{prefix}{PROP_SEP}{hashable}'
    if len(res) <= max_len:
        return res
    return f'''{prefix}{PROP_SEP}{get_hash(hashable)}'''


def setup_property(obj, prop_name: str, value=None, **kwargs):
    if value is None and obj.get(prop_name):
        return
    prop_prefix = prop_name.split(PROP_SEP, 1)[0]
    args = PROP_ARGS[type(obj), prop_prefix]
    if value is None:
        value = args.get('default', value)
    obj[prop_name] = value
    id_props = obj.id_properties_ui(prop_name)
    for k, v in args.items():
        print(f'{k}: {v}')
        id_props.update(**{k: v})


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


def create_camera(cam_name: str, bone, armature, clip_start: float, clip_end: float, fov: float, focus_obj = None):
    cam = bpy.data.cameras.new(cam_name)
    cam.clip_start, cam.clip_end = clip_start, clip_end

    cam.dof.use_dof = True
    cam.dof.focus_object = focus_obj
    cam.lens_unit = 'FOV'
    cam.angle = fov
    cam.passepartout_alpha = 0

    cam_obj = bpy.data.objects.new(cam_name, cam)
    cam_obj.parent = armature
    cam_obj.parent_bone = cam_name
    cam_obj.parent_type = 'BONE'
    cam_obj.lock_location = [True] * 3
    cam_obj.lock_rotation = [True] * 3
    cam_obj.matrix_parent_inverse = mathutils.Matrix.LocRotScale(
        mathutils.Vector([0, -bone.length, 0]),
        mathutils.Matrix.Rotation(math.radians(90.0), 4, 'X').to_3x3(),
        None,
    )
    return cam_obj
