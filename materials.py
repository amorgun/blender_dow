import dataclasses
import enum
import math
import typing

import bpy
import mathutils


@enum.unique
class MaterialLayers(str, enum.Enum):
    DIFFUSE = 'diffuse'
    SPECULAR_MASK = 'specularity'
    SPECULAR_REFLECTION = 'reflection'
    SELF_ILLUMUNATION_MASK = 'self_illumination'
    SELF_ILLUMUNATION_COLOR = 'self_illumination_color'
    OPACITY = 'opacity'


@enum.unique
class TeamcolorLayers(str, enum.Enum):
    PRIMARY = 'primary'
    SECONDARY = 'secondary'
    TRIM = 'trim'
    WEAPONS = 'weapons'
    EYES = 'eyes'
    DIRT = 'dirt'
    DEFAULT = 'default'
    BADGE = 'badge'
    BANNER = 'banner'


TEAMCOLORABLE_LAYERS = {
    TeamcolorLayers.PRIMARY,
    TeamcolorLayers.SECONDARY, 
    TeamcolorLayers.TRIM,
    TeamcolorLayers.WEAPONS,
    TeamcolorLayers.EYES,
}
TEAMCOLOR_IMAGES = {TeamcolorLayers.BADGE, TeamcolorLayers.BANNER}

WHITE = mathutils.Color((1., 1., 1.))

Image = typing.TypeVar('Image')
Color = typing.TypeVar('Color')


@dataclasses.dataclass
class MaterialInfo:
    channel_images: dict[MaterialLayers, Image] = dataclasses.field(default_factory=dict)
    teamcolor_images: dict[TeamcolorLayers, Image] = dataclasses.field(default_factory=dict)
    default_color: Color = None
    default_specilar_mask_value: float = 0
    roughness_value: float = 0
    banner_size: tuple[int, int] = 64, 64
    banner_position: tuple[int, int] = None
    badge_size: tuple[int, int] = 64, 96
    badge_position: tuple[int, int] = None


@dataclasses.dataclass
class TeamcolorInfo:
    colors: dict[TeamcolorLayers, Color] = dataclasses.field(default_factory=dict)
    images: dict[TeamcolorLayers, Image] = dataclasses.field(default_factory=dict)


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


