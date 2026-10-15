import dataclasses
import enum
import math
import pathlib
import tempfile
import typing

import bpy
import mathutils
from PIL import Image as PilImage


@enum.unique
class MaterialType(str, enum.Enum):
    """Which game's shader a material uses; the value is the shader group name."""
    DOW1 = 'Dow1Shader'
    DOW2 = 'Dow2Shader'
    DOW3 = 'Dow3Shader'


@enum.unique
class MaterialLayers(str, enum.Enum):
    DIFFUSE = 'diffuse'
    SPECULAR_MASK = 'specularity'
    SPECULAR_REFLECTION = 'reflection'
    SELF_ILLUMUNATION_MASK = 'self_illumination'
    SELF_ILLUMUNATION_COLOR = 'self_illumination_color'
    OPACITY = 'opacity'
    NORMAL_MAP = 'normal_map'
    GLOSS = 'gloss'
    OCCLUSION = 'occlusion'
    DIRT = 'dirt_overlay'
    PATTERN = 'pattern'


@enum.unique
class TeamcolorLayers(str, enum.Enum):
    PRIMARY = 'primary'
    SECONDARY = 'secondary'
    TRIM = 'trim'
    TERTIARY = 'tertiary'
    WEAPONS = 'weapons'
    EYES = 'eyes'
    DIRT = 'dirt'
    DEFAULT = 'default'
    BADGE = 'badge'
    BANNER = 'banner'
    DECAL = 'decal'


TEAMCOLORABLE_LAYERS = {
    TeamcolorLayers.PRIMARY,
    TeamcolorLayers.SECONDARY,
    TeamcolorLayers.TRIM,
    TeamcolorLayers.WEAPONS,
    TeamcolorLayers.EYES,
}
TEAMCOLOR_IMAGES = {TeamcolorLayers.BADGE, TeamcolorLayers.BANNER}
# image slots apply_teamcolor may swap on an existing material
APPLYABLE_IMAGES = TEAMCOLOR_IMAGES | {TeamcolorLayers.DECAL}
# the three paint slots a DoW3 badge stencil carries
BADGE_PAINT_LAYERS = (TeamcolorLayers.PRIMARY, TeamcolorLayers.SECONDARY, TeamcolorLayers.TRIM)
# the four dow3 paint-scheme slots, masked by the pattern's R/G/B and inverted alpha
SCHEME_PAINT_LAYERS = (TeamcolorLayers.PRIMARY, TeamcolorLayers.SECONDARY, TeamcolorLayers.TRIM, TeamcolorLayers.TERTIARY)

WHITE = mathutils.Color((1., 1., 1.))

Image = typing.TypeVar('Image')
Color = typing.TypeVar('Color')

# shader group input name -> the material layer connected to it
SHADER_INPUT_SLOTS = {
    'diffuse': MaterialLayers.DIFFUSE,
    'specularity': MaterialLayers.SPECULAR_MASK,
    'reflection': MaterialLayers.SPECULAR_REFLECTION,
    'self_illumination': MaterialLayers.SELF_ILLUMUNATION_MASK,
    'self_illumination_color': MaterialLayers.SELF_ILLUMUNATION_COLOR,
    'opacity': MaterialLayers.OPACITY,
    'normal_map': MaterialLayers.NORMAL_MAP,
    'gloss': MaterialLayers.GLOSS,
    'occlusion': MaterialLayers.OCCLUSION,
    'dirt_overlay': MaterialLayers.DIRT,
    'pattern': MaterialLayers.PATTERN,
    'pattern_alpha': MaterialLayers.PATTERN,
}

# BSDF sockets fed by the shader group outputs; nothing else links into the BSDF
SHADER_OUTPUT_SOCKETS = [
    ('base_color', 'Base Color'),
    ('metallic', 'Metallic'),
    ('roughness', 'Roughness'),
    ('specular_tint', 'Specular Tint'),
    ('specular_ior', 'Specular IOR Level'),
    ('emission_color', 'Emission Color'),
    ('emission_strength', 'Emission Strength'),
    ('alpha', 'Alpha'),
    ('normal', 'Normal'),
]


@dataclasses.dataclass
class MaterialInfo:
    material_type: MaterialType = MaterialType.DOW1
    channel_images: dict[MaterialLayers, Image] = dataclasses.field(default_factory=dict)
    teamcolor_images: dict[TeamcolorLayers, Image] = dataclasses.field(default_factory=dict)
    default_color: Color = None
    default_specilar_mask_value: float = 0
    default_emission_strength: float = 0
    roughness_value: float = 0
    use_self_illumination_color: bool = True
    banner_size: tuple[int, int] = 64, 64
    banner_position: tuple[int, int] = None
    banner_rotation: float = 0
    badge_size: tuple[int, int] = 64, 96
    badge_position: tuple[int, int] = None
    badge_rotation: float = 0


@dataclasses.dataclass
class TeamcolorInfo:
    colors: dict[TeamcolorLayers, Color] = dataclasses.field(default_factory=dict)
    images: dict[TeamcolorLayers, Image] = dataclasses.field(default_factory=dict)
    badge_colors: dict[TeamcolorLayers, Color] = dataclasses.field(default_factory=dict)
    spec_colors: dict[TeamcolorLayers, Color] = dataclasses.field(default_factory=dict)
    gloss_values: dict[TeamcolorLayers, float] = dataclasses.field(default_factory=dict)


def setup_uv_offset(mat, location_x, location_y):
    links = mat.node_tree.links
    node_uv = mat.node_tree.nodes.new('ShaderNodeTexCoord')
    node_uv.location = location_x, location_y

    node_uv_offset_pre = mat.node_tree.nodes.new('ShaderNodeMapping')
    node_uv_offset_pre.inputs[1].default_value = -0.5, -0.5, 0
    node_uv_offset_pre.location = location_x + 200, location_y
    node_uv_offset_pre.name = 'UV offset pre'
    links.new(node_uv.outputs[2], node_uv_offset_pre.inputs['Vector'])

    node_uv_offset = mat.node_tree.nodes.new('ShaderNodeMapping')
    node_uv_offset.label = 'UV offset'
    node_uv_offset.name = 'UV offset'
    node_uv_offset.location = location_x + 400, location_y
    links.new(node_uv_offset_pre.outputs[0], node_uv_offset.inputs['Vector'])

    node_uv_offset_post = mat.node_tree.nodes.new('ShaderNodeMapping')
    node_uv_offset_post.inputs[1].default_value = 0.5, 0.5, 0
    node_uv_offset_post.location = location_x + 600, location_y
    node_uv_offset_post.name = 'UV offset post'
    links.new(node_uv_offset.outputs[0], node_uv_offset_post.inputs['Vector'])

    return node_uv_offset_post.outputs[0]


