import enum
import pathlib

from . import dow_layout

# pose-bone keys that belong to the addon tooling, not to marker params
INTERNAL_BONE_PROPS = ('dow_name', 'dow_stale', 'dow_last_action')


def serialize_matrix_4x3(matrix) -> list:
    """4x4 matrix -> the 12-float column-triplet layout of DATABONE/DATAMRKS records"""
    return [matrix[row][col] for col in range(4) for row in range(3)]


def marker_params(pose_bone) -> dict:
    return {key: pose_bone[key] for key in pose_bone.keys() if key not in INTERNAL_BONE_PROPS and not key.startswith('_')}


def weld_mesh(obj) -> tuple:
    """Corner-weld a mesh into per-vertex records for the game buffers.

    Returns (records, triangles): records are (position, normal, uvs, color,
    influences) in Blender frame with influences as ((bone name, weight), ...)
    sorted by weight and normalized to 4; triangles index into records."""
    mesh = obj.data
    mesh.calc_loop_triangles()
    uv_layers = [layer.data for layer in mesh.uv_layers]
    colors = mesh.color_attributes[0].data if mesh.color_attributes else None
    groups = obj.vertex_groups
    records = []
    record_index = {}
    triangles = []
    for tri in mesh.loop_triangles:
        corner_ids = []
        for loop_index in tri.loops:
            loop = mesh.loops[loop_index]
            vert = mesh.vertices[loop.vertex_index]
            position = tuple(vert.co)
            normal = tuple(loop.normal)
            uvs = tuple((layer[loop_index].uv[0], layer[loop_index].uv[1]) for layer in uv_layers)
            color = tuple(colors[loop.vertex_index].color_srgb) if colors is not None else None
            influences = []
            for group_element in vert.groups:
                if group_element.weight > 0:
                    influences.append((groups[group_element.group].name, group_element.weight))
            influences = sorted(influences, key=lambda i: -i[1])[:4]
            total = sum(w for _, w in influences) or 1.0
            influences = tuple((name, w / total) for name, w in influences)
            key = (position, normal, uvs, color, influences)
            index = record_index.get(key)
            if index is None:
                index = record_index[key] = len(records)
                records.append(key)
            corner_ids.append(index)
        triangles.append(tuple(corner_ids))
    return records, triangles


def find_root_collection(scene):
    """The imported model's root collection: the first scene child holding meshes"""
    for collection in scene.collection.children:
        if any(o.type == 'MESH' for o in collection.all_objects):
            return collection
    for collection in scene.collection.children:
        if collection.all_objects:
            return collection
    return scene.collection


def image_game_path(image) -> str:
    """The declared game path of a texture: the recorded export path, else its name"""
    path = (getattr(image, 'dow_export_path', '') or '').strip()
    if path:
        return path
    return pathlib.PurePath(image.name).stem


class FileDispatcher:
    @enum.unique
    class Layout(enum.Enum):
        FLAT = enum.auto()
        FLAT_FOLDERS = enum.auto()
        FULL_PATH = enum.auto()

    def __init__(self, root: str, layout: Layout, subfolder: str | None = None):
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
