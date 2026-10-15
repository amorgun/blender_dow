import bpy

from .. import materials


def create_shader_group():
    return materials.build_layered_shader_group(
        materials.MaterialType.DOW1,
        reflection=True,
    )


def setup_material(mat, material_data: materials.MaterialInfo, force_teamcolor: bool):
    materials.setup_layered_material(mat, material_data, force_teamcolor, create_shader_group())
