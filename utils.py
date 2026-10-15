import hashlib
import math
import os
import pathlib
import tempfile
import typing

import bpy


def set_viewport_shading(context, shading_type: str = 'MATERIAL'):
    for area in context.screen.areas:
        if area.type == 'VIEW_3D':
            space = area.spaces.active
            if space.type == 'VIEW_3D':
                space.shading.type = shading_type


def reset_scene():
    bpy.ops.wm.read_homefile(app_template='')
    for mesh in bpy.data.meshes:
        bpy.data.meshes.remove(mesh)
    for material in bpy.data.materials:
        material.user_clear()
        bpy.data.materials.remove(material)
    for cam in bpy.data.cameras:
        bpy.data.cameras.remove(cam)


def create_camera(collection, name: str, matrix, angle: float, clip_start: float, clip_end: float, focus_object=None):
    """Create a camera object the way the Dawn of War importers present cameras:
    FOV lens, optional depth-of-field focus target, quaternion rotation mode."""
    cam = bpy.data.cameras.new(name)
    cam.clip_start, cam.clip_end = clip_start, clip_end
    cam.lens_unit = 'FOV'
    cam.angle = angle
    if focus_object is not None:
        cam.dof.use_dof = True
        cam.dof.focus_object = focus_object
    cam_obj = bpy.data.objects.new(name, cam)
    cam_obj.matrix_basis = matrix
    cam_obj.rotation_mode = 'QUATERNION'
    collection.objects.link(cam_obj)
    return cam_obj


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


def bind_action_slot(obj, action, slot):
    if obj.animation_data is None:
        obj.animation_data_create()
    obj.animation_data.action = action
    obj.animation_data.action_slot = slot


def get_or_create_slot(action, id_type, name, identifier=None):
    for slot in action.slots:
        if identifier is not None:
            if slot.identifier == identifier:
                return slot
        elif slot.target_id_type == id_type and slot.name_display == name:
            return slot
    return action.slots.new(id_type=id_type, name=name)


class VirtualTree:
    """A callback-defined file tree materialized as a temp directory one browsed level ahead"""

    def __init__(self, populate: typing.Callable[[tuple[str, ...]], dict[str, bool]], prefix: str = 'blender_dow_tree_'):
        self.populate = populate
        self.cache = {}
        self.tmp = tempfile.TemporaryDirectory(prefix=prefix)
        self.root = pathlib.Path(self.tmp.name)
        self.materialized = set()
        self.visit(self.root)

    def entries(self, parts: tuple[str, ...]) -> dict[str, bool]:
        entries = self.cache.get(parts)
        if entries is None:
            entries = self.cache[parts] = self.populate(parts)
        return entries

    def materialize(self, parts: tuple[str, ...]):
        if parts in self.materialized:
            return
        folder = self.root.joinpath(*parts)
        for name, is_dir in self.entries(parts).items():
            if is_dir:
                (folder / name).mkdir(exist_ok=True)
            else:
                (folder / name).touch()
        self.materialized.add(parts)

    def visit(self, directory):
        parts = pathlib.Path(directory).relative_to(self.root).parts
        self.materialize(parts)
        for name, is_dir in self.entries(parts).items():
            if is_dir:
                self.materialize(parts + (name,))

    def cleanup(self):
        self.tmp.cleanup()


_browse_tree = None
_browse_on_select = None
_browse_draw_handler = None
_browse_snapping_back = False


def _browse_tree_draw():
    """Materialize the next tree level whenever the file browser draws a directory inside it,
    and snap the browser back when it navigates outside the tree"""
    global _browse_snapping_back
    params = bpy.context.space_data.params
    if params is None or _browse_tree is None:
        return
    if pathlib.Path(os.fsdecode(params.directory)).is_relative_to(_browse_tree.root):
        _browse_tree.visit(os.fsdecode(params.directory))
    elif not _browse_snapping_back:
        _browse_snapping_back = True

        def snap_back(params=params, root=_browse_tree.root):
            global _browse_snapping_back
            _browse_snapping_back = False
            try:
                params.directory = os.fsencode(str(root))
            except ReferenceError:  # the browser area closed before the timer fired
                pass

        bpy.app.timers.register(snap_back)


def browse_tree(
    populate: typing.Callable[[tuple[str, ...]], dict[str, bool]],
    browse_operator: str,
    **operator_args,
) -> set:
    """Open the file browser over a virtual tree defined by `populate`.

    `browse_operator` is the bl_idname of a TreeBrowseOperator subclass; `operator_args`
    initialize its properties (shown in the file browser side panels)."""
    global _browse_tree, _browse_draw_handler
    cleanup_tree()
    _browse_tree = VirtualTree(populate)
    _browse_draw_handler = bpy.types.SpaceFileBrowser.draw_handler_add(_browse_tree_draw, (), 'WINDOW', 'POST_PIXEL')
    category, _, name = browse_operator.partition('.')
    op = getattr(getattr(bpy.ops, category), name)
    return op('INVOKE_DEFAULT', directory=str(_browse_tree.root), **operator_args)


def cleanup_tree():
    global _browse_tree, _browse_draw_handler
    if _browse_draw_handler is not None:
        bpy.types.SpaceFileBrowser.draw_handler_remove(_browse_draw_handler, 'WINDOW')
        _browse_draw_handler = None
    if _browse_tree is not None:
        _browse_tree.cleanup()
        _browse_tree = None


def run_in_view3d(callback: typing.Callable[[], None]):
    def run():
        window = bpy.context.window_manager.windows[0]
        area = next(a for a in window.screen.areas if a.type == 'VIEW_3D')
        region = next(r for r in area.regions if r.type == 'WINDOW')
        with bpy.context.temp_override(window=window, area=area, region=region):
            callback()

    bpy.app.timers.register(run)


class TreeBrowseOperator:
    """File-selector operator over a virtual tree started by browse_tree.

    Subclasses declare their own option props (drawable in file browser panels),
    filter_glob, and on_select(relative)."""

    filepath: bpy.props.StringProperty(subtype='FILE_PATH', options={'SKIP_SAVE'})
    directory: bpy.props.StringProperty(subtype='DIR_PATH', options={'HIDDEN', 'SKIP_SAVE'})

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        relative = pathlib.Path(self.filepath).relative_to(_browse_tree.root)
        cleanup_tree()
        self.on_select(relative)
        return {'FINISHED'}

    def cancel(self, context):
        cleanup_tree()

    def on_select(self, relative: pathlib.PurePath):
        raise NotImplementedError
