import dataclasses
import enum
import io
import struct

from ..chunky import ChunkReader, ChunkyVersion

@enum.unique
class ParamType(enum.IntEnum):
    """Typed values in DATAMRKS marker params and DATACUAT model attributes"""
    INT = 0
    FLOAT = 1
    VECTOR = 4
    BOOL = 10
    STRING = 11


@enum.unique
class MaterialVarType(enum.IntEnum):
    """DATA\\x00VAR shader parameter types (shared with Dawn of War II)"""
    FLOAT = 1
    FLOAT_ARRAY = 5
    STRING = 9
    BOOL = 10


@dataclasses.dataclass
class Submesh:
    name: str
    triangles: list        # (a, b, c) local vertex indices
    positions: list        # (x, y, z) floats
    normals: list          # (x, y, z) floats (empty if absent)
    uv_sets: list          # uv-set lists of (u, v) floats, file v orientation
    colours: list          # (r, g, b, a) u8 (empty if absent)
    skin: list             # per vertex [(skin_bone_index, weight), ...]


@dataclasses.dataclass
class SkinBone:
    name: str
    bind: tuple            # 12 floats, 4x3 column triplets
    inverse_bind: tuple


@dataclasses.dataclass
class Geometry:
    name: str              # enclosing FOLDMRGM chunk name
    material: str
    submeshes: list
    skin_bones: list       # SkinBone; blend indices index into this


@dataclasses.dataclass
class Material:
    shader: str
    params: dict           # shader variable name -> str path / float / tuple / bool


@dataclasses.dataclass
class Bone:
    name: str
    parent: int            # index into the bone list, -1 = root
    matrix: tuple          # 12 floats, local 4x3


@dataclasses.dataclass
class Marker:
    name: str
    parent: str
    matrix: tuple          # 12 floats, local 4x3
    params: dict


@dataclasses.dataclass
class Camera:
    name: str
    position: tuple        # (x, y, z)
    rotation: tuple        # quaternion (x, y, z, w)
    fov: float             # degrees
    clip_start: float
    clip_end: float


@dataclasses.dataclass
class Model:
    geometries: list
    materials: dict        # material name -> Material
    skeleton: list         # Bone
    markers: list          # Marker
    attributes: dict       # DATACUAT model custom attributes
    cameras: list = dataclasses.field(default_factory=list)     # Camera
    partitions: list = dataclasses.field(default_factory=list)  # (name, first bone, bone count)


def parse_rgm(data: bytes) -> Model:
    reader = ChunkReader(io.BytesIO(data), ChunkyVersion.V4)
    reader.skip_relic_chunky()
    header = reader.read_header('FOLDMODL')
    model = Model([], {}, [], [], {})
    _read_model(reader.read_folder(header), model)
    return model


def _read_model(reader: ChunkReader, model: Model):
    # index-then-dispatch: the chunk tree guarantees no sibling order
    positions = reader.index_chunks()

    def chunks(typeid: str):
        for chunk_position in positions.get(typeid, ()):
            yield reader.seek_chunk(chunk_position)

    for chunk in chunks('FOLDSKEL'):
        _read_skeleton(reader.read_folder(chunk), model)
    for chunk in chunks('FOLDMTRL'):
        model.materials[chunk.name] = _read_material(reader.read_folder(chunk))
    for typeid in ('FOLDMESH', 'FOLDMGRP'):
        for chunk in chunks(typeid):
            _read_model(reader.read_folder(chunk), model)
    for chunk in chunks('FOLDMRGM'):
        _read_merged_mesh(reader.read_folder(chunk), chunk.name, model)
    for chunk in chunks('FOLDTRIM'):
        _read_trim_mesh(reader.read_folder(chunk), chunk.name, model)
    for chunk in chunks('DATAMRKS'):
        _read_markers(reader.read_folder(chunk), model)
    for chunk in chunks('DATACUAT'):
        sub = reader.read_folder(chunk)
        model.attributes.update(_read_typed_params(sub, sub.read_one('<L')))
    for chunk in chunks('DATACMRA'):
        values = reader.read_struct('<10f')
        model.cameras.append(Camera(chunk.name, values[0:3], values[3:7], values[7], values[8], values[9]))


