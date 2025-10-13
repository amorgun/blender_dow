import ast
import collections
import contextlib
import dataclasses
import datetime
import enum
import math
import pathlib
import shutil
import tempfile

import bpy
import mathutils
from bpy_extras import anim_utils
from PIL import Image as PilImage

from . import textures, utils, props, dow_layout
from .chunky import ChunkWriter


@enum.unique
class ExportFormat(enum.Enum):
    WHM = enum.auto()
    SGM = enum.auto()

@enum.unique
class MaterialExportFormat(str, enum.Enum):
    RSH = 'rsh',
    RTX = 'rtx',


class FileDispatcher:
    @enum.unique
    class Layout(enum.Enum):
        FLAT = enum.auto()
        FLAT_FOLDERS = enum.auto()
        FULL_PATH = enum.auto()

    def __init__(self, root: str, layout: Layout, subfolder: str | None):
        self.root = pathlib.Path(root)
        self.layout = layout
        self.file_info = []
        self.subfolder = subfolder
        
    def get_path(self, path: str, require_parent=False) -> pathlib.Path:
        path = pathlib.Path(path)
        path_parts = [self.subfolder] if self.subfolder else []
        match self.layout:
            case FileDispatcher.Layout.FLAT: path_parts.append(path.name)
            case FileDispatcher.Layout.FLAT_FOLDERS: path_parts.extend([path.parent.name, path.name] if require_parent else [path.name])
            case FileDispatcher.Layout.FULL_PATH: path_parts.extend(path.parts)
        return dow_layout.try_find_path(self.root, *path_parts)

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
            'FOLDSTXT':  {'version': 1},
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

def get_chunk_versions(export_format: ExportFormat, material_format: MaterialExportFormat):
    import copy

    res = copy.deepcopy(CHUNK_VERSIONS[export_format])
    if export_format == ExportFormat.WHM and material_format == MaterialExportFormat.RTX:
        res['FOLDRSGM']['version'] = 4
    return res