def get_uv_offset_node(material):
    candidates = [
        node for node in material.node_tree.nodes
        if node.bl_idname == 'ShaderNodeMapping'
        and node.label == 'UV offset'
    ]
    return candidates[0] if candidates else None


def get_shader_group_node(mat):
    for node in mat.node_tree.nodes:
        if node.bl_idname != 'ShaderNodeGroup' or node.node_tree is None:
            continue
        base_name = node.node_tree.name.split('.')[0]
        if any(base_name == t.value for t in MaterialType):
            return node
    return None


def get_material_type(mat) -> MaterialType | None:
    node = get_shader_group_node(mat)
    if node is None:
        return None
    return MaterialType(node.node_tree.name.split('.')[0])


def setup_material(mat, material_data: MaterialInfo, force_teamcolor: bool):
    """Build the material's node tree for its game (material_data.material_type)."""
    if material_data.material_type == MaterialType.DOW3:
        from .dow3 import materials as game
    elif material_data.material_type == MaterialType.DOW2:
        from .dow2 import materials as game
    else:
        from .dow1 import materials as game
    game.setup_material(mat, material_data, force_teamcolor)


def _new_group(name: str, version: int = 0):
    """Reuse a group of this name only when its stamped version matches; a blend
    holding a group built by an older addon gets a fresh (suffixed) group so new
    imports never inherit stale node graphs."""
    for group in bpy.data.node_groups:
        if group.name.split('.')[0] == name and group.get('bd_group_version', 0) == version:
            return group, False
    group = bpy.data.node_groups.new(name, 'ShaderNodeTree')
    group['bd_group_version'] = version
    return group, True


def _group_input(interface, name, socket_type, default=None, parent=None,
                 min_value=None, max_value=None, subtype=None):
    socket = interface.new_socket(name=name, in_out='INPUT', socket_type=socket_type, parent=parent)
    if default is not None:
        socket.default_value = default
    if subtype is not None:
        socket.subtype = subtype
    if min_value is not None:
        socket.min_value = min_value
    if max_value is not None:
        socket.max_value = max_value
    return socket


def _factor_input(interface, name, default=0.0, parent=None):
    """A [0, 1] float slider input."""
    return _group_input(interface, name, 'NodeSocketFloat', default, parent,
                        min_value=0.0, max_value=1.0, subtype='FACTOR')