def _read_typed_params(reader: ChunkReader, count: int) -> dict:
    params = {}
    for _ in range(count):
        key = reader.read_str()
        match ParamType(reader.read_one('<L')):
            case ParamType.INT:
                params[key] = reader.read_one('<l')
            case ParamType.FLOAT:
                params[key] = reader.read_one('<f')
            case ParamType.VECTOR:
                params[key] = reader.read_struct('<3f')
            case ParamType.BOOL:
                params[key] = bool(reader.read_one('<B'))
            case ParamType.STRING:
                params[key] = reader.read_str()
    return params


def _read_material(reader: ChunkReader) -> Material:
    material = Material('', {})
    for chunk in reader.iter_chunks():
        match chunk.typeid:
            case 'DATAINFO':
                material.shader = reader.read_str()
            case 'DATA\x00VAR':
                var_name = reader.read_str()
                var_type, var_size = reader.read_struct('<2L')
                payload = reader.stream.read(var_size)
                match var_type:
                    case MaterialVarType.FLOAT:
                        material.params[var_name] = struct.unpack('<f', payload[:4])[0]
                    case MaterialVarType.FLOAT_ARRAY:
                        material.params[var_name] = struct.unpack(f'<{var_size // 4}f', payload)
                    case MaterialVarType.STRING:
                        material.params[var_name] = str(payload.rstrip(b'\0'), 'utf8', errors='ignore')
                    case MaterialVarType.BOOL:
                        material.params[var_name] = bool(payload[0])
            case _:
                reader.skip(chunk.size)
    return material


def _read_merged_mesh(reader: ChunkReader, name: str, model: Model):
    # a group holds one full-detail DATADATA v8 plus progressive-reduction copies
    # (DATAMRFM siblings); the full geometry is the largest one
    positions = reader.index_chunks()
    candidates = [cp for cp in positions.get('DATADATA', ()) if cp[0].version == 8]
    if not candidates:
        return
    chunk = reader.seek_chunk(max(candidates, key=lambda cp: cp[0].size))
    model.geometries.append(_read_geometry(reader.read_folder(chunk), name))


def _read_trim_mesh(reader: ChunkReader, name: str, model: Model):
    """A single-material mesh (DATADATA v5): the merged-form vertex block with one
    plain index buffer; the chunk name is '<geo name>:<material>'."""
    positions = reader.index_chunks()
    candidates = [cp for cp in positions.get('DATADATA', ()) if cp[0].version == 5]
    if not candidates:
        return
    r = reader.read_folder(reader.seek_chunk(max(candidates, key=lambda cp: cp[0].size)))
    verts, normals, uv_lists, colours, skins = _read_vertex_block(r)
    r.skip(8)  # u32 0, u32 index count
    verts_per_face, index_count = r.read_struct('<2L')
    indices = r.read_struct(f'<{index_count}H')
    material = r.read_str()
    skin_bones = _read_skin_bones(r)
    triangles = [tuple(indices[i:i + verts_per_face]) for i in range(0, index_count, verts_per_face)]
    geo_name = name.split(':')[0]
    submesh = Submesh(geo_name, triangles, verts, normals, uv_lists, colours, skins)
    model.geometries.append(Geometry(geo_name, material, [submesh], skin_bones))


def _read_skin_bones(reader: ChunkReader) -> list:
    skin_bones = []
    for _ in range(reader.read_one('<L')):
        bind = reader.read_struct('<12f')
        inverse_bind = reader.read_struct('<12f')
        skin_bones.append(SkinBone(reader.read_str(), bind, inverse_bind))
    return skin_bones


