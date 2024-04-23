import collections
import contextlib
import datetime
import math
import os
import pathlib
import struct

import bpy
import mathutils

from .utils import print

class ChunkWriter:
    def __init__(self, stream):
        self.stream = stream
        self.curr_data_size = 0
        self.curr_typeid = None

    @contextlib.contextmanager
    def start_chunk(
        self,
        typeid: str,
        version: int = 1,
        name: str = '',
    ):
        assert len(typeid) == 8, f'Incorrect typeid {repr(typeid)}'
        assert typeid[:4] in ('FOLD', 'DATA'), f'Incorrect typeid {repr(typeid)}'
        assert self.curr_typeid is None or self.curr_typeid[:4] == 'FOLD', f'Chunk of type {self.curr_typeid} cannot have children'
        parent_data_size = self.curr_data_size
        parent_typeid = self.curr_typeid
        self.curr_data_size = 0
        typeid_bytes = bytes(typeid, 'ascii')
        name_bytes = bytes(name, 'utf8')
        if name and not name_bytes.endswith(b'\0'):
            name_bytes += b'\0'
        header_fmt = f'<8slll{len(name_bytes)}s'
        self.stream.write(struct.pack(header_fmt, typeid_bytes, version, 0, len(name_bytes), name_bytes))
        current_pos = self.stream.tell()
        yield self
        self.stream.seek(current_pos - struct.calcsize(f'<ll{len(name_bytes)}s'), os.SEEK_SET)
        self.stream.write(struct.pack('<l', self.curr_data_size))
        self.curr_data_size = parent_data_size + struct.calcsize(header_fmt) + self.curr_data_size
        self.curr_typeid = parent_typeid
        self.stream.seek(0, os.SEEK_END)

    def write(self, data: bytes):
        self.curr_data_size += len(data)
        return self.stream.write(data)

    def write_struct(self, fmt: str, *args):
        self.write(struct.pack(fmt, *args))
    
    def write_str(self, s: str, encoding: str = 'utf8'):
        data = bytes(s, encoding)
        self.write_struct('<l', len(data))
        self.write(data)       


class FileDispatcher:
    def __init__(self, root: str, is_flat: bool):
        self.is_flat = is_flat
        self.root = pathlib.Path(root)
        self.file_info = []
        
    def add_path(self, path: str) -> pathlib.Path:
        rel_path = pathlib.Path(path).name if self.is_flat else path
        self.file_info.append((rel_path, path))
        return self.root / rel_path

    def dump_info(self):
        with open(self.root / 'info.txt', 'w') as f:
            for filename, dst in self.file_info:
                f.write(f'{filename} -> {dst}\n')

