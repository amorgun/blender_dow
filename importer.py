import dataclasses
import io
import pathlib
import math
import tempfile

import bpy
import mathutils

from . import textures, utils, props
from .chunky import ChunkReader, ChunkHeader
from .dow_layout import DowLayout, LayoutPath, DirectoryPath


def open_reader(path: LayoutPath) -> ChunkReader:
    return ChunkReader(io.BytesIO(path.read_bytes()))


@dataclasses.dataclass
class BoneData:  # -- Structure To Hold Bone Data (4, X, 4, 28)
    name: str = None
    parent_idx: int = None
    pos: list[float] = dataclasses.field(default_factory=lambda: [0] * 3)
    rot: list[float] = dataclasses.field(default_factory=lambda: [0] * 4)


@dataclasses.dataclass
class SkinVertice:
    weights: list[float] = dataclasses.field(default_factory=lambda: [0] * 4)
    bone: list[float] = dataclasses.field(default_factory=lambda: [0] * 4)


class WhmLoader:
    TEAMCOLORABLE_LAYERS = {
        textures.TeamcolorLayers.PRIMARY,
        textures.TeamcolorLayers.SECONDARY, 
        textures.TeamcolorLayers.TRIM,
        textures.TeamcolorLayers.WEAPONS,
        textures.TeamcolorLayers.EYES,
    }
    TEAMCOLORABLE_IMAGES = {'badge', 'banner'}

    def __init__(self, root: pathlib.Path, load_wtp: bool = True, stric_mode: bool = True, context=None):
        self.root = root
        self.layout = DowLayout.from_mod_folder(root)
        self.wtp_load_enabled = load_wtp
        self.stric_mode = stric_mode
        self.bpy_context = context
        if self.bpy_context is None:
            self.bpy_context = bpy.context
        self.messages = []

    def _reset(self):
        self.texture_count = 0
        self.loaded_material_paths = set()
        self.loaded_resource_stats = {'attempted': 0, 'errors': 0}
        self.bone_array = []
        self.xref_bone_array = []
        self.blender_mesh_root = None
        self.blender_shadow_mesh_root = None
        self.bone_orig_transform = {}
        self.bone_transform = {}
        self.created_materials = {}
        self.created_meshes = {}
        self.created_cameras = {}
        self.animated_cameras = {}
        self.model_root_collection = None

        self.armature = bpy.data.armatures.new('Armature')
        self.armature_obj = bpy.data.objects.new('Armature', self.armature)
        self.armature_obj.show_in_front = True

        self.default_image = bpy.data.images.new('NOT_SET', 1, 1)
        self.default_image['PLACEHOLDER'] = True
        self.default_image.use_fake_user = True

    def ensure(self, condition: bool, message: str, level: str = 'WARNING'):
        if self.stric_mode:
            assert condition, message
            return
        if not condition:
            self.messages.append((level, f'Assestion violated: {message}'))
        return condition

    def CH_DATASSHR(self, reader: ChunkReader):  # CH_DATASSHR > - Chunk Handler - Material Data
        material_path = reader.read_str()  # -- Read Texture Path
        self.loaded_resource_stats['attempted'] += 1

        if material_path not in self.loaded_material_paths:
            full_material_path = f'{material_path}.rsh'
            material_data = self.layout.find(full_material_path)
            if not material_data:
                self.messages.append(('WARNING', f'Cannot find material file "{full_material_path}"'))
                self.loaded_resource_stats['errors'] += 1
                return
            material = self.load_rsh(open_reader(material_data), material_path)  # -- create new material
            if self.wtp_load_enabled:
                teamcolor_path = f'{material_path}_default.wtp'
                teamcolor_data = self.layout.find(teamcolor_path)
                if not teamcolor_data:
                    self.messages.append(('INFO', f'Cannot find {teamcolor_path}'))
                else:
                    self.load_wtp(open_reader(teamcolor_data), material_path, material)
            self.loaded_material_paths.add(material_path)

    def load_rsh(self, reader: ChunkReader, material_path: str):
        reader.skip_relic_chunky()
        current_chunk = reader.read_header('FOLDSHRF')  # Skip 'Folder SHRF' Header
        loaded_textures = {}
        material = None
        for current_chunk in reader.iter_chunks():
            match current_chunk.typeid:
                case 'FOLDTXTR': loaded_textures[current_chunk.name.lower()] = self.CH_FOLDTXTR(reader, current_chunk.name)  # FOLDTXTR - Internal Texture
                case 'FOLDSHDR': material = self.CH_FOLDSHDR(reader, current_chunk, material_path, loaded_textures)
                case _: reader.skip(current_chunk.size)
        return material

    def CH_FOLDSTXT(self, texture_path: str):  # Chunk Handler - Reference to an external rtx
        self.loaded_resource_stats['attempted'] += 1
        full_texture_path = f'{texture_path}.rtx'
        texture_data = self.layout.find(full_texture_path)
        if not texture_data:
            self.messages.append(('WARNING', f'Cannot find texture file "{full_texture_path}"'))
            self.loaded_resource_stats['errors'] += 1
        return self.load_rtx(open_reader(texture_data))

    def load_rtx(self, reader: ChunkReader):
        reader.skip_relic_chunky()
        current_chunk = reader.read_header('FOLDTXTR')
        return self.CH_FOLDTXTR(reader, current_chunk.name)  # FOLDTXTR - Internal Texture

    def CH_FOLDTXTR(self, reader: ChunkReader, texture_path: str):  # Chunk Handler - Internal Texture
        for current_chunk in reader.iter_chunks():
            match current_chunk.typeid:
                case 'DATAHEAD':
                    image_type, num_images = reader.read_struct('<2l')
                case 'DATAINFO':
                    reader.skip(current_chunk.size)
                case 'FOLDIMAG':
                    break
        current_chunk = reader.read_header('DATAATTR')
        image_format, width, height, num_mips = reader.read_struct('<4l')
        current_chunk = reader.read_header('DATADATA')

        texture_name = pathlib.Path(texture_path).name
        with tempfile.TemporaryDirectory() as tmpdir:
            is_tga = image_format in (0, 2)
            if is_tga:
                with open(f'{tmpdir}/{texture_name}.tga', 'wb') as f:
                    textures.write_tga(
                        reader.stream, f, current_chunk.size, width, height)
            else:
                with open(f'{tmpdir}/{texture_name}.dds', 'wb') as f:
                    textures.write_dds(
                        reader.stream, f, current_chunk.size, width, height, num_mips, image_format)
            image = bpy.data.images.load(f.name)
            if is_tga:
                image = utils.flip_image_y(image)
            image.pack()
            image.use_fake_user = True
        return image

    def CH_FOLDSHDR(self, reader: ChunkReader, header: ChunkHeader, material_path: str, loaded_textures: dict):  # Chunk Handler - Material
        reader = reader.read_folder(header)
        channels = []
        for current_chunk in reader.iter_chunks():
            match current_chunk.typeid:
                case 'DATAINFO':
                    num_images, *info_bytes = reader.read_struct('<2L 4B L x')
                case 'DATACHAN':
                    channel_idx, method, *colour_mask = reader.read_struct('<2l4B')
                    channel_texture_name = reader.read_str()
                    num_coords = reader.read_one('<4x l 4x')
                    for _ in range(4):  # always 4, not num_coords
                        for ref_idx in range(4):
                            x, y = reader.read_struct('<2f')
                    channels.append({
                        'idx': channel_idx,
                        'texture_name': channel_texture_name,
                    })

        if material_path in self.created_materials:
            return self.created_materials[material_path]
        material_name = pathlib.Path(material_path).name
        mat = bpy.data.materials.new(name=material_name)
        props.setup_property(mat, 'full_path', material_path)
        mat.blend_method = 'CLIP'  # FIXME it doesn't work now
        mat.show_transparent_back = False
        mat.use_nodes = True
        links = mat.node_tree.links
        node_final = mat.node_tree.nodes[0]
        node_final.inputs['Specular IOR Level'].default_value = 0

        node_uv = mat.node_tree.nodes.new('ShaderNodeTexCoord')
        node_uv.location = -800, 200
        node_uv_offset = mat.node_tree.nodes.new('ShaderNodeMapping')
        node_uv_offset.label = 'UV offset'
        node_uv_offset.name = 'Mapping'
        node_uv_offset.location = -600, 200
        links.new(node_uv.outputs[2], node_uv_offset.inputs['Vector'])

        node_object_info = mat.node_tree.nodes.new('ShaderNodeObjectInfo')
        node_object_info.location = -600, 400

        has_legacy_specular = any(c['idx'] == 2 and c['texture_name'] != '' for c in channels)
        if has_legacy_specular:
            node_calc_spec = mat.node_tree.nodes.new('ShaderNodeMix')
            node_calc_spec.data_type = 'RGBA'
            node_calc_spec.clamp_result = True
            node_calc_spec.inputs[0].default_value = 0
            node_calc_spec.label = 'Apply spec'
            node_calc_spec.location = -150, 400
            links.new(node_calc_spec.outputs['Result'], node_final.inputs['Base Color'])

        node_calc_alpha = mat.node_tree.nodes.new('ShaderNodeMath')
        node_calc_alpha.operation = 'MULTIPLY'
        node_calc_alpha.use_clamp = True
        node_calc_alpha.inputs[1].default_value = 1
        node_calc_alpha.location = -150, 150
        links.new(node_object_info.outputs['Alpha'], node_calc_alpha.inputs[0])
        links.new(node_calc_alpha.outputs[0], node_final.inputs['Alpha'])

        created_tex_nodes = {}
        for channel in channels:
            if (texture_name := channel['texture_name'].lower()) == '':
                continue
            channel_idx = channel['idx']
            inputs, node_label = {
                0: ([node_calc_spec.inputs['A'] if has_legacy_specular else node_final.inputs['Base Color'], node_final.inputs['Emission Color']], textures.MaterialLayers.DIFFUSE),
                1: ([node_calc_spec.inputs['Factor']] if has_legacy_specular else [node_final.inputs['Specular IOR Level'], node_final.inputs['Specular Tint']], textures.MaterialLayers.SPECULAR_MASK),
                2: ([node_calc_spec.inputs['B']] if has_legacy_specular else [], textures.MaterialLayers.SPECULAR_REFLECTION),
                3: ([node_final.inputs['Emission Strength']], textures.MaterialLayers.SELF_ILLUMUNATION),
                4: ([node_final.inputs['Alpha']], textures.MaterialLayers.OPACITY),
                5: [[], None],  # Unused
                6: [[], None],  # TODO Probably emission color
            }[channel_idx]
            if node_label is None:
                continue
            node_tex = created_tex_nodes.get(texture_name)
            if not node_tex:
                node_tex = mat.node_tree.nodes.new('ShaderNodeTexImage')
                node_image = loaded_textures.get(texture_name)
                if node_image is None:
                    self.messages.append(('WARNING', f'Material "{material_name}": cannot find {node_label} texture ("{texture_name}")'))
                    continue
                node_tex.image = node_image
                node_tex.location = -430, 400 - 320 * len(created_tex_nodes)
                node_tex.label = node_label
                created_tex_nodes[texture_name] = node_tex
            if channel_idx == 2:
                node_global_to_camera = mat.node_tree.nodes.new('ShaderNodeVectorTransform')
                node_global_to_camera.convert_to = 'CAMERA'
                node_global_to_camera.location = -600, -200
                links.new(node_uv.outputs[6], node_global_to_camera.inputs['Vector'])
                node_fix_reflect = mat.node_tree.nodes.new('ShaderNodeMapping')
                node_fix_reflect.label = 'Rotate reflection vector'
                node_fix_reflect.vector_type = 'VECTOR'
                node_fix_reflect.inputs['Rotation'].default_value = math.pi, 0, 0
                node_fix_reflect.location = -600, -400
                links.new(node_global_to_camera.outputs[0], node_fix_reflect.inputs['Vector'])
                links.new(node_fix_reflect.outputs[0], node_tex.inputs['Vector'])
            else:
                links.new(node_uv_offset.outputs[0], node_tex.inputs['Vector'])
            if channel_idx in (0, 4):
                links.new(node_tex.outputs['Alpha'], node_calc_alpha.inputs[1])
            if channel_idx != 4:
                for i in inputs:
                    links.new(node_tex.outputs[0], i)

        self.created_materials[material_path] = mat
        return mat

    def load_wtp(self, reader: ChunkReader, material_path: str, material):
        reader.skip_relic_chunky()
        current_chunk = reader.read_header('FOLDTPAT')
        loaded_textures = {}
        current_chunk = reader.read_header('DATAINFO')
        width, height = reader.read_struct('<2L')
        layer_names = {
            0: textures.TeamcolorLayers.PRIMARY,
            1: textures.TeamcolorLayers.SECONDARY,
            2: textures.TeamcolorLayers.TRIM,
            3: textures.TeamcolorLayers.WEAPONS,
            4: textures.TeamcolorLayers.EYES,
            5: textures.TeamcolorLayers.DIRT,
            -1: textures.TeamcolorLayers.DEFAULT,
        }
        material_name = pathlib.Path(material_path).name
        default_image_size = width, height
        badge_data = None
        banner_data = None
        for current_chunk in reader.iter_chunks():
            match current_chunk.typeid:
                case 'DATAPTLD':
                    layer_in, data_size = reader.read_struct('<2L')
                    with tempfile.TemporaryDirectory() as tmpdir:
                        with open(f'{tmpdir}/{material_name}_{layer_names[layer_in].value}.tga', 'wb') as f:
                            textures.write_tga(
                                reader.stream, f, data_size, width, height, grayscale=True)
                        image = bpy.data.images.load(f.name)
                        image = utils.flip_image_y(image)
                        image.pack()
                        image.use_fake_user = True
                        loaded_textures[layer_names[layer_in]] = image
                case 'FOLDIMAG':
                    current_chunk = reader.read_header('DATAATTR')
                    image_format, width, height, num_mips = reader.read_struct('<4L')
                    current_chunk = reader.read_header('DATADATA')
                    layer_in = -1
                    with tempfile.TemporaryDirectory() as tmpdir:
                        with open(f'{tmpdir}/{material_name}_{layer_names[layer_in].value}.tga', 'wb') as f:
                            textures.write_tga(
                                reader.stream, f, current_chunk.size, width, height, grayscale=False)
                        image = bpy.data.images.load(f.name)
                        image = utils.flip_image_y(image)
                        image.pack()
                        image.use_fake_user = True
                        loaded_textures[layer_names[layer_in]] = image
                case 'DATAPTBD':  # badge - 64 by 64
                    badge_data = reader.read_struct('<4f')
                case 'DATAPTBN':  # banner - 96 by 64
                    banner_data = reader.read_struct('<4f')
                case _:
                    self.messages.append(('INFO', f'Unknown .wtp chunk {current_chunk.typeid} ({material_path})'))
                    reader.skip(current_chunk.size)

        links = material.node_tree.links
        common_node_pos_x, common_node_pos_y = -600, 3100
        aply_teamcolor = material.node_tree.nodes.new('ShaderNodeGroup')
        aply_teamcolor.node_tree = self.create_teamcolor_group()
        aply_teamcolor.location = common_node_pos_x + 600, common_node_pos_y - 600
        uf_offset_node = [
            node for node in material.node_tree.nodes
            if node.bl_idname == 'ShaderNodeMapping'
            and node.label == 'UV offset'
        ][0]
        created_tex_nodes = {}
        for layer_name in layer_names.values():
            node_tex = material.node_tree.nodes.new('ShaderNodeTexImage')
            node_pos_x, node_pos_y = common_node_pos_x, common_node_pos_y - 290 * len(created_tex_nodes)
            created_tex_nodes[layer_name] = node_tex
            if layer_name in loaded_textures:
                node_tex.image = loaded_textures[layer_name]
            else:
                node_tex.hide = True
                node_tex.image = self.default_image
            node_tex.location = node_pos_x + 200, node_pos_y
            node_tex.label = f'color_layer_{layer_name.value}'
            links.new(uf_offset_node.outputs[0], node_tex.inputs['Vector'])
            links.new(node_tex.outputs[0], aply_teamcolor.inputs[f'{layer_name.value}_{"value" if layer_name != textures.TeamcolorLayers.DEFAULT else "color"}'])

        img_size_node = material.node_tree.nodes.new('ShaderNodeCombineXYZ')
        img_size_node.inputs['X'].default_value = default_image_size[0]
        img_size_node.inputs['Y'].default_value = default_image_size[1]
        img_size_node.label = 'color_layer_size'
        img_size_node.location = common_node_pos_x - 300, common_node_pos_y - 290 * len(created_tex_nodes) + 200

        flip_texture_node = material.node_tree.nodes.new('ShaderNodeMapping')
        flip_texture_node.label = 'Flip'
        flip_texture_node.location = common_node_pos_x - 450, common_node_pos_y - 290 * len(created_tex_nodes) + 200
        flip_texture_node.inputs['Location'].default_value = (0, 1, 0)
        flip_texture_node.inputs['Scale'].default_value = (1, -1, 1)
        links.new(uf_offset_node.outputs[0], flip_texture_node.inputs['Vector'])

        for layer_name, layer_data in [
            ('badge', badge_data),
            ('banner', banner_data),
        ]:
            if layer_data is None:
                node_name = f'UNUSED_{layer_name}'
                layer_data = 0, 0, 0, 0
                default_image = self.default_image
            else:
                node_name = layer_name
                default_image = None
            node_pos_x, node_pos_y = common_node_pos_x, common_node_pos_y - 290 * len(created_tex_nodes)
            data_pos_node = material.node_tree.nodes.new('ShaderNodeCombineXYZ')
            data_pos_node.inputs['X'].default_value = layer_data[0]
            data_pos_node.inputs['Y'].default_value = layer_data[1]
            data_pos_node.location = node_pos_x - 300, node_pos_y
            data_pos_node.label = f'{layer_name}_position'

            data_size_node = material.node_tree.nodes.new('ShaderNodeCombineXYZ')
            data_size_node.inputs['X'].default_value = layer_data[2]
            data_size_node.inputs['Y'].default_value = layer_data[3]
            data_size_node.location = node_pos_x - 300, node_pos_y - 150
            data_size_node.label = f'{layer_name}_display_size'

            calc_pos_node = material.node_tree.nodes.new('ShaderNodeVectorMath')
            calc_pos_node.operation = 'DIVIDE'
            calc_pos_node.location = node_pos_x - 150, node_pos_y
            links.new(data_pos_node.outputs[0], calc_pos_node.inputs[0])
            links.new(img_size_node.outputs[0], calc_pos_node.inputs[1])

            calc_scale_node = material.node_tree.nodes.new('ShaderNodeVectorMath')
            calc_scale_node.operation = 'DIVIDE'
            calc_scale_node.location = node_pos_x - 150, node_pos_y - 150
            links.new(data_size_node.outputs[0], calc_scale_node.inputs[0])
            links.new(img_size_node.outputs[0], calc_scale_node.inputs[1])

            scale_node = material.node_tree.nodes.new('ShaderNodeMapping')
            scale_node.vector_type = 'TEXTURE'
            scale_node.location = node_pos_x, node_pos_y
            links.new(flip_texture_node.outputs[0], scale_node.inputs['Vector'])
            links.new(calc_pos_node.outputs[0], scale_node.inputs['Location'])
            links.new(calc_scale_node.outputs[0], scale_node.inputs['Scale'])

            node_tex = material.node_tree.nodes.new('ShaderNodeTexImage')
            created_tex_nodes[layer_name] = node_tex
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

        if textures.TeamcolorLayers.DEFAULT in loaded_textures:
            for node in material.node_tree.nodes:
                if node.label == 'Apply spec':
                    links.new(aply_teamcolor.outputs[0], node.inputs['A'])
            links.new(aply_teamcolor.outputs[0], material.node_tree.nodes[0].inputs['Emission Color'])
        else:
            self.messages.append(('WARNING', f'Material {material_path} is missing the default layer'))

    def create_teamcolor_group(self, name: str = 'ApplyTeamcolor'):
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

        for c in sorted(self.TEAMCOLORABLE_LAYERS):
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
        for c in self.TEAMCOLORABLE_IMAGES:
            panel = interface.new_panel(name=c, default_closed=True)
            interface.new_socket(
                name=f'{c}_color',
                in_out='INPUT',
                socket_type='NodeSocketColor',
                parent=panel,
            )
            interface.new_socket(
                name=f'{c}_alpha',
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
        for idx, layer_name in enumerate(sorted(self.TEAMCOLORABLE_LAYERS)):
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

        for layer_name in self.TEAMCOLORABLE_IMAGES:
            idx += 1
            node_mix = group.nodes.new('ShaderNodeMixRGB')
            node_mix.blend_type = 'MIX'
            node_mix.location = common_node_pos_x + 200, common_node_pos_y - 220 * idx
            links.new(group_inputs.outputs[f'{layer_name}_alpha'], node_mix.inputs['Fac'])
            links.new(prev_color_output, node_mix.inputs['Color1'])
            links.new(group_inputs.outputs[f'{layer_name}_color'], node_mix.inputs['Color2'])
            prev_color_output = node_mix.outputs[0]

        group.links.new(prev_color_output, group_outputs.inputs['result'])
        return group

    def CH_DATASKEL(self, reader: ChunkReader, xref: bool):  # Chunk Handler - Skeleton Data
        # ---< READ BONES >---

        num_bones = reader.read_one('<l') # -- Read Number Of Bones
        bone_array = self.xref_bone_array if xref else self.bone_array
        for _ in range(num_bones):  # -- Read Each Bone Data
            bone = BoneData()  # -- Reset Bonedata Structure
            bone.name = reader.read_str()  # -- Read Bone Name
            bone.parent_idx = reader.read_one('<l')  # -- Read Bone Hierarchy Level
            bone.pos = reader.read_struct('<3f')  # -- Read Bone X, Y and Z Positions
            bone.rot = reader.read_struct('<4f')  # -- Read Bone X, Y, Z and W Rotation
            bone_array.append(bone)  #-- Add Bone To Bone Array

        if xref:
            return

        # ---< CREATE BONES >---
        self.bpy_context.view_layer.objects.active = self.armature_obj
        bpy.ops.object.mode_set(mode='EDIT', toggle=True)
        bone_collection = self.armature.collections.new('Skeleton')
        bone_transforms = []
        created_bones_array = []

        for bone_idx, bone in enumerate(bone_array):  # -- read each bone data
            # ---< CREATE BONE >---

            new_bone = self.armature.edit_bones.new(bone.name)  # -- Create Bone and Set Name
            new_bone.head = (0, 0, 0)
            new_bone.tail = (0.5, 0, 0)
            new_bone.inherit_scale = 'NONE'  # -- Stretch Off
            bone_collection.assign(new_bone)
            orig_transform = mathutils.Matrix.LocRotScale(
                mathutils.Vector([-bone.pos[0], bone.pos[1], bone.pos[2]]),
                mathutils.Quaternion([bone.rot[3], bone.rot[0], -bone.rot[1], -bone.rot[2]]),  # Mirror along the X-axis. See https://stackoverflow.com/a/33999726
                None,
            )

            # ---< LINK BONE >---

            if bone.parent_idx != -1:
                new_bone.parent = created_bones_array[bone.parent_idx]
            
            created_bones_array.append(new_bone)  # -- Add New Bone To Created Bones Array

            # ---< POSITION & ROTATION >---

            if bone.parent_idx != -1:
                parent_mat = bone_transforms[bone.parent_idx]
            else:
                parent_mat = mathutils.Matrix.Rotation(math.radians(90.0), 4, 'X')
            bone_transform = parent_mat @ orig_transform
            new_bone.matrix = bone_transform @ mathutils.Matrix.Rotation(math.radians(-90.0), 4, 'Z')
            self.bone_orig_transform[bone.name] = orig_transform
            self.bone_transform[bone.name] = bone_transform
            bone_transforms.append(bone_transform)

        for bone in created_bones_array:
            if len(bone.children) == 1:
                new_length = (bone.children[0].head - bone.head).length
                if new_length > 1e-3:
                    bone.length = new_length
        bpy.ops.object.mode_set(mode='EDIT', toggle=True)

    def CH_FOLDMSGR(self, reader: ChunkReader):  # Chunk Handler - Mesh Data
        for current_chunk in reader.iter_chunks():                                # Read FOLDMSLC Chunks
            match current_chunk.typeid:
                case "FOLDMSLC": self.CH_FOLDMSLC(reader, current_chunk.name, False)  # 	 - Mesh Data
                case "DATADATA": self.CH_DATADATA(reader)                             # -- DATADATA - Mesh List
                case "DATABVOL":                                                      # -- DATABVOL - Unknown
                    bbox_flag, *bbox_center = reader.read_struct('<b3f')
                    bbox_size = reader.read_struct('<3f')
                    bbox_rot_mat = reader.read_struct('<9f')
                    return True

    def CH_DATAMARK(self, reader: ChunkReader):
        self.bpy_context.view_layer.objects.active = self.armature_obj
        bpy.ops.object.mode_set(mode='EDIT', toggle=True)
        bone_collection = self.armature.collections.new('Markers')

        coord_transform = mathutils.Matrix([[-1, 0, 0], [0, 1, 0], [0, 0, 1]]).to_4x4()
        coord_transform_inv = coord_transform.inverted()

        num_markers = reader.read_one('<l')  # -- Read Number Of Markers
        for i in range(num_markers):  # -- Read All Markers
            marker_name = reader.read_str()  # -- Read Marker Name
            parent_name = reader.read_str()  # -- Read Parent Name
            rot = mathutils.Matrix().to_3x3()
            for row_idx in range(3):  # -- Read Matrix
                rot[row_idx][:3] = reader.read_struct('<3f')
            pos = reader.read_struct('<3f')

            transform = coord_transform_inv @ mathutils.Matrix.LocRotScale(
                mathutils.Vector(pos),
                rot.transposed(),
                None,
            ) @ coord_transform

            marker = self.armature.edit_bones.new(marker_name)  # -- Create Bone and Set Name
            marker.head = (0, 0, 0)
            marker.tail = (0.15, 0, 0)
            bone_collection.assign(marker)
            marker.color.palette = 'CUSTOM'
            marker.color.custom.normal = mathutils.Color([14, 255, 2]) / 255  # -- Set Color Of New Marker
            marker.color.custom.active = mathutils.Color([255, 98, 255]) / 255

            if marker_name in self.armature.bones:
                continue  # FIXME
            self.ensure(marker_name not in self.armature.bones, f'Marker "{marker_name}": name collision with a bone')

            parent = self.armature.edit_bones.get(parent_name)
            if parent is None:
                if parent_name.strip():
                    self.messages.append(('WARNING', f'Marker "{marker_name}" is attached to non-existent bone "{parent_name}"'))
                parent_mat = mathutils.Matrix.Rotation(math.radians(90.0), 4, 'X')
            else:
                marker.parent = parent  # -- Set Parent Of New Marker
                parent_mat = self.bone_transform[parent_name]
            marker.matrix = parent_mat @ transform
            self.bone_transform[marker_name] = parent_mat @ transform
        bpy.ops.object.mode_set(mode='EDIT', toggle=True)

        custom_shape_template = bpy.data.objects.new('marker_custom_shape_template', None)
        custom_shape_template.empty_display_type = 'ARROWS'
        custom_shape_template.use_fake_user = True
        for bone in bone_collection.bones:
            pose_bone = self.armature_obj.pose.bones[bone.name]
            pose_bone.custom_shape = custom_shape_template
            pose_bone.custom_shape_scale_xyz = -1, 1, 1

    def CH_DATACAMS(self, reader: ChunkReader):
        cameras_collection = bpy.data.collections.new('Cameras')
        self.model_root_collection.children.link(cameras_collection)

        coord_transform = mathutils.Matrix([[-1, 0, 0], [0, 0, 1], [0, -1, 0]]).to_4x4()
        world_rot = (
            mathutils.Matrix.Rotation(math.radians(180.0), 4, 'Y').to_quaternion()
            @ mathutils.Matrix.Rotation(math.radians(90.0), 4, 'X').to_quaternion()
        )
        coord_transform_inv = coord_transform.inverted()

        num_cams = reader.read_one('<l')
        for _ in range(num_cams):
            cam_name = reader.read_str()
            pos = reader.read_struct('<3f')
            rot = reader.read_struct('<4f')
            fov, clip_start, clip_end = reader.read_struct('<3f')
            focus_point = reader.read_struct('<3f')

            transform = coord_transform_inv @ mathutils.Matrix.LocRotScale(
                mathutils.Vector(pos),
                mathutils.Quaternion([rot[3], *rot[:3]]) @ world_rot,
                None,
            ) @ coord_transform

            focus_obj = bpy.data.objects.new(f'{cam_name}_focus', None)
            cameras_collection.objects.link(focus_obj)
            focus_obj.matrix_basis = mathutils.Matrix.Translation([-focus_point[0], -focus_point[2], focus_point[1]])
            focus_obj.empty_display_type = 'SPHERE'

            cam = bpy.data.cameras.new(cam_name)
            cam.clip_start, cam.clip_end = clip_start, clip_end

            cam.dof.use_dof = True
            cam.dof.focus_object = focus_obj
            cam.lens_unit = 'FOV'
            cam.angle = 2 * math.pi - 4 * math.atan(math.pi / 9 + 2.14 / fov)  # magic

            cam_obj = bpy.data.objects.new(cam_name, cam)
            cam_obj.matrix_basis = transform
            cam_obj.rotation_mode = 'QUATERNION'

            cameras_collection.objects.link(cam_obj)
            self.created_cameras[cam_name] = cam_obj

    def CH_FOLDANIM(self, reader: ChunkReader):  # Chunk Handler - Animations
        # ---< DATADATA >---

        current_chunk = reader.read_header('DATADATA')

        animation_name = current_chunk.name
        num_frames = reader.read_one('<l')  # -- Read Number Of Frames
        duration = reader.read_one('<f')  # num_frames / fps
        fps = num_frames / duration

        if animation_name in bpy.data.actions:
            animation = bpy.data.actions[animation_name]
        else:
            animation = bpy.data.actions.new(name=animation_name)
        animation.use_fake_user = True

        animation.frame_range = 0, num_frames - 1  # -- Set Start & End Frames
        props.setup_property(animation, 'fps', fps)

        def create_animation_data(obj, slot):
            if obj.animation_data is None:
                obj.animation_data_create()
            obj.animation_data.action = animation
            obj.animation_data.action_slot = slot

        # ---< BONES >---

        num_bones = reader.read_one('<l')  # -- Read Number Of Bones
        if num_bones:
            create_animation_data(self.armature_obj, animation.slots.new(id_type='OBJECT', name='Skeleton'))
        for bone_idx in range(num_bones):  # -- Read Bones
            bone_name = reader.read_str()  # -- Read Bone Name
            bone = self.armature_obj.pose.bones.get(bone_name)
            if bone is None:
                self.messages.append(('WARNING', f'Animation "{animation_name}" uses unknown bone "{bone_name}"'))
            else:
                orig_transform = self.bone_orig_transform[bone_name]

            delta = mathutils.Matrix.Rotation(math.radians(-90.0), 4, 'Z').to_4x4()
            keys_pos = reader.read_one('<l')  # -- Read Number Of Postion Keys
            for _ in range(keys_pos):  # -- Read Postion Keys
                frame = reader.read_one('<f') * (num_frames - 1)  # -- Read Frame Number
                x, y, z = reader.read_struct('<3f')  # -- Read Position
                new_transform = mathutils.Matrix.Translation(mathutils.Vector([-x, y, z]))
                if bone is None:
                    continue
                new_mat = delta.inverted() @ orig_transform.inverted() @ new_transform @ delta
                loc, *_ = new_mat.decompose()
                bone.location = loc
                self.armature_obj.keyframe_insert(data_path=f'pose.bones["{bone_name}"].location', frame=frame, group=bone_name)

            keys_rot = reader.read_one('<l')  # -- Read Number Of Rotation Keys
            if bone is not None:
                orig_rot = self.bone_orig_transform[bone_name].to_quaternion()  # FIXME
                delta = delta.to_quaternion()
                bone.matrix_basis = mathutils.Matrix()
            for _ in range(keys_rot):
                frame = reader.read_one('<f') * (num_frames - 1)  # -- Read Frame Number
                key_rot = reader.read_struct('<4f')  # -- Read Rotation X, Y, Z, W
                new_transform = mathutils.Quaternion([key_rot[3], key_rot[0], -key_rot[1], -key_rot[2]])
                if bone is None:
                    continue
                new_rot = delta.inverted() @ orig_rot.inverted() @ new_transform @ delta
                new_rot.make_compatible(bone.rotation_quaternion)  # Fix random axis flipping
                bone.rotation_quaternion = new_rot
                self.armature_obj.keyframe_insert(data_path=f'pose.bones["{bone_name}"].rotation_quaternion', frame=frame, group=bone_name)
            stale = not reader.read_one('<b')  # -- Read Stale Property
            # if stale == 0 then setUserProp bone "Stale" "Yes"											-- Set Stale Property
            if stale and bone is not None:
                # bone.dow_settings.stale = stale
                props.setup_property(bone, 'stale', True)
                self.armature_obj.keyframe_insert(data_path=f'pose.bones["{bone_name}"]["stale"]', frame=0, group=bone_name)

        # ---< MESHES & TEXTURES >---

        visible_meshes = set()
        num_meshes = reader.read_one('<l')  # -- Read Number Of Meshes

        for i in range(num_meshes):
            obj_name = reader.read_str()  # -- Read Mesh Name
            mode = reader.read_one('<l')
            if mode == 2:  # -- Mesh
                reader.skip(8)  # -- Skip 8 Bytes (Unknown, zeros)
                keys_vis = reader.read_one('<l') - 1  # -- Read Number Of Visibility Keys
                reader.skip(4)  # -- Skip 4 Bytes (Unknown, zeros)
                force_invisible = reader.read_one('<f') == 0  #-- Read ForceInvisible Property
                is_invisible = False
                if force_invisible:
                    if obj_name not in visible_meshes:
                        is_invisible = True
                else:
                    visible_meshes.add(obj_name)
                mesh = self.created_meshes[obj_name.lower()]
                create_animation_data(mesh, animation.slots.new(id_type='OBJECT', name=obj_name))

                props.setup_property(mesh, 'force_invisible')
                mesh['force_invisible'] = is_invisible
                mesh.keyframe_insert(data_path='["force_invisible"]', frame=0)
                for j in range(keys_vis):  # -- Read Visibility Keys
                    frame = reader.read_one('<f') * (num_frames - 1)  # -- Read Frame Number
                    key_vis = reader.read_one('<f')  # -- Read Visibility
                    mesh.color[3] = key_vis
                    if j == 0:
                        mesh.keyframe_insert(data_path='color', frame=0, index=3)
                    mesh.keyframe_insert(data_path='color', frame=frame, index=3)
                mesh.color[3] = 1.0
            elif mode == 0:  # -- Texture
                reader.skip(4)  # -- Skip 4 Bytes (Unknown, zeros)
                tex_anim_type = reader.read_one('<l')  # -- 1-U 2-V 3-TileU 4-TileV
                keys_tex = reader.read_one('<l')  # -- Read Number Of Texture Keys
                material = self.created_materials.get(obj_name)
                if material is not None:
                    mapping_node = material.node_tree.nodes['Mapping']
                    create_animation_data(material.node_tree, animation.slots.new(id_type='NODETREE', name=material.name))
                else:
                    self.messages.append(('WARNING', f'Cannot find loaded material "{obj_name}"'))
                for j in range(keys_tex):  # -- Read Texture Keys
                    frame = reader.read_one('<f') * (num_frames - 1)  # -- Read Frame Number
                    key_tex = reader.read_one('<f')
                    if material is None:
                        continue
                    match tex_anim_type:
                        case 1:
                            mapping_node.inputs[1].default_value[0] = key_tex
                            material.node_tree.keyframe_insert(data_path='nodes["Mapping"].inputs[1].default_value', frame=frame, index=0)
                        case 2:
                            mapping_node.inputs[1].default_value[1] = -key_tex
                            material.node_tree.keyframe_insert(data_path='nodes["Mapping"].inputs[1].default_value', frame=frame, index=1)
                        case 3:
                            self.messages.append(('INFO', 'TEST UV_TILING 1'))
                            mapping_node.inputs[3].default_value[0] = -key_tex
                            material.node_tree.keyframe_insert(data_path='nodes["Mapping"].inputs[3].default_value', frame=frame, index=0)
                        case 4:
                            self.messages.append(('INFO', 'TEST UV_TILING 2'))
                            mapping_node.inputs[3].default_value[1] = -key_tex
                            material.node_tree.keyframe_insert(data_path='nodes["Mapping"].inputs[3].default_value', frame=frame, index=1)
        # ---< CAMERA >---

        if current_chunk.version >= 2:  # -- Read Camera Data If DATADATA Chunk Version 2

            coord_transform = mathutils.Matrix([[-1, 0, 0], [0, 0, 1], [0, -1, 0]]).to_4x4()
            world_rot = (
                mathutils.Matrix.Rotation(math.radians(180.0), 4, 'Y')
                @ mathutils.Matrix.Rotation(math.radians(90.0), 4, 'X')
            )
            coord_transform_inv = coord_transform.inverted()

            num_cams = reader.read_one('<l')  # -- Read Number Of Cameras
            for cam_idx in range(num_cams):  # -- Read Cameras
                cam_name = reader.read_str()  # -- Read Camera Name
                camera_obj = self.created_cameras.get(cam_name)
                create_animation_data(camera_obj, animation.slots.new(id_type='OBJECT', name=cam_name))
                cam_pos_keys = reader.read_one('<l')  # -- Read Number Of Camera Position Keys
                for _ in range(cam_pos_keys):
                    frame = reader.read_one('<f') * (num_frames - 1)  # -- Read Frame Number
                    pos = reader.read_struct('<3f')
                    if camera_obj is None:
                        continue
                    transform = (
                        coord_transform_inv
                        @ mathutils.Matrix.Translation(mathutils.Vector(pos))
                        @ coord_transform
                    )
                    loc, *_ = transform.decompose()
                    camera_obj.location = loc
                    camera_obj.keyframe_insert(data_path='location', frame=frame)

                cam_rot_keys = reader.read_one('<l')  # -- Read Number Of Camera Rotation Keys
                for _ in range(cam_rot_keys):
                    frame = reader.read_one('<f') * (num_frames - 1)  # -- Read Frame Number
                    key_rot = reader.read_struct('<4f')
                    if camera_obj is None:
                        continue
                    transform = (
                        coord_transform_inv
                        @ mathutils.Quaternion([key_rot[3], *key_rot[:3]]).to_matrix().to_4x4()
                        @ world_rot
                        @ coord_transform
                    )
                    new_rot = transform.to_quaternion()
                    new_rot.make_compatible(camera_obj.rotation_quaternion)  # Fix random axis flipping
                    camera_obj.rotation_quaternion = new_rot
                    camera_obj.keyframe_insert(data_path='rotation_quaternion', frame=frame)

       # ---< DATAANBV >---

        current_chunk = reader.read_header('DATAANBV')
        reader.skip(current_chunk.size)  # -- Skip DATAANBV Chunk

    def CH_FOLDMSLC(self, reader: ChunkReader, mesh_name: str, xref: bool, group_name: str = None):  # Chunk Handler - FOLDMSGR Sub Chunk - Mesh Data
        #------------------------
        #---[ READ MESH DATA ]---
        #------------------------

        #---< DATADATA CHUNK >---

        bone_array = self.xref_bone_array if xref else self.bone_array

        current_chunk = reader.read_header('DATADATA')
        rsv0_a, flag, num_polygons, rsv0_b = reader.read_struct('<l b l l') # -- skip 13 bytes (unknown)
        self.ensure(flag == 1, f'Mesh "{mesh_name}": {flag=}', level='INFO')
        self.ensure(rsv0_a == 0 and rsv0_b == 0, f'Mesh "{mesh_name}": {rsv0_a=} {rsv0_b=}', level='INFO')
        num_skin_bones = reader.read_one('<l')  # -- get number of bones mesh is weighted to

        #---< SKIN BONES >---

        idx_to_bone_name = {}
        for _ in range(num_skin_bones):
            bone_name = reader.read_str()  # -- read bone name
            bone_idx = reader.read_one('<L')
            idx_to_bone_name[bone_idx] = bone_name

        #---< VERTICES >---

        num_vertices = reader.read_one('<l')  # -- read number of vertices
        vertex_size_id = reader.read_one('<l')  # 37 or 39
        self.ensure((num_skin_bones != 0) * 2 == vertex_size_id - 37, f'Mesh "{mesh_name}": {num_skin_bones=} and {vertex_size_id=}')

        vert_array = []       # -- array to store vertex data
        for _ in range(num_vertices):
            x, z, y = reader.read_struct('<3f')
            vert_array.append((-x, -y, z))

        #---< SKIN >---

        skin_vert_array = []  # -- array to store skin vertices
        if num_skin_bones:
            skin_data_warn = False
            for _ in range(num_vertices):
                skin_vert = SkinVertice()  # -- Reset Structure
                skin_vert.weights[:3] = reader.read_struct('<3f')  # -- Read 1st, 2nd and 3rd Bone Weight
                skin_vert.weights[3] = 1 - sum(skin_vert.weights[:3])  # -- Calculate 4th Bone Weight

                # -- Read Bones
                for bone_slot in range(4):
                    bone_idx = reader.read_one('<B')
                    if bone_idx == 255:
                        skin_vert.bone[bone_slot] = None
                        continue
                    bone_name = idx_to_bone_name.get(bone_idx)
                    if bone_name is None:
                        if bone_idx >= len(bone_array):
                            if not skin_data_warn:
                                self.messages.append(('WARNING', f'Mesh "{mesh_name}": bone index {bone_idx} (slot {bone_slot}) is out of range ({len(bone_array) - 1})'))
                                skin_data_warn = True
                            skin_vert.bone[bone_slot] = None
                            continue
                        bone_name = bone_array[bone_idx].name
                    skin_vert.bone[bone_slot] = bone_name

                # -- Add Vertex To Array
                skin_vert_array.append(skin_vert)

        #---< NORMALS >---

        normal_array = []     # -- array to store normal data
        for _ in range(num_vertices):
            x, z, y = reader.read_struct('<3f')
            normal_array.append(mathutils.Vector([-x, -y, z]))

        #---< UVW MAP >---

        face_array = []       # -- array to store face data
        uv_array = []        # -- array to store texture coordinates
        for _ in range(num_vertices):
            u, v = reader.read_struct('<2f')
            uv_array.append([u, 1 - v])

        #-- skip to texture path
        unk_bytes = reader.read_struct('<4B')  # -- skip 4 bytes (unknown, zeros)
        self.ensure(not any(unk_bytes), f'Mesh "{mesh_name}": unexpected non-zero data: {unk_bytes}', level='INFO')

        #---< MATERIALS >---

        num_materials = reader.read_one('<l')  # -- read number of materials
        materials = []
        matid_array = []      # -- array to store material id's
        
        #-- read materials
        for _ in range(num_materials):
            texture_path = reader.read_str()  # -- read texture path
            material = self.created_materials.get(texture_path)
            if material is not None:
                materials.append(material)

            #-- read number of faces connected with this material
            num_faces = reader.read_one('<l') // 3  # -- faces are given as a number of vertices that makes them - divide by 3

            #-- read faces connected with this material
            mat_faces = []
            for __ in range(num_faces):
                x, z, y = reader.read_struct('<3H')
                mat_faces.append((x, y, z))
                if material:
                    matid_array.append(len(materials) - 1)
                else:
                    matid_array.append(0)  # Default material
            face_array.extend(mat_faces)
            # -- Skip 8 Bytes To Next Texture Name Length. 4 data bytes + 4 zeros
            data_min_vertex_idx, data_vertex_cnt, bytes_zero = reader.read_struct('<2Hl')
            real_min_vertex_idx = min((i for t in mat_faces for i in t), default=0)
            real_vertex_cnt = max((i for t in mat_faces for i in t), default=0) + 1 - real_min_vertex_idx
            self.ensure(bytes_zero == 0, f'Mesh "{mesh_name}:{texture_path}" has non-zero flags: {bytes_zero}', level='INFO')
            self.ensure(data_min_vertex_idx == real_min_vertex_idx, f'Mesh "{mesh_name}:{texture_path}" min_vertex_idx: {data_min_vertex_idx} != {real_min_vertex_idx}')
            self.ensure(data_vertex_cnt == real_vertex_cnt, f'Mesh "{mesh_name}:{texture_path}" vertex_cnt: {data_vertex_cnt} != {real_vertex_cnt}')

        self.ensure(num_polygons == len(face_array), f'Mesh "{mesh_name}": {num_polygons} != {len(face_array)}')

        #---< SHADOW VOLUME >---

        num_shadow_vertices = reader.read_one('<L')  # -- zero is ok
        shadow_vertices = []
        for _ in range(num_shadow_vertices):
            x, z, y = reader.read_struct('<3f')
            shadow_vertices.append((-x, -y, z))

        num_shadow_faces = reader.read_one('<L')  # -- zero is ok
        shadow_faces = []
        shadow_face_normals = []
        for _ in range(num_shadow_faces):
            norm_x, norm_z, norm_y, x, z, y = reader.read_struct('<3f3L')
            shadow_faces.append((x, y, z))
            shadow_face_normals.append((-norm_x, -norm_y, norm_z))

        num_shadow_edges = reader.read_one('<L')  # -- zero is ok
        shadow_edges = []
        for _ in range(num_shadow_edges):
            # vert1, vert2, face1, face2, vert_pos1, vert_pos2
            shadow_edges.append(reader.read_struct('<4L6f'))

        #---< DATABVOL CHUNK >---

        current_chunk = reader.read_header('DATABVOL')
        bbox_flag, *bbox_center = reader.read_struct('<b3f')
        bbox_size = reader.read_struct('<3f')
        bbox_rot_mat = reader.read_struct('<9f')

        #---------------------
        #---[ CREATE MESH ]---
        #---------------------

        #---< CREATE MESH >---

        new_mesh = bpy.data.meshes.new(mesh_name)
        new_mesh.from_pydata(vert_array, [], face_array)  # -- Create New Mesh

        # TODO capture output
        # Note: redirect_stdout doesn't work. See https://eli.thegreenplace.net/2015/redirecting-all-kinds-of-stdout-in-python/
        has_errors = new_mesh.validate(verbose=True)

        if has_errors:
            self.messages.append(('WARNING', f'Mesh {mesh_name} has some errors'))

        #---< MESH PROPERTIES >---

        #new_mesh.wireColor = (color 28 89 177)												-- Set Color (Blue)
        for p in new_mesh.polygons:
            p.use_smooth = True
        new_mesh.normals_split_custom_set_from_vertices(normal_array)

        for mat in materials:  # -- Set Material
            new_mesh.materials.append(mat)
        
        new_mesh.polygons.foreach_set('material_index', matid_array)

        obj = bpy.data.objects.new(mesh_name, new_mesh)
        obj.parent = self.armature_obj
        self.created_meshes[mesh_name.lower()] = obj

        #---< SET BONE MESH >---

        vertex_groups = {}
        for vert_idx, vert in enumerate([] if xref else skin_vert_array):
            for bone_weight, bone_name in zip(vert.weights, vert.bone):
                if bone_name is None or bone_weight == 0:
                    continue
                vertex_group =  vertex_groups.get(bone_name)
                if vertex_group is None:
                    vertex_group = vertex_groups[bone_name] = obj.vertex_groups.new(name=bone_name)
                vertex_group.add([vert_idx], bone_weight, 'REPLACE')

        #---< UV MAP >---

        uv_layer = new_mesh.uv_layers.new()
        
        # From https://blenderartists.org/t/importing-uv-coordinates/595872/5

        #initialize an empty list
        per_loop_list = [0.0] * len(new_mesh.loops)

        for loop in new_mesh.loops:
            per_loop_list[loop.index] = uv_array[loop.vertex_index]

        # flattening
        per_loop_list = [uv for pair in per_loop_list for uv in pair]

        uv_layer.data.foreach_set('uv', per_loop_list)  # -- Set UVW Coordinates

        if self.blender_mesh_root is None:
            self.blender_mesh_root = bpy.data.collections.new('Meshes')
            self.model_root_collection.children.link(self.blender_mesh_root)
        if group_name:
            extra_collection = bpy.data.collections.get(group_name)
            if extra_collection is None:
                extra_collection = bpy.data.collections.new(group_name)
                self.model_root_collection.children.link(extra_collection)
            extra_collection.objects.link(obj)
        armature_mod = obj.modifiers.new('Skeleton', 'ARMATURE')
        armature_mod.object = self.armature_obj
        self.blender_mesh_root.objects.link(obj)

        if shadow_faces:
            shadow_mesh_name = f'{mesh_name}_shadow'
            shadow_mesh = bpy.data.meshes.new(shadow_mesh_name)
            shadow_mesh.from_pydata(shadow_vertices, [i[:2] for i in shadow_edges], shadow_faces)
            for face, expected_normal in zip(shadow_mesh.polygons, shadow_face_normals):
                if face.normal.dot(expected_normal) < 0:
                    face.flip()

            shadow_obj = bpy.data.objects.new(shadow_mesh_name, shadow_mesh)
            if self.blender_shadow_mesh_root is None:
                self.blender_shadow_mesh_root = bpy.data.collections.new('Shadows')
                self.model_root_collection.children.link(self.blender_shadow_mesh_root)
                layer_collection = bpy.context.view_layer.layer_collection.children[self.model_root_collection.name].children['Shadows']
                layer_collection.hide_viewport = True
            shadow_armature_mod = shadow_obj.modifiers.new('Skeleton', 'ARMATURE')
            shadow_armature_mod.object = self.armature_obj
            self.blender_shadow_mesh_root.objects.link(shadow_obj)
            obj.dow_shadow_mesh = shadow_obj
        return obj

    def CH_DATADATA(self, reader: ChunkReader):  # - Chunk Handler - Sub Chunk Of FOLDMSGR - Mesh List
        num_meshes = reader.read_one('<l')  # -- Read Number Of Meshes
        loaded_messages = set()
        for i in range(num_meshes):  # -- Read Each Mesh
            mesh_name = reader.read_str()  # -- Read Mesh Name
            mesh_path: pathlib.Path = pathlib.Path(reader.read_str())  # -- Read Mesh Path
            if mesh_path and mesh_path != pathlib.Path(''):
                self.loaded_resource_stats['attempted'] += 1
                filename = mesh_path.with_suffix('.whm')
                file_data = self.layout.find(filename)
                if file_data:
                    if mesh_path not in loaded_messages:
                        loaded_messages.add(mesh_path)
                        self.messages.append(('INFO', f'Loading {mesh_path}'))
                    xreffile = open_reader(file_data)
                    xreffile.skip_relic_chunky()
                    chunk = xreffile.read_header('DATAFBIF')  # -- Read 'File Burn Info' Header
                    xreffile.skip(chunk.size)  # -- Skip 'File Burn Info' Chunk
                    chunk = xreffile.read_header('FOLDRSGM')	# -- Skip 'Folder SGM' Header
                    group_name = f'xref_{chunk.name}'
                    for current_chunk in xreffile.iter_chunks():  # -- Read Chunks Until End Of File
                        match current_chunk.typeid:
                            case 'DATASSHR': self.CH_DATASSHR(xreffile)  # -- DATASSHR - Texture Data
                            case 'DATASKEL': self.CH_DATASKEL(xreffile, xref=True)  # -- FOLDMSLC - Skeleton Data
                            case 'FOLDMSGR':  # -- Read FOLDMSLC Chunks
                                for current_chunk in xreffile.iter_chunks():  # -- Read FOLDMSLC Chunks
                                    if current_chunk.typeid == 'FOLDMSLC' and current_chunk.name.lower() == mesh_name.lower():
                                        mesh_obj = self.CH_FOLDMSLC(xreffile, mesh_name, xref=True, group_name=group_name)
                                        props.setup_property(mesh_obj, 'xref_source', str(mesh_path))
                                    else:
                                        xreffile.skip(current_chunk.size)
                                    if current_chunk.typeid == 'DATABVOL':
                                        break
                            # case 'DATAMARK': self.CH_DATAMARK(xreffile)
                            case _: xreffile.skip(current_chunk.size)
                else:
                    self.messages.append(('WARNING', f'Cannot find file {filename}'))
                    self.loaded_resource_stats['errors'] += 1
            mesh_parent_idx = reader.read_one('<l')  # -- Read Mesh Parent
            if mesh_parent_idx != -1:
                mesh = self.created_meshes.get(mesh_name.lower())
                if mesh is None:
                    continue
                bone_name = self.bone_array[mesh_parent_idx].name
                if len(mesh.vertex_groups) == 0:
                    mesh.vertex_groups.new(name=bone_name).add(
                        list(range(len(mesh.data.vertices))), 1.0, 'REPLACE')
                else:
                    self.messages.append(('WARNING', f'Cannot attach "{mesh_name}" to "{bone_name}" because it already weighed to other bones'))
                if (shadow_mesh := mesh.dow_shadow_mesh) is not None:
                    shadow_mesh.vertex_groups.new(name=bone_name).add(
                        list(range(len(shadow_mesh.data.vertices))), 1.0, 'REPLACE')
    
    def load(self, reader: ChunkReader):
        self._reset()
        reader.skip_relic_chunky()
        header = reader.read_header()
        if header.typeid == 'DATAFBIF':  # Read 'File Burn Info' Header
            reader.skip(header.size)       # Skip 'File Burn Info' Chunk
            header = reader.read_header('FOLDRSGM')  # Skip 'Folder SGM' Header
        else:
            assert header.typeid == 'FOLDRSGM', f'Expected FOLDRSGM, got {header.typeid}'
        self.model_root_collection = bpy.data.collections.new(header.name)
        self.bpy_context.scene.collection.children.link(self.model_root_collection)
        self.model_root_collection.objects.link(self.armature_obj)

        internal_textures = {}

        for current_chunk in reader.iter_chunks():  # Read Chunks Until End Of File
            match current_chunk.typeid:
                case "DATASSHR": self.CH_DATASSHR(reader)  # DATASSHR - Texture Data
                case "FOLDSTXT":  # FOLDSTXT - Reference to an external rtx
                    internal_textures[current_chunk.name] = self.CH_FOLDSTXT(current_chunk.name)
                case "FOLDTXTR":  # FOLDTXTR - Internal Texture
                    internal_textures[current_chunk.name] = self.CH_FOLDTXTR(reader, current_chunk.name)
                case "FOLDSHDR":  # FOLDSHDR - Internal Material
                    mat = self.CH_FOLDSHDR(reader, current_chunk, current_chunk.name, internal_textures)
                    props.setup_property(mat, 'internal', True)
                case "DATASKEL": self.CH_DATASKEL(reader, xref=False)  # DATASKEL - Skeleton Data
                case "FOLDMSGR": self.CH_FOLDMSGR(reader)  # FOLDMSGR - Mesh Data
                case "DATAMARK": self.CH_DATAMARK(reader)  # DATAMARK - Marker Data
                case "FOLDANIM": self.CH_FOLDANIM(reader)  # FOLDANIM - Animations
                case "DATACAMS": self.CH_DATACAMS(reader)  # DATACAMS - Cameras
                case _:
                    self.messages.append(('INFO', f'Skipped unknown chunk {current_chunk.typeid}'))
                    reader.skip(current_chunk.size)  # Skipping Chunks By Default

        if self.armature_obj.pose is not None:
            for bone in self.armature_obj.pose.bones:
                bone.matrix_basis = mathutils.Matrix()
        self.armature_obj.hide_set(True)
        for k, _ in self.armature_obj.items():
            if k.startswith(f'visibility{props.SEP}'):
                self.armature_obj[k] = 1.
        for mesh in self.created_meshes.values():
            mesh.color[3] = 1.
        if self.loaded_resource_stats['errors'] == self.loaded_resource_stats['attempted'] > 1:
            self.messages.append(('INFO', f'It looks like no resources were loaded. Make sure the "Mod folder" in the Add-on properties is set correctly'))

    def load_teamcolor(self, path: pathlib.Path | str) -> dict:
        from .slpp import slpp as lua

        with open(path, 'r') as f:
            text = f.read()
            teamcolor = lua.decode(f'{{{text}}}')
        res = {}
        for k in self.TEAMCOLORABLE_LAYERS:
            color = teamcolor.get('UnitCustomization', {}).get(k.title())
            if color:
                res[k] = mathutils.Color([color[i] / 255. for i in 'rgb'])
        for k in self.TEAMCOLORABLE_IMAGES:
            path = teamcolor.get('LocalInfo', {}).get(f'{k}_name')
            if path is None:
                continue
            path = f'art/{k}s/{path}.tga'
            data = self.layout.find(path)
            if not data:
                self.messages.append(('WARNING', f'Cannot find {k} {path}'))
                continue
            if isinstance(data, DirectoryPath):
                path = data.full_path
            res[k] = path
        return res

    def apply_teamcolor(self, teamcolor: dict):
        color_node_names = {f'color_{i}' for i in self.TEAMCOLORABLE_LAYERS}
        images = {}
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = pathlib.Path(tmpdir)
            for key in self.TEAMCOLORABLE_IMAGES:
                if (img_path := teamcolor.get(key)) is None:
                    continue
                data_path = pathlib.Path(img_path)
                if not data_path.exists():
                    data_path = self.layout.find(data_path)
                if not data_path:
                    continue
                tmpfile = tmpdir / pathlib.Path(img_path).name
                tmpfile.write_bytes(data_path.read_bytes())
                images[key] = image = bpy.data.images.load(str(tmpfile))
                image.pack()
        for mat in bpy.data.materials:
            if mat.node_tree is None:
                continue
            for node in mat.node_tree.nodes:
                if node.bl_idname == 'ShaderNodeValToRGB' and node.label in color_node_names:
                    key = node.label[len('color_'):]
                    if teamcolor.get(key) is None:
                        continue
                    node.color_ramp.elements[-1].color[:3] = teamcolor[key].from_srgb_to_scene_linear()[:3]
                if node.bl_idname == 'ShaderNodeTexImage' and node.label in self.TEAMCOLORABLE_IMAGES and images.get(node.label) is not None:
                    node.image = images[node.label]
                if node.bl_idname == 'ShaderNodeGroup' and node.node_tree == bpy.data.node_groups.get('ApplyTeamcolor', None):
                    for c in self.TEAMCOLORABLE_LAYERS:
                        if teamcolor.get(c, None) is None:
                            continue
                        node.inputs[f'{c.value}_color'].default_value[:3] = teamcolor[c].copy().from_srgb_to_scene_linear()  # TODO Investigate why the default color scheme doesn't apply without copy()


def import_whm(module_root: pathlib.Path, target_path: pathlib.Path, teamcolor_path: pathlib.Path = None):
    print('------------------')

    for action in bpy.data.actions:
        bpy.data.actions.remove(action)

    for material in bpy.data.materials:
        material.user_clear()
        bpy.data.materials.remove(material)
    
    for image in bpy.data.images:
        bpy.data.images.remove(image)

    for mesh in bpy.data.meshes:
        bpy.data.meshes.remove(mesh)

    for cam in bpy.data.cameras:
        bpy.data.cameras.remove(cam)

    with target_path.open('rb') as f:
        reader = ChunkReader(f)
        loader = WhmLoader(module_root, load_wtp=teamcolor_path is not None)
        try:
            loader.load(reader)
            if teamcolor_path:
                teamcolor = loader.load_teamcolor(teamcolor_path)
                loader.apply_teamcolor(teamcolor)
        finally:
            for _, msg in loader.messages:
                print(msg)