def _read_geometry(reader: ChunkReader, name: str) -> Geometry:
    header_field = reader.read_one('<L')
    reader.skip(1)  # flag byte

    submesh_records = []
    for _ in range(header_field // 256):
        index_count = reader.read_one('<L')
        indices = reader.read_struct(f'<{index_count}H')
        reader.skip(13)  # aabb centre + flag byte
        submesh_records.append((indices, reader.read_str()))

    positions, normals, uv_lists, colours, skins = _read_vertex_block(reader)

    reader.skip(4)  # constant 3
    material = reader.read_str()
    skin_bones = _read_skin_bones(reader)

    def slc(lst, rng):
        return [lst[i] for i in rng] if lst else []

    submeshes = []
    for indices, submesh_name in submesh_records:
        triangles = [tuple(indices[i:i + 3]) for i in range(0, len(indices), 3)]
        lo = min(indices)
        rng = range(lo, max(indices) + 1)
        submeshes.append(Submesh(
            submesh_name,
            [(a - lo, b - lo, c - lo) for a, b, c in triangles],
            slc(positions, rng),
            slc(normals, rng),
            [slc(s, rng) for s in uv_lists],
            slc(colours, rng),
            [skins[i] for i in rng],
        ))

    return Geometry(name, material, submeshes, skin_bones)


def _read_vertex_block(reader: ChunkReader):
    element_count = reader.read_one('<L')
    rows = [reader.read_struct('<3L') for _ in range(element_count)]
    vertex_count, stride = reader.read_struct('<2L')
    attrs = []             # (usage, byte offset, byte size)
    offset = 0
    for usage, _type, components in rows:
        size = (components - 1) * 4
        attrs.append((usage, offset, size))
        offset += size
    if offset != stride:
        raise ValueError(f'element table {offset} bytes != stride {stride}')
    data = reader.stream.read(vertex_count * stride)

    def dqn(c):
        return c / 127.5 - 1.0

    positions, normals, colours, skins = [], [], [], []
    uv_usages = [u for u, _, _ in attrs if u in (8, 9, 10)]
    uv_sets_all = {u: [] for u in uv_usages}
    for v in range(vertex_count):
        vo = v * stride
        blend_indices = blend_weights = None
        for usage, ao, size in attrs:
            p = vo + ao
            if usage == 0:
                positions.append(struct.unpack_from('<3f', data, p))
            elif usage == 1:
                blend_indices = data[p:p + 4]
            elif usage == 2:
                blend_weights = data[p:p + 4]
            elif usage == 3:
                normals.append((dqn(data[p + 2]), dqn(data[p + 1]), dqn(data[p])))
            elif usage in (8, 9, 10):
                if size == 8:
                    uv_sets_all[usage].append(struct.unpack_from('<2f', data, p))
                else:
                    a, b = struct.unpack_from('<2H', data, p)
                    uv_sets_all[usage].append((a / 65535.0, b / 65535.0))
            elif usage == 6:
                colours.append(tuple(data[p:p + 4]))
        if blend_indices is not None and blend_weights is not None:
            paired = (blend_weights[2], blend_weights[1], blend_weights[0], blend_weights[3])
            skins.append([(blend_indices[j], paired[j] / 255.0) for j in range(4) if paired[j] > 0])
        else:
            skins.append([])

    uv_lists = [uv_sets_all[u] for u in uv_usages]
    return positions, normals, uv_lists, colours, skins


def _read_skeleton(reader: ChunkReader, model: Model):
    for chunk in reader.iter_chunks():
        if chunk.typeid == 'DATASPRT':
            first, count = reader.read_struct('<2H')
            model.partitions.append((chunk.name, first, count))
            continue
        if chunk.typeid != 'DATABONE':
            reader.skip(chunk.size)
            continue
        chunk_end = reader.stream.tell() + chunk.size
        parent, _ = reader.read_struct('<lL')
        matrix = reader.read_struct('<12f')
        model.skeleton.append(Bone(chunk.name, parent, matrix))
        reader.skip(chunk_end - reader.stream.tell())


def _read_markers(reader: ChunkReader, model: Model):
    for _ in range(reader.read_one('<L')):
        name = reader.read_str()
        parent = reader.read_str()
        matrix = reader.read_struct('<12f')
        params = _read_typed_params(reader, reader.read_one('<L'))
        model.markers.append(Marker(name, parent, matrix, params))
