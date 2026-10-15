import math
import pathlib
import struct

import bpy
import mathutils

from .. import materials, textures
from ..chunky import ChunkWriter, ChunkyVersion
from ..exporting import FileDispatcher, find_root_collection, image_game_path, marker_params, serialize_matrix_4x3, weld_mesh
from .importer import BONE_ROLL_INV, CONV, CONV_INV, MARKER_ROLL, TEXTURE_VARS

CHUNK_VERSIONS = {
    'FOLDMODL': {
        'version': 1,
        'FOLDMTRL': {'version': 1, 'DATAINFO': {'version': 1}, 'DATA\x00VAR': {'version': 1}},
        'FOLDMESH': {'version': 3, 'FOLDMGRP': {
            'version': 3,
            'DATANODE': {'version': 3},
            'FOLDMESH': {'version': 3, 'FOLDIMDG': {'version': 1, 'FOLDMESH': {'version': 3, 'FOLDIMOD': {
                'version': 4,
                'DATADATA': {'version': 9},
                'FOLDMESH': {'version': 3, 'FOLDTRIM': {
                    'version': 7, 'DATADATA': {'version': 7}, 'DATABVOL': {'version': 2}}},
            }}}},
        }},
        'DATAMRKS': {'version': 1},
        'FOLDSKEL': {'version': 3, 'DATAINFO': {'version': 1}, 'DATABONE': {'version': 7}},
        'FOLDMSBP': {'version': 1, 'DATAMSD ': {'version': 2}, 'DATACNBP': {'version': 3}},
        'DATADTBP': {'version': 3},
    },
}

MARKER_ROLL_INV = MARKER_ROLL.inverted()

# vertex layers the importer round-trips; tangents/binormals (slots 4/5) are omitted
ELEMENT_SIZES = {2: 4, 3: 8, 4: 12, 14: 4}

SOCKET_VARS = (
    ('alphaTest', 'alpha_test', 10),
    ('bHighlight', 'highlight', 10),
    ('unitOcclusionFlag', 'occlusion_flag', 0),
    ('dirtVisibility', 'dirt_visibility', 1),
)
BADGE_TEX_VARS = {'badge1Tex': materials.TeamcolorLayers.BADGE, 'badge2Tex': materials.TeamcolorLayers.BANNER}
LAYER_TEX_VARS = {
    'diffuseTex': materials.MaterialLayers.DIFFUSE,
    'normalMap': materials.MaterialLayers.NORMAL_MAP,
    'specularTex': materials.MaterialLayers.SPECULAR_MASK,
    'glossTex': materials.MaterialLayers.GLOSS,
    'occlusionTex': materials.MaterialLayers.OCCLUSION,
    'dirtTex': materials.MaterialLayers.DIRT,
    'emissiveTex': materials.MaterialLayers.SELF_ILLUMUNATION_COLOR,
    'teamTex': materials.MaterialLayers.PATTERN,
}


def to_file_point(v) -> tuple:
    return -v[0], v[2], -v[1]


def to_file_matrix(matrix) -> mathutils.Matrix:
    return CONV_INV @ matrix @ CONV


def snorm_byte(value: float) -> int:
    return max(0, min(255, round((value + 1.0) * 127.5)))