def build_layered_shader_group(material_type: MaterialType, *, reflection: bool,
                               extra_inputs: tuple[str, ...] = (),
                               extra_scalars: tuple[tuple[str, float], ...] = ()):
    """The Dawn of War I/II surface: additive specular blended into the albedo, the
    specular mask doubling as metalness. reflection adds the legacy environment-map
    branch; extra_inputs/extra_scalars are declared but not routed to any output."""
    group, created = _new_group(material_type.value)
    if not created:
        return group
    interface = group.interface

    _group_input(interface, 'diffuse', 'NodeSocketColor', (0.8, 0.8, 0.8, 1.0))
    _group_input(interface, 'opacity', 'NodeSocketFloat', 1.0)
    _group_input(interface, 'specularity', 'NodeSocketColor', (0.0, 0.0, 0.0, 1.0))
    if reflection:
        _group_input(interface, 'reflection', 'NodeSocketColor', (0.0, 0.0, 0.0, 1.0))
        _group_input(interface, 'use_reflection', 'NodeSocketFloat', 0.0)
    _group_input(interface, 'gloss', 'NodeSocketFloat', 0.0)
    _group_input(interface, 'use_gloss', 'NodeSocketFloat', 0.0)
    _group_input(interface, 'normal_map', 'NodeSocketColor', (0.5, 0.5, 1.0, 1.0))
    _group_input(interface, 'self_illumination', 'NodeSocketFloat', 0.0)
    _group_input(interface, 'self_illumination_color', 'NodeSocketColor', (0.0, 0.0, 0.0, 1.0))
    _group_input(interface, 'use_self_illumination_color', 'NodeSocketFloat', 0.0)
    for name in extra_inputs:
        _group_input(interface, name, 'NodeSocketColor', (0.0, 0.0, 0.0, 1.0))
    for name, default in extra_scalars:
        _group_input(interface, name, 'NodeSocketFloat', default)
    _group_input(interface, 'teamcolor', 'NodeSocketColor', (0.8, 0.8, 0.8, 1.0))
    _group_input(interface, 'roughness_value', 'NodeSocketFloat', 0.0)

    for name, socket_type in [
        ('base_color', 'NodeSocketColor'),
        ('metallic', 'NodeSocketFloat'),
        ('roughness', 'NodeSocketFloat'),
        ('specular_tint', 'NodeSocketColor'),
        ('specular_ior', 'NodeSocketFloat'),
        ('emission_color', 'NodeSocketColor'),
        ('emission_strength', 'NodeSocketFloat'),
        ('alpha', 'NodeSocketFloat'),
        ('normal', 'NodeSocketVector'),
    ]:
        interface.new_socket(name=name, in_out='OUTPUT', socket_type=socket_type)

    nodes = group.nodes
    links = group.links
    inp = nodes.new('NodeGroupInput')
    inp.location = -700, 0
    out = nodes.new('NodeGroupOutput')
    out.location = 700, 0

    spec_add = nodes.new('ShaderNodeMix')
    spec_add.data_type = 'RGBA'
    spec_add.blend_type = 'ADD'
    spec_add.clamp_result = True
    spec_add.inputs['Factor'].default_value = 0.5
    spec_add.label = 'Apply spec'
    spec_add.location = -150, 300
    links.new(inp.outputs['teamcolor'], spec_add.inputs['A'])
    links.new(inp.outputs['specularity'], spec_add.inputs['B'])

    metallic_gamma = nodes.new('ShaderNodeGamma')
    metallic_gamma.inputs['Gamma'].default_value = 0.4545
    metallic_gamma.location = -150, 50
    links.new(inp.outputs['specularity'], metallic_gamma.inputs['Color'])

    if reflection:
        legacy_mix = nodes.new('ShaderNodeMix')
        legacy_mix.data_type = 'RGBA'
        legacy_mix.blend_type = 'MIX'
        legacy_mix.location = 100, 450
        links.new(inp.outputs['specularity'], legacy_mix.inputs['Factor'])
        links.new(inp.outputs['teamcolor'], legacy_mix.inputs['A'])
        links.new(inp.outputs['reflection'], legacy_mix.inputs['B'])

        base = nodes.new('ShaderNodeMix')
        base.data_type = 'RGBA'
        base.blend_type = 'MIX'
        base.location = 350, 300
        links.new(inp.outputs['use_reflection'], base.inputs['Factor'])
        links.new(spec_add.outputs['Result'], base.inputs['A'])
        links.new(legacy_mix.outputs['Result'], base.inputs['B'])
        links.new(base.outputs['Result'], out.inputs['base_color'])

        no_reflection = nodes.new('ShaderNodeMath')
        no_reflection.operation = 'SUBTRACT'
        no_reflection.inputs[0].default_value = 1.0
        no_reflection.location = 100, 50
        links.new(inp.outputs['use_reflection'], no_reflection.inputs[1])
        metallic = nodes.new('ShaderNodeMath')
        metallic.operation = 'MULTIPLY'
        metallic.location = 350, 50
        links.new(metallic_gamma.outputs[0], metallic.inputs[0])
        links.new(no_reflection.outputs[0], metallic.inputs[1])
        links.new(metallic.outputs[0], out.inputs['metallic'])
    else:
        links.new(spec_add.outputs['Result'], out.inputs['base_color'])
        links.new(metallic_gamma.outputs[0], out.inputs['metallic'])

    links.new(inp.outputs['specularity'], out.inputs['specular_tint'])

    ior = nodes.new('ShaderNodeValue')
    ior.outputs[0].default_value = 0.0
    ior.location = 350, -100
    links.new(ior.outputs[0], out.inputs['specular_ior'])

    invert_gloss = nodes.new('ShaderNodeMath')
    invert_gloss.operation = 'SUBTRACT'
    invert_gloss.inputs[0].default_value = 1.0
    invert_gloss.location = -400, -150
    links.new(inp.outputs['gloss'], invert_gloss.inputs[1])
    roughness = nodes.new('ShaderNodeMix')
    roughness.data_type = 'FLOAT'
    roughness.location = -150, -150
    links.new(inp.outputs['use_gloss'], roughness.inputs['Factor'])
    links.new(inp.outputs['roughness_value'], roughness.inputs[2])
    links.new(invert_gloss.outputs[0], roughness.inputs[3])
    links.new(roughness.outputs['Result'], out.inputs['roughness'])

    emission_color = nodes.new('ShaderNodeMix')
    emission_color.data_type = 'RGBA'
    emission_color.blend_type = 'MIX'
    emission_color.location = -150, -350
    links.new(inp.outputs['use_self_illumination_color'], emission_color.inputs['Factor'])
    links.new(inp.outputs['teamcolor'], emission_color.inputs['A'])
    links.new(inp.outputs['self_illumination_color'], emission_color.inputs['B'])
    links.new(emission_color.outputs['Result'], out.inputs['emission_color'])
    links.new(inp.outputs['self_illumination'], out.inputs['emission_strength'])

    object_info = nodes.new('ShaderNodeObjectInfo')
    object_info.location = -400, -550
    alpha = nodes.new('ShaderNodeMath')
    alpha.operation = 'MULTIPLY'
    alpha.use_clamp = True
    alpha.location = -150, -550
    links.new(inp.outputs['opacity'], alpha.inputs[0])
    links.new(object_info.outputs['Alpha'], alpha.inputs[1])
    links.new(alpha.outputs[0], out.inputs['alpha'])

    normal_map = nodes.new('ShaderNodeNormalMap')
    normal_map.location = -150, -750
    links.new(inp.outputs['normal_map'], normal_map.inputs['Color'])
    links.new(normal_map.outputs['Normal'], out.inputs['normal'])

    return group


def link_shader_group_to_bsdf(mat, group_node, node_final):
    links = mat.node_tree.links
    for output_name, bsdf_input in SHADER_OUTPUT_SOCKETS:
        if output_name in group_node.outputs:
            links.new(group_node.outputs[output_name], node_final.inputs[bsdf_input])


