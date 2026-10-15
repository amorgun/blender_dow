"""Dawn of War III .rgd attribute blueprints (Relic Chunky V4).

Besides the unit blueprints (ebps/sbps), the paint system lives here: a
paint_scheme names a paint per slot (paint_primary/secondary/trim), and each
paint under attrib/paint/<finish>/ carries its diffuse colour as a packed
signed ARGB int.

An .rgd holds two chunks: DATAAEGD v2 with the attribute tree, and DATAKEYS v1 with a
per-file dictionary of the CityHash64 key hashes the tree uses, so every key resolves
to its source name.

DATAAEGD payload = u32 unk, u32 data_size, then a TABLE entry. TABLE (type 100) =
u32 count, count x {u64 key_hash, u32 type, u32 offset}, then the entry data (offsets
relative to the end of the entry table). LIST (type 101) has the identical byte layout
but every element shares one key hash, so it decodes to a Python list (a unit's
additional_animator_blueprints holds body + weapons + head as same-keyed elements).
Scalar types: 0 float, 1 int, 2 bool, 3 string, 4 wstring.
"""
import dataclasses
import enum
import pathlib
import struct
import typing

from .. import chunky
from ..dow_layout import DowLayout, LayoutPath


class Key(typing.NamedTuple):
    """Blueprint field name plus an index keeping repeated fields distinct.

    Hashes and equates to the bare field name, so plain string lookups
    like node.get('animator') resolve to the field's first occurrence.
    """
    value: str
    index: int = 1

    def __hash__(self):
        return hash(self.value)

    def __eq__(self, other):
        if isinstance(other, Key):
            return self.value == other.value and self.index == other.index
        return self.value == other


@enum.unique
class EntryType(enum.IntEnum):
    """DATAAEGD attribute tree entry types"""
    FLOAT = 0
    INT = 1
    BOOL = 2
    STRING = 3
    WSTRING = 4
    TABLE = 100
    LIST = 101


def read_datakeys(data: bytes) -> dict[int, str]:
    names = {}
    for depth, typeid, version, size, name, payload_pos in chunky.walk_chunks(data):
        if typeid != 'DATAKEYS':
            continue
        count = struct.unpack_from('<L', data, payload_pos)[0]
        pos = payload_pos + 4
        for _ in range(count):
            key_hash, length = struct.unpack_from('<QL', data, pos)
            names[key_hash] = data[pos + 12:pos + 12 + length].decode('ascii', errors='replace')
            pos += 12 + length
    return names


def parse_rgd(data: bytes) -> dict:
    names = read_datakeys(data)
    for depth, typeid, version, size, name, payload_pos in chunky.walk_chunks(data):
        if typeid == 'DATAAEGD':
            unk, data_size = struct.unpack_from('<2L', data, payload_pos)
            return read_entry(data[payload_pos:payload_pos + size], 8, EntryType.TABLE, data_size, names)
    raise ValueError('No DATAAEGD chunk found')


def read_entry(data: bytes, pos: int, type_: int, size: int, names: dict):
    match type_:
        case EntryType.TABLE | EntryType.LIST:
            count = struct.unpack_from('<L', data, pos)[0]
            entries = [struct.unpack_from('<QLL', data, pos + 4 + i * 16) for i in range(count)]
            base = pos + 4 + count * 16
            table_size = size - 4 - count * 16
            ordered = sorted(entries, key=lambda e: e[2])
            children = []
            for index, (key, entry_type, offset) in enumerate(ordered):
                end = ordered[index + 1][2] if index + 1 < count else table_size
                children.append((key, read_entry(data, base + offset, entry_type, end - offset, names)))
            if type_ == EntryType.LIST:
                return [value for _, value in children]
            out = {}
            for key, value in children:
                name = names.get(key, f'{key:016x}')
                index = 1
                while Key(name, index) in out:
                    index += 1
                out[Key(name, index)] = value
            return out
        case EntryType.STRING:
            return data[pos:pos + size].split(b'\0')[0].decode('ascii', errors='replace')
        case EntryType.WSTRING:
            return data[pos:pos + size].decode('utf-16-le', errors='replace').rstrip('\0')
        case EntryType.FLOAT:
            return struct.unpack_from('<f', data, pos)[0]
        case EntryType.INT:
            return struct.unpack_from('<l', data, pos)[0]
        case EntryType.BOOL:
            return bool(data[pos])
        case unknown:
            return ('?', unknown, data[pos:pos + min(size, 16)].hex())


INVISIBLE_ANIMATOR = 'invisible_animator'

PAINT_FINISHES = ('paint_gloss', 'paint_matte', 'metal_brushed', 'metal_shiny', 'exotic')