class Exporter:
    def __init__(self, paths: FileDispatcher, convert_textures: bool = True, context=None):
        self.paths = paths
        self.convert_textures = convert_textures
        self.bpy_context = context or bpy.context
        self.messages = []
        self.exported_images = set()

    def export(self, writer: ChunkWriter, object_name: str):
        scene = self.bpy_context.scene
        armature_obj = next((o for o in scene.objects if o.type == 'ARMATURE'), None)
        root_collection = find_root_collection(scene)
        states = self.collect_states(root_collection)
        mats = sorted({slot.material
                       for _, _, objects in states for o in objects for slot in o.material_slots
                       if slot.material is not None}, key=lambda m: m.name)

        writer.write_relic_chunky()
        with writer.start_chunk('FOLDMODL', name=object_name):
            for mat in mats:
                self.write_material(writer, mat)
            self.write_meshes(writer, states, armature_obj)
            self.write_markers(writer, armature_obj)
            if armature_obj is not None:
                self.write_skeleton(writer, armature_obj)
            self.write_states(writer, root_collection, states)

    def collect_states(self, root_collection) -> list:
        """(lod level, state name, mesh objects) from the imported lod/state collection tree"""
        states = []
        for lod_collection in root_collection.children:
            base = lod_collection.name.split('.')[0]
            if not (base.startswith('lod') and base[len('lod'):].isdigit()):
                continue
            level = int(base[len('lod'):])
            for state_collection in lod_collection.children:
                objects = [o for o in state_collection.all_objects if o.type == 'MESH']
                states.append((level, state_collection.name.split('.')[0], objects))
        if not states:
            objects = [o for o in root_collection.all_objects if o.type == 'MESH']
            if objects:
                states.append((0, 'healthy', objects))
        return sorted(states, key=lambda s: (s[0], s[1]))

    def texture_var_path(self, mat, var: str, image) -> str | None:
        if image is not None and not image.get('PLACEHOLDER'):
            return image_game_path(image)
        stored = mat.get(var)
        return str(stored) if stored else None

    def write_material(self, writer: ChunkWriter, mat):
        info = materials.extract_material_info(mat, use_random_diffuse_fallback=False)
        group_node = materials.get_shader_group_node(mat)
        offset_node = materials.get_uv_offset_node(mat)
        badge_images = {}
        for node in mat.node_tree.nodes:
            if node.bl_idname == 'ShaderNodeTexImage' and node.image is not None and not node.image.get('PLACEHOLDER'):
                label = node.label.removeprefix('UNUSED_')
                if label in (materials.TeamcolorLayers.BADGE, materials.TeamcolorLayers.BANNER):
                    badge_images.setdefault(label, node.image)

        with writer.start_chunk('FOLDMTRL', name=mat.name):
            with writer.start_chunk('DATAINFO', name='Material Info'):
                writer.write_str(mat.get('dow2_shader', 'dow2_unit'))

            def write_var(name, var_type, payload: bytes):
                with writer.start_chunk('DATA\x00VAR', name='Material Variable'):
                    writer.write_str(name)
                    writer.write_struct('<2L', var_type, len(payload))
                    writer.write(payload)

            for var in TEXTURE_VARS:
                if var in BADGE_TEX_VARS:
                    image = badge_images.get(BADGE_TEX_VARS[var])
                else:
                    image = info.channel_images.get(LAYER_TEX_VARS[var])
                path = self.texture_var_path(mat, var, image)
                if path is None:
                    continue
                write_var(var, 9, str(pathlib.PureWindowsPath(path)).encode('utf8') + b'\0')
                self.export_texture(image, path)
            write_var('emissiveMultiplier', 1, struct.pack('<f', info.default_emission_strength))
            if group_node is not None:
                for var, socket, var_type in SOCKET_VARS:
                    if socket not in group_node.inputs:
                        continue
                    value = group_node.inputs[socket].default_value
                    if var_type == 10:
                        write_var(var, 10, bytes([1 if value else 0]))
                    elif var_type == 0:
                        write_var(var, 0, struct.pack('<L', round(value)))
                    else:
                        write_var(var, 1, struct.pack('<f', value))
            location = offset_node.inputs['Location'].default_value if offset_node is not None else (0.0, 0.0)
            write_var('uOffset', 1, struct.pack('<f', location[0]))
            write_var('vOffset', 1, struct.pack('<f', location[1]))
            self.write_badge_rects(write_var, info)
            write_var('World', 8, b'\0' * 64)

    def write_badge_rects(self, write_var, info):
        diffuse = info.channel_images.get(materials.MaterialLayers.DIFFUSE)
        if diffuse is None or not diffuse.size[0] or not diffuse.size[1]:
            return
        width, height = diffuse.size
        for slot, position, size, rotation in (
            ('badge1', info.badge_position, info.badge_size, info.badge_rotation),
            ('badge2', info.banner_position, info.banner_size, info.banner_rotation),
        ):
            if position is None:
                continue
            u_axis = mathutils.Vector((math.cos(rotation), math.sin(rotation))) * (size[0] / width)
            v_axis = mathutils.Vector((-math.sin(rotation), math.cos(rotation))) * (-size[1] / height)
            inverse = mathutils.Matrix(((u_axis.x, v_axis.x), (u_axis.y, v_axis.y)))
            if not inverse.determinant():
                continue
            matrix = inverse.inverted()
            before = mathutils.Vector((position[0] / width, position[1] / height)) - v_axis
            before.y += 1
            translate = -(matrix @ before)
            write_var(f'{slot}MatrixRow1Row2', 5,
                      struct.pack('<4f', matrix[0][0], matrix[1][0], matrix[0][1], matrix[1][1]))
            write_var(f'{slot}Translate', 5, struct.pack('<4f', translate.x, translate.y, 0.0, 0.0))

    def export_texture(self, image, game_path: str):
        if not self.convert_textures or image is None or image.get('PLACEHOLDER'):
            return
        posix = pathlib.PureWindowsPath(game_path).as_posix()
        if posix in self.exported_images:
            return
        self.exported_images.add(posix)
        dst = self.paths.get_path(f'{posix}.dds')
        dst.parent.mkdir(parents=True, exist_ok=True)
        pil = textures.img2pil(image)
        *_, stream = textures.encode_dds(pil)
        dst.write_bytes(stream.getvalue())
        self.paths.add_info(f'{posix}.dds', dst)

    def write_meshes(self, writer: ChunkWriter, states, armature_obj):
        by_state = {}
        for level, state, objects in states:
            by_state.setdefault(state, []).append((level, objects))
        with writer.start_chunk('FOLDMESH'):
            with writer.start_chunk('FOLDMGRP'):
                for state, lods in by_state.items():
                    with writer.start_chunk('FOLDMESH', name=state):
                        with writer.start_chunk('FOLDIMDG', name=state):
                            for level, objects in lods:
                                with writer.start_chunk('FOLDMESH', name=f'{state}:lod{level}'):
                                    with writer.start_chunk('FOLDIMOD', name=f'{state}:lod{level}'):
                                        with writer.start_chunk('DATADATA'):
                                            writer.write_struct('<L', level)
                                        for obj in objects:
                                            name = obj.name.split('.')[0]
                                            with writer.start_chunk('FOLDMESH', name=name):
                                                with writer.start_chunk('FOLDTRIM', name=name):
                                                    self.write_trim(writer, obj, armature_obj)
                for state in by_state:
                    with writer.start_chunk('DATANODE'):
                        writer.write_str(state)
                        writer.write_str(state)

    def write_trim(self, writer: ChunkWriter, obj, armature_obj):
        records, triangles = weld_mesh(obj)
        has_skin = armature_obj is not None and any(rec[4] for rec in records)
        num_uvs = max((len(rec[2]) for rec in records), default=1) or 1
        has_color = any(rec[3] is not None for rec in records)
        bone_names = []
        file_verts = []
        for position, normal, uvs, color, influences in records:
            if has_skin:
                bone_names.extend(name for name, _ in influences if name not in bone_names)
            file_verts.append((to_file_point(position), to_file_point(normal), uvs, color, influences))
        armature_order = {b.name: i for i, b in enumerate(armature_obj.data.bones)} if has_skin else {}
        bone_names.sort(key=lambda n: armature_order.get(n, len(armature_order)))
        bone_index = {name: i for i, name in enumerate(bone_names)}

        with writer.start_chunk('DATADATA'):
            elements = [(0, 4, 4)]
            if has_skin:
                elements += [(1, 4, 14), (2, 4, 2)]
            elements.append((3, 4, 2))
            if has_color:
                elements.append((6, 4, 2))
            elements += [(8 + i, 4, 3) for i in range(num_uvs)]
            writer.write_struct('<L', len(elements))
            for row in elements:
                writer.write_struct('<3L', *row)
            stride = sum(ELEMENT_SIZES[row[2]] for row in elements)
            writer.write_struct('<2L', len(file_verts), stride)
            for position, normal, uvs, color, influences in file_verts:
                writer.write_struct('<3f', *position)
                if has_skin:
                    indices = [bone_index[name] for name, _ in influences]
                    weights = [max(0, min(255, round(w * 255))) for _, w in influences]
                    if weights:
                        weights[0] += 255 - sum(weights)
                    indices += [0] * (4 - len(indices))
                    weights += [0] * (4 - len(weights))
                    writer.write_struct('<4B', *indices)
                    writer.write_struct('<4B', weights[2], weights[1], weights[0], weights[3])
                writer.write_struct('<4B', snorm_byte(normal[2]), snorm_byte(normal[1]), snorm_byte(normal[0]), 0)
                if has_color:
                    value = [max(0, min(255, round(c * 255))) for c in (color or (0.0, 0.0, 0.0, 1.0))]
                    writer.write_struct('<4B', value[2], value[1], value[0], value[3])
                for i in range(num_uvs):
                    u, v = uvs[i] if i < len(uvs) else (0.0, 0.0)
                    writer.write_struct('<2f', u, 1.0 - v)

            indices = [i for tri in triangles for i in (tri[0], tri[2], tri[1])]
            writer.write_struct('<2L', 1, len(indices))
            writer.write_struct('<2L', 3, len(indices))
            writer.write_struct(f'<{len(indices)}H', *indices)
            material = next((slot.material for slot in obj.material_slots if slot.material is not None), None)
            writer.write_str(material.name if material is not None else '')
            writer.write_struct('<L', len(bone_names))
            for name in bone_names:
                bone = armature_obj.data.bones.get(name)
                world = bone.matrix_local @ BONE_ROLL_INV if bone is not None else mathutils.Matrix.Identity(4)
                bind = to_file_matrix(world)
                writer.write_struct('<12f', *serialize_matrix_4x3(bind))
                writer.write_struct('<12f', *serialize_matrix_4x3(bind.inverted()))
                writer.write_str(name)
            writer.write(b'\0' * 8)

        # one volume for the whole submesh, then one per skin bone over its influenced verts
        groups = [file_verts]
        groups += [[vert for vert in file_verts if any(n == name and w > 0 for n, w in vert[4])]
                   for name in bone_names]
        for verts in groups:
            with writer.start_chunk('DATABVOL'):
                if verts:
                    lo = [min(v[0][k] for v in verts) for k in range(3)]
                    hi = [max(v[0][k] for v in verts) for k in range(3)]
                    center = [(a + b) / 2 for a, b in zip(lo, hi)]
                    extents = [(b - a) / 2 for a, b in zip(lo, hi)]
                else:
                    center = extents = (0.0, 0.0, 0.0)
                writer.write_struct('<B6f', 1, *center, *extents)
                writer.write_struct('<9f', 1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)

    def write_markers(self, writer: ChunkWriter, armature_obj):
        markers = []
        if armature_obj is not None:
            collection = armature_obj.data.collections.get('Markers')
            for bone in (collection.bones if collection is not None else ()):
                world = bone.matrix_local @ MARKER_ROLL_INV
                local = world
                parent = bone.parent.name if bone.parent is not None else ''
                if bone.parent is not None:
                    local = (bone.parent.matrix_local @ BONE_ROLL_INV).inverted() @ world
                markers.append((bone.name, parent, local, marker_params(armature_obj.pose.bones[bone.name])))
        with writer.start_chunk('DATAMRKS'):
            writer.write_struct('<L', len(markers))
            for name, parent, local, params in markers:
                writer.write_str(name)
                writer.write_str(parent)
                writer.write_struct('<12f', *serialize_matrix_4x3(to_file_matrix(local)))
                writer.write_struct('<L', len(params))
                for key, value in params.items():
                    writer.write_str(key)
                    writer.write_struct('<L', 11)
                    writer.write_str(str(value))

    def write_skeleton(self, writer: ChunkWriter, armature_obj):
        armature = armature_obj.data
        marker_collection = armature.collections.get('Markers')
        marker_bones = {b.name for b in marker_collection.bones} if marker_collection is not None else set()
        bones = [b for b in armature.bones if b.name not in marker_bones]
        order = {b.name: i for i, b in enumerate(bones)}
        with writer.start_chunk('FOLDSKEL'):
            with writer.start_chunk('DATAINFO'):
                writer.write_struct('<L', len(bones))
            for bone in bones:
                world = bone.matrix_local @ BONE_ROLL_INV
                local = world
                parent = -1
                if bone.parent is not None and bone.parent.name in order:
                    parent = order[bone.parent.name]
                    local = (bone.parent.matrix_local @ BONE_ROLL_INV).inverted() @ world
                with writer.start_chunk('DATABONE', name=bone.name):
                    writer.write_struct('<lL', parent, 0xffffffff)
                    writer.write_struct('<12f', *serialize_matrix_4x3(to_file_matrix(local)))
                    writer.write(b'\0' * 8)

    def write_states(self, writer: ChunkWriter, root_collection, states):
        present = {}
        for _, state, objects in states:
            present[state] = present.get(state, False) or bool(objects)
        state_props = [(key[len('states_'):], [str(v) for v in root_collection[key]])
                       for key in root_collection.keys() if key.startswith('states_')]
        state_names = list(dict.fromkeys(name for _, values in state_props for name in values)) or list(present)
        with writer.start_chunk('FOLDMSBP'):
            with writer.start_chunk('DATAMSD ', version=1):
                writer.write_struct('<L', len(present))
            for state, has_meshes in present.items():
                with writer.start_chunk('DATAMSD '):
                    writer.write_str(state)
                    meshes = [state] if has_meshes else []
                    writer.write_struct('<L', len(meshes))
                    for name in meshes:
                        writer.write_str(name)
            with writer.start_chunk('DATACNBP'):
                writer.write_struct('<L', 0)
        with writer.start_chunk('DATADTBP'):
            writer.write_struct('<L', 0)
            writer.write_struct('<L', len(state_props))
            for name, values in state_props:
                writer.write_str(name)
                writer.write_struct('<L', len(values))
                for value in values:
                    writer.write_str(value)
            writer.write_str(str(root_collection.get('state_default', state_names[0] if state_names else '')))
            variables = [(key[len('variable_'):], list(root_collection[key]))
                         for key in root_collection.keys() if key.startswith('variable_')]
            writer.write_struct('<L', len(variables))
            for name, values in variables:
                writer.write_str(name)
                writer.write_struct('<3f', *values[:3])
                writer.write_struct('<B', round(values[3]) if len(values) > 3 else 0)


def export_model(filepath, paths: FileDispatcher, object_name: str, convert_textures: bool = True, context=None) -> list:
    exporter = Exporter(paths, convert_textures=convert_textures, context=context)
    with open(filepath, 'wb') as f:
        writer = ChunkWriter(f, CHUNK_VERSIONS, ChunkyVersion.V3)
        exporter.export(writer, object_name)
    return exporter.messages