def setup_layered_material(mat, material_data: MaterialInfo, force_teamcolor: bool, shader_group):
    """The shared Dawn of War I/II material tree: image nodes feed the game's shader
    group by named input, the shader group alone feeds the principled BSDF."""
    mat.blend_method = 'CLIP'  # FIXME it doesn't work now
    mat.show_transparent_back = False
    placehoder_image = create_placeholder_image()

    links = mat.node_tree.links
    node_final = mat.node_tree.nodes[0]
    node_final.location = 300, 400
    mat.node_tree.nodes[1].location = node_final.location[0] + 400, node_final.location[1]

    group_node = mat.node_tree.nodes.new('ShaderNodeGroup')
    group_node.node_tree = shader_group
    group_node.location = node_final.location[0] - 300, node_final.location[1]
    link_shader_group_to_bsdf(mat, group_node, node_final)

    group_node.inputs['roughness_value'].default_value = material_data.roughness_value
    group_node.inputs['self_illumination'].default_value = material_data.default_emission_strength
    if material_data.default_color:
        group_node.inputs['diffuse'].default_value[:3] = material_data.default_color.from_srgb_to_scene_linear()
    spec_default = material_data.default_specilar_mask_value
    group_node.inputs['specularity'].default_value = spec_default, spec_default, spec_default, 1.0

    uv_vector = setup_uv_offset(mat, node_final.location[1] - 1600, node_final.location[1] - 200)

    num_created_tex_nodes = 0
    for layer in MaterialLayers:
        node_image = material_data.channel_images.get(layer)
        if node_image is None:
            continue
        input_name = next((name for name, slot in SHADER_INPUT_SLOTS.items()
                           if slot == layer and name in group_node.inputs), None)
        if input_name is None:
            continue
        node_tex = mat.node_tree.nodes.new('ShaderNodeTexImage')
        node_tex.image = node_image
        node_tex.location = -430, node_final.location[1] - 320 * num_created_tex_nodes
        node_tex.label = layer
        num_created_tex_nodes += 1
        if layer == MaterialLayers.SPECULAR_REFLECTION:
            node_global_to_camera = mat.node_tree.nodes.new('ShaderNodeVectorTransform')
            node_global_to_camera.convert_to = 'CAMERA'
            node_global_to_camera.location = -600, -200
            links.new(uv_vector, node_global_to_camera.inputs['Vector'])
            node_fix_reflect = mat.node_tree.nodes.new('ShaderNodeMapping')
            node_fix_reflect.label = 'Rotate reflection vector'
            node_fix_reflect.vector_type = 'VECTOR'
            node_fix_reflect.inputs['Rotation'].default_value = math.pi, 0, 0
            node_fix_reflect.location = -600, -400
            links.new(node_global_to_camera.outputs[0], node_fix_reflect.inputs['Vector'])
            links.new(node_fix_reflect.outputs[0], node_tex.inputs['Vector'])
        else:
            links.new(uv_vector, node_tex.inputs['Vector'])
        if layer == MaterialLayers.OPACITY:
            links.new(node_tex.outputs['Alpha'], group_node.inputs['opacity'])
        else:
            links.new(node_tex.outputs[0], group_node.inputs[input_name])
        if layer == MaterialLayers.DIFFUSE and MaterialLayers.OPACITY not in material_data.channel_images:
            links.new(node_tex.outputs['Alpha'], group_node.inputs['opacity'])
        if layer == MaterialLayers.SPECULAR_REFLECTION:
            group_node.inputs['use_reflection'].default_value = 1.0
        if layer == MaterialLayers.GLOSS:
            group_node.inputs['use_gloss'].default_value = 1.0
        if layer == MaterialLayers.SELF_ILLUMUNATION_COLOR and material_data.use_self_illumination_color:
            group_node.inputs['use_self_illumination_color'].default_value = 1.0

    # the shader group reads its albedo from 'teamcolor'; without a teamcolor
    # chain the plain diffuse feeds it
    if group_node.inputs['diffuse'].links:
        links.new(group_node.inputs['diffuse'].links[0].from_socket, group_node.inputs['teamcolor'])
    else:
        group_node.inputs['teamcolor'].default_value = tuple(group_node.inputs['diffuse'].default_value)

    if force_teamcolor:
        setup_teamcolor_nodes(mat, material_data, group_node, uv_vector, placehoder_image)


def setup_teamcolor_nodes(mat, material_data: MaterialInfo, group_node, uv_vector, placehoder_image):
    """The Dawn of War I/II teamcolor block: layer images into the shared ApplyTeamcolor
    group, whose result becomes the shader group's teamcolor input."""
    links = mat.node_tree.links
    common_node_pos_x, common_node_pos_y = -600, 3100
    aply_teamcolor = mat.node_tree.nodes.new('ShaderNodeGroup')
    aply_teamcolor.node_tree = create_teamcolor_group()
    aply_teamcolor.location = common_node_pos_x + 600, common_node_pos_y - 600
    num_created_tex_nodes = 0
    for layer_name in TEAMCOLORABLE_LAYERS | {TeamcolorLayers.DIRT, TeamcolorLayers.DEFAULT}:
        node_tex = mat.node_tree.nodes.new('ShaderNodeTexImage')
        node_pos_x, node_pos_y = common_node_pos_x, common_node_pos_y - 290 * num_created_tex_nodes
        num_created_tex_nodes += 1
        if layer_name in material_data.teamcolor_images:
            node_tex.image = material_data.teamcolor_images[layer_name]
        else:
            node_tex.hide = True
            node_tex.image = placehoder_image
        node_tex.location = node_pos_x + 200, node_pos_y
        node_tex.label = f'color_layer_{layer_name.value}'
        links.new(uv_vector, node_tex.inputs['Vector'])
        links.new(node_tex.outputs[0], aply_teamcolor.inputs[f'{layer_name.value}_{"value" if layer_name != TeamcolorLayers.DEFAULT else "color"}'])

    img_size_node = mat.node_tree.nodes.new('ShaderNodeCombineXYZ')
    default_layer_img = material_data.teamcolor_images.get(TeamcolorLayers.DEFAULT)
    if default_layer_img is not None:
        size = default_layer_img.size
        img_size_node.inputs['X'].default_value = size[0]
        img_size_node.inputs['Y'].default_value = size[1]
    img_size_node.label = 'color_layer_size'
    img_size_node.location = common_node_pos_x - 300, common_node_pos_y - 290 * num_created_tex_nodes + 200

    flip_texture_node = mat.node_tree.nodes.new('ShaderNodeMapping')
    flip_texture_node.label = 'Flip'
    flip_texture_node.location = common_node_pos_x - 450, common_node_pos_y - 290 * num_created_tex_nodes + 200
    flip_texture_node.inputs['Location'].default_value = (0, 1, 0)
    flip_texture_node.inputs['Scale'].default_value = (1, -1, 1)
    links.new(uv_vector, flip_texture_node.inputs['Vector'])

    for layer_name, position, size in [
        ('badge', material_data.badge_position, material_data.badge_size),
        ('banner', material_data.banner_position, material_data.banner_size),
    ]:
        if position is None:
            node_name = f'UNUSED_{layer_name}'
            position = 0, 0
            default_image = placehoder_image
        else:
            node_name = layer_name
            default_image = None
        node_pos_x, node_pos_y = common_node_pos_x, common_node_pos_y - 290 * num_created_tex_nodes
        data_pos_node = mat.node_tree.nodes.new('ShaderNodeCombineXYZ')
        data_pos_node.inputs['X'].default_value = position[0]
        data_pos_node.inputs['Y'].default_value = position[1]
        data_pos_node.location = node_pos_x - 300, node_pos_y
        data_pos_node.label = f'{layer_name}_position'

        data_size_node = mat.node_tree.nodes.new('ShaderNodeCombineXYZ')
        data_size_node.inputs['X'].default_value = size[0]
        data_size_node.inputs['Y'].default_value = size[1]
        data_size_node.location = node_pos_x - 300, node_pos_y - 150
        data_size_node.label = f'{layer_name}_display_size'

        calc_pos_node = mat.node_tree.nodes.new('ShaderNodeVectorMath')
        calc_pos_node.operation = 'DIVIDE'
        calc_pos_node.location = node_pos_x - 150, node_pos_y
        links.new(data_pos_node.outputs[0], calc_pos_node.inputs[0])
        links.new(img_size_node.outputs[0], calc_pos_node.inputs[1])

        calc_scale_node = mat.node_tree.nodes.new('ShaderNodeVectorMath')
        calc_scale_node.operation = 'DIVIDE'
        calc_scale_node.location = node_pos_x - 150, node_pos_y - 150
        links.new(data_size_node.outputs[0], calc_scale_node.inputs[0])
        links.new(img_size_node.outputs[0], calc_scale_node.inputs[1])

        scale_node = mat.node_tree.nodes.new('ShaderNodeMapping')
        scale_node.vector_type = 'TEXTURE'
        scale_node.location = node_pos_x, node_pos_y
        scale_node.inputs['Rotation'].default_value = (0, 0, getattr(material_data, f'{layer_name}_rotation'))
        links.new(flip_texture_node.outputs[0], scale_node.inputs['Vector'])
        links.new(calc_pos_node.outputs[0], scale_node.inputs['Location'])
        links.new(calc_scale_node.outputs[0], scale_node.inputs['Scale'])

        node_tex = mat.node_tree.nodes.new('ShaderNodeTexImage')
        num_created_tex_nodes += 1
        node_tex.extension = 'CLIP'
        node_tex.location = node_pos_x + 200, node_pos_y
        node_tex.label = node_name
        if default_image is not None:
            node_tex.image = default_image
            node_tex.hide = True
        links.new(scale_node.outputs[0], node_tex.inputs['Vector'])
        links.new(node_tex.outputs[0], aply_teamcolor.inputs[f'{layer_name}_color'])
        links.new(node_tex.outputs[1], aply_teamcolor.inputs[f'{layer_name}_alpha'])

    if TeamcolorLayers.DEFAULT in material_data.teamcolor_images:
        links.new(aply_teamcolor.outputs[0], group_node.inputs['teamcolor'])


