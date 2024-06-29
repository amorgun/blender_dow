import collections
import contextlib
import datetime
import enum
import io
import math
import pathlib

import bpy
import mathutils

from . import textures
from .chunky import ChunkWriter
from .utils import print


@enum.unique
class ExportFormat(enum.Enum):
    WHM = enum.auto()
    SGM = enum.auto()


class FileDispatcher:
    @enum.unique
    class Layout(enum.Enum):
        FLAT = enum.auto()
        FLAT_FOLDERS = enum.auto()
        FULL_PATH = enum.auto()

    def __init__(self, root: str, layout: Layout):
        self.root = pathlib.Path(root)
        self.layout = layout
        self.file_info = []
        
    def get_path(self, path: str, require_parent=False) -> pathlib.Path:
        path = pathlib.Path(path)
        match self.layout:
            case FileDispatcher.Layout.FLAT: rel_path = path.name
            case FileDispatcher.Layout.FLAT_FOLDERS: rel_path = f'{path.parent.name}/{path.name}' if require_parent else path.name
            case FileDispatcher.Layout.FULL_PATH: rel_path = path
        return self.root / rel_path

    def add_info(self, declared_path: str, real_path: pathlib.Path):
        self.file_info.append((real_path.relative_to(self.root), declared_path))

    def dump_info(self):
        if self.layout is FileDispatcher.Layout.FULL_PATH:
            return
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / 'info.txt', 'w') as f:
            for filename, dst in self.file_info:
                f.write(f'{filename} -> {dst}\n')


CHUNK_VERSIONS = {
    ExportFormat.WHM: {
        'DATAFBIF': {'version': 1},
        'FOLDRSGM': {
            'version': 3,
            'DATASSHR': {'version': 2},
            'DATASKEL': {'version': 5},
            'FOLDMSGR': {
                'version': 1,
                'FOLDMSLC': {
                    'version': 1,
                    'DATADATA': {'version': 2},
                    'DATABVOL': {'version': 2},
                },
                'DATADATA': {'version': 1},
                'DATABVOL': {'version': 2},
            },
            'DATAMARK': {'version': 1},
            'FOLDANIM': {
                'version': 3,
                'DATADATA': {'version': 2},
                'DATAANBV': {'version': 1},
            },
        }
    },
    ExportFormat.SGM: {
        'FOLDRSGM': {
            'version': 1,
            'DATASSHR': {'version': 1},
            'FOLDSKEL': {
                'version': 3,
                'DATAINFO': {'version': 1},
                'DATABONE': {'version': 5},
            },
            'FOLDMSGR': {
                'version': 1,
                'FOLDMSLC': {
                    'version': 1,
                    'DATADATA': {'version': 2},
                    'DATABVOL': {'version': 2},
                },
                'DATADATA': {'version': 1},
                'DATABVOL': {'version': 2},
            },
            'DATAMARK': {'version': 1},
            'FOLDANIM': {
                'version': 2,
                'FOLDDATA': {
                    'version': 3,
                    'DATAINFO': {'version': 5},
                    'DATABANM': {'version': 2},
                    'DATACANM': {'version': 2},
                }
            }
        }
    }
}