class WhmExporter:
    def __init__(self, paths: FileDispatcher, convert_textures: bool = True) -> None:
        self.messages = []
        self.paths = paths
        self.convert_textures = convert_textures
    
    def export(self, writer: ChunkWriter, object_name: str, meta: str = ''):
        self.write_relic_chunky(writer)
        self.write_meta(writer, meta)
        with writer.start_chunk('FOLDRSGM', version=3, name=object_name):
            self.write_textures(writer)
            self.write_skel(writer)
            self.write_meshes(writer)
            self.write_marks(writer)
            self.write_anims(writer)

    def write_relic_chunky(self, writer: ChunkWriter):
        writer.write(struct.pack('<12s3l', b'Relic Chunky', 1706509, 1, 1))

    def write_meta(self, writer: ChunkWriter, meta: str):
        with writer.start_chunk('DATAFBIF', name='FileBurnInfo'):
            writer.write_str('https://github.com/amorgun/blender_dow')
            writer.write_struct('<l', 0)
            writer.write_str(meta)
            writer.write_str(datetime.datetime.utcnow().strftime('%B %d, %I:%M:%S %p'))
        
    def write_textures(self, writer: ChunkWriter):
        for mat in bpy.data.materials:
            mat_path = mat.get('full_path')
            if mat_path is None:
                self.messages.append(('WARNING', f'No full_path for material {mat.name}'))
                continue
            with writer.start_chunk('DATASSHR', version=2, name=mat_path):
                writer.write_str(mat_path)
            for link in mat.node_tree.links:
                if (
                    link.to_node.bl_idname == 'ShaderNodeBsdfPrincipled'
                    and link.to_socket.bl_idname == 'NodeSocketColor'
                    and link.from_node.bl_idname == 'ShaderNodeTexImage'
                ):
                    image = link.from_node.image
                    data_path = pathlib.Path(image.filepath)
                    dst_path = self.paths.add_path(f'{mat_path}.rsh')
                    if data_path.suffix != '.dds':
                        self.messages.append(('WARNING', 'Cannot convert {image.filepath} image to .rsh.'))
                    
                    dst_path.parent.mkdir(exist_ok=True, parents=True)
                    with dst_path.open('wb') as f:
                        f.write(image.packed_file.data)
                    # TODO

    @classmethod
    def is_marker(cls, bone):
        return bone.name.startswith('marker_') or any('marker' in c.name.lower() for c in bone.collections)

    def write_skel(self, writer: ChunkWriter):
        if not bpy.data.armatures:
            return
        armature = bpy.data.armatures[0]
        if len(bpy.data.armatures) > 1:
            self.messages.append('WARNING', f'Found multiple armatures. Will export only the first one ({armature.name})')
        bone_tree = {}

        def insert_bone(bone):
            if bone.parent:
                data = insert_bone(bone.parent)
            else:
                data = bone_tree
            return data.setdefault(bone, {})
        
        bones = [b for b in armature.bones if not self.is_marker(b)]
        for bone in bones:
            insert_bone(bone)

        def iter_bones(data, lvl):
            for bone, child in data.items():
                yield bone, lvl
                yield from iter_bones(child, lvl + 1)

        self.bone_transforms = {}
        self.bone_to_idx = {}
        with writer.start_chunk('DATASKEL', version=5):
            writer.write_struct('<l', len(bones))
            delta = mathutils.Matrix.Rotation(math.radians(-90.0), 4, 'Z')
            for bone_idx, (bone, level) in enumerate(iter_bones(bone_tree, -1)):
                writer.write_str(bone.name)
                writer.write_struct('<l', level)
                if bone.parent:
                    parent_mat = self.bone_transforms[bone.parent]
                else:
                    parent_mat = mathutils.Matrix.Rotation(math.radians(90.0), 4, 'X')
                relative_matrix = parent_mat.inverted() @ bone.matrix_local @ delta.inverted()
                loc, rot, _ = relative_matrix.decompose()
                writer.write_struct('<3f', -loc.x, loc.y, loc.z)
                writer.write_struct('<4f', rot.x, -rot.y, -rot.z, rot.w)
                self.bone_to_idx[bone.name] = bone_idx
                self.bone_transforms[bone] = bone.matrix_local @ delta.inverted()

    def write_meshes(self, writer: ChunkWriter):
        no_custom_normals_warn = False
        with writer.start_chunk('FOLDMSGR'):
            for obj in bpy.data.objects:
                if obj.type != 'MESH':
                    continue
                mesh = obj.data
                many_bones_warn = False
                with writer.start_chunk('FOLDMSLC', name=mesh.name):
                    with writer.start_chunk('DATADATA', version=2):
                        writer.write_struct('<4xb8x', 1)
                        writer.write_struct('<l', len(obj.vertex_groups))
                        for g in obj.vertex_groups:
                            writer.write_str(g.name)
                            writer.write_struct('<l', self.bone_to_idx[g.name])
                        writer.write_struct('<l', len(mesh.vertices))
                        writer.write_struct('<l', 39 if obj.vertex_groups else 37)
                        for v in mesh.vertices:
                            writer.write_struct('<3f', -v.co.x, v.co.z, -v.co.y)
                        # TODO implicit bones
                        if obj.vertex_groups:
                            for v in mesh.vertices:
                                groups = sorted(v.groups, key=lambda x: -x.weight)
                                if len(groups) > 4:
                                    if not many_bones_warn:
                                        many_bones_warn = True
                                        self.messages.append('WARNING', f'Mesh {mesh.name} contains vertices weighted to more than 4 bones')
                                weights, bones_ids = [], []
                                for i in range(4):
                                    if i < len(groups):
                                        weights.append(groups[i].weight)
                                        bones_ids.append(self.bone_to_idx[obj.vertex_groups[groups[i].group].name])
                                    else:
                                        weights.append(0)
                                        bones_ids.append(-1)
                                writer.write_struct('<3f', *weights[:3])
                                writer.write_struct('<4b', *bones_ids)
                        normal_array = [mathutils.Vector([0, 0, 0]) for _ in mesh.vertices]
                        normal_cnt = [0] * len(mesh.vertices)
                        if not mesh.corner_normals:
                            if not no_custom_normals_warn:
                                self.messages.append('WARNING', f'Mesh {mesh.name} is missing custom normals')
                                no_custom_normals_warn = True
                        for normal, vert_idx in zip(mesh.corner_normals, (v for p in mesh.polygons for v in p.vertices)):
                            normal_array[vert_idx] += normal.vector
                            normal_cnt[vert_idx] += 1
                        for normal, cnt in zip(normal_array, normal_cnt):
                            normal /= cnt
                            writer.write_struct('<3f', -normal.x, normal.z, -normal.y)
                        if len(mesh.uv_layers) != 1:
                            self.messages.append('ERROR', f'Mesh {mesh.name} mush have exactly 1 uv layer')
                        uv_array = [mathutils.Vector([0, 0]) for _ in mesh.vertices]
                        for uv, loop in zip(mesh.uv_layers[0].uv, mesh.loops):
                            uv_array[loop.vertex_index] = uv.vector
                        for uv in uv_array:
                            writer.write_struct('<2f', uv.x, 1 - uv.y)
                        writer.write_struct('<4x')
                        materials = [(idx, m) for idx, m in enumerate(mesh.materials) if 'full_path' in m]
                        mat_faces = {}
                        for p in mesh.polygons:
                            mat_faces.setdefault(p.material_index, []).append(p.vertices)
                        writer.write_struct('<l', len(materials))
                        for mat_idx, mat in materials:
                            writer.write_str(mat['full_path'])
                            writer.write_struct('<l', len(mat_faces[mat_idx]) * 3)
                            for p in mat_faces[mat_idx]:
                                writer.write_struct('<3h', p[0], p[2], p[1])
                            writer.write_struct('<8x')
                        writer.write_struct('<lll', 0, 0, 0)
                    with writer.start_chunk('DATABVOL'):
                        writer.write_struct('<b60x', 1)  # TODO
            with writer.start_chunk('DATABVOL'):
                writer.write_struct('<b60x', 1)  # TODO

    def write_marks(self, writer: ChunkWriter):
        if not bpy.data.armatures:
            return
        armature = bpy.data.armatures[0]
        markers = [b for b in armature.bones if self.is_marker(b)]
        with writer.start_chunk('DATAMARK'):
            writer.write_struct('<l', len(markers))
            delta = mathutils.Matrix.Rotation(math.radians(-90.0), 4, 'Z')
            for marker in markers:
                writer.write_str(marker.name)
                writer.write_str(marker.parent.name if marker.parent else '')
                if marker.parent:
                    parent_mat = self.bone_transforms[marker.parent]
                else:
                    parent_mat = mathutils.Matrix.Rotation(math.radians(90.0), 4, 'X')
                transform = parent_mat.inverted() @ marker.matrix_local @ delta.inverted()
                for row_idx in range(3):
                    writer.write_struct('<3f', *transform[row_idx][:3])
                writer.write_struct('<3f', -transform[0][3], transform[1][3], transform[2][3])

    def write_anims(self, writer: ChunkWriter):
        anim_objects = [o for o in bpy.data.objects if o.animation_data and o.animation_data.action]
        if len(anim_objects) > 1:
            self.messages.append(('WARNING', 'Something is very wrong with animations'))
        for action in bpy.data.actions:
            anim_root = anim_objects[0]
            anim_sections = collections.defaultdict(dict)
            prop_fcurves = collections.defaultdict(dict)
            for fcurve in action.fcurves:
                if fcurve.is_empty:
                    continue
                attr = None
                if fcurve.data_path.endswith(']'):
                    path, attr = fcurve.data_path.rsplit('[', 1)
                    attr = attr[1:-2]
                    if '__' in attr:
                        prop_group, obj_name = attr.split('__', 1)
                        prop_fcurves[prop_group.lower()].setdefault(obj_name, []).append(fcurve)
                else:
                    for suffix in ['.rotation_quaternion', '.location']:
                        if fcurve.data_path.endswith(suffix):
                            path = fcurve.data_path[:-len(suffix)]
                            attr = suffix[1:]
                            break
                if not attr:
                    continue
                anim_obj = anim_root.path_resolve(path) if path else anim_root
                anim_sections[attr].setdefault(anim_obj, []).append(fcurve)

            with writer.start_chunk('FOLDANIM', version=3, name=action.name):
                with writer.start_chunk('DATADATA', version=2, name=action.name):
                    writer.write_struct('<l', int(action.frame_end) + 1)
                    writer.write_struct('<f', (action.frame_end + 1) / 30)
                    bones = {b for k in ['rotation_quaternion', 'location', 'stale'] for b in anim_sections[k]}
                    bones = sorted(bones, key=lambda x: self.bone_to_idx[x.name])
                    writer.write_struct('<l', len(bones))

                    delta = mathutils.Matrix.Rotation(math.radians(-90.0), 4, 'Z').to_4x4()
                    for bone in bones:
                        writer.write_str(bone.name)

                        if bone.bone.parent:
                            parent_mat = self.bone_transforms[bone.bone.parent]
                        else:
                            parent_mat = mathutils.Matrix.Rotation(math.radians(90.0), 4, 'X')

                        loc_fcurves = [None] * 3
                        loc_frames = {}
                        for fcurve in anim_sections['location'].get(bone, []):
                            loc_fcurves[fcurve.array_index] = fcurve
                            for keyframe in fcurve.keyframe_points:
                                frame, val = keyframe.co
                                frame_data = loc_frames.setdefault(frame, [None] * 3)
                                frame_data[fcurve.array_index] = val

                        writer.write_struct('<l', len(loc_frames))
                        curr_loc = bone.bone.head_local.copy()
                        for frame in sorted(loc_frames):
                            writer.write_struct('<f', frame / max(action.frame_end, 1))
                            frame_data = loc_frames[frame]
                            for idx in range(3):
                                if idx in frame_data:
                                    curr_loc[idx] = frame_data[idx]
                                elif loc_fcurves[idx] is not None:
                                    curr_loc[idx] = loc_fcurves[idx].evaluate(frame)
                            
                            relative_matrix = parent_mat.inverted() @ bone.bone.matrix_local @ mathutils.Matrix.Translation(curr_loc) @ delta.inverted()
                            loc, rot, _ = relative_matrix.decompose()
                            writer.write_struct('<3f', -loc.x, loc.y, loc.z)

                        rot_fcurves = [None] * 4
                        rot_frames = {}
                        for fcurve in anim_sections['rotation_quaternion'].get(bone, []):
                            rot_fcurves[fcurve.array_index] = fcurve
                            for keyframe in fcurve.keyframe_points:
                                frame, val = keyframe.co
                                frame_data = rot_frames.setdefault(frame, [None] * 4)
                                frame_data[fcurve.array_index] = val

                        writer.write_struct('<l', len(rot_frames))
                        curr_rot = bone.bone.matrix_local.to_quaternion()
                        for frame in sorted(rot_frames):
                            writer.write_struct('<f', frame / max(action.frame_end, 1))
                            frame_data = rot_frames[frame]
                            for idx in range(4):
                                if idx in frame_data:
                                    curr_rot[idx] = frame_data[idx]
                                elif rot_fcurves[idx] is not None:
                                    curr_rot[idx] = rot_fcurves[idx].evaluate(frame)
                            
                            relative_matrix = parent_mat.inverted() @ bone.bone.matrix_local @ curr_rot.to_matrix().to_4x4() @ delta.inverted()
                            loc, rot, _ = relative_matrix.decompose()
                            writer.write_struct('<4f', rot.x, -rot.y, -rot.z, rot.w)

                        stale_flag = int(bone not in anim_sections['Stale'])
                        writer.write_struct('<b', stale_flag)

                    mesh_fcurves = prop_fcurves['visibility'].keys() | prop_fcurves['force_invisible'].keys()
                    num_tex_fcurves = sum(len(fc) for g in ['uv_offset', 'uv_tiling'] for fc in prop_fcurves[g].values())
                    writer.write_struct('<l', len(mesh_fcurves) + num_tex_fcurves)
                    for mesh_name in mesh_fcurves:
                        writer.write_str(mesh_name)
                        writer.write_struct('<l', 2)  # mode
                        writer.write_struct('<8x')
                        vis_fcurves = prop_fcurves['visibility'].get(mesh_name, [])
                        if vis_fcurves:
                            fcurve = vis_fcurves[0]
                            keypoints = fcurve.keyframe_points
                            if list(keypoints[0].co) == [0., 1.]:
                                keypoints = keypoints[1:]
                            writer.write_struct('<l', len(keypoints) + 1)
                        else:
                            keypoints = []
                            writer.write_struct('<l', 0)
                        writer.write_struct('<4x')
                        force_invisible_fcurves = prop_fcurves['force_invisible'].get(mesh_name, [])
                        force_invisible = force_invisible_fcurves[0].keyframe_points[0].co[1]  if force_invisible_fcurves else 1
                        writer.write_struct('<l', int(not force_invisible))
                        for point in keypoints:
                            frame, val = point.co
                            writer.write_struct('<2f', frame / max(action.frame_end, 1), val)

                    for group in ['uv_offset', 'uv_tiling']:
                         for tex_short_name, fcurves in prop_fcurves[group].items():
                             mat = bpy.data.materials[tex_short_name]
                             mat_path = mat.get('full_path')
                             if mat_path is None:
                                 continue
                             for fcurve in fcurves:
                                writer.write_str(mat_path)
                                writer.write_struct('<l', 0)  # mode
                                writer.write_struct('<4x')
                                tex_anim_type, mult = {
                                    ('uv_offset', 0): (1, 1),
                                    ('uv_offset', 1): (2, -1),
                                    ('uv_tiling', 0): (3, -1),
                                    ('uv_tiling', 1): (4, -1),
                                }[group, fcurve.index]
                                writer.write_struct('<l', tex_anim_type, len(fcurve.keyframes))
                                for point in fcurve.keyframes:
                                    frame, val = point.co
                                    writer.write_struct('<2f', frame / max(action.frame_end, 1), val * mult)
                    writer.write_struct('<l', 0)  # cameras
                with writer.start_chunk('DATAANBV', version=1, name=action.name):
                    writer.write_struct('<24x')  # TODO


def export_whm(path: str):
    print('------------------')
    with open(path, 'wb') as f:
        writer = ChunkWriter(f)
        paths = FileDispatcher(pathlib.Path(path).with_suffix(''), is_flat=True)
        exporter = WhmExporter(paths)
        exporter.export(writer, object_name=pathlib.Path(bpy.data.filepath).stem, meta='amorgun')
        paths.dump_info()