def create_teamcolor_group(name: str = 'ApplyTeamcolor'):
    """The shared Dawn of War I/II teamcolor node group: pre-baked layer value images
    tinted by their colors with OVERLAY, plus the badge and banner image slots."""
    if name in bpy.data.node_groups:
        return bpy.data.node_groups[name]

    group = bpy.data.node_groups.new(name, 'ShaderNodeTree')

    interface = group.interface

    interface.new_socket(
        name='default_color',
        in_out='INPUT',
        socket_type='NodeSocketColor',
    )
    interface.new_socket(
        name='dirt_value',
        in_out='INPUT',
        socket_type='NodeSocketFloat',
    )

    for c in sorted(TEAMCOLORABLE_LAYERS):
        panel = interface.new_panel(name=c, default_closed=True)
        interface.new_socket(
            name=f'{c.value}_color',
            in_out='INPUT',
            socket_type='NodeSocketColor',
            parent=panel,
        )
        interface.new_socket(
            name=f'{c.value}_value',
            in_out='INPUT',
            socket_type='NodeSocketFloat',
            parent=panel,
        )
    for c in TEAMCOLOR_IMAGES:
        panel = interface.new_panel(name=c, default_closed=True)
        interface.new_socket(
            name=f'{c.value}_color',
            in_out='INPUT',
            socket_type='NodeSocketColor',
            parent=panel,
        )
        interface.new_socket(
            name=f'{c.value}_alpha',
            in_out='INPUT',
            socket_type='NodeSocketFloat',
            parent=panel,
        )

    interface.new_socket(
        name='result',
        in_out='OUTPUT',
        socket_type='NodeSocketColor',
    )

    group_inputs = group.nodes.new('NodeGroupInput')
    group_inputs.location = -650, 0

    group_outputs = group.nodes.new('NodeGroupOutput')
    group_outputs.location = 400, 0

    links = group.links

    prev_color_output = None
    common_node_pos_x, common_node_pos_y = -100, 500
    for idx, layer_name in enumerate(sorted(TEAMCOLORABLE_LAYERS)):
        node_pos_x, node_pos_y = common_node_pos_x, common_node_pos_y - 220 * idx

        correct_color = group.nodes.new('ShaderNodeGamma')
        correct_color.inputs['Gamma'].default_value = 0.4545
        correct_color.location = node_pos_x - 200, node_pos_y - 70
        links.new(group_inputs.outputs[f'{layer_name.value}_color'], correct_color.inputs['Color'])

        correct_mask = group.nodes.new('ShaderNodeGamma')
        correct_mask.inputs['Gamma'].default_value = 0.4545
        correct_mask.location = node_pos_x - 200, node_pos_y + 30
        links.new(group_inputs.outputs[f'{layer_name.value}_value'], correct_mask.inputs['Color'])

        layer_color_node = group.nodes.new('ShaderNodeMixRGB')
        layer_color_node.blend_type = 'OVERLAY'
        layer_color_node.inputs['Fac'].default_value = 1
        layer_color_node.label = layer_name
        layer_color_node.location = node_pos_x, node_pos_y

        links.new(correct_mask.outputs[0], layer_color_node.inputs['Color1'])
        links.new(correct_color.outputs[0], layer_color_node.inputs['Color2'])
        if prev_color_output is None:
            prev_color_output = layer_color_node.outputs[0]
        else:
            node_mix = group.nodes.new('ShaderNodeMixRGB')
            node_mix.blend_type = 'ADD'
            node_mix.inputs['Fac'].default_value = 1
            node_mix.location = node_pos_x + 200, node_pos_y
            links.new(prev_color_output, node_mix.inputs['Color1'])
            links.new(layer_color_node.outputs['Color'], node_mix.inputs['Color2'])
            prev_color_output = node_mix.outputs[0]

    idx += 1
    node_pos_x, node_pos_y = common_node_pos_x + 200, common_node_pos_y - 220 * idx
    correct_color_dirt = group.nodes.new('ShaderNodeGamma')
    correct_color_dirt.inputs['Gamma'].default_value = 0.4545
    correct_color_dirt.location = node_pos_x - 200, node_pos_y - 70
    links.new(group_inputs.outputs['default_color'], correct_color_dirt.inputs['Color'])

    correct_mask_dirt = group.nodes.new('ShaderNodeGamma')
    correct_mask_dirt.inputs['Gamma'].default_value = 0.4545
    correct_mask_dirt.location = node_pos_x - 200, node_pos_y + 30
    links.new(group_inputs.outputs['dirt_value'], correct_mask_dirt.inputs['Color'])

    node_mix_dirt = group.nodes.new('ShaderNodeMixRGB')
    node_mix_dirt.blend_type = 'ADD'
    node_mix_dirt.location = node_pos_x, node_pos_y
    links.new(correct_mask_dirt.outputs[0], node_mix_dirt.inputs['Fac'])
    links.new(prev_color_output, node_mix_dirt.inputs['Color1'])
    links.new(correct_color_dirt.outputs[0], node_mix_dirt.inputs['Color2'])

    combined_color = group.nodes.new('ShaderNodeGamma')
    combined_color.inputs['Gamma'].default_value = 2.2
    combined_color.location = node_pos_x + 200, node_pos_y
    links.new(node_mix_dirt.outputs[0], combined_color.inputs['Color'])
    prev_color_output = combined_color.outputs[0]

    for layer_name in TEAMCOLOR_IMAGES:
        idx += 1
        node_mix = group.nodes.new('ShaderNodeMixRGB')
        node_mix.blend_type = 'MIX'
        node_mix.location = common_node_pos_x + 200, common_node_pos_y - 220 * idx
        links.new(group_inputs.outputs[f'{layer_name.value}_alpha'], node_mix.inputs['Fac'])
        links.new(prev_color_output, node_mix.inputs['Color1'])
        links.new(group_inputs.outputs[f'{layer_name.value}_color'], node_mix.inputs['Color2'])
        prev_color_output = node_mix.outputs[0]

    group.links.new(prev_color_output, group_outputs.inputs['result'])
    return group