class Exporter:
    def __init__(
            self,
            paths: FileDispatcher,
            format: ExportFormat = ExportFormat.WHM,
            convert_textures: bool = True,
            default_texture_path: str = '',
            context=None,
        ) -> None:
        self.messages = []
        self.paths = paths
        self.format = format
        self.convert_textures = convert_textures
        self.default_texture_path = pathlib.PurePosixPath(default_texture_path)
        self.bpy_context = context
        if self.bpy_context is None:
            self.bpy_context = bpy.context

        self.armature_obj = None
        self.exported_bones = None
        self.exported_meshes = None
        self.exported_materials = None

    def export(self, writer: ChunkWriter, object_name: str, meta: str = ''):
        current_mode = bpy.context.mode
        mode_context = contextlib.ExitStack()
        if current_mode != 'OBJECT':
            bpy.ops.object.mode_set(mode='OBJECT', toggle=True)
            mode_context.callback(bpy.ops.object.mode_set, mode='OBJECT', toggle=True)
        with mode_context:
            self.write_relic_chunky(writer)
            if self.format is ExportFormat.WHM:
                self.write_meta(writer, meta)
            with writer.start_chunk('FOLDRSGM', name=object_name):
                self.write_textures(writer)
                self.write_skel(writer)
                orig_pose = {}
                for bone in self.armature_obj.pose.bones:
                    orig_pose[bone] = bone.matrix_basis.copy()
                    bone.matrix_basis = mathutils.Matrix()
                self.write_meshes(writer)
                for bone in self.armature_obj.pose.bones:
                    bone.matrix_basis = orig_pose[bone]
                self.write_marks(writer)
                self.write_anims(writer)

    def start_chunk(self, writer: ChunkWriter, format: ExportFormat, *args, **kwargs):
        if format == self.format:
            return writer.start_chunk(*args, **kwargs)
        return contextlib.nullcontext()

    def write_relic_chunky(self, writer: ChunkWriter):
        writer.write_struct('<12s3l', b'Relic Chunky', 1706509, 1, 1)

    def write_meta(self, writer: ChunkWriter, meta: str):
        with writer.start_chunk('DATAFBIF', name='FileBurnInfo'):
            writer.write_str('https://github.com/amorgun/blender_dow')
            writer.write_struct('<l', 0)
            writer.write_str(meta)
            writer.write_str(datetime.datetime.utcnow().strftime('%B %d, %I:%M:%S %p'))

    def write_textures(self, writer: ChunkWriter):
        self.exported_materials = {}
        for mat in bpy.data.materials:
            if not mat.node_tree:
                self.messages.append(('WARNING', f'No nodes for material {mat.name}'))
                continue
            mat_path = mat.get('full_path', self.default_texture_path / mat.name)
            mat_path = pathlib.PurePosixPath(mat_path)

            exported_nodes = {k: mat.node_tree.nodes.get(k) for k in ['diffuse', 'specularity', 'reflection', 'self_illumination', 'opacity']}
            for slot, input_idname in [
                ('diffuse', 'Base Color'),
                ('specularity', 'Specular IOR Level'),
                ('reflection', 'Specular Tint'),
                ('self_illumination', 'Emission Strength'),
            ]:
                if not exported_nodes[slot]:
                    for link in mat.node_tree.links:
                        if (
                            link.to_node.bl_idname == 'ShaderNodeBsdfPrincipled'
                            and link.to_socket.label == input_idname
                            and link.from_node.bl_idname == 'ShaderNodeTexImage'
                        ):
                            exported_nodes[slot] = link.from_node
                            break
            if not exported_nodes['diffuse']:
                for node in mat.node_tree.nodes:
                    if node.bl_idname == 'ShaderNodeTexImage':
                        exported_nodes['diffuse'] = node
                        break

            if not exported_nodes['diffuse']:
                self.messages.append(('WARNING', f'Cannot find a texture for material {mat.name}'))
                continue

            with writer.start_chunk('DATASSHR', name=str(mat_path)):
                writer.write_str(str(mat_path))
            self.exported_materials[mat.name] = str(mat_path)

            if self.convert_textures:
                dst_path = self.paths.get_path(f'{mat_path}.rsh')
                if self.export_rsh(
                    {k: v.image if v else v for k, v in exported_nodes.items()},
                    dst_path,
                    mat_path,
                ):
                    self.paths.add_info(f'{mat_path}.rsh', dst_path)
                    continue
            for slot, node in exported_nodes.items():
                if node is None:
                    continue
                image_name = mat_path.name or pathlib.Path(node.image.filepath).name or node.image.name

                def get_suffix(path: str):
                    suffix = pathlib.Path(path).suffix
                    if suffix and any(i.isalpha() for i in suffix):
                        return suffix
                    return ''

                suffix = get_suffix(node.image.filepath) or get_suffix(node.image.name) or get_suffix(mat.name)
                dst_file = self.paths.get_path((mat_path / image_name).with_suffix(suffix), require_parent=True)
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                if node.image.packed_files:
                    with dst_file.open('wb') as f:
                        f.write(node.image.packed_file.data)
                else:
                    try:
                        node.image.save(filepath=str(dst_file))
                    except Exception as e:
                        self.messages.append(('WARNING', f'Error while converting image {node.image.name}: {e!r}'))
                        continue
                self.paths.add_info(f'{mat_path}.rsh', dst_file)

    def export_rsh(self, images, dst_path: pathlib.Path, declared_path: pathlib.PurePosixPath) -> bool:
        import tempfile
        import shutil

        with tempfile.TemporaryDirectory() as t:
            temp_dir = pathlib.Path(t)
            exported_file = temp_dir / dst_path.name
            texture_declared_paths = {}
            with exported_file.open('wb') as f:
                writer = ChunkWriter(f, {
                    'FOLDSHRF': {
                        'version': 1,
                        'FOLDTXTR': {
                            'version': 1,
                            'DATAHEAD': {'version': 1},
                            'FOLDIMAG': {
                                'version': 1,
                                'DATAATTR': {'version': 2},
                                'DATADATA': {'version': 2},
                            }
                        },
                        'FOLDSHDR': {
                            'version': 1,
                            'DATAINFO': {'version': 1},
                            'DATACHAN': {'version': 3},
                        }
                    }
                })
                self.write_relic_chunky(writer)
                with writer.start_chunk('FOLDSHRF', name=str(declared_path)):
                    for key in ['diffuse', 'specularity', 'reflection', 'self_illumination', 'opacity']:
                        image = images.get(key)
                        if not image:
                            continue
                        image_path = pathlib.Path(image.filepath)
                        if '.dds' in image_path.name:
                            texture_filename = image_path.name[:image_path.name.rfind('.dds')]
                        else:
                            texture_filename = image_path.name or image.name
                        texture_declared_path = declared_path.parent / texture_filename
                        texture_declared_paths[key] = texture_declared_path

                        if image.packed_files:
                            dds_stream = io.BytesIO(image.packed_file.data)
                        else:
                            try:
                                dds_path = (temp_dir/texture_filename).with_suffix('.dds')
                                image.save(filepath=str(dds_path))
                                dds_stream = dds_path.open('rb')
                            except Exception as e:
                                self.messages.append(('WARNING', f'Error while converting {image_path}: {e!r}'))
                                return False
                        with dds_stream:
                            try:
                                is_dds, height, width, data_size, num_mips, image_format, image_type = textures.read_dds_header(dds_stream)
                            except Exception as e:
                                self.messages.append(('WARNING', f'Error while converting {image_path}: {e!r}'))
                                return False
                            if not is_dds:
                                self.messages.append(('WARNING', f'Can only convert .dds to .rsh ({image.name})'))
                                return False
                            with writer.start_chunk('FOLDTXTR', name=str(texture_declared_path)):
                                with writer.start_chunk('DATAHEAD'):
                                    writer.write_struct('<2l', image_type, 1)  # num_images
                                with writer.start_chunk('FOLDIMAG'):
                                    with writer.start_chunk('DATAATTR'):
                                        writer.write_struct('<4l', image_format, width, height, num_mips)
                                    with writer.start_chunk('DATADATA'):
                                        writer.write(dds_stream.read(data_size))
                    with writer.start_chunk('FOLDSHDR', name=str(declared_path)):
                        with writer.start_chunk('DATAINFO'):
                            writer.write_struct('<8x B 8x', 1)
                        for channel_idx, key in enumerate(['diffuse', 'specularity', 'reflection', 'self_illumination', 'opacity', 'unknown']):
                            with writer.start_chunk('DATACHAN'):
                                writer.write_struct('<2l4B', channel_idx, 0, *([255, 255, 255, 255] if channel_idx == 4 else [0, 0, 0, 0]))
                                writer.write_str(str(texture_declared_paths.get(key, '')))
                                writer.write_struct('<4x l 4x', 4)  # num_coords
                                for c in [(1.0, 0.0), (0.0, 0.0), (0.0, 1.0), (0.0, 0.0)]:
                                    writer.write_struct('<2f', *c)
            dst_path.parent.mkdir(exist_ok=True, parents=True)
            shutil.copy(exported_file, dst_path)
        return True

    @classmethod
    def is_marker(cls, bone):
        return bone.name.startswith('marker_') or any('marker' in c.name.lower() for c in bone.collections)

    def write_skel(self, writer: ChunkWriter):
        all_armatures = [a for a in bpy.data.objects if a.type == 'ARMATURE' if a.data.bones]
        if not all_armatures:
            self.messages.append(('WARNING', f'Cannot find an armature.'))
            return
        self.armature_obj = all_armatures[0]
        armature = self.armature_obj.data
        if len(all_armatures) > 1:
            self.messages.append(('WARNING', f'Found multiple armatures. Will export only the first one ({armature.name})'))
        bone_tree = {}

        def insert_bone(bone):
            if bone.parent:
                data = insert_bone(bone.parent)
            else:
                data = bone_tree
            return data.setdefault(bone, {})
        
        if 'Skeleton' in armature.collections:
            bones = armature.collections['Skeleton'].bones
        else:
            bones = [b for b in armature.bones if not self.is_marker(b)]
        self.exported_bones = bones
        for bone in bones:
            insert_bone(bone)

        def iter_bones(data, lvl):
            for bone, child in data.items():
                yield bone, lvl
                yield from iter_bones(child, lvl + 1)

        self.bone_transforms = {}
        self.bone_to_idx = {}
        with self.start_chunk(writer, ExportFormat.WHM, 'DATASKEL'), \
            self.start_chunk(writer, ExportFormat.SGM, 'FOLDSKEL'):
            with self.start_chunk(writer, ExportFormat.SGM, 'DATAINFO'):
                writer.write_struct('<l', len(bones))
            delta = mathutils.Matrix.Rotation(math.radians(-90.0), 4, 'Z')
            for bone_idx, (bone, level) in enumerate(iter_bones(bone_tree, -1)):
                with self.start_chunk(writer, ExportFormat.SGM, 'DATABONE', name=bone.name):
                    if self.format is ExportFormat.WHM:
                        writer.write_str(bone.name)
                    writer.write_struct('<l', self.bone_to_idx[bone.parent.name] if bone.parent else -1)
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
        single_bone_meshes = {}
        self.exported_meshes = []
        depsgraph = self.bpy_context.evaluated_depsgraph_get()
        global_min_corner, global_max_corner = mathutils.Vector([float('+inf')]*3), mathutils.Vector([float('-inf')]*3)
        with writer.start_chunk('FOLDMSGR'):
            for obj_orig in bpy.data.objects:
                if obj_orig.type != 'MESH':
                    continue
                obj = obj_orig.evaluated_get(depsgraph)
                mesh = obj.data
                if len(mesh.materials) == 0:
                    self.messages.append(('WARNING', f'Skipping mesh {obj.name} because it has no materials'))
                    continue

                self.exported_meshes.append(obj.name)
                vert_warn = False
                many_bones_warn = False
                with writer.start_chunk('FOLDMSLC', name=obj.name):
                    with writer.start_chunk('DATADATA'):
                        writer.write_struct('<4xbL4x', 1, len(mesh.polygons))
                        vertex_groups = [v for v in obj.vertex_groups if v.name in self.bone_to_idx]
                        if len(vertex_groups) == 1 and all(len(v.groups) == 1 and v.groups[0].weight > 0.995 for v in mesh.vertices):
                            assert vertex_groups[0].name in self.bone_to_idx, f'Cannot find bone {vertex_groups[0].name} for mesh {obj.name}'
                            single_bone_meshes[obj.name] = self.bone_to_idx[vertex_groups[0].name]
                            vertex_groups = []
                        writer.write_struct('<l', len(vertex_groups))
                        for g in vertex_groups:
                            writer.write_str(g.name)
                            writer.write_struct('<l', self.bone_to_idx[g.name])
                        writer.write_struct('<l', len(mesh.vertices))
                        writer.write_struct('<l', 39 if vertex_groups else 37)
                        for v in mesh.vertices:
                            writer.write_struct('<3f', -v.co.x, v.co.z, -v.co.y)
                        exported_vertex_groups = {g.name for g in vertex_groups}
                        if vertex_groups:
                            for v in mesh.vertices:
                                groups = sorted([
                                    g for g in v.groups if obj.vertex_groups[g.group].name in exported_vertex_groups],
                                    key=lambda x: -x.weight)
                                if len(groups) > 4:
                                    if not many_bones_warn:
                                        many_bones_warn = True
                                        self.messages.append(('WARNING', f'Mesh {obj.name} contains vertices weighted to more than 4 bones'))
                                weights, bones_ids = [], []
                                for i in range(4):
                                    if i < len(groups):
                                        weights.append(groups[i].weight)
                                        bones_ids.append(self.bone_to_idx[obj.vertex_groups[groups[i].group].name])
                                    else:
                                        weights.append(0)
                                        bones_ids.append(255)
                                writer.write_struct('<3f', *weights[:3])
                                writer.write_struct('<4B', *bones_ids)
                        normal_array = [mathutils.Vector([0, 0, 0]) for _ in mesh.vertices]
                        normal_cnt = [0] * len(mesh.vertices)
                        if not mesh.corner_normals:
                            if not no_custom_normals_warn:
                                self.messages.append(('WARNING', f'Mesh {obj.name} is missing custom normals'))
                                no_custom_normals_warn = True
                        for normal, vert_idx in zip(mesh.corner_normals, (v for p in mesh.polygons for v in p.vertices)):
                            normal_array[vert_idx] += normal.vector
                            normal_cnt[vert_idx] += 1
                        for normal, cnt in zip(normal_array, normal_cnt):
                            normal = normal / cnt if cnt else normal
                            writer.write_struct('<3f', -normal.x, normal.z, -normal.y)
                        if len(mesh.uv_layers) != 1:
                            self.messages.append(('ERROR', f'Mesh {obj.name} mush have exactly 1 uv layer'))
                        uv_array = [mathutils.Vector([0, 0]) for _ in mesh.vertices]
                        for uv, loop in zip(mesh.uv_layers[0].uv, mesh.loops):
                            uv_array[loop.vertex_index] = uv.vector
                        for uv in uv_array:
                            writer.write_struct('<2f', uv.x, 1 - uv.y)
                        if self.format is ExportFormat.WHM:
                            writer.write_struct('<4x')
                        mat_faces = {}
                        for p in mesh.polygons:
                            mat_faces.setdefault(p.material_index, []).append(p.vertices)
                        materials = [(idx, m) for idx, m in enumerate(mesh.materials) if idx in mat_faces]
                        writer.write_struct('<l', len(materials))
                        for mat_idx, mat in materials:
                            writer.write_str(self.exported_materials.get(mat.name, f'missing_mat_{mat_idx}'))
                            writer.write_struct('<l', len(mat_faces[mat_idx]) * 3)
                            min_poly_idx, max_poly_idx = float('+inf'), float('-inf')
                            for p in mat_faces[mat_idx]:
                                assert len(p) == 3, f'Encountered non-triangular face at mesh {obj.name}. Triangulate it before exporting.'
                                if not all(0 <= v <= 65535 for v in p):
                                    if not vert_warn:
                                        vert_warn = True
                                        self.messages.append(('WARNING', f'Mesh {obj.name} contains more than 65535 vertices'))
                                    p = [0,0,0]
                                writer.write_struct('<3H', p[0], p[2], p[1])
                                min_poly_idx = min(min_poly_idx, *p)
                                max_poly_idx = max(max_poly_idx, *p)
                            writer.write_struct('<2H', min_poly_idx, max_poly_idx - min_poly_idx + 1)
                            if self.format is ExportFormat.WHM:
                                writer.write_struct('<4x')
                        if self.format is ExportFormat.WHM:
                            writer.write_struct('<lll', 0, 0, 0)  # SHADOW VOLUME
                    with writer.start_chunk('DATABVOL'):
                        min_corner = mathutils.Vector([
                            min(-v.co.x for v in mesh.vertices),
                            min(v.co.z for v in mesh.vertices),
                            min(-v.co.y for v in mesh.vertices),
                        ])
                        max_corner = mathutils.Vector([
                            max(-v.co.x for v in mesh.vertices),
                            max(v.co.z for v in mesh.vertices),
                            max(-v.co.y for v in mesh.vertices),
                        ])
                        global_min_corner = mathutils.Vector([min(v, g) for v, g in zip(min_corner, global_min_corner)])
                        global_max_corner = mathutils.Vector([max(v, g) for v, g in zip(max_corner, global_max_corner)])
                        writer.write_struct(
                            '<b 3f 3f 9f',
                            1, *(max_corner + min_corner) / 2,
                            *(max_corner - min_corner) / 2,
                            *[i for r in mathutils.Matrix.Identity(3) for i in r]
                        )
            with writer.start_chunk('DATADATA'):
                # TODO xrefs
                writer.write_struct('<l', len(self.exported_meshes))
                for mesh_name in self.exported_meshes:
                    writer.write_str(mesh_name)
                    writer.write_str('')  # mesh path
                    writer.write_struct('<l', single_bone_meshes.get(mesh_name, -1))
            with writer.start_chunk('DATABVOL'):
                writer.write_struct(
                    '<b 3f 3f 9f',
                    1, *(global_max_corner + global_min_corner) / 2,
                    *(global_max_corner - global_min_corner) / 2,
                    *[i for r in mathutils.Matrix.Identity(3) for i in r]
                )

    def write_marks(self, writer: ChunkWriter):
        if not bpy.data.armatures:
            return
        armature = bpy.data.armatures[0]
        if 'Markers' in armature.collections:
            markers = armature.collections['Markers'].bones
        else:
            markers = [b for b in armature.bones if self.is_marker(b)]
        with self.start_chunk(writer, ExportFormat.WHM, 'DATAMARK'):
            if self.format is ExportFormat.WHM:
                writer.write_struct('<l', len(markers))
            delta = mathutils.Matrix.Rotation(math.radians(-90.0), 4, 'Z')
            for marker in markers:
                with self.start_chunk(writer, ExportFormat.SGM, 'DATAMARK', name=marker.name):
                    if self.format is ExportFormat.WHM:
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
                self.bone_transforms[marker] = marker.matrix_local @ delta.inverted()

    def write_anims(self, writer: ChunkWriter):
        anim_objects = [o for o in bpy.data.objects if o.type == 'ARMATURE' and o.animation_data]
        if len(anim_objects) == 0:
            self.messages.append(('WARNING', 'Cannot find the animation root object'))
            return
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
                anim_sections[attr.lower()].setdefault(anim_obj, []).append(fcurve)

            with writer.start_chunk('FOLDANIM', name=action.name):
                with self.start_chunk(writer, ExportFormat.WHM, 'DATADATA', name=action.name), \
                    self.start_chunk(writer, ExportFormat.SGM, 'FOLDDATA', name=action.name):
                    frame_end = action.frame_end or max((k.co[0] for fcurve in action.fcurves for k in fcurve.keyframe_points), default=0)
                    with self.start_chunk(writer, ExportFormat.SGM, 'DATAINFO'):
                        writer.write_struct('<l', int(frame_end) + 1)
                        writer.write_struct('<f', (frame_end + 1) / 30)
                    bones = [anim_root.pose.bones[b.name] for b in self.exported_bones]
                    bones = sorted(bones, key=lambda x: self.bone_to_idx[x.name])
                    if self.format is ExportFormat.WHM:
                        writer.write_struct('<l', len(bones))

                    delta = mathutils.Matrix.Rotation(math.radians(-90.0), 4, 'Z').to_4x4()
                    for bone_obj in bones:
                        bone = bone_obj.bone
                        if bone.parent:
                            parent_mat = self.bone_transforms[bone.parent]
                        else:
                            parent_mat = mathutils.Matrix.Rotation(math.radians(90.0), 4, 'X')
                        if self.format is ExportFormat.WHM:
                            writer.write_str(bone.name)

                        with self.start_chunk(writer, ExportFormat.SGM, 'DATABANM', name=bone.name):
                            loc_fcurves = [None] * 3
                            loc_frames = {}
                            for fcurve in anim_sections['location'].get(bone_obj, []):
                                loc_fcurves[fcurve.array_index] = fcurve
                                for keyframe in fcurve.keyframe_points:
                                    frame, val = keyframe.co
                                    frame_data = loc_frames.setdefault(frame, [None] * 3)
                                    frame_data[fcurve.array_index] = val

                            writer.write_struct('<l', len(loc_frames))
                            curr_loc = bone.head_local.copy()
                            for frame in sorted(loc_frames):
                                writer.write_struct('<f', frame / max(frame_end, 1))
                                frame_data = loc_frames[frame]
                                for idx in range(3):
                                    if idx in frame_data:
                                        curr_loc[idx] = frame_data[idx]
                                    elif loc_fcurves[idx] is not None:
                                        curr_loc[idx] = loc_fcurves[idx].evaluate(frame)

                                relative_matrix = parent_mat.inverted() @ bone.matrix_local @ mathutils.Matrix.Translation(curr_loc) @ delta.inverted()
                                loc, rot, _ = relative_matrix.decompose()
                                writer.write_struct('<3f', -loc.x, loc.y, loc.z)

                            rot_fcurves = [None] * 4
                            rot_frames = {}
                            for fcurve in anim_sections['rotation_quaternion'].get(bone_obj, []):
                                rot_fcurves[fcurve.array_index] = fcurve
                                for keyframe in fcurve.keyframe_points:
                                    frame, val = keyframe.co
                                    frame_data = rot_frames.setdefault(frame, [None] * 4)
                                    frame_data[fcurve.array_index] = val

                            writer.write_struct('<l', len(rot_frames))
                            curr_rot = bone.matrix_local.to_quaternion()
                            prev_rot = curr_rot
                            for frame in sorted(rot_frames):
                                writer.write_struct('<f', frame / max(frame_end, 1))
                                frame_data = rot_frames[frame]
                                for idx in range(4):
                                    if idx in frame_data:
                                        curr_rot[idx] = frame_data[idx]
                                    elif rot_fcurves[idx] is not None:
                                        curr_rot[idx] = rot_fcurves[idx].evaluate(frame)

                                relative_matrix = parent_mat.inverted() @ bone.matrix_local @ curr_rot.to_matrix().to_4x4() @ delta.inverted()
                                loc, rot, _ = relative_matrix.decompose()
                                rot.make_compatible(prev_rot)
                                prev_rot = rot
                                writer.write_struct('<4f', rot.x, -rot.y, -rot.z, rot.w)

                            stale_flag = int(bone_obj not in anim_sections['stale'])
                            writer.write_struct('<b', stale_flag)

                    mesh_fcurves = prop_fcurves['visibility'].keys() | prop_fcurves['force_invisible'].keys()
                    num_tex_fcurves = sum(len(fc) for g in ['uv_offset', 'uv_tiling'] for fc in prop_fcurves[g].values())
                    if self.format is ExportFormat.WHM:
                        writer.write_struct('<l', len(self.exported_meshes) + num_tex_fcurves)

                    for mesh_name in self.exported_meshes:
                        if self.format is ExportFormat.WHM:
                            writer.write_str(mesh_name)
                        with self.start_chunk(writer, ExportFormat.SGM, 'DATACANM', name=mesh_name):
                            writer.write_struct('<l', 2)  # mode
                            writer.write_struct('<8x')
                            vis_fcurves = prop_fcurves['visibility'].get(mesh_name, [])
                            if vis_fcurves:
                                fcurve = vis_fcurves[0]
                                keypoints = fcurve.keyframe_points
                                if list(keypoints[0].co) == [0., 1.]:
                                    keypoints = keypoints[1:]
                            else:
                                keypoints = []
                            writer.write_struct('<l', len(keypoints) + 1)
                            writer.write_struct('<4x')
                            force_invisible_fcurves = prop_fcurves['force_invisible'].get(mesh_name, [])
                            force_invisible = force_invisible_fcurves[0].keyframe_points[0].co[1] if force_invisible_fcurves else 0
                            writer.write_struct('<f', not force_invisible)
                            for point in keypoints:
                                frame, val = point.co
                                writer.write_struct('<2f', frame / max(frame_end, 1), val)

                    for group in ['uv_offset', 'uv_tiling']:
                         for tex_short_name, fcurves in prop_fcurves[group].items():
                             mat = bpy.data.materials[tex_short_name]
                             mat_path = mat.get('full_path')
                             if mat_path is None:
                                 continue
                             for fcurve in fcurves:
                                if self.format is ExportFormat.WHM:
                                    writer.write_str(mat_path)
                                with self.start_chunk(writer, ExportFormat.SGM, 'DATACANM', name=mat_path):
                                    writer.write_struct('<l', 0)  # mode
                                    writer.write_struct('<4x')
                                    tex_anim_type, mult = {
                                        ('uv_offset', 0): (1, 1),
                                        ('uv_offset', 1): (2, -1),
                                        ('uv_tiling', 0): (3, -1),
                                        ('uv_tiling', 1): (4, -1),
                                    }[group, fcurve.array_index]
                                    writer.write_struct('<2l', tex_anim_type, len(fcurve.keyframe_points))
                                    for point in fcurve.keyframe_points:
                                        frame, val = point.co
                                        writer.write_struct('<2f', frame / max(frame_end, 1), val * mult)
                    if self.format is ExportFormat.WHM:
                        writer.write_struct('<l', 0)  # cameras
                        # TODO DATACMRA
                if self.format is ExportFormat.WHM:
                    with writer.start_chunk('DATAANBV', name=action.name):
                        writer.write_struct('<24x')  # TODO


def export_whm(path: str):
    print('------------------')
    with open(path, 'wb') as f:
        fmt = ExportFormat.WHM
        writer = ChunkWriter(f, CHUNK_VERSIONS[fmt])
        paths = FileDispatcher(pathlib.Path(path).with_suffix(''), layout=FileDispatcher.Layout.FLAT)
        exporter = Exporter(paths, format=fmt)
        try:
            exporter.export(writer, object_name=pathlib.Path(bpy.data.filepath).stem, meta='amorgun')
            paths.dump_info()
        finally:
            for _, msg in exporter.messages:
                print(msg)
