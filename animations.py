import typing

import bpy
from bpy_extras import anim_utils

from . import utils

Keyframe = tuple[float, float]


def insert_keyframes(
        channelbag: bpy.types.ActionChannelbag,
        data_path: str,
        index: int,
        keyframes: list[Keyframe],
        group: str | None = None,
        interpolation: str = 'LINEAR',
    ) -> None:
    fcurve = channelbag.fcurves.new(data_path, index=index)
    fcurve.keyframe_points.add(len(keyframes))
    fcurve.keyframe_points.foreach_set('co', [value for keyframe in keyframes for value in keyframe])
    for point in fcurve.keyframe_points:
        point.interpolation = interpolation
    fcurve.update()
    if group is not None:
        group_data = channelbag.groups.get(group)
        if group_data is None:
            group_data = channelbag.groups.new(group)
        fcurve.group = group_data


def bake_material_uv_offset(action, mat, axis_samples: dict[int, list[Keyframe]]) -> bool:
    """Key a material's UV offset mapping node inside an action.

    axis_samples maps axis index (0 = u, 1 = v) to (frame, value) samples; v must
    already be in Blender's flipped orientation. Binds the material's node tree to
    the action through a NODETREE slot named after the material."""
    from . import materials

    mapping_node = materials.get_uv_offset_node(mat)
    if mapping_node is None:
        return False
    slot = utils.get_or_create_slot(action, 'NODETREE', mat.name, identifier='NT' + mat.name)
    utils.bind_action_slot(mat.node_tree, action, slot)
    channelbag = utils.ensure_channelbag_exists(action, slot)
    uv_path = mapping_node.inputs[1].path_from_id('default_value')
    for axis, samples in axis_samples.items():
        insert_keyframes(channelbag, uv_path, axis, samples)
    return True


def get_keyframed_flag(anim_data, data_paths: list[str], default: bool = False) -> bool:
    channelbag = anim_utils.action_get_channelbag_for_slot(anim_data.action, anim_data.action_slot)
    if channelbag is None:
        return default
    for fcurve in channelbag.fcurves:
        if fcurve.is_empty:
            continue
        if any(fcurve.data_path == p for p in data_paths):
            return fcurve.keyframe_points[0].co[1]
    return default


def set_keyframed_flag(anim_data, data_paths: list[str], value: bool, default: bool = False, group: str | None = None) -> None:
    action = anim_data.action
    utils.ensure_channelbag_exists(action, anim_data.action_slot)
    channelbag = anim_utils.action_get_channelbag_for_slot(action, anim_data.action_slot)
    fcurves = channelbag.fcurves
    for fcurve in list(fcurves):
        if fcurve.is_empty:
            continue
        if any(fcurve.data_path == p for p in data_paths):
            fcurves.remove(fcurve)
    if value != default:
        insert_keyframes(channelbag, data_paths[0], 0, [(0, float(value))], group=group)