def apply_teamcolor_scheme(layout, unpacked_texture_folder, teamcolor_dict: dict):
    """Apply a {TeamcolorLayers: color, image name: path} scheme to every material in the file"""
    teamcolor = TeamcolorInfo()
    for layer in TEAMCOLORABLE_LAYERS | {TeamcolorLayers.TERTIARY}:
        if layer in teamcolor_dict:
            teamcolor.colors[layer] = teamcolor_dict[layer]
        if (spec_color := teamcolor_dict.get(f'spec_{layer.value}')) is not None:
            teamcolor.spec_colors[layer] = spec_color
        if (gloss := teamcolor_dict.get(f'gloss_{layer.value}')) is not None:
            teamcolor.gloss_values[layer] = gloss
    for layer in BADGE_PAINT_LAYERS:
        if (badge_color := teamcolor_dict.get(f'badge_{layer.value}')) is not None:
            teamcolor.badge_colors[layer] = badge_color
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = pathlib.Path(tmpdir)
        for key in TEAMCOLOR_IMAGES:
            if (img_path := teamcolor_dict.get(key.value)) is None:
                continue
            data_path = pathlib.Path(img_path)
            if not data_path.exists():
                data_path = layout.find(data_path)
            if not data_path:
                continue
            tmpfile = tmpdir / pathlib.Path(img_path).name
            tmpfile.write_bytes(data_path.read_bytes())
            if tmpfile.suffix.lower() == '.dds':  # Blender cannot decode the game's dds variants
                png_file = tmpfile.with_suffix('.png')
                PilImage.open(tmpfile).save(png_file, compress_level=1)
                tmpfile = png_file
            teamcolor.images[key] = image = bpy.data.images.load(str(tmpfile))
            image.pack()
            image.packed_files[-1].filepath = image.filepath_raw = str(unpacked_texture_folder / tmpfile.name)
    for mat in bpy.data.materials:
        if mat.node_tree is None:
            continue
        apply_teamcolor(mat, teamcolor)


def srgb_to_overlay_linear(color):
    """Socket value whose Gamma-0.4545 re-encode inside the ApplyTeamcolor groups
    restores the sRGB paint value exactly, as the game shaders consume it raw"""
    return [c ** 2.2 for c in color[:3]]


def apply_teamcolor(mat, teamcolor: TeamcolorInfo):
    color_node_names = {f'color_{i.value}' for i in TEAMCOLORABLE_LAYERS}
    for node in mat.node_tree.nodes:
        if node.bl_idname == 'ShaderNodeValToRGB' and node.label in color_node_names:
            key = TeamcolorLayers(node.label[len('color_'):])
            if teamcolor.colors.get(key) is None:
                continue
            node.color_ramp.elements[-1].color[:3] = srgb_to_overlay_linear(teamcolor.colors[key])
        if node.bl_idname == 'ShaderNodeTexImage' and node.label in APPLYABLE_IMAGES and teamcolor.images.get(node.label) is not None:
            node.image = teamcolor.images[node.label]
        if node.bl_idname == 'ShaderNodeGroup' and node.node_tree is not None and node.node_tree.name.startswith('ApplyTeamcolor'):
            for c in TEAMCOLORABLE_LAYERS | {TeamcolorLayers.TERTIARY}:
                if teamcolor.colors.get(c, None) is None:
                    continue
                if f'{c.value}_color' not in node.inputs:
                    continue
                node.inputs[f'{c.value}_color'].default_value[:3] = srgb_to_overlay_linear(teamcolor.colors[c])
            # the dow2_unit_2uv shader's paint loop reads only the colour slots
            if not (mat.get('dow2_shader') or '').endswith('_2uv'):
                for c, spec_color in teamcolor.spec_colors.items():
                    # spec colours join the raw (Non-Color) spc sample in the shader's
                    # linear spec path - applied raw, no gamma conversion
                    if f'{c.value}_spec_color' in node.inputs:
                        node.inputs[f'{c.value}_spec_color'].default_value[:3] = spec_color[:3]
                for c, gloss in teamcolor.gloss_values.items():
                    # gloss overlays in gamma space on the raw byte value - no conversion
                    if f'{c.value}_gloss' in node.inputs:
                        node.inputs[f'{c.value}_gloss'].default_value = gloss
            for c in BADGE_PAINT_LAYERS:
                if teamcolor.badge_colors.get(c, None) is None:
                    continue
                if f'badge_{c.value}_color' not in node.inputs:
                    continue
                node.inputs[f'badge_{c.value}_color'].default_value[:3] = srgb_to_overlay_linear(teamcolor.badge_colors[c])


