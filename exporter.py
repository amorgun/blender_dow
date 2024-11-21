import ast
import collections
import contextlib
import dataclasses
import datetime
import enum
import io
import math
import pathlib
import shutil
import tempfile

import bpy
import mathutils

from . import textures, utils, props
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


@dataclasses.dataclass
class VertexInfo:
    position: list
    vertex_groups: list
    normal: list
    uv: list


CHUNK_VERSIONS = {
    ExportFormat.WHM: {
        'DATAFBIF': {'version': 1},
        'FOLDRSGM': {
            'version': 3,
            'DATASSHR': {'version': 2},
            'FOLDTXTR': {
                'version': 1,
                'DATAHEAD': {'version': 1},
                'DATAINFO': {'version': 3},
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
            },
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
            'DATACAMS': {'version': 1},
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
            'FOLDTXTR': {
                'version': 1,
                'DATAHEAD': {'version': 1},
                'DATAINFO': {'version': 3},
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
            },
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
            'DATACMRA': {'version': 1},
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
            max_texture_size: int = 1024,
            make_oe_compatable_textures: bool = True,
            context=None,
        ) -> None:
        self.messages = []
        self.paths = paths
        self.format = format
        self.convert_textures = convert_textures
        self.default_texture_path = pathlib.PurePosixPath(default_texture_path)
        self.max_texture_size = max_texture_size
        self.make_oe_compatable_textures = make_oe_compatable_textures
        self.bpy_context = context if context is not None else bpy.context

        self.armature_obj = None
        self.exported_bones = None
        self.exported_meshes = None
        self.exported_materials = None
        self.bone_to_idx = {}

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
                self.write_materials(writer)
                self.write_skel(writer)
                orig_pose = {}
                if self.armature_obj:
                    for bone in self.armature_obj.pose.bones:
                        orig_pose[bone] = bone.matrix_basis.copy()
                        bone.matrix_basis = mathutils.Matrix()
                self.write_meshes(writer)
                if self.armature_obj:
                    for bone in self.armature_obj.pose.bones:
                        bone.matrix_basis = orig_pose[bone]
                self.write_marks(writer)
                self.write_anims(writer)
        self.messages.append(('INFO', f'Model exported successfully'))

    def start_chunk(self, writer: ChunkWriter, format: ExportFormat, *args, **kwargs):
        if format == self.format:
            return writer.start_chunk(*args, **kwargs)
        return contextlib.nullcontext()

    def get_material_path(self, mat) -> str:
        user_path = mat.get('full_path', None)
        if user_path and user_path.strip():
            return user_path
        return str(self.default_texture_path / mat.name)

    def write_relic_chunky(self, writer: ChunkWriter):
        writer.write_struct('<12s3l', b'Relic Chunky', 1706509, 1, 1)

    def write_meta(self, writer: ChunkWriter, meta: str):
        with writer.start_chunk('DATAFBIF', name='FileBurnInfo'):
            writer.write_str('https://github.com/amorgun/blender_dow')
            writer.write_struct('<l', 0)
            writer.write_str(meta)
            writer.write_str(datetime.datetime.utcnow().strftime('%B %d, %I:%M:%S %p'))

    def write_materials(self, writer: ChunkWriter):
        self.exported_materials = {}
        mat_users = bpy.data.user_map(subset=bpy.data.materials)
        for mat in bpy.data.materials:
            if not mat_users[mat]:
                continue
            try:
                self.write_material(writer, mat)
            except Exception:
                self.messages.append(('ERROR', f'Error while exporting material {mat.name}'))
                raise

    def write_material(self, writer: ChunkWriter, mat) -> bool:
        if not mat.node_tree:
            self.messages.append(('WARNING', f'No nodes for material {mat.name}'))
            return

        mat_path = pathlib.PurePosixPath(self.get_material_path(mat))
        exported_nodes = {node.label: node
                        for node in mat.node_tree.nodes
                        if node.bl_idname == 'ShaderNodeTexImage'
                        and node.label in ('diffuse', 'specularity', 'reflection', 'self_illumination', 'opacity')}
        for slot, input_idname in [
            ('diffuse', 'Base Color'),
            ('specularity', 'Specular IOR Level'),
            ('reflection', 'Specular Tint'),
            ('self_illumination', 'Emission Strength'),
        ]:
            if slot not in exported_nodes:
                for link in mat.node_tree.links:
                    if (
                        link.to_node.bl_idname == 'ShaderNodeBsdfPrincipled'
                        and link.to_socket.label == input_idname
                        and link.from_node.bl_idname == 'ShaderNodeTexImage'
                    ):
                        exported_nodes[slot] = link.from_node
                        break
        if 'diffuse' not in exported_nodes:
            for node in mat.node_tree.nodes:
                if node.bl_idname == 'ShaderNodeTexImage':
                    exported_nodes['diffuse'] = node
                    break

        if 'diffuse' not in exported_nodes:
            self.messages.append(('WARNING', f'Cannot find a texture for material {mat.name}'))
            return

        images_to_export = {k: v.image if v else v for k, v in exported_nodes.items()}
        if mat.get('internal'):
            export_success = self.write_rsh_chunks(writer, images_to_export, mat_path, mat.name)
            if not export_success:
                return
            self.exported_materials[mat.name] = str(mat_path)
            return

        with writer.start_chunk('DATASSHR', name=str(mat_path)):  # Unused, can be deleted
            writer.write_str(str(mat_path))
        self.exported_materials[mat.name] = str(mat_path)

        teamcolor_node_labels = {
            f'color_layer_{slot}' for slot in ('primary', 'secondary', 'trim', 'weapons', 'eyes' ,'dirt', 'default')
        } | {'badge', 'banner'}
        teamcolor_image_nodes = {
            node.label: node
            for node in mat.node_tree.nodes
            if node.bl_idname == 'ShaderNodeTexImage'
                and node.label in teamcolor_node_labels
        }
        teamcolor_badge_info = {
            node.label[len('badge_'):]: (
                node.inputs['X'].default_value,
                node.inputs['Y'].default_value,
            )
            for node in mat.node_tree.nodes
            if node.bl_idname == 'ShaderNodeCombineXYZ'
                and node.label in ('badge_position', 'badge_display_size')
        }
        teamcolor_banner_info = {
            node.label[len('banner_'):]: (
                node.inputs['X'].default_value,
                node.inputs['Y'].default_value,
            )
            for node in mat.node_tree.nodes
            if node.bl_idname == 'ShaderNodeCombineXYZ'
                and node.label in ('banner_position', 'banner_display_size')
        }

        if self.convert_textures:
            rsh_path = self.paths.get_path(f'{mat_path}.rsh')
            if self.export_rsh(
                images_to_export,
                rsh_path,
                mat_path,
                mat.name,
            ):
                self.paths.add_info(f'{mat_path}.rsh', rsh_path)

            wtp_path = self.paths.get_path(f'{mat_path}_default.wtp')
            if self.export_wtp(
                {k: v.image if v else v for k, v in teamcolor_image_nodes.items()},
                teamcolor_badge_info,
                teamcolor_banner_info,
                wtp_path,
                mat_path,
                mat.name,
            ):
                self.paths.add_info(f'{mat_path}_default.wtp', wtp_path)
        else:
            for image_prefix, dst_suffix, images in [
                ('', '.rsh', {
                    slot: node for slot, node in exported_nodes.items()
                    if node is not None and node.image is not None
                }),
                ('teamcolour_', '_default.wtp', {
                    slot: node for slot, node in teamcolor_image_nodes.items()
                    if node is not None and node.image is not None
                }),
            ]:

                for slot, node in images.items():
                    if node is None:
                        continue
                    image_name, image_suffix = self.guess_image_name_and_suffix(node.image, mat)
                    if len(exported_nodes) == 1:
                        image_name = mat_path.name

                    dst_file = self.paths.get_path((mat_path / f'{image_prefix}{image_name}.{image_suffix}'), require_parent=True)
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
                    self.paths.add_info(f'{mat_path}{dst_suffix}', dst_file)

    def guess_image_name_and_suffix(self, image, material) -> tuple[str, str]:
            image_name = pathlib.Path(image.filepath).name or image.name

            def get_suffix(path: str):
                suffix = pathlib.Path(path).suffix
                if suffix and any(i.isalpha() for i in suffix):
                    return suffix
                return ''

            suffix = get_suffix(image.filepath) or get_suffix(image.name) or get_suffix(material.name)
            return image_name, suffix

    def export_rsh(self, images: dict, dst_path: pathlib.Path, declared_path: pathlib.PurePosixPath, mat_name: str) -> bool:
        with tempfile.TemporaryDirectory() as t:
            temp_dir = pathlib.Path(t)
            exported_file = temp_dir / dst_path.name
            with exported_file.open('wb') as f:
                writer = ChunkWriter(f, {
                    'FOLDSHRF': {
                        'version': 1,
                        'FOLDTXTR': {
                            'version': 1,
                            'DATAHEAD': {'version': 1},
                            'DATAINFO': {'version': 3},
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
                with writer.start_chunk('FOLDSHRF', name=mat_name):
                    export_success = self.write_rsh_chunks(writer, images, declared_path, mat_name)
                    if not export_success:
                        return False
            dst_path.parent.mkdir(exist_ok=True, parents=True)
            shutil.copy(exported_file, dst_path)
        return True

    def write_rsh_chunks(self, writer: ChunkWriter, images: dict, declared_path: pathlib.PurePosixPath, mat_name: str) -> bool:
        texture_declared_paths = {}
        known_keys = {
            'diffuse': '',
            'specularity': '_spec',
            'reflection': '_reslect',
            'self_illumination': '_self_illum',
            'opacity': '_alpha',
        }
        with tempfile.TemporaryDirectory() as t:
            temp_dir = pathlib.Path(t)
            for key, path_suffix in known_keys.items():
                image = images.get(key)
                if not image:
                    continue
                texture_declared_path = f'{declared_path}{path_suffix}'  # used for locating .wtp
                texture_declared_paths[key] = texture_declared_path

                texture_data = None
                if image.packed_files:
                    texture_data = image.packed_file.data
                else:
                    try:
                        packed_image = image.copy()
                        packed_image.pack()
                        texture_data = packed_image.packed_file.data
                        bpy.data.images.remove(packed_image)
                    except Exception as e:
                        self.messages.append(('WARNING', f'Error while converting {image.name}: {e!r}'))
                        return False
                texture_stream = io.BytesIO(texture_data)

                from PIL import Image
                import quicktex.dds
                import quicktex.s3tc.bc1
                import quicktex.s3tc.bc3

                texture_stream.seek(0)
                pil_image = Image.open(texture_stream)
                pil_image.thumbnail((self.max_texture_size, self.max_texture_size))
                level = 10
                color_mode = quicktex.s3tc.bc1.BC1Encoder.ColorMode
                mode = color_mode.ThreeColor
                bc1_encoder = quicktex.s3tc.bc1.BC1Encoder(level, mode)
                bc3_encoder = quicktex.s3tc.bc3.BC3Encoder(level)
                if 'A' not in pil_image.mode:
                    has_alpha = False
                else:
                    alpha_hist = pil_image.getchannel('A').histogram()
                    has_alpha = any([a > 0 for a in alpha_hist[:-1]])
                    # TODO test for 1-bit alpha
                tmp_dds_path = temp_dir / key
                if has_alpha:
                    quicktex.dds.encode(pil_image, bc3_encoder, 'DXT5').save(tmp_dds_path)
                else:
                    quicktex.dds.encode(pil_image, bc1_encoder, 'DXT1').save(tmp_dds_path)
                texture_stream = tmp_dds_path.open('rb')
                is_dds, width, height, declared_data_size, num_mips, image_format, image_type = textures.read_dds_header(texture_stream)

                with writer.start_chunk('FOLDTXTR', name=str(texture_declared_path)):
                    with writer.start_chunk('DATAHEAD'):
                        writer.write_struct('<2l', image_type, 1)  # num_images
                    if self.make_oe_compatable_textures:
                        with writer.start_chunk('DATAINFO'):
                            writer.write_struct('<4l', image_type, width, height, 1)  # num_images
                    with writer.start_chunk('FOLDIMAG'):
                        with writer.start_chunk('DATAATTR'):
                            writer.write_struct('<4l', image_format, width, height, num_mips)
                        with writer.start_chunk('DATADATA'):
                            with texture_stream:
                                shutil.copyfileobj(texture_stream, writer)
            with writer.start_chunk('FOLDSHDR', name=mat_name):
                has_extra_layers = any(images.get(k) is not None for k in known_keys if k != 'diffuse')
                with writer.start_chunk('DATAINFO'):
                    writer.write_struct('<2L4BLx', 6, 7, 204 + has_extra_layers, 204, 204, 61, 1)
                for channel_idx, key in enumerate(list(known_keys) + ['unknown']):
                    with writer.start_chunk('DATACHAN'):
                        has_data = images.get(key) is not None
                        colour_mask = {
                            0: [0, 0, 0, 255] if has_extra_layers else [150, 150, 150, 255],
                            1: [0, 0, 0, 255] if has_extra_layers else [229, 229, 229, 255],
                            4: [0, 0, 0, 255] if has_data else [255, 255, 255, 255],
                        }.get(channel_idx, [0, 0, 0, 255])
                        writer.write_struct('<2l4B', channel_idx, int(has_data), *colour_mask)
                        writer.write_str(str(texture_declared_paths.get(key, '')))
                        writer.write_struct('<3L', int(has_data), 4, 6 if key == 'reflection' else 0)  # num_coords
                        for idx in range(4):
                            pairs = [(1.0, 0.0), (0.0, 0.0), (0.0, 1.0), (0.0, 0.0)]
                            if idx % 2 == 1:
                                pairs = pairs[3:] + pairs[:3]
                            for c in pairs:
                                writer.write_struct('<2f', *c)
        return True

    def export_wtp(self, images: dict, badge_info: dict, banner_info: dict, dst_path: pathlib.Path, declared_path: pathlib.PurePosixPath, mat_name: str) -> bool:
        images = {k: v for k, v in images.items() if v and not v.get('PLACEHOLDER', False)}
        if not any(images.values()) or images.get('color_layer_default') is None or images.get('color_layer_dirt') is None :
            return False
        with tempfile.TemporaryDirectory() as t:
            temp_dir = pathlib.Path(t)
            exported_file = temp_dir / dst_path.name
            with exported_file.open('wb') as f:
                writer = ChunkWriter(f, {
                    'FOLDTPAT': {
                        'version': 3,
                        'DATAINFO': {'version': 1},
                        'DATAPTLD': {'version': 1},
                        'FOLDIMAG': {
                            'version': 1,
                            'DATAATTR': {'version': 2},
                            'DATADATA': {'version': 2},
                        },
                        'DATAPTBD': {'version': 1},
                        'DATAPTBN': {'version': 1},
                    }
                })
                self.write_relic_chunky(writer)

                def image_to_tga(image, dst: str | pathlib.Path, width: int, height: int, grayscale: bool = False):
                    image = image.copy()
                    if not image.packed_files:
                        image.pack()
                    image.scale(width, height)
                    image = utils.flip_image_y(image)

                    backup_file_format = self.bpy_context.scene.render.image_settings.file_format
                    backup_color_mode = self.bpy_context.scene.render.image_settings.color_mode
                    backup_color_depth = self.bpy_context.scene.render.image_settings.color_depth
                    backup_compression = self.bpy_context.scene.render.image_settings.compression

                    self.bpy_context.scene.render.image_settings.file_format = 'TARGA_RAW'
                    self.bpy_context.scene.render.image_settings.color_mode = 'BW' if grayscale else 'RGBA'
                    self.bpy_context.scene.render.image_settings.color_depth = '8'
                    self.bpy_context.scene.render.image_settings.compression = 0

                    image.save_render(str(dst), scene=self.bpy_context.scene)

                    self.bpy_context.scene.render.image_settings.file_format = backup_file_format
                    self.bpy_context.scene.render.image_settings.color_mode = backup_color_mode
                    self.bpy_context.scene.render.image_settings.color_depth = backup_color_depth
                    self.bpy_context.scene.render.image_settings.compression = backup_compression
                    bpy.data.images.remove(image)

                with writer.start_chunk('FOLDTPAT', name='default'):
                    width, height = next((map(int, i.size) for k, i in images.items()
                                          if i is not None and k not in ('badge', 'banner')),
                                          (512, 512))  # FIXME
                    with writer.start_chunk('DATAINFO'):
                        writer.write_struct('<2L', width, height)
                    for layer_idx, layer_name in [
                        (0, 'primary'),
                        (1, 'secondary'),
                        (2, 'trim'),
                        (3, 'weapons'),
                        (4, 'eyes'),
                        (5, 'dirt'),
                    ]:
                        if not (img := images.get(f'color_layer_{layer_name}')):
                            continue
                        with writer.start_chunk('DATAPTLD'):
                            tmp_file = temp_dir / f'{layer_name}.tga'
                            try:
                                image_to_tga(img, tmp_file, width, height, grayscale=True)
                            except Exception as e:
                                self.messages.append(('WARNING', f'Error while converting {img.name}: {e!r}'))
                                return False
                            with tmp_file.open('rb') as f:
                                tga_data = f.read()
                            tga_data = tga_data[18 + tga_data[0]:]
                            writer.write_struct('<2L', layer_idx, len(tga_data))
                            writer.write(tga_data)

                    if default_image := images.get('color_layer_default'):
                        with writer.start_chunk('FOLDIMAG'):
                            with writer.start_chunk('DATAATTR'):
                                writer.write_struct('<4L', 0, width, height, 1)
                            with writer.start_chunk('DATADATA'):
                                tmp_file = temp_dir / 'default.tga'
                                try:
                                    image_to_tga(default_image, tmp_file, width, height, grayscale=False)
                                except Exception as e:
                                    self.messages.append(('WARNING', f'Error while converting {default_image.name}: {e!r}'))
                                    return False
                                with tmp_file.open('rb') as f:
                                    tga_data = f.read()
                                tga_data = tga_data[18 + tga_data[0]:]
                                writer.write(tga_data)
                    for key, data, chunk_name, default_size in [
                        ('badge', badge_info, 'DATAPTBD', (64, 64)),
                        ('banner', banner_info, 'DATAPTBN', (64, 96)),
                    ]:
                        if not images.get(key):
                            continue
                        with writer.start_chunk(chunk_name):
                            writer.write_struct('<4f', *data.get('position', [0, 0]), *data.get('display_size', default_size))
            dst_path.parent.mkdir(exist_ok=True, parents=True)
            shutil.copy(exported_file, dst_path)
        return True

    @classmethod
    def is_marker(cls, bone):
        return bone.name.startswith('marker_') or any('marker' in c.name.lower() for c in bone.collections)

    @classmethod
    def is_camera(cls, bone):
        return any('camera' in c.name.lower() for c in bone.collections)

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
        
        bones = [b for b in armature.bones if not (self.is_marker(b) or self.is_camera(b))]
        self.exported_bones = bones
        if not self.exported_bones:
            return
        for bone in bones:
            insert_bone(bone)

        def iter_bones(data, lvl):
            for bone, child in data.items():
                yield bone, lvl
                yield from iter_bones(child, lvl + 1)

        self.bone_transforms = {}
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
                        parent_mat = self.armature_obj.matrix_world.inverted() @ mathutils.Matrix.Rotation(math.radians(90.0), 4, 'X')
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
        mesh_xrefs = {}
        with writer.start_chunk('FOLDMSGR'):
            for obj_orig in bpy.data.objects:
                if obj_orig.type != 'MESH':
                    continue
                not_weighted_vertices_warn = False
                obj = obj_orig.evaluated_get(depsgraph)
                mesh = obj.data
                if len(mesh.materials) == 0:
                    self.messages.append(('WARNING', f'Skipping mesh {obj.name} because it has no materials'))
                    continue

                self.exported_meshes.append(obj.name)
                vert_warn = False
                many_bones_warn = False

                vertex_groups = [v for v in obj.vertex_groups if v.name in self.bone_to_idx]

                if len(vertex_groups) == 0 or all(len(v.groups) == 0 or v.groups[0].weight < 0.001 for v in mesh.vertices):
                    self.messages.append(('WARNING', f'Mesh "{obj.name}" seems to be not weighted to any bones'))
                    vertex_groups = []
                if len(vertex_groups) == 1 and all(len(v.groups) == 1 and v.groups[0].weight > 0.995 for v in mesh.vertices):
                    assert vertex_groups[0].name in self.bone_to_idx, f'Cannot find bone "{vertex_groups[0].name}" for mesh "{obj.name}"'
                    single_bone_meshes[obj.name] = self.bone_to_idx[vertex_groups[0].name]
                    vertex_groups = []

                xref_source = obj_orig.get('xref_source', '').strip()
                if xref_source:
                    # TODO check if file exists
                    if vertex_groups != []:
                        self.messages.append(('WARNING', f'Mesh "{obj.name}" is weighted to {len(vertex_groups)} bones. Xrefed meshes cannot be attached to more than 1 bone.'))
                        xref_source = ''
                    else:
                        mesh_xrefs[obj.name] = xref_source
                        continue

                with writer.start_chunk('FOLDMSLC', name=obj.name):
                    with writer.start_chunk('DATADATA'):
                        writer.write_struct('<4xbL4x', 1, len(mesh.loop_triangles))

                        writer.write_struct('<l', len(vertex_groups))
                        for g in vertex_groups:
                            writer.write_str(g.name)
                            writer.write_struct('<l', self.bone_to_idx[g.name])

                        exported_vertex_groups = {g.name for g in vertex_groups}
                        extended_vertices: list[VertexInfo] = []
                        extended_polygons = []
                        seen_data = {}

                        if len(mesh.uv_layers) != 1:
                            self.messages.append(('WARNING', f'Mesh "{obj.name}" has {len(mesh.uv_layers)} UV layers. It must have exactly 1 UV layer.'))
                        for poly in mesh.loop_triangles:
                            poly_vertices = []
                            for loop_idx in poly.loops:
                                orig_vertex_idx = mesh.loops[loop_idx].vertex_index
                                uv = mesh.uv_layers[0].uv[loop_idx].vector.copy().freeze()
                                vertex_normal = mesh.corner_normals[loop_idx].vector.copy().freeze()
                                vertex_key = uv, vertex_normal
                                seen_vertex_data = seen_data.setdefault(orig_vertex_idx, {})
                                vertex_idx = seen_vertex_data.get(vertex_key)
                                if vertex_idx is None:
                                    vertex_idx = len(extended_vertices)
                                    seen_vertex_data[vertex_key] = vertex_idx
                                    vertex = mesh.vertices[orig_vertex_idx]
                                    vertex_info = VertexInfo(
                                        position=obj.matrix_world @ vertex.co,
                                        vertex_groups=[g for g in vertex.groups
                                                        if obj.vertex_groups[g.group].name in exported_vertex_groups],
                                        normal=obj.matrix_world.to_3x3() @ mesh.corner_normals[loop_idx].vector,
                                        uv=mesh.uv_layers[0].uv[loop_idx].vector,
                                    )
                                    extended_vertices.append(vertex_info)
                                poly_vertices.append(vertex_idx)
                            extended_polygons.append(poly_vertices)

                        writer.write_struct('<l', len(extended_vertices))
                        writer.write_struct('<l', 39 if vertex_groups else 37)
                        for v in extended_vertices:
                            writer.write_struct('<3f', -v.position.x, v.position.z, -v.position.y)
                        if vertex_groups:
                            for v in extended_vertices:
                                groups = sorted(v.vertex_groups, key=lambda x: -x.weight)
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
                                if weights[0] == 0 and not not_weighted_vertices_warn:
                                    not_weighted_vertices_warn = True
                                    self.messages.append(('WARNING', f'Some vertices of mesh {obj.name} are not weighted to any bones and may not be displayed properly'))
                                total_weight = sum(weights[:4]) or 1
                                writer.write_struct('<3f', *[w / total_weight for w in weights[:3]])
                                writer.write_struct('<4B', *bones_ids)
                        if not mesh.corner_normals:
                            if not no_custom_normals_warn:
                                self.messages.append(('WARNING', f'Mesh {obj.name} is missing custom normals'))
                                no_custom_normals_warn = True
                        for v in extended_vertices:
                            writer.write_struct('<3f', -v.normal.x, v.normal.z, -v.normal.y)
                        for v in extended_vertices:
                            writer.write_struct('<2f', v.uv.x, 1 - v.uv.y)
                        if self.format is ExportFormat.WHM:
                            writer.write_struct('<4x')
                        mat_faces = {}
                        for poly, extended_verts in zip(mesh.loop_triangles, extended_polygons):
                            mat_faces.setdefault(poly.material_index, []).append(extended_verts)
                        materials = [(idx, m) for idx, m in enumerate(mesh.materials) if idx in mat_faces]
                        writer.write_struct('<l', len(materials))
                        for mat_idx, mat in materials:
                            writer.write_str(self.exported_materials.get(mat.name, f'missing_mat_{mat_idx}'))
                            writer.write_struct('<l', len(mat_faces[mat_idx]) * 3)
                            min_poly_idx, max_poly_idx = float('+inf'), float('-inf')
                            for p in mat_faces[mat_idx]:
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
                writer.write_struct('<l', len(self.exported_meshes))
                for mesh_name in self.exported_meshes:
                    writer.write_str(mesh_name)
                    writer.write_str(mesh_xrefs.get(mesh_name, ''))
                    writer.write_struct('<l', single_bone_meshes.get(mesh_name, -1))
            with writer.start_chunk('DATABVOL'):
                writer.write_struct(
                    '<b 3f 3f 9f',
                    1, *(global_max_corner + global_min_corner) / 2,
                    *(global_max_corner - global_min_corner) / 2,
                    *[i for r in mathutils.Matrix.Identity(3) for i in r]
                )

    def write_marks(self, writer: ChunkWriter):
        if not self.armature_obj:
            return
        armature = self.armature_obj.data
        if 'Markers' in armature.collections:
            markers = armature.collections['Markers'].bones
        else:
            markers = [b for b in armature.bones if self.is_marker(b)]
        if not markers:
            return
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
                        parent_mat = self.armature_obj.matrix_world.inverted() @ mathutils.Matrix.Rotation(math.radians(90.0), 4, 'X')
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
                    py_path = f'x{fcurve.data_path}' if fcurve.data_path.startswith('[') else f'x.{fcurve.data_path}'
                    attr = ast.parse(py_path, mode='single').body[0].value.slice.value
                    path = fcurve.data_path.rsplit(bpy.utils.escape_identifier(attr), 1)[0][:-2]
                    if props.SEP in attr:
                        prop_group, obj_name = attr.split(props.SEP, 1)
                        prop_fcurves[prop_group.lower()].setdefault(obj_name.lower(), []).append(fcurve)
                else:
                    for suffix in ['.rotation_quaternion', '.location']:
                        if fcurve.data_path.endswith(suffix):
                            path = fcurve.data_path[:-len(suffix)]
                            attr = suffix[1:]
                            break
                if not attr:
                    continue
                try:
                    anim_obj = anim_root.path_resolve(path) if path else anim_root
                except Exception:
                    self.messages.append(('WARNING', f'Cannot resolve path "{path}" in the action "{action.name}"'))
                    continue
                anim_sections[attr.lower()].setdefault(anim_obj, []).append(fcurve)

            def get_prop_fcurves(prop: str, obj_name: str) -> list:
                prop_data = prop_fcurves[prop]
                obj_name = obj_name.lower()
                return prop_data.get(obj_name, []) or prop_data.get(utils.get_hash(obj_name), [])

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
                            parent_mat = self.armature_obj.matrix_world.inverted() @ mathutils.Matrix.Rotation(math.radians(90.0), 4, 'X')
                        if self.format is ExportFormat.WHM:
                            writer.write_str(bone.name)

                        with self.start_chunk(writer, ExportFormat.SGM, 'DATABANM', name=bone.name):
                            loc_fcurves = [None] * 3
                            loc_frames = set()

                            for fcurve in anim_sections['location'].get(bone_obj, []):
                                loc_fcurves[fcurve.array_index] = fcurve
                                for keyframe in fcurve.keyframe_points:
                                    frame, val = keyframe.co
                                    loc_frames.add(frame)

                            rot_fcurves = [None] * 4
                            rot_frames = set()
                            for fcurve in anim_sections['rotation_quaternion'].get(bone_obj, []):
                                rot_fcurves[fcurve.array_index] = fcurve
                                for keyframe in fcurve.keyframe_points:
                                    frame, val = keyframe.co
                                    rot_frames.add(frame)

                            all_frames = sorted(loc_frames | rot_frames)
                            frame_data = []
                            for frame in all_frames:
                                loc = mathutils.Vector([fcurve.evaluate(frame) if fcurve is not None else 0
                                                        for fcurve in loc_fcurves])
                                rot = mathutils.Quaternion([fcurve.evaluate(frame) if fcurve is not None else 0
                                                            for fcurve in rot_fcurves])
                                frame_matrix = mathutils.Matrix.LocRotScale(loc, rot, None)
                                relative_matrix = parent_mat.inverted() @ bone.matrix_local @ frame_matrix @ delta.inverted()
                                frame_data.append(relative_matrix.decompose())

                            writer.write_struct('<l', len(all_frames))
                            for frame, (loc, rot, _) in zip(all_frames, frame_data):
                                writer.write_struct('<f', frame / max(frame_end, 1))
                                writer.write_struct('<3f', -loc.x, loc.y, loc.z)

                            writer.write_struct('<l', len(all_frames))
                            prev_rot = None
                            for frame, (loc, rot, _) in zip(all_frames, frame_data):
                                writer.write_struct('<f', frame / max(frame_end, 1))
                                if prev_rot is not None:
                                    rot.make_compatible(prev_rot)
                                prev_rot = rot
                                writer.write_struct('<4f', rot.x, -rot.y, -rot.z, rot.w)

                            stale_flag = 1
                            if bone_obj in anim_sections['stale']:
                                keyframes = anim_sections['stale'][bone_obj][0].keyframe_points
                                if len(keyframes) > 1:
                                    self.messages.append(('WARNING', f'''Found {len(keyframes)} values for "stale" property of bone "{bone_obj.name}" in the action "{action.name}". Only the first one is used.'''))
                                if keyframes[0].co[1]:
                                    stale_flag = 0
                            writer.write_struct('<b', stale_flag)

                    mesh_fcurves = prop_fcurves['visibility'].keys() | prop_fcurves['force_invisible'].keys()
                    exported_tex_fcurves = {
                        (g, mat_name): get_prop_fcurves(g, mat_name)
                        for mat_name in self.exported_materials
                        for g in ['uv_offset', 'uv_tiling']
                    }
                    num_tex_fcurves = sum(len(fc) for fc in exported_tex_fcurves.values())
                    if self.format is ExportFormat.WHM:
                        writer.write_struct('<l', len(self.exported_meshes) + num_tex_fcurves)

                    for mesh_name in self.exported_meshes:
                        if self.format is ExportFormat.WHM:
                            writer.write_str(mesh_name)
                        with self.start_chunk(writer, ExportFormat.SGM, 'DATACANM', name=mesh_name):
                            writer.write_struct('<l', 2)  # mode
                            writer.write_struct('<8x')
                            vis_fcurves = get_prop_fcurves('visibility', mesh_name)
                            if vis_fcurves:
                                fcurve = vis_fcurves[0]
                                keypoints = fcurve.keyframe_points
                                if list(keypoints[0].co) == [0., 1.]:
                                    keypoints = keypoints[1:]
                            else:
                                keypoints = []
                            writer.write_struct('<l', len(keypoints) + 1)
                            writer.write_struct('<4x')
                            force_invisible_fcurves = get_prop_fcurves('force_invisible', mesh_name)
                            force_invisible = 0
                            if force_invisible_fcurves:
                                keyframes = force_invisible_fcurves[0].keyframe_points
                                if len(keyframes) > 1:
                                    self.messages.append(('WARNING', f'''Found {len(keyframes)} values for "force_invisible" property of mesh "{mesh_name}" in the action "{action.name}". Only the first one is used.'''))
                                force_invisible = keyframes[0].co[1]
                            writer.write_struct('<f', not force_invisible)
                            for point in keypoints:
                                frame, val = point.co
                                writer.write_struct('<2f', frame / max(frame_end, 1), val)

                    for mat in bpy.data.materials:
                        if mat.name not in self.exported_materials:
                            continue
                        for group in ['uv_offset', 'uv_tiling']:
                            fcurves = exported_tex_fcurves[group, mat.name]
                            mat_path = self.get_material_path(mat)
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
        exporter = Exporter(paths, format=fmt, max_texture_size=768)
        try:
            exporter.export(writer, object_name=pathlib.Path(bpy.data.filepath).stem, meta='amorgun')
            paths.dump_info()
        finally:
            for _, msg in exporter.messages:
                print(msg)
