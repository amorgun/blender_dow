import functools

import bpy 

from . import utils


SEP = '__'

ARGS = {
    (bpy.types.Material, 'full_path'): {
        'default': '',
        'description': 'Path to where the game will look for this material. Useful for reusing the same material between multiple models',
    },
    (bpy.types.Material, 'internal'): {
        'default': False,
        'description': 'Do not export this material into a separate file and keep it inside the model file',
    },
    (bpy.types.PoseBone, 'stale'): {
        'default': False,
        'description': 'Apply it to each bone you want to disable in an animation.',
    },
    (bpy.types.Object, 'force_invisible'): {
        'default': False,
        'description': 'Force the mesh to be invisible in the current animation. Used for creating vis_ animations to conditionally display meshes, e.g. weapon upgrades and random heads',  # Usage: https://dawnofwar.org.ru/publ/27-1-0-177
    },
    (bpy.types.Object, 'visibility'): {
        'default': 1.0,
        'min': 0,
        'max': 1,
        'description': 'Used for changing the mesh visibility during an animation',
    },
    (bpy.types.Object, 'xref_source'): {
        'default': '',
        'description': "If set don't export the mesh data and instead reference it from the specified model",
    },
    (bpy.types.Object, 'uv_offset'): {
        'default': [0., 0.],
        'description': 'Used for animating moving textures, e.g. tank tracks and chainsword teeth',
    },
    (bpy.types.Object, 'uv_tiling'): {
        'default': [1., 1.],
        'description': "Used for animating UV tiling of the material. I haven't seen any examples of it in the real models",
    },
    (bpy.types.Action, 'fps'): {
        'default': 30.,
        'description': "FPS used when this action is exported",
    }
}

REMOTE_PROPS = {
    'MESH': ['force_invisible', 'visibility'],
    'MATERIAL': ['uv_offset', 'uv_tiling'],
}


def create_prop_name(prefix: str, hashable: str, max_len: int = 63) -> str:
    hashable = hashable.lower()
    res = f'{prefix}{SEP}{hashable}'
    if len(res) <= max_len:
        return res
    return f'''{prefix}{SEP}{utils.get_hash(hashable)}'''


def get_prop_prefix(prop_name: str) -> str:
    return prop_name.split(SEP, 1)[0]


def setup_property(obj, prop_name: str, value=None, **kwargs):
    if value is None and obj.get(prop_name):
        return
    prop_prefix = get_prop_prefix(prop_name)
    args = ARGS[type(obj), prop_prefix]
    if value is None:
        value = args.get('default', value)
    obj[prop_name] = value
    id_props = obj.id_properties_ui(prop_name)
    for k, v in args.items():
        id_props.update(**{k: v})


def setup_drivers(obj, target_obj, prop_name: str):
    add_driver = functools.partial(utils.add_driver, target_id=target_obj)

    match get_prop_prefix(prop_name):
        case 'visibility':
            add_driver(obj=obj, obj_prop_path='color', target_data_path=f'["{prop_name}"]', fallback_value=1.0, index=3)
        case 'uv_offset':
            add_driver(obj=obj.node_tree, obj_prop_path='nodes["Mapping"].inputs[1].default_value', target_data_path=f'["{prop_name}"][0]', fallback_value=0, index=0)
            add_driver(obj=obj.node_tree, obj_prop_path='nodes["Mapping"].inputs[1].default_value', target_data_path=f'["{prop_name}"][1]', fallback_value=0, index=1)
        case 'uv_tiling':
            add_driver(obj=obj.node_tree, obj_prop_path='nodes["Mapping"].inputs[3].default_value', target_data_path=f'["{prop_name}"][0]', fallback_value=1, index=0)
            add_driver(obj=obj.node_tree, obj_prop_path='nodes["Mapping"].inputs[3].default_value', target_data_path=f'["{prop_name}"][1]', fallback_value=1, index=1)


def clear_drivers(obj, prop_name: str):
    driver_obj = obj
    driver_paths = set()
    match get_prop_prefix(prop_name):
        case 'visibility':
            driver_paths = {('color', 3)}
        case 'uv_offset':
            driver_obj = obj.node_tree
            driver_paths = {('nodes["Mapping"].inputs[1].default_value', i) for i in (0, 1)}
        case 'uv_tiling':
            driver_obj = obj.node_tree
            driver_paths = {('nodes["Mapping"].inputs[3].default_value', i) for i in (0, 1)}
    if driver_obj.animation_data is None:
        return
    for d in driver_obj.animation_data.drivers:
        if (d.data_path, d.array_index) in driver_paths:
            driver_obj.driver_remove(d.data_path, d.array_index)


def get_mesh_prop_owner(mesh_obj):
    if mesh_obj.parent is None or mesh_obj.parent.type != 'ARMATURE':
        return None
    return mesh_obj.parent


def get_material_prop_owner(mat):
    if mat.node_tree.animation_data is None:
        return None
    for driver in mat.node_tree.animation_data.drivers:
        if driver.data_path.startswith('nodes["Mapping"].inputs'):
            try:
                target = driver.driver.variables[0].targets[0]
                return target.id
            except Exception:
                continue