def _is_correct_image_node(node):
    return (
        node.bl_idname == 'ShaderNodeTexImage'
        and node.image is not None
        and not node.image.get('PLACEHOLDER', False)
    )


def _extract_from_groups(mat, result: MaterialInfo, shader_node) -> None:
    """Layer detection by the connections into the shader / teamcolor group inputs."""
    teamcolor_nodes = []
    for link in mat.node_tree.links:
        if link.to_node == shader_node and _is_correct_image_node(link.from_node):
            slot = SHADER_INPUT_SLOTS.get(link.to_socket.name)
            if slot is None:
                continue
            if slot == MaterialLayers.OPACITY and link.from_node.image == result.channel_images.get(MaterialLayers.DIFFUSE):
                continue
            if slot == MaterialLayers.GLOSS and link.from_node.image == result.channel_images.get(MaterialLayers.SPECULAR_MASK):
                continue
            result.channel_images.setdefault(slot, link.from_node.image)
        if (
            link.to_node == shader_node
            and link.to_socket.name == 'teamcolor'
            and link.from_node.bl_idname == 'ShaderNodeGroup'
            and link.from_node.node_tree is not None
            and link.from_node.node_tree.name.startswith('ApplyTeamcolor')
        ):
            teamcolor_nodes.append(link.from_node)

    # the OPACITY image may connect before the diffuse is known; re-check
    opacity = result.channel_images.get(MaterialLayers.OPACITY)
    if opacity is not None and opacity == result.channel_images.get(MaterialLayers.DIFFUSE):
        del result.channel_images[MaterialLayers.OPACITY]
    gloss = result.channel_images.get(MaterialLayers.GLOSS)
    if gloss is not None and gloss == result.channel_images.get(MaterialLayers.SPECULAR_MASK):
        del result.channel_images[MaterialLayers.GLOSS]

    for tc_node in teamcolor_nodes:
        for link in mat.node_tree.links:
            if link.to_node != tc_node or not _is_correct_image_node(link.from_node):
                continue
            socket = link.to_socket.name
            if socket == 'default_color':
                result.teamcolor_images.setdefault(TeamcolorLayers.DEFAULT, link.from_node.image)
            elif socket == 'pattern':
                result.channel_images.setdefault(MaterialLayers.PATTERN, link.from_node.image)
            elif socket == 'badge_color':
                result.teamcolor_images.setdefault(TeamcolorLayers.BADGE, link.from_node.image)
            elif socket == 'banner_color':
                result.teamcolor_images.setdefault(TeamcolorLayers.BANNER, link.from_node.image)
            elif socket == 'badge':
                result.teamcolor_images.setdefault(TeamcolorLayers.BADGE, link.from_node.image)
            elif socket == 'decal_color':
                result.teamcolor_images.setdefault(TeamcolorLayers.DECAL, link.from_node.image)
            elif socket.endswith('_value'):
                try:
                    slot = TeamcolorLayers(socket[:-len('_value')])
                except ValueError:
                    continue
                result.teamcolor_images.setdefault(slot, link.from_node.image)

    if 'roughness_value' in shader_node.inputs:
        result.roughness_value = shader_node.inputs['roughness_value'].default_value
    if 'self_illumination' in shader_node.inputs and not shader_node.inputs['self_illumination'].links:
        result.default_emission_strength = shader_node.inputs['self_illumination'].default_value
    if 'use_self_illumination_color' in shader_node.inputs:
        result.use_self_illumination_color = shader_node.inputs['use_self_illumination_color'].default_value > 0.5
    if 'diffuse' in shader_node.inputs and not shader_node.inputs['diffuse'].links:
        result.default_color = mathutils.Color(shader_node.inputs['diffuse'].default_value[:3]).from_scene_linear_to_srgb()
    if 'specularity' in shader_node.inputs and not shader_node.inputs['specularity'].links:
        result.default_specilar_mask_value = sum(shader_node.inputs['specularity'].default_value[:3]) / 3


