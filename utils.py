import importlib
import math
import pathlib
import subprocess
import sys

import addon_utils
import bpy


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


def install_packages(*packages: list[str], only_binary: bool = True, packages_location: pathlib.Path, invalidate_caches: bool = True):
    subprocess.run([sys.executable, '-m', 'ensurepip'], check=True, capture_output=True, encoding='utf8')
    subprocess.run([sys.executable, '-m', 'pip', 'install', '-U', 'pip'], check=True, capture_output=True, encoding='utf8')
    subprocess.run([sys.executable, '-m', 'pip', 'install', *packages,
                    *(f'--only-binary={i}' for i in packages if only_binary),
                    '--target', str(packages_location)],
                    check=True, capture_output=True, encoding='utf8')
    if invalidate_caches:
        importlib.invalidate_caches()


def get_addon_location(addon_name: str) -> pathlib.Path:
    for mod in addon_utils.modules():
        if mod.bl_info['name'] == addon_name:
            return pathlib.Path(mod.__file__).parent


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
