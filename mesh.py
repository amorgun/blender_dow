import dataclasses

import bpy
import mathutils


@dataclasses.dataclass
class MeshData:
    vert_array: list
    face_array: list
    skin_vert_array: list | None = None             # per-vertex (bone names, weights Vector)
    matid_array: list | None = None                 # per-face material indices
    color_array: list | None = None                 # per-vertex colors
    split_normals: list | None = None               # per-corner
    corner_uv_arrays: list = dataclasses.field(default_factory=list)  # per-corner flat uv floats per layer


def build_corners(data: MeshData, vertex_normals: list | None, vertex_uvs: list) -> MeshData:
    data.split_normals = None if vertex_normals is None else [
        vertex_normals[i] for face in data.face_array for i in face]
    data.corner_uv_arrays = [
        [f for face in data.face_array for i in face for f in uvs[i]]
        for uvs in vertex_uvs]
    return data


def merge(
        data: MeshData,
        vertex_normals: list,
        vertex_uvs: list,
        *,
        position_threshold: float = 0.001,
        normal_threshold: float = 1.99,
        weight_threshold: float = 0.01,
    ) -> MeshData:
    """Merge vertices closer than the thresholds, dropping degenerate and duplicate faces.

    Corner data (split_normals, corner_uv_arrays) keeps the original per-corner values."""
    vertex_kd = mathutils.kdtree.KDTree(len(data.vert_array))
    for idx, v in enumerate(data.vert_array):
        vertex_kd.insert(v, idx)
    vertex_kd.balance()
    default_skin = (0, mathutils.Vector.Fill(4))
    vertex_group_by_position = {}
    seen_data = {}
    remap = [0] * len(data.vert_array)
    kept = []
    for orig_idx, v in enumerate(data.vert_array):
        for _, index, _ in vertex_kd.find_range(v, position_threshold):
            if index == orig_idx:
                continue
            if index in vertex_group_by_position:
                vertex_group_key = vertex_group_by_position[index]
                break
        else:
            vertex_group_key = vertex_group_by_position[orig_idx] = orig_idx
        seen_vertex_data = seen_data.setdefault(vertex_group_key, [])
        normal = vertex_normals[orig_idx]
        bone_ids, bone_weights = data.skin_vert_array[orig_idx] if data.skin_vert_array is not None else default_skin
        merged_idx = None
        for idx, other_normal, other_bone_ids, other_bone_weights in seen_vertex_data:
            if (
                (other_normal - normal).length < normal_threshold
                and bone_ids == other_bone_ids
                and (bone_weights - other_bone_weights).length < weight_threshold
            ):
                merged_idx = idx
                break
        if merged_idx is None:
            merged_idx = len(kept)
            seen_vertex_data.append((merged_idx, normal, bone_ids, bone_weights))
            kept.append(orig_idx)
        remap[orig_idx] = merged_idx

    face_array = []
    kept_faces = []
    seen_faces = set()
    for face_idx, face in enumerate(data.face_array):
        new_face = [remap[i] for i in face]
        if not (new_face[0] != new_face[1] != new_face[2] != new_face[0]):
            continue
        face_key = tuple(sorted(new_face))
        if face_key in seen_faces:
            continue
        seen_faces.add(face_key)
        face_array.append(new_face)
        kept_faces.append(face_idx)

    merged = MeshData(
        vert_array=[data.vert_array[i] for i in kept],
        face_array=face_array,
        skin_vert_array=None if data.skin_vert_array is None else [data.skin_vert_array[i] for i in kept],
        matid_array=None if data.matid_array is None else [data.matid_array[i] for i in kept_faces],
        color_array=None if data.color_array is None else [data.color_array[i] for i in kept],
    )
    corners = build_corners(
        MeshData(data.vert_array, [data.face_array[i] for i in kept_faces]),
        vertex_normals, vertex_uvs)
    merged.split_normals = corners.split_normals
    merged.corner_uv_arrays = corners.corner_uv_arrays
    return merged


def create_mesh(
        data: MeshData,
        materials: list,
        name: str,
        *,
        armature_obj: bpy.types.Object | None = None,
        rigid_bone: str | None = None,
        messages: list | None = None,
        uv_names: list | None = None,
    ) -> bpy.types.Object:
    """Create a mesh object from a MeshData with corner data filled by merge or build_corners.

    skin_vert_array entries are (bone names, weights); the caller links the object into its collections."""
    new_mesh = bpy.data.meshes.new(name)
    new_mesh.from_pydata(data.vert_array, [], data.face_array, shade_flat=False)
    if data.split_normals is not None:
        new_mesh.normals_split_custom_set(data.split_normals)
    for material in materials:
        new_mesh.materials.append(material)
    if data.matid_array is not None:
        new_mesh.polygons.foreach_set('material_index', data.matid_array)
    for layer_idx, corner_uvs in enumerate(data.corner_uv_arrays):
        layer_name = uv_names[layer_idx] if uv_names is not None and layer_idx < len(uv_names) else 'UVMap'
        uv_layer = new_mesh.uv_layers.new(name=layer_name)
        uv_layer.data.foreach_set('uv', corner_uvs)
    if data.color_array is not None:
        color_attr = new_mesh.color_attributes.new('Color', 'BYTE_COLOR', 'POINT')
        color_attr.data.foreach_set('color', [c for v in data.color_array for c in v])
    if new_mesh.validate(verbose=True) and messages is not None:
        messages.append(('WARNING', f'Mesh {name} has some errors'))

    obj = bpy.data.objects.new(name, new_mesh)
    if armature_obj is None:
        return obj
    obj.parent = armature_obj
    if data.skin_vert_array is not None:
        vertex_groups = {}
        for vert_idx, (bone_names, bone_weights) in enumerate(data.skin_vert_array):
            for bone_name, bone_weight in zip(bone_names, bone_weights):
                if bone_name is None or bone_weight == 0:
                    continue
                vertex_group = vertex_groups.get(bone_name)
                if vertex_group is None:
                    vertex_group = vertex_groups[bone_name] = obj.vertex_groups.new(name=bone_name)
                vertex_group.add([vert_idx], bone_weight, 'REPLACE')
    elif rigid_bone is not None:
        group = obj.vertex_groups.new(name=rigid_bone)
        group.add(range(len(data.vert_array)), 1.0, 'REPLACE')
    armature_mod = obj.modifiers.new('Skeleton', 'ARMATURE')
    armature_mod.object = armature_obj
    return obj