# the campaign faction scheme per ebps race; other races use the skirmish player-one default
DEFAULT_PAINT_SCHEMES = {
    'space_marines': 'space_marines/blood_ravens',
    'eldar': 'eldar/biel_tan',
    'orks': 'orks/gorgutz',
    'imperial_guard_campaign': 'astra_militarum/imperial_guard',
    'house_varlock_campaign': 'imperial_knights/house_varlock',
}
FALLBACK_PAINT_SCHEME = 'defaults/team_local'


def unit_race(blueprint_path: str) -> str | None:
    """The race segment of an ebps blueprint path, e.g. 'space_marines'"""
    parts = pathlib.PurePosixPath(blueprint_path).parts
    return parts[parts.index('races') + 1] if 'races' in parts else None


@dataclasses.dataclass
class PartEntry:
    """One additional_animator_blueprints element: the default part art path plus its skin variants."""
    art_path: str
    skins: dict[str, str]


class AttribDb:
    def __init__(self, layout: DowLayout, blueprint_paths: list[str]):
        self.layout = layout
        self.blueprint_paths = blueprint_paths
        self._trees = {}

    @classmethod
    def load(cls, layout: DowLayout) -> 'AttribDb | None':
        paths = []

        def scan(dir_path):
            for child in layout.iterdir(dir_path):
                child_path = f'{dir_path}/{child.name}'
                if child.is_dir():
                    scan(child_path)
                elif child.name.endswith('.rgd'):
                    paths.append(child_path)

        for top in ('attrib/ebps', 'attrib/sbps'):
            if layout.find(top) is not None:
                scan(top)
        if not paths:
            return None
        return cls(layout, sorted(paths))

    def decode(self, path: str) -> dict:
        tree = self._trees.get(path)
        if tree is None:
            tree = self._trees[path] = parse_rgd(self.layout.find(path).read_bytes())
        return tree

    def unit_animator(self, path: str) -> str | None:
        """Archetype art path of a unit blueprint (skeleton + animations, no mesh)"""
        return self.decode(path).get('entity_blueprint_ext', {}).get('animator') or None

    def unit_parts(self, path: str) -> list[PartEntry]:
        """Mesh part art paths of a unit blueprint, with their skin-variant overrides"""
        blueprint = self.decode(path).get('entity_blueprint_ext', {})
        parts = []
        for element in blueprint.get('additional_animator_blueprints', []):
            if not isinstance(element, dict):
                continue
            overrides = element.get('animator_overrides', {})
            if isinstance(overrides, dict):
                overrides = list(overrides.values())
            skins = {}
            for override in overrides:
                if isinstance(override, dict) and override.get('skin') and override.get('animator'):
                    skins[override['skin']] = override['animator']
            art_path = element.get('animator', '')
            if not art_path or (INVISIBLE_ANIMATOR in art_path and not skins):
                continue
            parts.append(PartEntry(art_path, skins))
        return parts

    def paint_info(self, paint_name: str) -> dict | None:
        """A paint's diffuse/specular sRGB colours and gloss level"""
        for finish in PAINT_FINISHES:
            path = self.layout.find(f'attrib/paint/{finish}/{paint_name}.rgd')
            if path is not None:
                bag = parse_rgd(path.read_bytes()).get('paint_bag', {})

                def rgb(key):
                    argb = bag.get(key, 0) & 0xffffffff
                    return tuple((argb >> shift & 0xff) / 255 for shift in (16, 8, 0))

                return {'diffuse': rgb('diffuse'), 'specular': rgb('specular'), 'gloss': (bag.get('gloss', 0) & 0xff) / 255}
        return None

    def paint_color(self, paint_name: str) -> tuple | None:
        """A paint's diffuse colour as sRGB floats"""
        info = self.paint_info(paint_name)
        return info['diffuse'] if info is not None else None

    def default_paint_colors(self, race: str | None) -> dict:
        """The primary/secondary/trim sRGB colours of a race's default paint scheme"""
        return self.paint_scheme_info(race)['colors']

    def default_paint_scheme(self, race: str | None) -> str:
        return DEFAULT_PAINT_SCHEMES.get(race, FALLBACK_PAINT_SCHEME)

    def paint_schemes(self, race: str | None) -> list[str]:
        """Scheme names selectable for a race: its own schemes first (default at the
        top), then the game defaults"""
        default = self.default_paint_scheme(race)
        race_dir = pathlib.PurePosixPath(default).parts[0]
        names = []
        for dir_name in dict.fromkeys((race_dir, 'defaults')):
            base = f'attrib/paint_scheme/{dir_name}'
            if self.layout.find(base) is None:
                continue
            names.extend(sorted(
                f'{dir_name}/{child.name[:-len(".rgd")]}'
                for child in self.layout.iterdir(base)
                if child.name.endswith('.rgd')
            ))
        names.sort(key=lambda name: name != default)
        return names

    def paint_scheme_info(self, race: str | None, scheme: str | None = None) -> dict:
        """A paint scheme's layer colours and badge: {'colors', 'badge', 'badge_stencil',
        'badge_colors'}. scheme None resolves the race default."""
        if scheme is None:
            scheme = self.default_paint_scheme(race)
        result = {'colors': {}, 'colors_spec': {}, 'gloss': {}, 'badge': None, 'badge_stencil': None, 'badge_colors': {}}
        path = self.layout.find(f'attrib/paint_scheme/{scheme}.rgd')
        if path is None:
            return result
        bag = parse_rgd(path.read_bytes()).get('paint_scheme_bag', {})
        for slot, layer in (('paint_primary', 'primary'), ('paint_secondary', 'secondary'),
                            ('paint_trim', 'trim'), ('paint_tertiary', 'tertiary')):
            paint = self.paint_info(bag.get(slot, ''))
            if paint is not None:
                result['colors'][layer] = paint['diffuse']
                result['colors_spec'][layer] = paint['specular']
                result['gloss'][layer] = paint['gloss']
        badge = bag.get('badge_primary') or None
        if badge and (badge_info := self.badge_info(badge)) is not None:
            result['badge'] = badge
            result['badge_stencil'] = badge_info['stencil']
            result['badge_colors'] = badge_info['colors']
        return result

    def badges(self) -> list[str]:
        """All badge blueprint names"""
        base = 'attrib/badges'
        if self.layout.find(base) is None:
            return []
        return sorted(
            child.name[:-len('.rgd')]
            for child in self.layout.iterdir(base)
            if child.name.endswith('.rgd')
        )

    def badge_info(self, badge_name: str) -> dict | None:
        """A badge's stencil texture art path and its own paint colours"""
        path = self.layout.find(f'attrib/badges/{badge_name}.rgd')
        if path is None:
            return None
        bag = parse_rgd(path.read_bytes()).get('badge_bag', {})
        colors = {}
        for slot, layer in (('paint_primary', 'primary'), ('paint_secondary', 'secondary'), ('paint_trim', 'trim')):
            color = self.paint_color(bag.get(slot, ''))
            if color is not None:
                colors[layer] = color
        return {'stencil': bag.get('stencil_texture') or None, 'colors': colors}

    def unit_focus_perspective(self, unit_path: str) -> dict | None:
        """The armoury inspection camera for a unit: the army_unit blueprint names a
        camera_perspective/army_unit_focus preset the UI orbits around the unit.
        Returns {'name', 'length', 'theta', 'camera_height', 'lookat_height', 'roll'}."""
        if not hasattr(self, '_focus_perspectives'):
            index = {}
            squads = {}

            def scan(dir_path, store):
                if self.layout.find(dir_path) is None:
                    return
                for child in self.layout.iterdir(dir_path):
                    full = f'{dir_path}/{child.name}'
                    if child.is_dir():
                        scan(full, store)
                    elif child.name.endswith('.rgd'):
                        store(full)

            def store_squad(path):
                squads[pathlib.PurePosixPath(path).stem] = path

            scan('attrib/sbps', store_squad)

            def store_army_unit(path):
                bag = parse_rgd(self.layout.find(path).read_bytes()).get('army_unit_bag', {})
                focus = bag.get('forge_info', {}).get('perspective_focus')
                squad = squads.get(bag.get('sbp', ''))
                if not focus or squad is None:
                    return
                loadout = self.decode(squad).get('squad_loadout_ext', {}).get('unit_list', [])
                for entry in loadout:
                    if isinstance(entry, dict) and entry.get('type'):
                        index[entry['type']] = focus

            scan('attrib/army_unit', store_army_unit)
            self._focus_perspectives = index
        focus = self._focus_perspectives.get(pathlib.PurePosixPath(unit_path).stem)
        if focus is None:
            return None
        path = self.layout.find(f'attrib/camera_perspective/army_unit_focus/{focus}.rgd')
        if path is None:
            return None
        bag = parse_rgd(path.read_bytes()).get('perspective_bag', {})
        location = bag.get('location', {})
        lookat = bag.get('lookat_location', {})
        return {
            'name': focus,
            'length': location.get('offset_length', 10.0),
            'theta': location.get('offset_theta_radians', 0.0),
            'camera_height': location.get('offset_height', 0.0),
            'lookat_height': lookat.get('offset_height', 0.0),
            'roll': lookat.get('roll_radians', 0.0),
        }

    def unit_decal(self, unit_path: str) -> str | None:
        """The unit's squad decal source texture art path (unit_badge texture override)"""
        name = self.decode(unit_path).get('entity_blueprint_ext', {}).get('unit_badge')
        race = unit_race(unit_path)
        if not name or not race:
            return None
        override = self.layout.find(f'attrib/texture_override/{race}/{name}.rgd')
        if override is None:
            return None
        return parse_rgd(override.read_bytes()).get('texture_override_bag', {}).get('source_texture') or None