def _extract_legacy(mat, result: MaterialInfo, used_nodes: set) -> None:
    """Layer detection for materials predating the shader groups: node labels and
    direct BSDF connections."""
    for node in mat.node_tree.nodes:
        if node.label == 'self_illumination_color_unused' and _is_correct_image_node(node):
            # the retired slot for the DoW1 texture the engine ignores
            result.channel_images.setdefault(MaterialLayers.SELF_ILLUMUNATION_COLOR, node.image)
            result.use_self_illumination_color = False
            used_nodes.add(node)
            continue
        if node.label in MaterialLayers.__members__.values() and _is_correct_image_node(node):
            result.channel_images[MaterialLayers(node.label.lower())] = node.image
            used_nodes.add(node)
    for slot, input_idname in [
        (MaterialLayers.DIFFUSE, 'Base Color'),
        (MaterialLayers.SPECULAR_MASK, 'Specular IOR Level'),
        (MaterialLayers.SPECULAR_MASK, 'Metallic'),
        (MaterialLayers.SELF_ILLUMUNATION_MASK, 'Emission Strength'),
        (MaterialLayers.SELF_ILLUMUNATION_COLOR, 'Emission Color'),
    ]:
        if slot in result.channel_images:
            continue
        for link in mat.node_tree.links:
            if (
                link.to_node.bl_idname == 'ShaderNodeBsdfPrincipled'
                and link.to_socket.identifier == input_idname
                and _is_correct_image_node(link.from_node)
            ):
                if slot not in result.channel_images:
                    result.channel_images[slot] = link.from_node.image
                used_nodes.add(link.from_node)

    teamcolor_node_labels = {
        f'color_layer_{slot.value}'
        for slot in TeamcolorLayers
        if slot not in APPLYABLE_IMAGES
    }
    for node in mat.node_tree.nodes:
        if (
            node.label in teamcolor_node_labels
            and _is_correct_image_node(node)
        ):
            used_nodes.add(node)
            result.teamcolor_images[TeamcolorLayers(node.label[len('color_layer_'):])] = node.image

    for link in mat.node_tree.links:
        if (
            link.to_node.bl_idname == 'ShaderNodeGroup'
            and link.to_node.node_tree == bpy.data.node_groups.get('ApplyTeamcolor', None)
            and link.to_socket.identifier.endswith('_value')
            and _is_correct_image_node(link.from_node)
        ):
            try:
                slot = TeamcolorLayers(link.to_socket.identifier[:-len('_value')])
            except KeyError:
                continue
            if slot not in result.teamcolor_images:
                result.teamcolor_images[slot] = link.from_node.image
                used_nodes.add(link.from_node)

    for node in mat.node_tree.nodes:
        if node.bl_idname == 'ShaderNodeBsdfPrincipled':
            result.roughness_value = node.inputs['Roughness'].default_value
            result.default_specilar_mask_value = node.inputs['Metallic'].default_value
            result.default_emission_strength = node.inputs['Emission Strength'].default_value
            default_color = mathutils.Color(node.inputs['Base Color'].default_value[:3]).from_scene_linear_to_srgb()
            initial_default_color = default_color * (1 + result.default_specilar_mask_value) - WHITE * result.default_specilar_mask_value
            result.default_color = mathutils.Color([max(i, 0) for i in initial_default_color])
            break


def extract_material_info(mat, use_random_diffuse_fallback: bool = True) -> MaterialInfo:
    result = MaterialInfo()
    used_nodes = set()

    shader_node = get_shader_group_node(mat)
    if shader_node is not None:
        result.material_type = MaterialType(shader_node.node_tree.name.split('.')[0])
        _extract_from_groups(mat, result, shader_node)
        used_nodes.update(node for node in mat.node_tree.nodes if _is_correct_image_node(node))
    else:
        _extract_legacy(mat, result, used_nodes)

    has_banner, has_badge = False, False
    for node in mat.node_tree.nodes:
        if _is_correct_image_node(node):
            if node.label == TeamcolorLayers.BADGE:
                used_nodes.add(node)
                has_badge = True
            if node.label == TeamcolorLayers.BANNER:
                used_nodes.add(node)
                has_banner = True
            if node.label in (TeamcolorLayers.BADGE, TeamcolorLayers.BANNER):
                for link in node.inputs['Vector'].links:
                    if link.from_node.bl_idname == 'ShaderNodeMapping':
                        rotation = link.from_node.inputs['Rotation'].default_value[2]
                        setattr(result, f'{node.label}_rotation', rotation)
    for node in mat.node_tree.nodes:
        if node.bl_idname != 'ShaderNodeCombineXYZ':
            continue
        val = node.inputs['X'].default_value, node.inputs['Y'].default_value
        if has_badge:
            if node.label == 'badge_position':
                result.badge_position = val
            if node.label == 'badge_display_size':
                result.badge_size = val
        if has_banner:
            if node.label == 'banner_position':
                result.banner_position = val
            if node.label == 'banner_display_size':
                result.banner_size = val
    if has_badge and result.badge_position is None:
        result.badge_position = 0, 0
    if has_banner and result.banner_position is None:
        result.banner_position = 0, 0

    if use_random_diffuse_fallback and MaterialLayers.DIFFUSE not in result.channel_images:
        for node in mat.node_tree.nodes:
            if node not in used_nodes and _is_correct_image_node(node):
                result.channel_images[MaterialLayers.DIFFUSE] = node.image
                break

    return result


def extract_teamcolor_info(mat) -> TeamcolorInfo:
    teamcolor = TeamcolorInfo()
    color_node_names = {f'color_{i.value}' for i in TEAMCOLORABLE_LAYERS}
    for node in mat.node_tree.nodes:
        if node.bl_idname == 'ShaderNodeValToRGB' and node.label in color_node_names:
            key = TeamcolorLayers(node.label[len('color_'):])
            teamcolor.colors[key] = mathutils.Color(node.color_ramp.elements[-1].color[:3]).from_scene_linear_to_srgb()
        if node.bl_idname == 'ShaderNodeTexImage' and node.label in APPLYABLE_IMAGES:
            teamcolor.images[node.label] = node.image
        if node.bl_idname == 'ShaderNodeGroup' and node.node_tree is not None and node.node_tree.name.startswith('ApplyTeamcolor'):
            for c in TEAMCOLORABLE_LAYERS | {TeamcolorLayers.TERTIARY}:
                if f'{c.value}_color' in node.inputs:
                    teamcolor.colors[c] = mathutils.Color(node.inputs[f'{c.value}_color'].default_value[:3]).from_scene_linear_to_srgb()
            for c in BADGE_PAINT_LAYERS:
                if f'badge_{c.value}_color' in node.inputs:
                    teamcolor.badge_colors[c] = mathutils.Color(node.inputs[f'badge_{c.value}_color'].default_value[:3]).from_scene_linear_to_srgb()
    return teamcolor


def create_placeholder_image() -> Image:
    for i in bpy.data.images:
        if i.name == 'NOT_SET' and i.get('PLACEHOLDER', False):
            return i
    result = bpy.data.images.new('NOT_SET', 1, 1)
    result['PLACEHOLDER'] = True
    result.use_fake_user = True
    return result