def setup_material(
        mat,
        material_data: MaterialInfo,
        force_teamcolor: bool,
    ):
    mat.blend_method = 'CLIP'  # FIXME it doesn't work now
    mat.show_transparent_back = False
    placehoder_image = create_placeholder_image()

    links = mat.node_tree.links
    node_final = mat.node_tree.nodes[0]
    node_final.inputs['Roughness'].default_value = material_data.roughness_value
    if material_data.roughness_value > 0:
        node_final.inputs['Metallic'].default_value = material_data.default_specilar_mask_value
    if material_data.default_color:
        default_color = (material_data.default_color + material_data.default_specilar_mask_value * WHITE) / (1 + material_data.default_specilar_mask_value)
        node_final.inputs['Base Color'].default_value[:3] = default_color.from_srgb_to_scene_linear()
    node_final.inputs['Specular IOR Level'].default_value = 0
    node_final.location = 150, 400
    mat.node_tree.nodes[1].location = node_final.location[0] + 400, node_final.location[1]

    uv_vector = setup_uv_offset(mat, node_final.location[1] - 1600, node_final.location[1] - 200)

    node_object_info = mat.node_tree.nodes.new('ShaderNodeObjectInfo')
    node_object_info.location = node_final.location[1] - 1600, node_final.location[1]

    has_specular = MaterialLayers.SPECULAR_MASK in material_data.channel_images
    has_legacy_specular = has_specular and MaterialLayers.SPECULAR_REFLECTION in material_data.channel_images
    if has_specular:
        node_calc_spec = mat.node_tree.nodes.new('ShaderNodeMix')
        node_calc_spec.data_type = 'RGBA'
        node_calc_spec.clamp_result = True
        if has_legacy_specular:
            node_calc_spec.inputs['Factor'].default_value = 0
            node_calc_spec.blend_type = 'MIX'
        else:
            node_calc_spec.inputs['Factor'].default_value = 0.5
            node_calc_spec.blend_type = 'ADD'
            node_calc_spec.inputs['A'].default_value = 1, 1, 1, 1
            node_calc_spec.inputs['B'].default_value = 0, 0, 0, 1
        node_calc_spec.label = 'Apply spec'
        node_calc_spec.location = node_final.location[0] - 300, node_final.location[1]

    node_calc_alpha = mat.node_tree.nodes.new('ShaderNodeMath')
    node_calc_alpha.operation = 'MULTIPLY'
    node_calc_alpha.use_clamp = True
    node_calc_alpha.inputs[1].default_value = 1
    node_calc_alpha.location = node_final.location[0] - 300, node_final.location[1] - 250,
    links.new(node_object_info.outputs['Alpha'], node_calc_alpha.inputs[0])
    links.new(node_calc_alpha.outputs[0], node_final.inputs['Alpha'])

    num_created_tex_nodes = 0
    for node_label, inputs in [
        (MaterialLayers.DIFFUSE, [node_calc_spec.inputs['A'] if has_specular else node_final.inputs['Base Color'], node_final.inputs['Emission Color']]),
        (MaterialLayers.SPECULAR_MASK, [node_calc_spec.inputs['Factor']] if has_legacy_specular else [node_calc_spec.inputs['B'], node_final.inputs['Specular Tint']] if has_specular else []),
        (MaterialLayers.SPECULAR_REFLECTION, [node_calc_spec.inputs['B']] if has_legacy_specular else []),
        (MaterialLayers.SELF_ILLUMUNATION_MASK, [node_final.inputs['Emission Strength']]),
        (MaterialLayers.OPACITY, [node_final.inputs['Alpha']]),
        (MaterialLayers.SELF_ILLUMUNATION_COLOR, [node_final.inputs['Emission Color']]),  # TODO Probably emission color
    ]:
        node_image = material_data.channel_images.get(node_label)
        if node_image is None:
            continue
        node_tex = mat.node_tree.nodes.new('ShaderNodeTexImage')
        node_tex.image = node_image
        node_tex.location = -430, node_final.location[1] - 320 * num_created_tex_nodes
        node_tex.label = node_label
        num_created_tex_nodes += 1
        if node_label == MaterialLayers.SPECULAR_REFLECTION:
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
        if node_label in (MaterialLayers.DIFFUSE, MaterialLayers.OPACITY):
            links.new(node_tex.outputs['Alpha'], node_calc_alpha.inputs[1])
        if node_label != MaterialLayers.OPACITY:
            for i in inputs:
                links.new(node_tex.outputs[0], i)
        if node_label == MaterialLayers.SPECULAR_MASK and not has_legacy_specular:
            correct_spec = mat.node_tree.nodes.new('ShaderNodeGamma')
            correct_spec.inputs['Gamma'].default_value = 0.4545
            correct_spec.location = -150, node_tex.location[1] - 200
            links.new(node_tex.outputs[0], correct_spec.inputs['Color'])
            links.new(correct_spec.outputs[0], node_final.inputs['Metallic'])

    if has_specular:
        links.new(node_calc_spec.outputs['Result'], node_final.inputs['Base Color'])

    if force_teamcolor:
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
            links.new(flip_texture_node.outputs[0], scale_node.inputs['Vector'])
            links.new(calc_pos_node.outputs[0], scale_node.inputs['Location'])
            links.new(calc_scale_node.outputs[0], scale_node.inputs['Scale'])

            node_tex = mat.node_tree.nodes.new('ShaderNodeTexImage')
            num_created_tex_nodes += 1
            # node_tex.hide = True
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
            for node in mat.node_tree.nodes:
                if node.label == 'Apply spec':
                    links.new(aply_teamcolor.outputs[0], node.inputs['A'])
                    break
            else:
                links.new(aply_teamcolor.outputs[0], mat.node_tree.nodes[0].inputs['Base Color'])
            links.new(aply_teamcolor.outputs[0], mat.node_tree.nodes[0].inputs['Emission Color'])

def create_teamcolor_group(name: str = 'ApplyTeamcolor'):
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


