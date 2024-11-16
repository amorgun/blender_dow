import dataclasses
import pathlib
import math
import tempfile

import bpy
import mathutils

from . import textures, utils
from .chunky import ChunkReader
from .utils import print


# class BonePropGroup(bpy.types.PropertyGroup):
#     stale: bpy.props.BoolProperty(name="Stale", default=False)

# bpy.utils.register_class(BonePropGroup)

# bpy.types.PoseBone.dow_settings = bpy.props.PointerProperty(name='DOW settings', type=BonePropGroup)


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


def setup_property(obj, prop_name: str, value=None, **kwargs):
    if value is None and obj.get(prop_name):
        return
    obj[prop_name] = value
    id_props = obj.id_properties_ui(prop_name)
    id_props.update(**kwargs)


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


class WhmLoader:
    TEAMCOLORABLE_LAYERS = {'primary', 'secondary', 'trim', 'weapons', 'eyes'}
    TEAMCOLORABLE_IMAGES = {'badge', 'banner'}

    def __init__(self, root: pathlib.Path, load_wtp: bool = True, create_cameras: bool = False, context=None):
        self.root = root
        self.wtp_load_enabled = load_wtp
        self.create_cameras = create_cameras
        self.bpy_context = context
        if self.bpy_context is None:
            self.bpy_context = bpy.context
        self.messages = []

    def _reset(self):
        self.texture_count = 0
        self.loaded_material_paths = set()
        self.bone_array = []
        self.xref_bone_array = []
        self.blender_mesh_root = None
        self.bone_orig_transform = {}
        self.bone_transform = {}
        self.created_materials = {}
        self.created_meshes = {}
        self.created_cameras = {}
        self.model_root_collection = None

        self.armature = bpy.data.armatures.new('Armature')
        self.armature_obj = bpy.data.objects.new('Armature', self.armature)
        self.armature_obj.show_in_front = True

        self.default_image = bpy.data.images.new('NOT_SET', 1, 1)
        self.default_image['PLACEHOLDER'] = True
        self.default_image.use_fake_user = True

    def CH_DATASSHR(self, reader: ChunkReader):  # CH_DATASSHR > - Chunk Handler - Material Data
        material_path = reader.read_str()  # -- Read Texture Path

        if material_path not in self.loaded_material_paths:
            full_material_path = self.root / 'Data' / f'{material_path}.rsh'
            if not full_material_path.exists():
                self.messages.append(('WARNING', f'Cannot find texture {full_material_path}'))
                return
            with full_material_path.open('rb') as f:
                material = self.load_rsh(ChunkReader(f), material_path)  # -- create new material
            if self.wtp_load_enabled:
                full_teamcolor_path = self.root / 'Data' / f'{material_path}_default.wtp'
                if not full_teamcolor_path.exists():
                    self.messages.append(('INFO', f'Cannot find {full_teamcolor_path}'))
                else:
                    with full_teamcolor_path.open('rb') as f:
                        self.load_wtp(ChunkReader(f), material_path, material)
            self.loaded_material_paths.add(material_path)

    def load_rsh(self, reader: ChunkReader, material_path: str):
        reader.skip_relic_chunky()
        current_chunk = reader.read_header('FOLDSHRF')  # Skip 'Folder SHRF' Header
        loaded_textures = {}
        material = None
        for current_chunk in reader.iter_chunks():
            match current_chunk.typeid:
                case 'FOLDTXTR': loaded_textures[current_chunk.name] = self.CH_FOLDTXTR(reader, current_chunk.name)  # FOLDTXTR - Internal Texture
                case 'FOLDSHDR': material = self.CH_FOLDSHDR(reader, material_path, loaded_textures)
                case _: reader.skip(current_chunk.size)
        return material

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

        import tempfile

        texture_name = pathlib.Path(texture_path).name
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(f'{tmpdir}/{texture_name}.dds', 'wb') as f: 
                textures.write_dds(
                    reader.stream, f, current_chunk.size, width, height, num_mips, image_format)
            image = bpy.data.images.load(f.name)
            image.pack()
            image.use_fake_user = True
        return image

    def CH_FOLDSHDR(self, reader: ChunkReader, material_path: str, loaded_textures: dict):  # Chunk Handler - Material
        if material_path in self.created_materials:
            return self.created_materials[material_path]
        material_name = pathlib.Path(material_path).name
        mat = bpy.data.materials.new(name=material_name)
        setup_property(mat, 'full_path', material_path, subtype='FILE_PATH', description='Path to export this material')
        mat.blend_method = 'CLIP'
        mat.show_transparent_back = False
        mat.use_nodes = True
        links = mat.node_tree.links
        node_final = mat.node_tree.nodes[0]

        current_chunk = reader.read_header('DATAINFO')
        num_images, *info_bytes = reader.read_struct('<2L 4B L x')

        node_uv = mat.node_tree.nodes.new('ShaderNodeUVMap')
        node_uv.location = -800, 200
        node_uv_offset = mat.node_tree.nodes.new('ShaderNodeMapping')
        node_uv_offset.label = 'UV offset'
        node_uv_offset.location = -600, 200
        links.new(node_uv.outputs[0], node_uv_offset.inputs['Vector'])

        node_object_info = mat.node_tree.nodes.new('ShaderNodeObjectInfo')
        node_object_info.location = -600, 400

        node_calc_alpha = mat.node_tree.nodes.new('ShaderNodeMath')
        node_calc_alpha.operation = 'MULTIPLY'
        node_calc_alpha.use_clamp = True
        node_calc_alpha.location = -150, 400
        links.new(node_object_info.outputs['Alpha'], node_calc_alpha.inputs[0])
        links.new(node_calc_alpha.outputs[0], node_final.inputs['Alpha'])

        created_tex_nodes = {}
        for _ in range(num_images):
            current_chunk = reader.read_header('DATACHAN')
            channel_idx, method, *colour_mask = reader.read_struct('<2l4B')
            channel_name = reader.read_str()
            num_coords = reader.read_one('<4x l 4x')
            for _ in range(4):  # always 4, not num_coords
                for ref_idx in range(4):
                    x, y = reader.read_struct('<2f')
            if channel_name == '':
                continue
            input_names, node_label = {
                0: (['Base Color', 'Emission Color'], 'diffuse'),
                1: (['Specular IOR Level'], 'specularity'),  # FIXME metallic
                2: (['Specular Tint'], 'reflection'),
                3: (['Emission Strength'], 'self_illumination'),
                4: (['Alpha'], 'opacity'),
            }[channel_idx]
            node_tex = created_tex_nodes.get(channel_name)
            if not node_tex:
                node_tex = mat.node_tree.nodes.new('ShaderNodeTexImage')
                node_tex.image = loaded_textures[channel_name]
                node_tex.location = -430, 400 - 320 * len(created_tex_nodes)
                node_tex.label = node_label
                created_tex_nodes[channel_name] = node_tex
            links.new(node_uv_offset.outputs[0], node_tex.inputs['Vector'])
            if channel_idx in (0, 4):
                links.new(node_tex.outputs['Alpha'], node_calc_alpha.inputs[1])
            if channel_idx != 4:
                for i in input_names:
                    links.new(node_tex.outputs[0], node_final.inputs[i])

        add_driver(mat.node_tree, 'nodes["Mapping"].inputs[1].default_value', self.armature_obj, f'["uv_offset__{material_name}"][0]', fallback_value=0, index=0)
        add_driver(mat.node_tree, 'nodes["Mapping"].inputs[1].default_value', self.armature_obj, f'["uv_offset__{material_name}"][1]', fallback_value=0, index=1)
        add_driver(mat.node_tree, 'nodes["Mapping"].inputs[3].default_value', self.armature_obj, f'["uv_tiling__{material_name}"][0]', fallback_value=1, index=0)
        add_driver(mat.node_tree, 'nodes["Mapping"].inputs[3].default_value', self.armature_obj, f'["uv_tiling__{material_name}"][1]', fallback_value=1, index=1)
        self.created_materials[material_path] = mat
        return mat

    def load_wtp(self, reader: ChunkReader, material_path: str, material):
        reader.skip_relic_chunky()
        current_chunk = reader.read_header('FOLDTPAT')
        loaded_textures = {}
        current_chunk = reader.read_header('DATAINFO')
        width, height = reader.read_struct('<2L')
        layer_names = {
            0: 'primary',
            1: 'secondary',
            2: 'trim',
            3: 'weapons',
            4: 'eyes',
            5: 'dirt',
            -1: 'default',
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
                        with open(f'{tmpdir}/{material_name}_{layer_names[layer_in]}.tga', 'wb') as f:
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
                        with open(f'{tmpdir}/{material_name}_{layer_names[layer_in]}.tga', 'wb') as f:
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
        uf_offset_node = [
            node for node in material.node_tree.nodes
            if node.bl_idname == 'ShaderNodeMapping'
            and node.label == 'UV offset'
        ][0]
        created_tex_nodes = {}
        prev_color_output = None
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
            node_tex.label = f'color_layer_{layer_name}'
            links.new(uf_offset_node.outputs[0], node_tex.inputs['Vector'])

            if layer_name in self.TEAMCOLORABLE_LAYERS:
                node_color = material.node_tree.nodes.new('ShaderNodeValToRGB')
                node_color.label = f'color_{layer_name}'
                node_color.location = node_pos_x + 480, node_pos_y
                node_color.width = 100
                links.new(node_tex.outputs[0], node_color.inputs['Fac'])
                if prev_color_output is None:
                    prev_color_output = node_color.outputs[0]
                else:
                    node_mix = material.node_tree.nodes.new('ShaderNodeMixRGB')
                    node_mix.blend_type = 'ADD'
                    node_mix.inputs['Fac'].default_value = 1
                    node_mix.location = node_pos_x + 650, node_pos_y
                    links.new(prev_color_output, node_mix.inputs['Color1'])
                    links.new(node_color.outputs['Color'], node_mix.inputs['Color2'])
                    prev_color_output = node_mix.outputs[0]

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

            node_mix = material.node_tree.nodes.new('ShaderNodeMixRGB')
            node_mix.blend_type = 'MIX'
            node_mix.location = node_pos_x + 480, node_pos_y
            links.new(node_tex.outputs['Alpha'], node_mix.inputs['Fac'])
            links.new(prev_color_output, node_mix.inputs['Color1'])
            links.new(node_tex.outputs['Color'], node_mix.inputs['Color2'])
            prev_color_output = node_mix.outputs[0]

        node_mix_dirt = material.node_tree.nodes.new('ShaderNodeMixRGB')
        node_mix_dirt.blend_type = 'ADD'
        node_mix_dirt.location = common_node_pos_x + 650, common_node_pos_y - 290 * (len(created_tex_nodes) - 1)
        links.new(created_tex_nodes['dirt'].outputs['Color'], node_mix_dirt.inputs['Fac'])
        links.new(prev_color_output, node_mix_dirt.inputs['Color1'])
        links.new(created_tex_nodes['default'].outputs['Color'], node_mix_dirt.inputs['Color2'])

        if 'default' in loaded_textures:
            links.new(node_mix_dirt.outputs[0], material.node_tree.nodes[0].inputs['Base Color'])
            links.new(node_mix_dirt.outputs[0], material.node_tree.nodes[0].inputs['Emission Color'])
        else:
            self.messages.append(('WARNING', f'Material {material_path} is missing the default layer'))

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
        bpy.ops.object.mode_set(mode='EDIT', toggle=True)
        bone_collection = self.armature.collections.new('Markers')

        num_markers = reader.read_one('<l')  # -- Read Number Of Markers
        for i in range(num_markers):  # -- Read All Markers
            marker_name = reader.read_str()  # -- Read Marker Name
            parent_name = reader.read_str()  # -- Read Parent Name
            transform = mathutils.Matrix().to_3x3()
            for row_idx in range(3):  # -- Read Matrix
                transform[row_idx][:3] = reader.read_struct('<3f')
            x, y, z = reader.read_struct('<3f')

            transform = mathutils.Matrix.LocRotScale([-x, y, z], transform, None)

            marker = self.armature.edit_bones.new(marker_name)  # -- Create Bone and Set Name
            marker.head = (0, 0, 0)
            marker.tail = (0.15, 0, 0)
            bone_collection.assign(marker)
            marker.color.palette = 'CUSTOM'
            marker.color.custom.normal = mathutils.Color([14, 255, 2]) / 255  # -- Set Color Of New Marker

            if marker_name in self.armature.bones:
                continue  # FIXME
            assert marker_name not in self.armature.bones, marker_name

            parent = self.armature.edit_bones.get(parent_name)
            if parent is None:
                parent_mat = mathutils.Matrix.Rotation(math.radians(90.0), 4, 'X')
            else:
                marker.parent = parent  # -- Set Parent Of New Marker
                parent_mat = self.bone_transform[parent_name]
            marker.matrix =  parent_mat @ transform @ mathutils.Matrix.Rotation(math.radians(-90.0), 4, 'Z')
            self.bone_transform[marker_name] = parent_mat @ transform
        bpy.ops.object.mode_set(mode='EDIT', toggle=True)

    def CH_DATACAMS(self, reader: ChunkReader):
        bpy.ops.object.mode_set(mode='EDIT', toggle=True)
        bone_collection = self.armature.collections.new('Cameras')
        cameras_collection = bpy.data.collections.new('Cameras')
        self.model_root_collection.children.link(cameras_collection)

        coord_transform = mathutils.Matrix([[-1, 0, 0], [0, 0, 1], [0, -1, 0]]).to_4x4()
        rot_180 = mathutils.Matrix.Rotation(math.radians(180.0), 4, 'Y').to_quaternion()
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
                mathutils.Quaternion([rot[3], *rot[:3]]) @ rot_180,
                None,
            ) @ coord_transform

            bone = self.armature.edit_bones.new(cam_name)
            bone.head = (0, 0, 0)
            bone.tail = (0.25, 0, 0)
            bone_collection.assign(bone)
            bone.color.palette = 'CUSTOM'
            bone.color.custom.normal = mathutils.Color([154, 17, 21]) / 255
            bone.matrix = transform
            self.bone_orig_transform[cam_name] = transform

            if not self.create_cameras:
                continue
            empty = bpy.data.objects.new(f'{cam_name}_focus', None)
            cameras_collection.objects.link(empty)
            empty.matrix_basis = mathutils.Matrix.Translation([-focus_point[0], -focus_point[2], focus_point[1]])
            empty.empty_display_type = 'SPHERE'
            cam_obj = utils.create_camera(
                cam_name=cam_name, bone=bone, armature=self.armature_obj,
                clip_start=clip_start, clip_end=clip_end, fov=fov*2, focus_obj=empty,
            )
            cameras_collection.objects.link(cam_obj)
            self.created_cameras[cam_name] = cam_obj
        bpy.ops.object.mode_set(mode='EDIT', toggle=True)

    def CH_FOLDANIM(self, reader: ChunkReader):  # Chunk Handler - Animations
        # ---< DATADATA >---

        current_chunk = reader.read_header('DATADATA')

        animation_name = current_chunk.name
        num_frames = reader.read_one('<l')  # -- Read Number Of Frames
        duration = reader.read_one('<f')  # num_frames / 30

        if animation_name in bpy.data.actions:
            animation = bpy.data.actions[animation_name]
        else:
            animation = bpy.data.actions.new(name=animation_name)
        animation.use_fake_user = True
        if self.armature_obj.animation_data is None:
            self.armature_obj.animation_data_create()
        self.armature_obj.animation_data.action = animation

        animation.frame_range = 0, num_frames - 1  # -- Set Start & End Frames

        # ---< BONES >---

        num_bones = reader.read_one('<l')  # -- Read Number Of Bones
        for bone_idx in range(num_bones):  # -- Read Bones
            bone_name = reader.read_str()  # -- Read Bone Name
            bone = self.armature_obj.pose.bones[bone_name]
            orig_transform = self.bone_orig_transform[bone_name]

            delta = mathutils.Matrix.Rotation(math.radians(-90.0), 4, 'Z').to_4x4()
            keys_pos = reader.read_one('<l')  # -- Read Number Of Postion Keys
            for _ in range(keys_pos):  # -- Read Postion Keys
                frame = reader.read_one('<f') * (num_frames - 1)  # -- Read Frame Number
                x, y, z = reader.read_struct('<3f')  # -- Read Position
                new_transform = mathutils.Matrix.Translation(mathutils.Vector([-x, y, z]))

                new_mat = delta.inverted() @ orig_transform.inverted() @ new_transform @ delta
                loc, *_ = new_mat.decompose()
                bone.location = loc
                self.armature_obj.keyframe_insert(data_path=f'pose.bones["{bone_name}"].location', frame=frame, group=bone_name)

            keys_rot = reader.read_one('<l')  # -- Read Number Of Rotation Keys
            orig_rot = self.bone_orig_transform[bone_name].to_quaternion()  # FIXME
            delta = delta.to_quaternion()
            for _ in range(keys_rot):
                frame = reader.read_one('<f') * (num_frames - 1)  # -- Read Frame Number
                key_rot = reader.read_struct('<4f')  # -- Read Rotation X, Y, Z, W
                new_transform = mathutils.Quaternion([key_rot[3], key_rot[0], -key_rot[1], -key_rot[2]])

                new_rot = delta.inverted() @ orig_rot.inverted() @ new_transform @ delta
                new_rot.make_compatible(bone.rotation_quaternion)  # Fix random axis flipping
                bone.rotation_quaternion = new_rot
                self.armature_obj.keyframe_insert(data_path=f'pose.bones["{bone_name}"].rotation_quaternion', frame=frame, group=bone_name)
            stale = not reader.read_one('<b')  # -- Read Stale Property
            # if stale == 0 then setUserProp bone "Stale" "Yes"											-- Set Stale Property
            if stale:
                # bone.dow_settings.stale = stale
                setup_property(bone, 'Stale', True, description='Apply it to each bone you want to disable in an animation.')
                # print(f'Stale {animation_name} {bone_name}')
                # self.armature_obj.keyframe_insert(data_path=f'pose.bones["{bone_name}"].dow_settings.stale', frame=0)
                self.armature_obj.keyframe_insert(data_path=f'pose.bones["{bone_name}"]["Stale"]', frame=0, group=bone_name)

        # ---< MESHES & TEXTURES >---

        visible_meshes = set()
        num_meshes = reader.read_one('<l')  # -- Read Number Of Meshes

        for i in range(num_meshes):
            obj_name = reader.read_str()  # -- Read Mesh Name
            mode = reader.read_one('<l')
            if mode == 2:  # -- Mesh
                # mesh = self.blender_mesh_root.all_objects[obj_name]
                reader.skip(8)  # -- Skip 8 Bytes (Unknown, zeros)
                keys_vis = reader.read_one('<l') - 1  # -- Read Number Of Visibility Keys
                reader.skip(4)  # -- Skip 4 Bytes (Unknown, zeros)
                force_invisible = reader.read_one('<f') == 0  #-- Read ForceInvisible Property
                force_invisible_prop_name = utils.create_prop_name('force_invisible__', obj_name)
                is_invisible = False
                if force_invisible:
                    if obj_name not in visible_meshes:
                        is_invisible = True
                else:
                    visible_meshes.add(obj_name)
                setup_property(self.armature_obj, force_invisible_prop_name, is_invisible, description='Force the mesh to be invisible in the current animation')  # -- Set ForceInvisible Property. Usage: https://dawnofwar.org.ru/publ/27-1-0-177
                self.armature_obj.keyframe_insert(data_path=f'["{force_invisible_prop_name}"]', frame=0, group=obj_name)
                prop_name = utils.create_prop_name('visibility__', obj_name)
                # if force_invisible == 0:
                # setup_property(self.armature_obj, prop_name, force_invisible, default=1.0, min=0, max=1, description='Hack for animatiing mesh visibility')
                # self.armature_obj.keyframe_insert(data_path=f'["{prop_name}"]', frame=0, group=obj_name)

                if keys_vis:
                    setup_property(self.armature_obj, prop_name, 1.0, default=1.0, min=0, max=1, description='Hack for animatiing mesh visibility')
                    self.armature_obj.keyframe_insert(data_path=f'["{prop_name}"]', frame=0, group=obj_name)
                
                for j in range(keys_vis):  # -- Read Visibility Keys
                    frame = reader.read_one('<f') * (num_frames - 1)  # -- Read Frame Number
                    key_vis = reader.read_one('<f')  # -- Read Visibility
                    self.armature_obj[prop_name] = key_vis
                    self.armature_obj.keyframe_insert(data_path=f'["{prop_name}"]', frame=frame, group=obj_name)
            elif mode == 0:  # -- Texture
                reader.skip(4)  # -- Skip 4 Bytes (Unknown, zeros)
                tex_anim_type = reader.read_one('<l')  # -- 1-U 2-V 3-TileU 4-TileV
                keys_tex = reader.read_one('<l')  # -- Read Number Of Texture Keys
                material = self.created_materials.get(obj_name)
                if material is not None:
                    if tex_anim_type in (1, 2):
                        prop_name = utils.create_prop_name('uv_offset__', material.name)
                        setup_property(self.armature_obj, prop_name, [0., 0.], default=[0., 0.], description='Hack for animatiing UV offset')
                    else:
                        prop_name = utils.create_prop_name('uv_tiling__', material.name)
                        setup_property(self.armature_obj, prop_name, [1., 1.], default=[1., 1.], description='Hack for animatiing UV tiling')
                else:
                    self.messages.append(('WARNING', f'Cannot find material {obj_name}'))
                for j in range(keys_tex):  # -- Read Texture Keys
                    frame = reader.read_one('<f') * (num_frames - 1)  # -- Read Frame Number
                    key_tex = reader.read_one('<f')
                    if material is None:
                        continue
                    match tex_anim_type:
                        case 1:
                            self.armature_obj[prop_name][0] = key_tex
                            self.armature_obj.keyframe_insert(data_path=f'["{prop_name}"]', frame=frame, group=prop_name, index=0)
                        case 2:
                            self.armature_obj[prop_name][1] = -key_tex
                            self.armature_obj.keyframe_insert(data_path=f'["{prop_name}"]', frame=frame, group=prop_name, index=1)
                        case 3:
                            self.messages.append(('INFO', 'TEST UV_TILING 1'))
                            self.armature_obj[prop_name][0] = -key_tex
                            self.armature_obj.keyframe_insert(data_path=f'["{prop_name}"]', frame=frame, group=prop_name, index=0)
                        case 4:
                            self.messages.append(('INFO', 'TEST UV_TILING 2'))
                            self.armature_obj[prop_name][1] = -key_tex
                            self.armature_obj.keyframe_insert(data_path=f'["{prop_name}"]', frame=frame, group=prop_name, index=1)
        # ---< CAMERA >---

        if current_chunk.version >= 2:  # -- Read Camera Data If DATADATA Chunk Version 2

            coord_transform = mathutils.Matrix([[-1, 0, 0], [0, 0, 1], [0, -1, 0]]).to_quaternion()
            rot_180 = mathutils.Matrix.Rotation(math.radians(180.0), 4, 'Y').to_quaternion()
            coord_transform_inv = coord_transform.inverted()

            num_cams = reader.read_one('<l')  # -- Read Number Of Cameras
            for cam_idx in range(num_cams):  # -- Read Cameras
                cam_name = reader.read_str()  # -- Read Camera Name
                if cam_name in self.created_cameras:
                    bone = self.armature_obj.pose.bones[cam_name]
                    orig_transform = self.bone_orig_transform[cam_name]
                cam_pos_keys = reader.read_one('<l')  # -- Read Number Of Camera Position Keys (?)
                for _ in range(cam_pos_keys):
                    frame = reader.read_one('<f') * (num_frames - 1)  # -- Read Frame Number
                    x, z, y = reader.read_struct('<3f')
                    if cam_name not in self.created_cameras:
                        continue
                    new_transform = mathutils.Matrix.Translation(mathutils.Vector([-x, -y, z]))

                    new_mat = orig_transform.inverted() @ new_transform
                    loc, *_ = new_mat.decompose()
                    bone.location = loc
                    self.armature_obj.keyframe_insert(data_path=f'pose.bones["{cam_name}"].location', frame=frame, group=bone_name)

                cam_rot_keys = reader.read_one('<l')  # -- Read Number Of Camera Rotation Keys (?)
                orig_rot = orig_transform.to_quaternion()  # FIXME
                for _ in range(cam_rot_keys):
                    frame = reader.read_one('<f') * (num_frames - 1)  # -- Read Frame Number
                    key_rot = reader.read_struct('<4f')
                    if cam_name not in self.created_cameras:
                        continue

                    new_transform = (
                        coord_transform_inv
                        @ mathutils.Quaternion([key_rot[3], *key_rot[:3]])
                        @ rot_180
                        @ coord_transform
                     )

                    new_rot = orig_rot.inverted() @ new_transform
                    new_rot.make_compatible(bone.rotation_quaternion)  # Fix random axis flipping
                    bone.rotation_quaternion = new_rot
                    self.armature_obj.keyframe_insert(data_path=f'pose.bones["{cam_name}"].rotation_quaternion', frame=frame, group=bone_name)
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
        assert flag == 1, f'{mesh_name}: {flag=}'
        assert rsv0_a == 0 and rsv0_b == 0
        num_skin_bones = reader.read_one('<l')  # -- get number of bones mesh is weighted to

        #---< SKIN BONES >---

        skin_bone_array = []  # -- array to store skin bone names
        for _ in range(num_skin_bones):
            bone_name = reader.read_str()  # -- read bone name
            skin_bone_array.append(bone_name)
            bone_idx = reader.read_one('<l')
            assert bone_array[bone_idx].name == bone_name

        #---< VERTICES >---

        num_vertices = reader.read_one('<l')  # -- read number of vertices
        vertex_size_id = reader.read_one('<l')  # 37 or 39
        assert (num_skin_bones != 0) * 2 == vertex_size_id - 37

        vert_array = []       # -- array to store vertex data
        for _ in range(num_vertices):
            x, z, y = reader.read_struct('<3f')
            vert_array.append((-x, -y, z))

        #---< SKIN >---

        skin_vert_array = []  # -- array to store skin vertices
        if num_skin_bones:
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
                    skin_vert.bone[bone_slot] = bone_array[bone_idx].name

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
        reader.skip(4)  # -- skip 4 bytes (unknown, zeros)

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
            for __ in range(num_faces):
                x, z, y = reader.read_struct('<3H')
                face_array.append((x, y, z))
                if material:
                    matid_array.append(len(materials) - 1)
                else:
                    matid_array.append(0)  # Default material
            reader.skip(8)  # -- Skip 8 Bytes To Next Texture Name Length. 4 data bytes + 4 zeros

        assert num_polygons == len(face_array), f'{mesh_name}: {num_polygons} != {len(face_array)}'

        #---< SHADOW VOLUME >---

        unknown_1 = reader.read_one('<l')  # -- Unknown Data 1, zero is ok
        reader.skip(unknown_1 * 12)  # -- Skip Unknown Data 1

        unknown_2 = reader.read_one('<l')  # -- Unknown Data 2, zero is ok
        reader.skip(unknown_2 * 24)  # -- Skip Unknown Data 2

        unknown_3 = reader.read_one('<l')  # -- Unknown Data 3, zero is ok
        reader.skip(unknown_3 * 40)  # -- Skip Unknown Data 3

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
        new_mesh.normals_split_custom_set_from_vertices(normal_array)
        
        for mat in materials:  # -- Set Material
            new_mesh.materials.append(mat)
        
        new_mesh.polygons.foreach_set('material_index', matid_array)

        obj = bpy.data.objects.new(mesh_name, new_mesh)
        add_driver(obj, 'color', self.armature_obj, f'["visibility__{mesh_name}"]', fallback_value=1.0, index=3)
        # add_driver(obj, 'hide_viewport', self.armature_obj, f'["force_invisible__{mesh_name}"]', fallback_value=False)  # works weirdly
        obj.parent = self.armature_obj
        self.created_meshes[mesh_name.lower()] = obj

        #---< SET BONE MESH >---

        vertex_groups = {}
        for vert_idx, vert in enumerate(skin_vert_array):
            for bone_weight, bone_name in zip(vert.weights, vert.bone):
                if bone_name is None or bone_weight == 0:
                    continue
                vertex_group =  vertex_groups.get(bone_name)
                if vertex_group is None:
                    vertex_group = vertex_groups[bone_name] = obj.vertex_groups.new(name=bone_name)
                vertex_group.add([vert_idx], bone_weight, 'REPLACE')
        if skin_bone_array == [mesh_name]:
            setup_property(new_mesh, 'ForceSkinning', True, description='Force a linked mesh to be treated as 100% skinned in the exporter')  # -- Mesh Is Weighted To Itself -> Force Skinning (https://dow.finaldeath.co.uk/rdnwiki/www.relic.com/rdn/wiki/ModTools/ToolsReleaseNotes.html)
            # print(f'ForceSkinning {mesh_name}')

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

        #---< WELD VERTICES >---

        obj.modifiers.new('Weld', 'WELD')

        if self.blender_mesh_root is None:
            self.blender_mesh_root = bpy.data.collections.new('Meshes')
            self.model_root_collection.children.link(self.blender_mesh_root)
        if group_name:
            extra_collection = bpy.data.collections.get(group_name)
            if extra_collection is None:
                extra_collection = bpy.data.collections.new(group_name)
                self.model_root_collection.children.link(extra_collection)
            extra_collection.objects.link(obj)
        armature_mod = obj.modifiers.new('Skeleton', "ARMATURE")
        armature_mod.object = self.armature_obj
        self.blender_mesh_root.objects.link(obj)
        return obj

    def CH_DATADATA(self, reader: ChunkReader):  # - Chunk Handler - Sub Chunk Of FOLDMSGR - Mesh List
        num_meshes = reader.read_one('<l')  # -- Read Number Of Meshes
        loaded_messages = set()
        for i in range(num_meshes):  # -- Read Each Mesh
            mesh_name = reader.read_str()  # -- Read Mesh Name
            mesh_path: pathlib.Path = pathlib.Path(reader.read_str())  # -- Read Mesh Path
            if mesh_path and mesh_path != pathlib.Path(''):
                filename = self.root / 'Data' / mesh_path.with_suffix('.whm')
                if filename.exists():
                    if filename not in loaded_messages:
                        loaded_messages.add(filename)
                        self.messages.append(('INFO', f'Loading {filename}'))
                    with filename.open('rb') as f:
                        xreffile = ChunkReader(f)
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
                                            setup_property(mesh_obj, 'xref_source', str(mesh_path), description='Reference this mesh from an external file instead of this model')
                                        else:
                                            xreffile.skip(current_chunk.size)
                                        if current_chunk.typeid == 'DATABVOL':
                                            break
                                # case 'DATAMARK': self.CH_DATAMARK(xreffile)
                                case _: xreffile.skip(current_chunk.size)
                else:
                    self.messages.append(('WARNING', f'Cannot find file {filename}'))
            mesh_parent_idx = reader.read_one('<l')  # -- Read Mesh Parent
            if mesh_parent_idx != -1:
                mesh = self.created_meshes[mesh_name.lower()]
                vertex_group = mesh.vertex_groups.new(name=self.bone_array[mesh_parent_idx].name)
                vertex_group.add(list(range(len(mesh.data.vertices))), 1.0, 'REPLACE')
            # if mesh_parent != -1 then
                # mesh = getNodeByName mesh_name exact:true													-- Get Mesh Node From The Scene
                # if mesh == created_bones_array[mesh_parent+1] then setUserProp mesh "ForceSkinning" "Yes"	-- Set Force Skinning If Parent Is The Same As Mesh
                # else mesh.parent = created_bones_array[mesh_parent+1]										-- Else Set New Parent

            # --print (mesh_name as string + " -|- " + mesh_path as string + " -|- " + mesh_parent as string)
    
    def load(self, reader: ChunkReader):
        self._reset()
        reader.skip_relic_chunky()
        header = reader.read_header('DATAFBIF')  # Read 'File Burn Info' Header
        reader.skip(header.size)       # Skip 'File Burn Info' Chunk
        header = reader.read_header('FOLDRSGM')  # Skip 'Folder SGM' Header
        self.model_root_collection = bpy.data.collections.new(header.name)
        self.bpy_context.scene.collection.children.link(self.model_root_collection)
        self.model_root_collection.objects.link(self.armature_obj)

        internal_textures = {}

        for current_chunk in reader.iter_chunks():  # Read Chunks Until End Of File
            match current_chunk.typeid:
                case "DATASSHR": self.CH_DATASSHR(reader)  # DATASSHR - Texture Data
                case "FOLDTXTR":  # FOLDTXTR - Internal Texture
                    internal_textures[current_chunk.name] = self.CH_FOLDTXTR(reader, current_chunk.name)
                case "FOLDSHDR":  # FOLDSHDR - Internal Material
                    mat = self.CH_FOLDSHDR(reader, current_chunk.name, internal_textures)
                    setup_property(mat, 'internal', True, description='Do not export this material to a separate file and keep it inside the model file')
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
        for k, v in self.armature_obj.items():
            if k.startswith('visibility_'):
                self.armature_obj[k] = 1.

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
            path = self.root / 'data/art' / f'{k}s' / f'{path}.tga'
            if not path.exists():
                self.messages.append(('WARNING', f'Cannot find {k} {path}'))
                continue
            res[k] = path
        return res

    def apply_teamcolor(self, teamcolor: dict):
        color_node_names = {f'color_{i}' for i in self.TEAMCOLORABLE_LAYERS}
        for mat in bpy.data.materials:
            if mat.node_tree is None:
                continue
            for node in mat.node_tree.nodes:
                if node.bl_idname == 'ShaderNodeValToRGB' and node.label in color_node_names:
                    key = node.label[len('color_'):]
                    if teamcolor.get(key) is None:
                        continue
                    node.color_ramp.elements[-1].color[:3] = teamcolor[key][:3]
                    continue
                if node.bl_idname == 'ShaderNodeTexImage' and node.label in self.TEAMCOLORABLE_IMAGES and teamcolor.get(node.label) is not None:
                        node.image = bpy.data.images.load(str(teamcolor[node.label]))
                        node.image.pack()


def import_whm(module_root: pathlib.Path, target_path: pathlib.Path, teamcolor_path: pathlib.Path = None, create_cameras: bool = True):
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
        loader = WhmLoader(module_root, load_wtp=teamcolor_path is not None, create_cameras=create_cameras)
        try:
            loader.load(reader)
            if teamcolor_path:
                teamcolor = loader.load_teamcolor(teamcolor_path)
                loader.apply_teamcolor(teamcolor)
        finally:
            for _, msg in loader.messages:
                print(msg)