class Exporter:
    def __init__(
            self,
            paths: FileDispatcher,
            override_files: bool = False,
            format: ExportFormat = ExportFormat.WHM,
            convert_textures: bool = True,
            material_export_format: MaterialExportFormat = MaterialExportFormat.RSH,
            default_texture_path: str = '',
            max_texture_size: int = 1024,
            make_oe_compatable_textures: bool = True,
            export_teamcolored_rtx: bool = True,
            teamcolored_rtx_suffix: str = '_default_0',
            vertex_position_merge_threshold: float = 0,
            vertex_normal_merge_threshold: float = 0.01,
            uv_merge_threshold: float = 0.001,
            use_legacy_marker_orientation: bool = False,
            context=None,
        ) -> None:
        self.messages = []
        self.paths = paths
        self.override_files = override_files
        self.format = format
        self.convert_textures = convert_textures
        self.material_export_format = material_export_format
        self.default_texture_path = pathlib.PurePosixPath(default_texture_path)
        self.max_texture_size = max_texture_size
        self.make_oe_compatable_textures = make_oe_compatable_textures
        self.export_teamcolored_rtx = export_teamcolored_rtx
        self.teamcolored_rtx_suffix = teamcolored_rtx_suffix
        self.vertex_position_merge_threshold = vertex_position_merge_threshold
        self.vertex_normal_merge_threshold = vertex_normal_merge_threshold
        self.uv_merge_threshold = uv_merge_threshold
        self.use_legacy_marker_orientation = use_legacy_marker_orientation
        self.bpy_context = context if context is not None else bpy.context

        self.armature_obj = None
        self.exported_bones = None
        self.exported_meshes = None
        self.exported_materials = None
        self.exported_images = None
        self.bone_to_idx = {}

    def copy_file(self, src: pathlib.Path, dst: pathlib.Path):
        if dst.is_file() and not self.override_files:
            self.messages.append(('INFO', f'Skipping {dst} because it already exists'))
            return
        shutil.copy(src, dst)

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
                self.write_cams(writer)
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
        self.exported_images = {}
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
                        and node.label in textures.MaterialLayers.__members__.values()}
        for slot, input_idname in [
            (textures.MaterialLayers.DIFFUSE, 'Base Color'),
            (textures.MaterialLayers.SPECULAR_MASK, 'Specular IOR Level'),
            (textures.MaterialLayers.SPECULAR_MASK, 'Metallic'),
            (textures.MaterialLayers.SELF_ILLUMUNATION_MASK, 'Emission Strength'),
            (textures.MaterialLayers.SELF_ILLUMUNATION_COLOR, 'Emission Color'),
        ]:
            for link in mat.node_tree.links:
                if (
                    link.to_node.bl_idname == 'ShaderNodeBsdfPrincipled'
                    and link.to_socket.identifier == input_idname
                    and link.from_node.bl_idname == 'ShaderNodeTexImage'
                ):
                    if slot not in exported_nodes:
                        exported_nodes[slot] = link.from_node
                    elif exported_nodes.get(slot) != link.from_node:
                        self.messages.append(('WARNING', f'Multiple candidates found for {slot.value.upper()} layer in material {mat.name}'))
                    break
        if (self.material_export_format == MaterialExportFormat.RSH
            and textures.MaterialLayers.DIFFUSE not in exported_nodes):
            for node in mat.node_tree.nodes:
                if node.bl_idname == 'ShaderNodeTexImage':
                    exported_nodes[textures.MaterialLayers.DIFFUSE] = node
                    break
            else:
                self.messages.append(('WARNING', f'Cannot find a texture for material {mat.name}'))
                return

        images_to_export = {k: v.image if v else v for k, v in exported_nodes.items()}
        if self.material_export_format == MaterialExportFormat.RSH:
            if mat.get('internal'):
                export_success = self.write_rsh_chunks(writer, images_to_export, mat_path, str(mat_path))
                if not export_success:
                    self.messages.append(('WARNING', f'Cannot export material {mat.name}'))
                    return
                self.exported_materials[mat.name] = str(mat_path)
                return

            with writer.start_chunk('DATASSHR', name=str(mat_path)):  # Unused, can be deleted
                writer.write_str(str(mat_path))
            self.exported_materials[mat.name] = str(mat_path)


        teamcolor_node_labels = {
            f'color_layer_{slot.value}' if slot not in (
                textures.TeamcolorLayers.BADGE,
                textures.TeamcolorLayers.BANNER,
            ) else slot.value for slot in textures.TeamcolorLayers
        }
        teamcolor_image_nodes = {}
        for node in mat.node_tree.nodes:
            if not (
                node.bl_idname == 'ShaderNodeTexImage'
                and node.label in teamcolor_node_labels
                and not node.get('PLACEHOLDER', False)
            ):
                continue
            try:
                teamcolor_image_nodes[textures.TeamcolorLayers(
                    node.label[len('color_layer_') if node.label.startswith('color_layer_') else 0:]
                )] = node
            except KeyError:
                self.messages.append(('WARNING', f'Unknown layer label "{node.label}" in material {mat.name}'))
                continue
        for link in mat.node_tree.links:
            if (
                link.to_node.bl_idname == 'ShaderNodeGroup'
                and link.to_node.node_tree == bpy.data.node_groups.get('ApplyTeamcolor', None)
                and link.to_socket.identifier.endswith('_value')
                and link.from_node.bl_idname == 'ShaderNodeTexImage'
                and not link.from_node.get('PLACEHOLDER', False)
            ):
                try:
                    slot = textures.TeamcolorLayers(link.to_socket.identifier[:-len('_value')])
                except KeyError:
                    continue
                if slot not in teamcolor_image_nodes:
                    teamcolor_image_nodes[slot] = link.from_node
                elif teamcolor_image_nodes.get(slot) != link.from_node:
                    self.messages.append(('WARNING', f'Multiple candidates found for {slot.value.upper()} layer in material {mat.name}'))

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
            if self.material_export_format == MaterialExportFormat.RSH:
                rsh_path = self.paths.get_path(f'{mat_path}.rsh')
                if self.export_rsh(
                    images_to_export,
                    rsh_path,
                    mat_path,
                    mat.name,
                ):
                    self.paths.add_info(f'{mat_path}.rsh', rsh_path)
            else:
                image_suffixes = {
                    textures.MaterialLayers.DIFFUSE: '',
                    textures.MaterialLayers.OPACITY: '_default_alpha',
                    textures.MaterialLayers.SPECULAR_MASK: '_default_spc',
                    textures.MaterialLayers.SPECULAR_REFLECTION: '_default_reflect',
                    textures.MaterialLayers.SELF_ILLUMUNATION_MASK: '_default_emi',
                    textures.MaterialLayers.SELF_ILLUMUNATION_COLOR: '_default_emi_color',
                }
                material_images = {}
                mat_path = exported_nodes.get(textures.MaterialLayers.DIFFUSE, {}).get('image_path', '').strip() or mat_path
                mat_name = pathlib.Path(mat_path).name
                for layer, image_node in exported_nodes.items():
                    if image_node is None or image_node.image is None:
                        continue
                    image = image_node.image
                    img_path = image_node.get('image_path', '').strip() or f'{mat_path}{image_suffixes[layer]}'
                    rtx_path = self.paths.get_path(f'{img_path}.rtx')
                    if img_path in self.exported_images:
                        material_images[layer] = img_path
                    if not self.export_de_rtx(
                        image,
                        rtx_path,
                        img_path,
                        force_type=textures.DdsType.DXT1 if layer == textures.MaterialLayers.SPECULAR_MASK else None,
                    ):
                        self.messages.append(('WARNING', f'Error while converting image {image.name}: {e!r}'))
                        continue
                    self.paths.add_info(f'{img_path}.rtx', rtx_path)
                    with writer.start_chunk('FOLDSTXT', name=str(img_path)):
                        pass
                    material_images[layer] = img_path
                    self.exported_images[img_path] = image
                material_images.setdefault(textures.MaterialLayers.OPACITY, material_images.get(textures.MaterialLayers.DIFFUSE, ''))
                material_images.setdefault(textures.MaterialLayers.SELF_ILLUMUNATION_COLOR, material_images.get(textures.MaterialLayers.SELF_ILLUMUNATION_MASK, ''))
                self.exported_materials[mat.name] = mat_name
                roughness = 0
                metallic = 0
                for node in mat.node_tree.nodes:
                    if node.bl_idname == 'ShaderNodeBsdfPrincipled':
                        roughness = node.inputs['Roughness'].default_value
                        metallic = node.inputs['Metallic'].default_value
                        break
                with writer.start_chunk('FOLDSHDR', name=mat_name):
                    with writer.start_chunk('DATAINFO'):
                        roughness_val = 10
                        if roughness != 0:
                            roughness_val = 2 ** (max(0.1, min(10, round(1 / roughness, 2) - 1)))
                        writer.write_struct('<2LfLx', 6, 7, roughness_val, 1)
                    for channel_idx, key in enumerate([
                        textures.MaterialLayers.DIFFUSE,
                        textures.MaterialLayers.SPECULAR_MASK,
                        textures.MaterialLayers.SPECULAR_REFLECTION,
                        textures.MaterialLayers.SELF_ILLUMUNATION_MASK,
                        textures.MaterialLayers.OPACITY,
                        'unknown',
                        textures.MaterialLayers.SELF_ILLUMUNATION_COLOR,
                    ]):
                        with writer.start_chunk('DATACHAN'):
                            has_data = material_images.get(key) is not None
                            colour_mask = {
                                textures.MaterialLayers.DIFFUSE: [255] * 4 if has_data else [0, 0, 0, 255],
                                textures.MaterialLayers.OPACITY: [255] * 4,
                                textures.MaterialLayers.SPECULAR_MASK: [int(255 * metallic)] * 3 + [255],
                                textures.MaterialLayers.SELF_ILLUMUNATION_COLOR: [255] * 4,
                            }.get(key, [0, 0, 0, 255])
                            writer.write_struct('<2l4B', channel_idx, int(has_data), *colour_mask)
                            writer.write_str(str(material_images.get(key, '')))
                            writer.write_struct('<3L', int(has_data), 5, 7 if key == textures.MaterialLayers.SPECULAR_REFLECTION else 0)
                            for idx in range(4):
                                pairs = [(1.0, 0.0), (0.0, 0.0), (0.0, 1.0), (0.0, 0.0)]
                                if idx % 2 == 1:
                                    pairs = pairs[3:] + pairs[:3]
                                for c in pairs:
                                    writer.write_struct('<2f', *c)

            wtp_path = self.paths.get_path(f'{mat_path}_default.wtp')
            teamcolor_images = {k: v.image for k, v in teamcolor_image_nodes.items() if v}
            if self.export_wtp(
                teamcolor_images,
                teamcolor_badge_info,
                teamcolor_banner_info,
                wtp_path,
            ):
                self.paths.add_info(f'{mat_path}_default.wtp', wtp_path)
            rtx_path = self.paths.get_path(f'{mat_path}{self.teamcolored_rtx_suffix}.rtx')

            teamcolor_colors = {}
            for node in mat.node_tree.nodes:
                if node.bl_idname == 'ShaderNodeValToRGB' and node.label.startswith('color_'):
                    try:
                        key = textures.TeamcolorLayers(node.label[len('color_'):])
                    except KeyError:
                        continue
                    teamcolor_colors[key] = mathutils.Color(node.color_ramp.elements[-1].color[:3]).from_scene_linear_to_srgb()
                if node.bl_idname == 'ShaderNodeGroup' and node.node_tree == bpy.data.node_groups.get('ApplyTeamcolor', None):
                    for key in textures.TeamcolorLayers:
                        input_name = f'{key.value}_color'
                        if input_name not in node.inputs:
                            continue
                        teamcolor_colors[key] = mathutils.Color(node.inputs[input_name].default_value[:3]).from_scene_linear_to_srgb()
            if self.export_teamcolored_rtx:
                if self.do_export_teamcolored_rtx(
                    teamcolor_images,
                    teamcolor_colors,
                    teamcolor_badge_info,
                    teamcolor_banner_info,
                    rtx_path,
                    mat.name,
                ):
                    self.paths.add_info(f'{mat_path}{self.teamcolored_rtx_suffix}.rtx', rtx_path)
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
            self.copy_file(exported_file, dst_path)
        return True

    def write_rsh_chunks(self, writer: ChunkWriter, images: dict, declared_path: pathlib.PurePosixPath, mat_name: str) -> bool:
        texture_declared_paths = {}
        known_keys = {
            textures.MaterialLayers.DIFFUSE: '',
            textures.MaterialLayers.SPECULAR_MASK: '_spec',
            textures.MaterialLayers.SPECULAR_REFLECTION: '_reflect',
            textures.MaterialLayers.SELF_ILLUMUNATION_MASK: '_self_illum',
            textures.MaterialLayers.OPACITY: '_alpha',
        }
        for key, path_suffix in known_keys.items():
            image = images.get(key)
            if not image:
                continue
            texture_declared_path = f'{declared_path}{path_suffix}'  # used for locating .wtp
            texture_declared_paths[key] = texture_declared_path

            pil_image = textures.img2pil(image)
            if pil_image is None:
                self.messages.append(('WARNING', f'Error while converting {image.name}'))
                return False

            pil_image.thumbnail((self.max_texture_size, self.max_texture_size))
            width, height, num_mips, image_format, image_type, texture_stream = textures.encode_dds(pil_image)

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

    def export_wtp(self, images: dict, badge_info: dict, banner_info: dict, dst_path: pathlib.Path) -> bool:
        if images.get(textures.TeamcolorLayers.DEFAULT) is None or images.get(textures.TeamcolorLayers.DIRT) is None :
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
                    for layer_idx, layer in [
                        (0, textures.TeamcolorLayers.PRIMARY),
                        (1, textures.TeamcolorLayers.SECONDARY),
                        (2, textures.TeamcolorLayers.TRIM),
                        (3, textures.TeamcolorLayers.WEAPONS),
                        (4, textures.TeamcolorLayers.EYES),
                        (5, textures.TeamcolorLayers.DIRT),
                    ]:
                        if not (img := images.get(layer)):
                            continue
                        with writer.start_chunk('DATAPTLD'):
                            tmp_file = temp_dir / f'{layer.value}.tga'
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

                    if default_image := images.get(textures.TeamcolorLayers.DEFAULT):
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
            self.copy_file(exported_file, dst_path)
        return True

    def export_de_rtx(self, image, dst_path: pathlib.Path, mat_name: str, force_type: textures.DdsType = None) -> bool:
        result = textures.img2pil(image)
        if result is None:
            return False
        with tempfile.TemporaryDirectory() as t:
            temp_dir = pathlib.Path(t)
            exported_file = temp_dir / dst_path.name
            with exported_file.open('wb') as f:
                writer = ChunkWriter(f, {
                    'FOLDTXTR': {
                        'version': 1,
                        'DATAHEAD': {'version': 1},
                        'DATAINFO': {'version': 3},
                        'FOLDIMAG': {
                            'version': 1,
                            'DATAATTR': {'version': 2},
                            'DATADATA': {'version': 2},
                        }
                    }
                })
                result.thumbnail((self.max_texture_size, self.max_texture_size))
                width, height, num_mips, image_format, image_type, texture_stream = textures.encode_dds(result, force_type=force_type)
                self.write_relic_chunky(writer)
                with writer.start_chunk('FOLDTXTR', name=mat_name):
                    with writer.start_chunk('DATAHEAD'):
                        writer.write_struct('<2l', image_type, 1)  # num_images
                    if self.make_oe_compatable_textures:
                        with writer.start_chunk('DATAINFO'):
                            writer.write_struct('<4l', image_type, width, height, 1)  # num_images
                    with writer.start_chunk('FOLDIMAG'):
                        with writer.start_chunk('DATAATTR'):
                            writer.write_struct('<4l', 0x0b if image_type == 0x07 else 0x08, width, height, num_mips)
                        with writer.start_chunk('DATADATA'):
                            with texture_stream:
                                shutil.copyfileobj(texture_stream, writer)
            dst_path.parent.mkdir(exist_ok=True, parents=True)
            self.copy_file(exported_file, dst_path)
        return True


    def do_export_teamcolored_rtx(self, teamcolor_images: dict, teamcolor_colors: dict, badge_info: dict, banner_info: dict, dst_path: pathlib.Path, mat_name: str) -> bool:
        from PIL import ImageChops

        base_image = teamcolor_images.get(textures.TeamcolorLayers.DEFAULT)
        if base_image is None:
            return False
        base_image = textures.img2pil(base_image)
        if base_image is None:
            return False
        result = PilImage.new('RGB', base_image.size)
        for layer in [
            textures.TeamcolorLayers.PRIMARY,
            textures.TeamcolorLayers.SECONDARY,
            textures.TeamcolorLayers.TRIM,
            textures.TeamcolorLayers.WEAPONS,
            textures.TeamcolorLayers.EYES,
        ]:
            color = teamcolor_colors.get(layer)
            mask = teamcolor_images.get(layer)
            if color is None or mask is None:
                continue
            mask = textures.img2pil(mask)
            if mask is None:
                continue
            result = ImageChops.add(result, ImageChops.overlay(mask.resize(base_image.size).convert('RGB'), PilImage.new('RGB', base_image.size, tuple(int(i * 255) for i in color))))
        result = result.convert('RGBA')
        for layer, data, default_size in [
            (textures.TeamcolorLayers.BADGE, badge_info, (64, 64)),
            (textures.TeamcolorLayers.BANNER, banner_info, (64, 96)),
        ]:
            pos = data.get('position')
            if pos is None:
                continue
            size = data.get('display_size', default_size)
            img = teamcolor_images.get(layer)
            if img is None:
                continue
            img = textures.img2pil(img)
            if img is None:
                continue
            img = img.transpose(PilImage.Transpose.FLIP_TOP_BOTTOM)
            result.alpha_composite(img.resize(tuple(map(int, size))), tuple(map(int, pos)))
        dirt_mask = teamcolor_images.get(textures.TeamcolorLayers.DIRT)
        if dirt_mask is not None:
            dirt_mask = textures.img2pil(dirt_mask)
            if dirt_mask is not None:
                result.paste(base_image, mask=dirt_mask.resize(base_image.size).convert('L'))

        with tempfile.TemporaryDirectory() as t:
            temp_dir = pathlib.Path(t)
            exported_file = temp_dir / dst_path.name
            with exported_file.open('wb') as f:
                writer = ChunkWriter(f, {
                    'FOLDTXTR': {
                        'version': 1,
                        'DATAHEAD': {'version': 1},
                        'DATAINFO': {'version': 3},
                        'FOLDIMAG': {
                            'version': 1,
                            'DATAATTR': {'version': 2},
                            'DATADATA': {'version': 2},
                        }
                    }
                })
                result.thumbnail((self.max_texture_size, self.max_texture_size))
                width, height, num_mips, image_format, image_type, texture_stream = textures.encode_dds(result)
                self.write_relic_chunky(writer)
                with writer.start_chunk('FOLDTXTR', name=mat_name):
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
            dst_path.parent.mkdir(exist_ok=True, parents=True)
            self.copy_file(exported_file, dst_path)
        return True

    @classmethod
    def is_marker(cls, bone, armature):
        has_collections = False
        for collection in armature.collections:
            if collection.name.lower() != 'markers':
                continue
            has_collections = True
            if bone.name in collection.bones:
                return True
        if has_collections:
            return False
        return bone.name.startswith('marker_')

    @classmethod
    def is_camera(cls, bone):
        return any('camera' in c.name.lower() for c in bone.collections)

    def write_skel(self, writer: ChunkWriter):
        all_armatures = [a for a in bpy.data.objects if a.type == 'ARMATURE' if a.data.bones]
        if not all_armatures:
            self.messages.append(('WARNING', f'Cannot find an armature.'))
            bones = []
        else:
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

            bones = [b for b in armature.bones if not (self.is_marker(b, armature) or self.is_camera(b))]
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
            if not bones:
                return
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
        orig_num_vertices = 0
        exported_num_vertices = 0
        shadow_mesh_objs = {o.dow_shadow_mesh for o in bpy.data.objects if o.type == 'MESH' and o.dow_shadow_mesh is not None}
        with writer.start_chunk('FOLDMSGR'):
            for obj_orig in bpy.data.objects:
                if obj_orig.type != 'MESH':
                    continue
                not_weighted_vertices_warn = False
                obj = obj_orig.evaluated_get(depsgraph)
                mesh = obj.data
                if len(mesh.materials) == 0:
                    if obj_orig not in shadow_mesh_objs:
                        self.messages.append(('WARNING', f'Skipping mesh {obj.name} because it has no materials'))
                    continue
                if len(mesh.vertices) == 0:
                    continue

                self.exported_meshes.append(obj.name)
                orig_num_vertices += len(mesh.vertices)
                vert_warn = False
                many_bones_warn = False

                vertex_groups = utils.get_weighted_vertex_groups(obj)
                vertex_groups = [v for v in vertex_groups if v.name in self.bone_to_idx]
                single_bone_name = utils.get_single_bone_name(obj, vertex_groups, self.bone_to_idx)

                if single_bone_name is None and (
                    len(vertex_groups) == 0 or all(len(v.groups) == 0 or v.groups[0].weight < 0.001 for v in mesh.vertices)
                ):
                    self.messages.append(('WARNING', f'Mesh "{obj.name}" seems to be not weighted to any bones'))
                    vertex_groups = []

                if single_bone_name is not None:
                    assert single_bone_name in self.bone_to_idx, f'Cannot find bone "{single_bone_name}" for mesh "{obj.name}"'
                    single_bone_meshes[obj.name] = self.bone_to_idx[single_bone_name]
                    vertex_groups = []

                xref_source = obj_orig.get('xref_source', '').strip()
                if xref_source:
                    # TODO check if file exists
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

                        if self.vertex_position_merge_threshold > 0:
                            vertex_kd = mathutils.kdtree.KDTree(len(mesh.vertices))
                            for i, v in enumerate(mesh.vertices):
                                vertex_kd.insert(obj.matrix_world @ v.co, i)
                            vertex_kd.balance()
                            merged_vertes_idx = {}

                        if len(mesh.uv_layers) == 0:
                            self.messages.append(('WARNING', f'Mesh "{obj.name}" has no UV layers.'))
                        uv_layer = mesh.uv_layers.active
                        for poly in mesh.loop_triangles:
                            poly_vertices = []
                            for loop_idx in poly.loops:
                                orig_vertex_idx = mesh.loops[loop_idx].vertex_index
                                vertex = mesh.vertices[orig_vertex_idx]
                                vertex_pos = obj.matrix_world @ vertex.co
                                if self.vertex_position_merge_threshold > 0:
                                    for (co, index, dist) in vertex_kd.find_range(vertex_pos, self.vertex_position_merge_threshold):
                                        if index == orig_vertex_idx:
                                            continue
                                        if index in merged_vertes_idx:
                                            vertex_pos_key = merged_vertes_idx[index]
                                            break
                                    else:
                                        vertex_pos_key = merged_vertes_idx[orig_vertex_idx] = orig_vertex_idx
                                else:
                                    vertex_pos_key = orig_vertex_idx
                                seen_vertex_data = seen_data.setdefault(vertex_pos_key, [])
                                uv = uv_layer.uv[loop_idx].vector
                                vertex_normal = obj.matrix_world.to_3x3() @ mesh.loops[loop_idx].normal
                                vertex_idx = None
                                for idx, other_normal, other_uv in seen_vertex_data:
                                    if (
                                        (other_normal - vertex_normal).length < self.vertex_normal_merge_threshold
                                        and (uv - other_uv).length < self.uv_merge_threshold
                                    ):
                                        vertex_idx = idx
                                        break
                                if vertex_idx is None:
                                    vertex_idx = len(extended_vertices)
                                    seen_vertex_data.append((vertex_idx, vertex_normal, uv))
                                    vertex_info = VertexInfo(
                                        position=vertex_pos,
                                        vertex_groups=[g for g in vertex.groups
                                                        if obj.vertex_groups[g.group].name in exported_vertex_groups],
                                        normal=vertex_normal,
                                        uv=uv,
                                    )
                                    extended_vertices.append(vertex_info)
                                poly_vertices.append(vertex_idx)
                            extended_polygons.append(poly_vertices)

                        writer.write_struct('<l', len(extended_vertices))
                        writer.write_struct('<l', 39 if vertex_groups else 37)
                        for v in extended_vertices:
                            writer.write_struct('<3f', -v.position.x, v.position.z, -v.position.y)
                        exported_num_vertices += len(extended_vertices)
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
                            # SHADOW VOLUME
                            if obj_orig.dow_shadow_mesh is None:
                                writer.write_struct('<3L', 0, 0, 0)
                            elif single_bone_name is None and len(vertex_groups) > 0:
                                self.messages.append(('WARNING', f'Mesh {obj.name} has a shadow but is attached to more than 1 bone. The shadow is not exported'))
                                writer.write_struct('<3L', 0, 0, 0)
                            else:
                                shadow_obj = obj_orig.dow_shadow_mesh.evaluated_get(depsgraph)
                                shadow_mesh = shadow_obj.data
                                writer.write_struct('<L', len(shadow_mesh.vertices))
                                for v in shadow_mesh.vertices:
                                    writer.write_struct('<3f', -v.co.x, v.co.z, -v.co.y)
                                writer.write_struct('<L', len(shadow_mesh.loop_triangles))
                                shadow_edge_data = {}
                                for p_idx, p in enumerate(shadow_mesh.loop_triangles):
                                    writer.write_struct(
                                        '<3f3L', -p.normal.x, p.normal.z, -p.normal.y,
                                        p.vertices[0], p.vertices[2], p.vertices[1],
                                    )
                                    for l_idx in p.loops:
                                        loop = shadow_mesh.loops[l_idx]
                                        edge_data = shadow_edge_data.get(loop.edge_index)
                                        if edge_data is not None:
                                            edge_data[0] = min(edge_data[0], p_idx)
                                            edge_data[1] = max(edge_data[1], p_idx)
                                        else:
                                            shadow_edge_data[loop.edge_index] = [p_idx, p_idx]
                                writer.write_struct('<L', len(shadow_mesh.edges))
                                for e in shadow_mesh.edges:
                                    edge_data = shadow_edge_data[e.index]
                                    vertices = min(e.vertices), max(e.vertices)
                                    v1, v2 = shadow_mesh.vertices[vertices[0]], shadow_mesh.vertices[vertices[1]]
                                    writer.write_struct(
                                        '<4L6f',
                                        vertices[0], vertices[1],
                                        edge_data[0], edge_data[1],
                                        -v1.co.x, v1.co.z, -v1.co.y, -v2.co.x, v2.co.z, -v2.co.y,
                                    )
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
            if exported_num_vertices != orig_num_vertices:
                self.messages.append((
                    'INFO',
                    f'Exported {exported_num_vertices} vertices ({"+" if exported_num_vertices > orig_num_vertices else "-"}{abs(exported_num_vertices - orig_num_vertices) / orig_num_vertices * 100:.2f}%)',
                ))

    def write_marks(self, writer: ChunkWriter):
        if not self.armature_obj:
            return
        armature = self.armature_obj.data
        markers = [b for b in armature.bones if self.is_marker(b, armature)]
        if not markers:
            return

        coord_transform = mathutils.Matrix([[-1, 0, 0], [0, 1, 0], [0, 0, 1]]).to_4x4()
        coord_transform_inv = coord_transform.inverted()

        with self.start_chunk(writer, ExportFormat.WHM, 'DATAMARK'):
            if self.format is ExportFormat.WHM:
                writer.write_struct('<l', len(markers))
            for marker in markers:
                with self.start_chunk(writer, ExportFormat.SGM, 'DATAMARK', name=marker.name):
                    if self.format is ExportFormat.WHM:
                        writer.write_str(marker.name)
                    writer.write_str(marker.parent.name if marker.parent else '')
                    if marker.parent:
                        parent_mat = self.bone_transforms[marker.parent]
                    else:
                        parent_mat = self.armature_obj.matrix_world.inverted() @ mathutils.Matrix.Rotation(math.radians(90.0), 4, 'X')
                    if self.use_legacy_marker_orientation:
                        delta = mathutils.Matrix.Rotation(math.radians(-90.0), 4, 'Z')
                        transform = parent_mat.inverted() @ marker.matrix_local @ delta.inverted()
                        for row_idx in range(3):
                            writer.write_struct('<3f', *transform[row_idx][:3])
                        writer.write_struct('<3f', -transform[0][3], transform[1][3], transform[2][3])
                        self.bone_transforms[marker] = marker.matrix_local @ delta.inverted()
                        continue
                    transform = coord_transform @ parent_mat.inverted() @ marker.matrix_local @ coord_transform_inv
                    loc, rot, _ = transform.decompose()
                    rot = rot.to_matrix().transposed()
                    for row_idx in range(3):
                        writer.write_struct('<3f', *rot[row_idx])
                    writer.write_struct('<3f', *loc)
                self.bone_transforms[marker] = marker.matrix_local

    def write_cams(self, writer: ChunkWriter):
        cameras = [i for i in bpy.data.objects if i.type == 'CAMERA']
        if not cameras:
            return
        with self.start_chunk(writer, ExportFormat.WHM, 'DATACAMS'):
            if self.format is ExportFormat.WHM:
                writer.write_struct('<l', len(cameras))

            coord_transform = mathutils.Matrix([[-1, 0, 0], [0, 0, 1], [0, -1, 0]]).to_4x4()
            world_rot_inv = (
                mathutils.Matrix.Rotation(math.radians(180.0), 4, 'Y')
                @ mathutils.Matrix.Rotation(math.radians(90.0), 4, 'X')
            ).inverted().to_quaternion()
            coord_transform_inv = coord_transform.inverted()

            for camera in cameras:
                with self.start_chunk(writer, ExportFormat.SGM, 'DATACMRA', name=camera.name):
                    if self.format is ExportFormat.WHM:
                        writer.write_str(camera.name)
                    matrix = coord_transform @ camera.matrix_basis @ coord_transform_inv
                    loc, rot, _ = matrix.decompose()
                    rot = rot @ world_rot_inv
                    writer.write_struct('<3f', *loc)
                    writer.write_struct('<4f', *rot[1:], rot[0])
                    fov = 2.14 / (math.tan((2 * math.pi - camera.data.angle) / 4) - math.pi / 9)
                    writer.write_struct('<3f', fov, camera.data.clip_start, camera.data.clip_end)
                    if camera.data.dof.use_dof and (focus_obj := camera.data.dof.focus_object) is not None:
                        writer.write_struct('<3f', -focus_obj.location[0], focus_obj.location[2], -focus_obj.location[1])
                    else:
                        writer.write_struct('<3f',  0, 0, 0)

    def write_anims(self, writer: ChunkWriter):
        anim_objects = [i for i in utils.iter_animatable() if getattr(i, 'animation_data', None) is not None]
        if len(anim_objects) == 0:
            self.messages.append(('WARNING', 'Cannot find the animation root object'))
            return
        slot_owers = {}
        for action in bpy.data.actions:
            for obj in anim_objects:
                orig_action = obj.animation_data.action
                obj.animation_data.action = action
                slot_owers.setdefault(obj.animation_data.action_slot, []).append(obj)
                obj.animation_data.action = orig_action
        for action in bpy.data.actions:
            anim_sections = collections.defaultdict(dict)
            prop_fcurves = collections.defaultdict(dict)
            max_fcurve_frame = 0
            for slot in action.slots:
                channelbag = anim_utils.action_get_channelbag_for_slot(action, slot)
                animated_cameras = []
                for anim_root in slot_owers.get(slot, []):
                    if anim_root.id_type == 'OBJECT' and anim_root.data.id_type == 'CAMERA':
                        animated_cameras.append(anim_root)
                    if channelbag is None:
                        continue
                    for fcurve in channelbag.fcurves:
                        if fcurve.is_empty:
                            continue
                        max_fcurve_frame = max(max_fcurve_frame, max((k.co[0] for k in fcurve.keyframe_points), default=0))
                        attr = None
                        if fcurve.data_path.endswith(']'):
                            py_path = f'x{fcurve.data_path}' if fcurve.data_path.startswith('[') else f'x.{fcurve.data_path}'
                            attr = ast.parse(py_path, mode='single').body[0].value.slice.value
                            path = fcurve.data_path.rsplit(bpy.utils.escape_identifier(attr), 1)[0][:-2]
                        if anim_root.id_type == 'OBJECT' and anim_root.data.id_type == 'ARMATURE':
                            if attr is not None:
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
                        else:
                            if attr is not None:
                                prop_group = attr
                            else:
                                prop_group = fcurve.data_path
                                if prop_group == 'color' and fcurve.array_index == 3:
                                    prop_group = 'visibility'
                                elif prop_group.startswith('nodes["Mapping"].inputs[1]'):
                                    for m in bpy.data.materials:
                                        if m.node_tree == anim_root:
                                            anim_root = m
                                            break
                                    prop_group = 'uv_offset'
                                elif prop_group.startswith('nodes["Mapping"].inputs[3]'):
                                    prop_group = 'uv_tiling'
                                    for m in bpy.data.materials:
                                        if m.node_tree == anim_root:
                                            anim_root = m
                                            break
                                elif anim_root.id_type == 'OBJECT' and anim_root.data.id_type == 'CAMERA':
                                    prop_group = f'{anim_root.data.id_type}_{prop_group}'
                                else:
                                    prop_group = prop_group.lower()
                            prop_fcurves[prop_group].setdefault(anim_root.name.lower(), []).append(fcurve)

            def get_prop_fcurves(prop: str, obj_name: str) -> list:
                prop_data = prop_fcurves[prop]
                obj_name = obj_name.lower()
                return prop_data.get(obj_name, []) or prop_data.get(utils.get_hash(obj_name), [])

            with writer.start_chunk('FOLDANIM', name=action.name):
                with self.start_chunk(writer, ExportFormat.WHM, 'DATADATA', name=action.name), \
                    self.start_chunk(writer, ExportFormat.SGM, 'FOLDDATA', name=action.name):
                    frame_end = action.frame_end or max_fcurve_frame
                    with self.start_chunk(writer, ExportFormat.SGM, 'DATAINFO'):
                        writer.write_struct('<l', int(frame_end) + 1)
                        writer.write_struct('<f', (frame_end + 1) / action.get('fps', 30))
                    bones = [self.armature_obj.pose.bones[b.name] for b in self.exported_bones or []]
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
                                if rot.magnitude > 0:
                                    rot.normalize()
                                frame_matrix = mathutils.Matrix.LocRotScale(loc, rot, None)
                                relative_matrix = parent_mat.inverted() @ bone.matrix_local @ frame_matrix @ delta.inverted()
                                frame_data.append(relative_matrix.decompose())

                            writer.write_struct('<l', len(all_frames))
                            for frame, (loc, rot, _) in zip(all_frames, frame_data):
                                writer.write_struct('<f', frame / max(frame_end, 1))
                                writer.write_struct('<3f', -loc.x, loc.y, loc.z)

                            writer.write_struct('<l', len(all_frames))
                            prev_rot = mathutils.Quaternion()
                            for frame, (loc, rot, _) in zip(all_frames, frame_data):
                                writer.write_struct('<f', frame / max(frame_end, 1))
                                rot.make_compatible(prev_rot)
                                prev_rot = rot
                                writer.write_struct('<4f', rot.x, -rot.y, -rot.z, rot.w)

                            stale_flag = 1
                            if bone_obj in anim_sections['stale']:
                                keyframes = anim_sections['stale'][bone_obj][0].keyframe_points
                                if len(keyframes) > 1:
                                    vals = {k.co[1] for k in keyframes}
                                    if len(vals) > 1:
                                        self.messages.append(('WARNING', f'''Found {len(vals)} values for "stale" property of bone "{bone_obj.name}" in the action "{action.name}". Only the first one is used.'''))
                                if keyframes[0].co[1]:
                                    stale_flag = 0
                            writer.write_struct('<b', stale_flag)

                    mesh_fcurves = prop_fcurves['visibility'].keys() | prop_fcurves['force_invisible'].keys()
                    exported_tex_fcurves = {
                        (g, mat_name): [
                            f for f in get_prop_fcurves(g, mat_name)
                            if f.array_index in (0, 1)
                        ]
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
                                if len(keypoints) >= 2 and keypoints[0].co[0] == 0. and keypoints[0].co[1] == keypoints[1].co[1]:
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
                                    vals = {k.co[1] for k in keyframes}
                                    if len(vals) > 1:
                                        self.messages.append(('WARNING', f'''Found {len(vals)} values for "force_invisible" property of mesh "{mesh_name}" in the action "{action.name}". Only the first one is used.'''))
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
                            mat_path = self.exported_materials[mat.name]
                            for fcurve in fcurves:
                                if self.format is ExportFormat.WHM:
                                    writer.write_str(mat_path)
                                with self.start_chunk(writer, ExportFormat.SGM, 'DATACANM', name=mat_path):
                                    writer.write_struct('<l', 0)  # mode
                                    writer.write_struct('<4x')
                                    tex_anim_type, mult, add = {
                                        ('uv_offset', 0): (1, 1, 0),
                                        ('uv_offset', 1): (2, -1, 0),
                                        ('uv_tiling', 0): (3, 1, -1),
                                        ('uv_tiling', 1): (4, 1, -1),
                                    }[group, fcurve.array_index]
                                    writer.write_struct('<2l', tex_anim_type, len(fcurve.keyframe_points))
                                    for point in fcurve.keyframe_points:
                                        frame, val = point.co
                                        writer.write_struct('<2f', frame / max(frame_end, 1), val * mult + add)
                    if self.format is ExportFormat.WHM:
                        writer.write_struct('<l', len(animated_cameras))
                        coord_transform = mathutils.Matrix([[-1, 0, 0], [0, 0, 1], [0, -1, 0]]).to_4x4()
                        world_rot_inv = (
                            mathutils.Matrix.Rotation(math.radians(180.0), 4, 'Y')
                            @ mathutils.Matrix.Rotation(math.radians(90.0), 4, 'X')
                        ).inverted().to_quaternion()
                        coord_transform_inv = coord_transform.inverted()
                        for cam in animated_cameras:
                            writer.write_str(cam.name)
                            cam_loc_fcurves = [None] * 3
                            cam_loc_frames = set()

                            for fcurve in get_prop_fcurves('CAMERA_location', cam.name):
                                cam_loc_fcurves[fcurve.array_index] = fcurve
                                for keyframe in fcurve.keyframe_points:
                                    frame, val = keyframe.co
                                    cam_loc_frames.add(frame)

                            cam_rot_fcurves = [None] * 4
                            cam_rot_frames = set()
                            for fcurve in get_prop_fcurves('CAMERA_rotation_quaternion', cam.name):
                                cam_rot_fcurves[fcurve.array_index] = fcurve
                                for keyframe in fcurve.keyframe_points:
                                    frame, val = keyframe.co
                                    cam_rot_frames.add(frame)

                            all_cam_frames = sorted(cam_loc_frames | cam_rot_frames)
                            cam_frame_data = []
                            for frame in all_cam_frames:
                                loc = mathutils.Vector([fcurve.evaluate(frame) if fcurve is not None else 0
                                                        for fcurve in cam_loc_fcurves])
                                rot = mathutils.Quaternion([fcurve.evaluate(frame) if fcurve is not None else 0
                                                            for fcurve in cam_rot_fcurves])
                                if rot.magnitude > 0:
                                    rot.normalize()
                                frame_matrix = coord_transform @ mathutils.Matrix.LocRotScale(loc, rot, None) @ coord_transform_inv
                                loc, rot, _ = frame_matrix.decompose()
                                rot = rot @ world_rot_inv
                                cam_frame_data.append([loc, rot])

                            writer.write_struct('<l', len(all_cam_frames))
                            for frame, (loc, rot) in zip(all_cam_frames, cam_frame_data):
                                writer.write_struct('<f', frame / max(frame_end, 1))
                                writer.write_struct('<3f', loc.x, loc.y, loc.z)

                            writer.write_struct('<l', len(all_cam_frames))
                            prev_rot = mathutils.Quaternion()
                            for frame, (loc, rot) in zip(all_cam_frames, cam_frame_data):
                                writer.write_struct('<f', frame / max(frame_end, 1))
                                rot.make_compatible(prev_rot)
                                prev_rot = rot
                                writer.write_struct('<4f', rot.x, rot.y, rot.z, rot.w)
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