def apply_teamcolor(mat, teamcolor: TeamcolorInfo):
    color_node_names = {f'color_{i.value}' for i in TEAMCOLORABLE_LAYERS}
    for node in mat.node_tree.nodes:
        if node.bl_idname == 'ShaderNodeValToRGB' and node.label in color_node_names:
            key = TeamcolorLayers(node.label[len('color_'):])
            if teamcolor.colors.get(key) is None:
                continue
            node.color_ramp.elements[-1].color[:3] = teamcolor.colors[key].from_srgb_to_scene_linear()[:3]
        if node.bl_idname == 'ShaderNodeTexImage' and node.label in TEAMCOLOR_IMAGES and teamcolor.images.get(node.label) is not None:
            node.image = teamcolor.images[node.label]
        if node.bl_idname == 'ShaderNodeGroup' and node.node_tree == bpy.data.node_groups.get('ApplyTeamcolor', None):
            for c in TEAMCOLORABLE_LAYERS:
                if teamcolor.colors.get(c, None) is None:
                    continue
                node.inputs[f'{c.value}_color'].default_value[:3] = teamcolor.colors[c].copy().from_srgb_to_scene_linear()  # TODO Investigate why the default color scheme doesn't apply without copy()


def extract_material_info(mat, use_random_diffuse_fallback: bool = True) -> MaterialInfo:
    result = MaterialInfo()
    used_nodes = set()

    def is_correct_image_node(node):
        return (
            node.bl_idname == 'ShaderNodeTexImage'
            and node.image is not None
            and not node.image.get('PLACEHOLDER', False)
        )

    for node in mat.node_tree.nodes:
        if node.label in MaterialLayers.__members__.values() and is_correct_image_node(node):
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
                and is_correct_image_node(link.from_node)
            ):
                if slot not in result.channel_images:
                    result.channel_images[slot] = link.from_node.image
                used_nodes.add(link.from_node)

    teamcolor_node_labels = {
        f'color_layer_{slot.value}' 
        for slot in TeamcolorLayers
        if slot not in (
            TeamcolorLayers.BADGE,
            TeamcolorLayers.BANNER,
        )
    }
    for node in mat.node_tree.nodes:
        if (
            node.label in teamcolor_node_labels
            and is_correct_image_node(node)
        ):
            used_nodes.add(node)
            result.teamcolor_images[TeamcolorLayers(node.label[len('color_layer_'):])] = node.image

    for link in mat.node_tree.links:
        if (
            link.to_node.bl_idname == 'ShaderNodeGroup'
            and link.to_node.node_tree == bpy.data.node_groups.get('ApplyTeamcolor', None)
            and link.to_socket.identifier.endswith('_value')
            and is_correct_image_node(link.from_node)
        ):
            try:
                slot = TeamcolorLayers(link.to_socket.identifier[:-len('_value')])
            except KeyError:
                continue
            if slot not in result.teamcolor_images:
                result.teamcolor_images[slot] = link.from_node.image
                used_nodes.add(link.from_node)

    has_banner, has_badge = False, False
    for node in mat.node_tree.nodes:
        if is_correct_image_node(node):
            if node.label == TeamcolorLayers.BADGE:
                used_nodes.add(node)
                has_badge = True
            if node.label == TeamcolorLayers.BANNER:
                used_nodes.add(node)
                has_banner = True
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

    for node in mat.node_tree.nodes:
        if node.bl_idname == 'ShaderNodeBsdfPrincipled':
            result.roughness_value = node.inputs['Roughness'].default_value
            result.default_specilar_mask_value = node.inputs['Metallic'].default_value
            default_color = mathutils.Color(node.inputs['Base Color'].default_value[:3]).from_scene_linear_to_srgb()
            initial_default_color = default_color * (1 + result.default_specilar_mask_value) - WHITE * result.default_specilar_mask_value
            result.default_color = mathutils.Color([max(i, 0) for i in initial_default_color])
            break

    if use_random_diffuse_fallback and MaterialLayers.DIFFUSE not in result.channel_images:
        for node in mat.node_tree.nodes:
            if node not in used_nodes and is_correct_image_node(node):
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
        if node.bl_idname == 'ShaderNodeTexImage' and node.label in TEAMCOLOR_IMAGES:
            teamcolor.images[node.label] = node.image
        if node.bl_idname == 'ShaderNodeGroup' and node.node_tree == bpy.data.node_groups.get('ApplyTeamcolor', None):
            for c in TEAMCOLORABLE_LAYERS:
                teamcolor.colors[c] = mathutils.Color(node.inputs[f'{c.value}_color'].default_value[:3]).from_scene_linear_to_srgb()
    return teamcolor


def create_placeholder_image() -> Image:
    for i in bpy.data.images:
        if i.name == 'NOT_SET' and i.get('PLACEHOLDER', False):
            return i
    result = bpy.data.images.new('NOT_SET', 1, 1)
    result['PLACEHOLDER'] = True
    result.use_fake_user = True
    return result
