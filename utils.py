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


def create_prop_name(prefix: str, hashable: str, max_len: int = 63) -> str:
    res = f'{prefix}{hashable}'
    if len(res) <= max_len:
        return res
    return f'''{prefix}{get_hash(hashable)}'''


PROP_ARGS = {
    (bpy.types.Material, 'full_path'): {
        'default': '',
        'subtype': 'FILE_PATH',
        'description': 'Path to export this material',
    },
    (bpy.types.Material, 'internal'): {
        'default': False,
        'description': 'Do not export this material to a separate file and keep it inside the model file',
    },
    (bpy.types.PoseBone, 'stale'): {
        'default': False,
        'description': 'Apply it to each bone you want to disable in an animation.',
    },
    (bpy.types.Object, 'force_invisible'): {
        'default': False,
        'description': 'Force the mesh to be invisible in the current animation',  # Usage: https://dawnofwar.org.ru/publ/27-1-0-177
    },
    (bpy.types.Object, 'visibility'): {
        'default': 1.0,
        'min': 0,
        'max': 1,
        'description': 'Hack for animatiing mesh visibility',
    },
    (bpy.types.Object, 'uv_offset'): {
        'default': [0., 0.],
        'description': 'Hack for animatiing UV offset',
    },
    (bpy.types.Object, 'uv_tiling'): {
        'default': [1., 1.],
        'description': 'Hack for animatiing UV tiling',
    },
    (bpy.types.Object, 'xref_source'): {
        'default': '',
        'description': 'Reference this mesh from an external file instead of this model',
    },
}


def setup_property(obj, prop_name: str, value=None, **kwargs):
    if value is None and obj.get(prop_name):
        return
    obj[prop_name] = value
    id_props = obj.id_properties_ui(prop_name)
    prop_pref = prop_name.split('__', 1)[0]
    id_props.update(**PROP_ARGS[type(obj), prop_pref])


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
