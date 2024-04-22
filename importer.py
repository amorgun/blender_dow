import contextlib
import dataclasses
import io
import pathlib
import struct
import math
import os
import typing

import bpy
import mathutils

from .utils import print



class BonePropGroup(bpy.types.PropertyGroup):
    stale: bpy.props.BoolProperty(name="Stale", default=False)

bpy.utils.register_class(BonePropGroup)

bpy.types.PoseBone.dow_settings = bpy.props.PointerProperty(name='DOW settings', type=BonePropGroup)


@dataclasses.dataclass
class ChunkHeader:  # -- Structure Holding Chunk Header Data
    typeid: str = None
    version: int = None
    size: int = None
    name_length: int = None
    name_bytes: bytes = None

    @property
    def name(self):
        return str(self.name_bytes.rstrip(b'\0'), 'utf8')


class ChunkReader:
    def __init__(self, stream):
        self.stream = stream
        
    def read_header(self) -> ChunkHeader:
        fields = self.read_struct('<8slll')
        if fields is None:
            return None
        typeid, version, size, name_length = fields
        name = self.stream.read(name_length)
        return ChunkHeader(str(typeid, 'ascii'), version, size, name_length, name)

    def read_struct(self, fmt: str) -> tuple | None:
        size = struct.calcsize(fmt)
        buf = self.stream.read(size)
        if len(buf) < size:
            return None
        return struct.unpack(fmt, buf)
    
    def read_one(self, fmt: str) -> typing.Any:
        fields = self.read_struct(fmt)
        if fields is None:
            return None
        assert len(fields) == 1, 'Need to parse exactly 1 value'
        return fields[0]
    
    def read_str(self, encoding='utf8'):
        str_len = self.read_one('<l')
        if str_len == 0:
            return ''
        return str(self.read_one(f'<{str_len}s'), encoding)
    
    def skip(self, nbytes: int) -> None:
        self.stream.seek(nbytes, os.SEEK_CUR)


@dataclasses.dataclass
class BoneData:  # -- Structure To Hold Bone Data (4, X, 4, 28)
    name: str = None
    level: int = None
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
    driver = obj.driver_add(obj_prop_path, index).driver    
    var = driver.variables.new()
    driver.type = 'SUM'
    var.targets[0].id = target_id
    var.targets[0].data_path = target_data_path
    var.targets[0].use_fallback_value = True
    var.targets[0].fallback_value = fallback_value


class WhmLoader:
    def __init__(self, root: pathlib.Path, context=None):
        self.root = root
        self.bpy_context = context
        if self.bpy_context is None:
            self.bpy_context = bpy.context

    def _reset(self):
        self.texture_count = 0
        self.texture_array = []
        self.bone_array = []
        self.xref_bone_array = []
        self.blender_mesh_root = None
        self.bone_orig_transform = {}
        self.bone_transform = {}
        self.created_materials = {}
        self.created_meshes = {}

        self.armature = bpy.data.armatures.new('Armature')
        self.armature_obj = bpy.data.objects.new('Armature', self.armature)
        self.armature_obj.show_in_front = True
        self.bpy_context.collection.objects.link(self.armature_obj)

        self.messages = []

    def CH_DATASSHR(self, reader: ChunkReader):  # CH_DATASSHR > - Chunk Handler - Material Data
        texture_path = reader.read_str()  # -- Read Texture Path

        if texture_path not in self.texture_array:
            full_texture_path = self.root / 'Data' / f'{texture_path}.rsh'

            if not full_texture_path.exists():
                self.messages.append(('WARNING', f'Cannot find texture {full_texture_path}'))
                # print(f'Cannot find texture {full_texture_path}')
                return
            
            with full_texture_path.open('rb') as f:
                self.load_texture(ChunkReader(f))  # -- create new material

    def load_texture(self, reader: ChunkReader):
        reader.skip(24)                # Skip 'Relic Chunky' Header
        current_chunk = reader.read_header()  # Skip 'Folder SHRF' Header
        while current_chunk := reader.read_header():
            match current_chunk.typeid:
                case "FOLDTXTR": self.CH_FOLDTXTR(reader, current_chunk.name, internal=False)  # FOLDTXTR - Internal Texture
                case _: reader.skip(current_chunk.size)  # TODO Look into FOLDSHDR

    def CH_FOLDTXTR(self, reader: ChunkReader, material_path: str, internal: bool):  # Chunk Handler - Internal Texture
        current_chunk = reader.read_header()  # DATAHEAD
        assert current_chunk.typeid == 'DATAHEAD'
        reader.skip(current_chunk.size)
        current_chunk = reader.read_header()  # FOLDIMAG
        current_chunk = reader.read_header()  # DATAATTR
        image_format, width, height, num_mips = reader.read_struct('<llll')
        current_chunk = reader.read_header()  # DATADATA
        assert current_chunk.typeid == 'DATADATA'

        import shutil
        import tempfile

        material_name = pathlib.Path(material_path).name
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(f'{tmpdir}/{material_name}.dds', 'wb') as f: 
                _DOW_DXT_FLAGS = 0x000A1007  # _DEFAULT_FLAGS | _dwF_MIPMAP | _dwF_LINEAR
                _ddsF_FOURCC = 0x00000004
                _DOW_DDSCAPS_FLAGS = 0x401008 # _ddscaps_F_TEXTURE | _ddscaps_F_COMPLEX | _ddscaps_F_MIPMAP_S
                fourCC = {8: b'DXT1', 10: b'DXT3', 13: b'DXT5'}[image_format]
                header = struct.Struct('<4s 7l 44x 2l 4s 20x 2l 12x').pack(
                    b'DDS ', 124, _DOW_DXT_FLAGS, width, height, current_chunk.size, 0, num_mips, 
                    32, _ddsF_FOURCC, fourCC,  # pixel format
                    _DOW_DDSCAPS_FLAGS, 0,  # ddscaps
                )
                f.write(header)
                shutil.copyfileobj(reader.stream, f, current_chunk.size)

            if material_name in bpy.data.materials:
                mat = bpy.data.materials[material_name]
            else:
                mat = bpy.data.materials.new(name=material_name)
            setup_property(mat, 'full_path', material_path, subtype='FILE_PATH', description='Path to export this texture')
            if internal:
                setup_property(mat, 'internal', True, description='Do not export this texture to separate file')
            elif mat.get('internal'):
                mat['internal'] = False
            mat.blend_method = 'BLEND'
            mat.show_transparent_back = False
            mat.use_nodes = True
            links = mat.node_tree.links
            node_final = mat.node_tree.nodes[0]
            
            node_uv = mat.node_tree.nodes.new('ShaderNodeUVMap')
            node_uv.from_instancer = True
            node_uv_offset = mat.node_tree.nodes.new('ShaderNodeMapping')
            node_uv_offset.label = 'UV offset'
            links.new(node_uv.outputs[0], node_uv_offset.inputs['Vector'])

            node_object_info = mat.node_tree.nodes.new('ShaderNodeObjectInfo')

            node_calc_alpha = mat.node_tree.nodes.new('ShaderNodeMath')
            node_calc_alpha.operation = 'MULTIPLY'
            node_calc_alpha.use_clamp = True
            links.new(node_object_info.outputs['Alpha'], node_calc_alpha.inputs[0])
            links.new(node_calc_alpha.outputs[0], node_final.inputs['Alpha'])

            node_tex = mat.node_tree.nodes.new('ShaderNodeTexImage')
            node_tex.image = bpy.data.images.load(f.name)
            node_tex.image.pack()
            links.new(node_uv_offset.outputs[0], node_tex.inputs['Vector'])
            links.new(node_tex.outputs['Alpha'], node_calc_alpha.inputs[1])

            links.new(node_tex.outputs[0], node_final.inputs['Base Color'])
            add_driver(mat.node_tree, 'nodes["Mapping"].inputs[1].default_value', self.armature_obj, f'["uv_offset__{material_name}"][0]', fallback_value=0, index=0)
            add_driver(mat.node_tree, 'nodes["Mapping"].inputs[1].default_value', self.armature_obj, f'["uv_offset__{material_name}"][1]', fallback_value=0, index=1)
            add_driver(mat.node_tree, 'nodes["Mapping"].inputs[3].default_value', self.armature_obj, f'["uv_tiling__{material_name}"][0]', fallback_value=1, index=0)
            add_driver(mat.node_tree, 'nodes["Mapping"].inputs[3].default_value', self.armature_obj, f'["uv_tiling__{material_name}"][1]', fallback_value=1, index=1)
            self.created_materials[material_path] = mat

    def CH_DATASKEL(self, reader: ChunkReader, xref: bool):  # Chunk Handler - Skeleton Data
        # ---< READ BONES >---

        num_bones = reader.read_one('<l') # -- Read Number Of Bones
        bone_array = self.xref_bone_array if xref else self.bone_array
        for _ in range(num_bones):  # -- Read Each Bone Data
            bone = BoneData()  # -- Reset Bonedata Structure
            bone.name = reader.read_str()  # -- Read Bone Name
            bone.level = reader.read_one('<l')  # -- Read Bone Hierarchy Level
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

            for parent_idx in range(bone_idx - 1, -1, -1):  # -- Go Backwards From Current Bone To The Root
                if bone_array[parent_idx].level < bone.level:  # -- Find First Bone With Smaller Bone Counter
                    new_bone.parent = created_bones_array[parent_idx]  # -- Link To Parent
                    break  # -- Exit Loop If Parent Is Found
            
            created_bones_array.append(new_bone)  # -- Add New Bone To Created Bones Array

            # ---< POSITION & ROTATION >---

            if bone.level != -1:
                parent_mat = bone_transforms[parent_idx]
            else:
                parent_mat = mathutils.Matrix.Rotation(math.radians(90.0), 4, 'X')
            bone_transform = parent_mat @ orig_transform
            new_bone.matrix = bone_transform @ mathutils.Matrix.Rotation(math.radians(-90.0), 4, 'Z')
            self.bone_orig_transform[bone.name] = orig_transform
            self.bone_transform[bone.name] = bone_transform
            bone_transforms.append(bone_transform)

        bpy.ops.object.mode_set(mode='EDIT', toggle=True)

    def CH_FOLDMSGR(self, reader: ChunkReader):  # Chunk Handler - Mesh Data
        while (current_chunk := reader.read_header()):                                # Read FOLDMSLC Chunks
            match current_chunk.typeid:
                case "FOLDMSLC": self.CH_FOLDMSLC(reader, current_chunk.name, False)  # 	 - Mesh Data
                case "DATADATA": self.CH_DATADATA(reader)                             # -- DATADATA - Mesh List
                case "DATABVOL":                                                      # -- DATABVOL - Unknown
                    reader.skip(current_chunk.size)
                    return True

    def CH_DATAMARK(self, reader: ChunkReader):
        bpy.ops.object.mode_set(mode='EDIT', toggle=True)
        bone_collection = self.armature.collections.new('Markers')

        num_markers = reader.read_one('<l')  # -- Read Number Of Markers
        for i in range(num_markers):  # -- Read All Markers
            marker_name = reader.read_str()  # -- Read Marker Name
            parent_name = reader.read_str()  # -- Read Parent Name
            transform = mathutils.Matrix()
            for row_idx in range(3):  # -- Read Matrix
                transform[row_idx][:3] = reader.read_struct('<3f')
            x, y, z = reader.read_struct('<3f')

            transform = mathutils.Matrix.Translation([-x, y, z]) @ transform

            marker = self.armature.edit_bones.new(marker_name)  # -- Create Bone and Set Name
            marker.head = (0, 0, 0)
            marker.tail = (0.15, 0, 0)
            bone_collection.assign(marker)
            marker.color.palette = 'CUSTOM'
            marker.color.custom.normal = mathutils.Color([14, 255, 2]) / 255  # -- Set Color Of New Marker

            if marker_name in self.armature.bones:
                continue  # FIXME
            assert marker_name not in self.armature.bones

            parent = self.armature.edit_bones.get(parent_name)
            if parent is None:
                parent_mat = mathutils.Matrix.Rotation(math.radians(90.0), 4, 'X')
            else:
                marker.parent = parent  # -- Set Parent Of New Marker
                parent_mat = self.bone_transform[parent_name]
            marker.matrix =  parent_mat @ transform @ mathutils.Matrix.Rotation(math.radians(-90.0), 4, 'Z')
            self.bone_transform[marker_name] = parent_mat @ transform
        bpy.ops.object.mode_set(mode='EDIT', toggle=True)

    def CH_FOLDANIM(self, reader: ChunkReader):  # Chunk Handler - Animations
        # ---< DATADATA >---

        current_chunk = reader.read_header()  # -- Read DATADATA Chunk Header

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

        # Debug me
        #    die frame 159
        # bone bloodthirster_bone_7_wing_l

        # debug vis_no_arms

        num_bones = reader.read_one('<l')  # -- Read Number Of Bones
        for bone_idx in range(num_bones):  # -- Read Bones
            bone_name = reader.read_str()  # -- Read Bone Name
            # bone_idx = self.name_2_bone_idx[bone_name]
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
                bone.rotation_quaternion = new_rot.to_matrix().to_quaternion()  # Fix random axis flipping
                self.armature_obj.keyframe_insert(data_path=f'pose.bones["{bone_name}"].rotation_quaternion', frame=frame, group=bone_name)
            stale = reader.read_one('<b')  # -- Read Stale Property
            # if stale == 0 then setUserProp bone "Stale" "Yes"											-- Set Stale Property
            if not stale:
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
                force_invisible = reader.read_one('<f')  #-- Read ForceInvisible Property
                force_invisible_prop_name = f'force_invisible__{obj_name}'
                is_invisible = False
                if force_invisible == 0:
                    if obj_name not in visible_meshes:
                        is_invisible = True
                else:
                    visible_meshes.add(obj_name)
                setup_property(self.armature_obj, force_invisible_prop_name, is_invisible, description='Force the mesh to be invisible in the current animation')  # -- Set ForceInvisible Property. Usage: https://dawnofwar.org.ru/publ/27-1-0-177
                self.armature_obj.keyframe_insert(data_path=f'["{force_invisible_prop_name}"]', frame=0, group=obj_name)

                if keys_vis:
                    prop_name = f'visibility__{obj_name}'
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
                        prop_name = f'uv_offset__{material.name}'
                        setup_property(self.armature_obj, prop_name, [0., 0.], default=[0., 0.], description='Hack for animatiing UV offset')
                    else:
                        prop_name = f'uv_tiling__{material.name}'
                        setup_property(self.armature_obj, prop_name, [1., 1.], default=[1., 1.], description='Hack for animatiing UV tiling')
                for j in range(keys_tex):  # -- Read Texture Keys
                    frame = reader.read_one('<f') * (num_frames - 1)  # -- Read Frame Number
                    key_tex = reader.read_one('<f')  # -- Read Offsetf'["force_invisible_{mesh_name}"]
                    match tex_anim_type:
                        case 1:
                            self.armature_obj[prop_name][0] = key_tex
                            self.armature_obj.keyframe_insert(data_path=f'["{prop_name}"]', frame=frame, group=prop_name, index=0)
                        case 2:
                            self.armature_obj[prop_name][1] = -key_tex
                            self.armature_obj.keyframe_insert(data_path=f'["{prop_name}"]', frame=frame, group=prop_name, index=1)
                        case 3:
                            raise Exception('TEST ME')
                            self.armature_obj[prop_name][0] = -key_tex
                            self.armature_obj.keyframe_insert(data_path=f'["{prop_name}"]', frame=frame, group=prop_name, index=0)
                        case 4:
                            raise Exception('TEST ME')
                            self.armature_obj[prop_name][1] = -key_tex
                            self.armature_obj.keyframe_insert(data_path=f'["{prop_name}"]', frame=frame, group=prop_name, index=1)
        # ---< CAMERA >---

        if current_chunk.version >= 2:  # -- Read Camera Data If DATADATA Chunk Version 2
            num_cams = reader.read_one('<l')  # -- Read Number Of Cameras
            if num_cams:
                self.messages.append(('INFO', 'We have cameras!'))

            for k in range(num_cams):  # -- Read Cameras
                cam_name = reader.read_str()  # -- Read Camera Name
                self.messages.append(('INFO', f'CAM NAME {cam_name}'))
                cam_pos_keys = reader.read_one('<l')  # -- Read Number Of Camera Position Keys (?)
                reader.skip(cam_pos_keys * 16)  # -- Skip Camera Position Keys
                cam_rot_keys = reader.read_one('<l')  # -- Read Number Of Camera Rotation Keys (?)
                reader.skip(cam_rot_keys * 20)  # -- Skip Camera Rotation Keys

        # ---< DATAANBV >---

        current_chunk = reader.read_header()  # -- Read DATAANBV Chunk Header
        reader.skip(current_chunk.size)  # -- Skip DATAANBV Chunk

    def CH_FOLDMSLC(self, reader: ChunkReader, mesh_name: str, xref: bool):  # Chunk Handler - FOLDMSGR Sub Chunk - Mesh Data
        #------------------------
        #---[ READ MESH DATA ]---
        #------------------------

        #---< DATADATA CHUNK >---

        current_chunk = reader.read_header()  # -- read DATADATA header
        rsv0_a, flag, unk, rsv0_b = reader.read_struct('<l b l l') # -- skip 13 bytes (unknown)
        assert rsv0_a == 0 and rsv0_b == 0
        # print(f'{mesh_name} ({self.num_meshes}): {flag}, {unk} {len(self.bone_array)}')
        # if unk < len(self.bone_array):
        #     print(f'BONE GUESS {self.bone_array[unk].name}')
        num_skin_bones = reader.read_one('<l')  # -- get number of bones mesh is weighted to

        #---< SKIN BONES >---

        skin_bone_array = []  # -- array to store skin bone names
        for _ in range(num_skin_bones):
            bone_name = reader.read_str()  # -- read bone name
            skin_bone_array.append(bone_name)
            bone_idx = reader.read_one('<l')
            assert self.bone_array[bone_idx].name == bone_name

        #---< VERTICES >---

        num_vertices = reader.read_one('<l')  # -- read number of vertices
        vertex_size_id = reader.read_one("<l")  # 37 or 39
        assert (num_skin_bones != 0) * 2 == vertex_size_id - 37

        vert_array = []       # -- array to store vertex data
        for _ in range(num_vertices):
            x, z, y = reader.read_struct('<3f')
            vert_array.append((-x, -y, z))

        #---< SKIN >---

        bone_array = self.xref_bone_array if xref else self.bone_array

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
        reader.skip(4)  # -- skip 4 bytes (unknown)

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
                x, z, y = reader.read_struct('<3h')
                face_array.append((x, y, z))
                if material:
                    matid_array.append(len(materials) - 1)
            reader.skip(8)  # -- Skip 8 Bytes To Next Texture Name Length

        #---< SHADOW VOLUME >---

        unknown_1 = reader.read_one('<l')  # -- Unknown Data 1
        reader.skip(unknown_1 * 12)  # -- Skip Unknown Data 1

        unknown_2 = reader.read_one('<l')  # -- Unknown Data 2
        reader.skip(unknown_2 * 24)  # -- Skip Unknown Data 2

        unknown_3 = reader.read_one('<l')  # -- Unknown Data 3
        reader.skip(unknown_3 * 40)  # -- Skip Unknown Data 3

        #---< DATABVOL CHUNK >---

        current_chunk = reader.read_header()  # -- Read DATABVOL Header
        reader.skip(current_chunk.size)  # -- Skip DATABVOL Chunk

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
        self.created_meshes[mesh_name] = obj

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

        #---< SKIN >---

        #if num_skin_bones > 0 then															-- Check If WHM File Contains Any Bones
        #(
        #if (skin_bone_array[1] as name) != (new_mesh.name as name) then						-- Mesh Is Not Weighted To Itself -> Create Skin
        #(
        #-- Create Skin Modifier
        #mod_skin = Skin filter_vertices:true filter_cross_sections:false filter_envelopes:false \
        #draw_all_gizmos:false envelopesAlwaysOnTop:false crossSectionsAlwaysOnTop:false showNoEnvelopes:true
        #-- Add Skin Modifier To Mesh
        #addModifier new_mesh mod_skin
        #max modify mode
        #select new_mesh

        #for i=1 to skin_bone_array.count do 
        #skinOps.addbone new_mesh.skin (getNodeByName skin_bone_array[i] exact:true) 0

        #select new_mesh
        #for i=1 to num_vertices do 
        #for j=1 to 4 do
        #if skin_vert_array[i].bone[j] != -1 then skinOps.SetVertexWeights new_mesh.skin i skin_vert_array[i].bone[j] skin_vert_array[i].weight[j]
        #)
        #else if num_skin_bones == 1 then setUserProp new_mesh "ForceSkinning" "Yes"			-- Mesh Is Weighted To Itself -> Force Skinning (https://dow.finaldeath.co.uk/rdnwiki/www.relic.com/rdn/wiki/ModTools/ToolsReleaseNotes.html)
        #)

        #update new_mesh																		-- Update Mesh
        #deselect new_mesh																	-- Deselect Mesh

        #--print (new_mesh.name + " - " + num_skin_bones as string + " - " + skin_bone_array as string)

        #---< WELD VERTICES >---

        # mod = obj.modifiers.new("WeldModifier", 'WELD')  # -- Weld Vertices
        # mod.merge_threshold = 0.00000001
        
        #meshop.weldVertsByThreshold new_mesh new_mesh.verts 0.00000001						-- Weld Vertices

        #)

        if self.blender_mesh_root is None:
            self.blender_mesh_root = bpy.data.collections.new('Meshes')
            self.bpy_context.scene.collection.children.link(self.blender_mesh_root)

        armature_mod = obj.modifiers.new('Skeleton', "ARMATURE")
        armature_mod.object = self.armature_obj
        self.blender_mesh_root.objects.link(obj)

    def CH_DATADATA(self, reader: ChunkReader):  # - Chunk Handler - Sub Chunk Of FOLDMSGR - Mesh List
        # chunk = reader.read_header()  # -- Currently Processed Chunk

    # maxpath = getINISetting (getMAXIniFile()) "Directories" "Startup Scripts"
    # dowpath = getINISetting (maxpath + "\WHMImport.ini") "Directories" "DawnOfWar"
    # modfolder = getINISetting (maxpath + "\WHMImport.ini") "Directories" "ModFolder"

        num_meshes = reader.read_one('<l')  # -- Read Number Of Meshes

        for i in range(num_meshes):  # -- Read Each Mesh
            mesh_name = reader.read_str()  # -- Read Mesh Name
            mesh_path: pathlib.Path = pathlib.Path(reader.read_str())  # -- Read Mesh Path
            if str(mesh_path) != '.':
                print(f'EXTERNAL MESH {mesh_path=}')
                filename = self.root / f'{mesh_path}.whm'
                if filename.exists():
                    raise Exception(f'FIX LOADING {filename}')
                # --filename = (dowpath + "/" + modfolder + "/Data/" + mesh_path + ".whm")
                # print(f'{mesh_name=} {mesh_path=}')
                # filename = pathlib.Path("../base_mesh") / mesh_path.stem / ".whm"
                # print(f'{filename=}')
                # --print ("Mesh: " + mesh_path)
                # --print ("Filename:" + filename)

                # xreffile = fopen filename "rbS"																-- Open WHM For Reading In Binary Format

                # if xreffile != undefined then
                # (
                # fseek xreffile 24 #seek_set																	-- Skip 'Relic Chunky' Header
                # chunk = ReadChunkHeader xreffile															-- Read 'File Burn Info' Header
                # SkipChunk xreffile chunk																	-- Skip 'File Burn Info' Chunk
                # chunk = ReadChunkHeader xreffile 															-- Skip 'Folder SGM' Header

                # while (chunk = ReadChunkHeader xreffile) != false do									-- Read Chunks Until End Of File
                # (
                # case chunk.typeid of
                # (
                # "DATASSHR": CH_DATASSHR xreffile																-- DATASSHR - Texture Data
                # "DATASKEL": CH_DATASKEL xreffile true																-- FOLDMSLC - Skeleton Data
                # "FOLDMSGR":
                # (
                # while (chunk = ReadChunkHeader xreffile) != false do									-- Read FOLDMSLC Chunks
                # (
                # case chunk.typeid of
                # (
                # "FOLDMSLC": 
                # (
                # if (chunk.name as name) != (mesh_name as name) then SkipChunk xreffile chunk
                # else CH_FOLDMSLC xreffile chunk.name true
                # )
                # "DATADATA": SkipChunk xreffile chunk
                # "DATABVOL":																					-- DATABVOL - Unknown
                # (
                # SkipChunk xreffile chunk
                # exit											
                # )
                # )
                # )
                # )
                # default: SkipChunk xreffile chunk 															-- Skipping Chunks By Default
                # )
                # )

                # fclose xreffile
                # )	
                # else messagebox "Can't open file!"															-- Show Error Msg Box

            mesh_parent_idx = reader.read_one('<l')  # -- Read Mesh Parent
            if mesh_parent_idx != -1:
                mesh = self.created_meshes[mesh_name]
                vertex_group = mesh.vertex_groups.new(name=self.bone_array[mesh_parent_idx].name)
                vertex_group.add(list(range(len(mesh.data.vertices))), 1.0, 'REPLACE')
            # if mesh_parent != -1 then
                # mesh = getNodeByName mesh_name exact:true													-- Get Mesh Node From The Scene
                # if mesh == created_bones_array[mesh_parent+1] then setUserProp mesh "ForceSkinning" "Yes"	-- Set Force Skinning If Parent Is The Same As Mesh
                # else mesh.parent = created_bones_array[mesh_parent+1]										-- Else Set New Parent

            # --print (mesh_name as string + " -|- " + mesh_path as string + " -|- " + mesh_parent as string)
    
    def load(self, reader: ChunkReader):
        self._reset()
        reader.skip(24)                # Skip 'Relic Chunky' Header
        header = reader.read_header()  # Read 'File Burn Info' Header
        reader.skip(header.size)       # Skip 'File Burn Info' Chunk
        header = reader.read_header()  # Skip 'Folder SGM' Header
        
        while (current_chunk := reader.read_header()):  # Read Chunks Until End Of File
            match current_chunk.typeid:
                case "DATASSHR": self.CH_DATASSHR(reader)  # DATASSHR - Texture Data
                case "FOLDTXTR": 
                    self.CH_FOLDTXTR(reader, current_chunk.name, internal=True)  # FOLDTXTR - Internal Texture
                    raise Exception('LOAD INTERNAL TEXTURE')
                case "DATASKEL": self.CH_DATASKEL(reader, False)  # DATASKEL - Skeleton Data
                case "FOLDMSGR": self.CH_FOLDMSGR(reader)  # FOLDMSGR - Mesh Data
                case "DATAMARK": self.CH_DATAMARK(reader)  # DATAMARK - Marker Data
                case "FOLDANIM": self.CH_FOLDANIM(reader)  # FOLDANIM - Animations
                case _: reader.skip(current_chunk.size)  # Skipping Chunks By Default

        for bone in self.armature_obj.pose.bones:
            bone.matrix_basis = mathutils.Matrix()
        self.armature_obj.hide_set(True)
        for k, v in self.armature_obj.items():
            if k.startswith('visibility_'):
                self.armature_obj[k] = 1.

def import_whm(module_root: pathlib.Path, target_path: pathlib.Path):
    for action in bpy.data.actions:
        bpy.data.actions.remove(action)

    for material in bpy.data.materials:
        material.user_clear()
        bpy.data.materials.remove(material)
    
    for image in bpy.data.images:
        bpy.data.images.remove(image)

    for mesh in bpy.data.meshes:
        bpy.data.meshes.remove(mesh)

    with target_path.open('rb') as f:
        reader = ChunkReader(f)
        loader = WhmLoader(module_root)
        loader.load(reader)
