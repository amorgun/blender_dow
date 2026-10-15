import io
import pathlib
import struct
import typing

from ..chunky import ChunkReader
from ..dow_layout import DowLayout


def path_race(blueprint_path: str) -> str | None:
    """The race segment of a blueprint path, e.g. 'race_marine'"""
    return next((part for part in pathlib.PureWindowsPath(blueprint_path).parts if part.startswith('race_')), None)


def art_category(art_path: str) -> str | None:
    """The wargear category segment of an art path, e.g. 'troops_wargear'"""
    return next((part for part in pathlib.PureWindowsPath(art_path).parts if part.endswith('_wargear')), None)


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


def parse_fieldnames(reader: ChunkReader) -> list[str]:
    count = reader.read_one('<L')
    reader.skip(count * 4)  # name offsets, but the names are contiguous anyway
    return [str(s, 'ascii', errors='replace') for s in reader.stream.read().split(b'\0')[:count]]


def parse_rb2(reader: ChunkReader) -> dict[str, bytes]:
    magic, count = reader.read_struct('<2L')
    assert magic == 0x12345678, hex(magic)
    paths = [reader.read_str(encoding='ascii') for _ in range(count)]
    blobs = []
    for _ in range(count):
        size = reader.read_one('<L')
        blobs.append(reader.stream.read(size))
    return dict(zip(paths, blobs))


def decode_rbf(reader: ChunkReader, keys: list[str]) -> dict:
    assert reader.stream.read(8) == b'RBF V0.1'
    (elements_offset, elements_count, refs_offset, refs_count,
     props_offset, props_count, data_offset, data_size) = reader.read_struct('<8L')
    reader.stream.seek(elements_offset)
    elements = [reader.read_struct('<2L') for _ in range(elements_count)]
    reader.stream.seek(props_offset)
    props = [reader.read_struct('<2L') for _ in range(props_count)]

    # element property spans tile the props table in order of their stored starts
    starts = {}
    pos = 0
    for i in sorted(range(elements_count), key=lambda i: (elements[i][1], i)):
        starts[i] = pos
        pos += elements[i][0]

    def read_data_str(off):
        reader.stream.seek(data_offset + off)
        return reader.read_str(encoding='ascii', errors='replace')

    def element(idx, path=()):
        if idx in path:  # the data contains recursive action references
            return None
        count, start = elements[idx][0], starts[idx]
        out = {}
        for desc, value in props[start:start+count]:
            key = keys[desc >> 16]
            match desc & 0xffff:
                case 0: v = bool(value)
                case 1: v = struct.unpack('<f', struct.pack('<L', value))[0]
                case 2: v = struct.unpack('<l', struct.pack('<L', value))[0]
                case 3: v = read_data_str(value)
                case 4: v = element(value, path + (idx,))
                case unknown_type: v = (unknown_type, value)
            index = 1
            while Key(key, index) in out:
                index += 1
            out[Key(key, index)] = v
        return out

    return element(0)


def tree_attachments(tree: dict) -> list[str]:
    blueprint = tree.get('entity_blueprint_ext', {})
    attachments = collect_attachments({k: v for k, v in blueprint.items() if 'attachment' in k.value})
    chapter_attachments = {art for arts in tree_chapter_attachments(tree).values() for art in arts}
    attachments += [a for a in collect_attachments(tree.get('sight_ext', {})) if a not in attachments and a not in chapter_attachments]
    return attachments


def tree_wargear_refs(tree: dict) -> list[str]:
    """Default-loadout wargear blueprint paths referenced by a unit blueprint"""
    refs = []

    def walk(node):
        for key, value in node.items():
            if isinstance(value, dict):
                walk(value)
            elif key.value == 'wargear':
                if value and value not in refs:
                    refs.append(value)

    walk(tree)
    return refs


def tree_attachment_levels(tree: dict) -> dict[str, int]:
    """Attachment art paths of a unit blueprint with their unlock ranks, including rank-gated ones"""
    result = {}

    def walk(node, level):
        for key, value in node.items():
            if isinstance(value, dict):
                if key.value.startswith('chapter_animator_attachment'):
                    continue
                walk(value, max(level, int(value.get('minimum_level', 0))))
            elif key.value in ('attachment', 'animator_attachment') and value:
                result.setdefault(value, level)

    blueprint = tree.get('entity_blueprint_ext', {})
    walk({k: v for k, v in blueprint.items() if 'attachment' in k.value}, 0)
    walk(tree.get('sight_ext', {}), 0)
    return result


def tree_chapter_attachments(tree: dict) -> dict[str, list[str]]:
    """Chapter (DLC skin) attachment art paths of a unit blueprint, by chapter key"""
    result = {}

    def walk(node):
        for key, value in node.items():
            if not isinstance(value, dict):
                continue
            chapter = value.get('chapter_key_name')
            if chapter:
                attachments = result.setdefault(chapter, [])
                attachments += [
                    a for a in collect_attachments(value, include_chapter_skins=True, include_level_gated=True)
                    if a not in attachments
                ]
            else:
                walk(value)

    walk(tree.get('entity_blueprint_ext', {}))
    if len(result) == 1:
        # single-chapter units keep the chapter's models as plain sight_ext attachments
        chapter, attachments = next(iter(result.items()))
        attachments += [
            a for a in collect_attachments(tree.get('sight_ext', {}), include_level_gated=True)
            if a not in attachments
        ]
    return result


def collect_attachments(node: dict, include_chapter_skins: bool = False, include_level_gated: bool = False) -> list[str]:
    result = []

    def walk(node):
        for key, value in node.items():
            if isinstance(value, dict):
                if key.value.startswith('chapter_animator_attachment') and not include_chapter_skins:
                    continue
                if not include_level_gated and int(value.get('minimum_level', 0)) > 1:
                    continue
                walk(value)
            elif key.value in ('attachment', 'animator_attachment'):
                if value not in result:
                    result.append(value)

    walk(node)
    return result


class AttribDb:
    def __init__(self, keys: list[str], blobs: dict[str, bytes]):
        self.keys = keys
        self.blobs = blobs
        self._units = None
        self._unit_squad_tags = None
        self._unit_squads = None
        self._wargear_by_tag = None
        self._untagged_wargear = None

    @classmethod
    def load(cls, layout: DowLayout) -> 'AttribDb | None':
        fieldnames = layout.find('simulation/attrib/fieldnames.flb')
        megabinary = layout.find('simulation/attrib/attribmegabinary.rb2')
        if fieldnames is None or megabinary is None:
            return None
        keys = parse_fieldnames(ChunkReader(io.BytesIO(fieldnames.read_bytes())))
        blobs = parse_rb2(ChunkReader(io.BytesIO(megabinary.read_bytes())))
        return cls(keys, blobs)

    def decode(self, path: str) -> dict:
        return decode_rbf(ChunkReader(io.BytesIO(self.blobs[path])), self.keys)

    def units(self) -> dict[str, tuple[str, int]]:
        """All blueprints that animate a model, as blueprint path -> (animator path, attachment count)"""
        if self._units is None:
            self._units = {}
            for path in self.blobs:
                tree = self.decode(path)
                animator = tree.get('entity_blueprint_ext', {}).get('animator')
                if animator:
                    self._units[path] = (animator, len(tree_attachments(tree)) + len(tree_wargear_refs(tree)))
        return self._units

    def unit_attachments(self, path: str) -> list[str]:
        """Default attachment model paths of a unit blueprint"""
        return tree_attachments(self.decode(path))

    def unit_attachment_levels(self, path: str) -> dict[str, int]:
        """All attachment model paths of a unit blueprint with their unlock ranks"""
        return tree_attachment_levels(self.decode(path))

    def unit_chapter_attachments(self, path: str) -> dict[str, list[str]]:
        """Chapter (DLC skin) attachment model paths of a unit blueprint, by chapter key"""
        return tree_chapter_attachments(self.decode(path))

    def wargear_chapter_models(self, wargear_path: str) -> dict[str, list[str]]:
        """Chapter (DLC) render model art paths of a wargear blueprint, by chapter key"""
        result = {}

        def walk(node):
            for key, value in node.items():
                if not isinstance(value, dict):
                    continue
                chapter = value.get('chapter_key_name')
                if chapter:
                    models = result.setdefault(chapter, [])
                    models += [v for k, v in value.items() if k.value == 'render_model' and v and v not in models]
                else:
                    walk(value)

        walk(self.decode(wargear_path).get('wargear', {}))
        return result

    def unit_squad_tags(self, path: str) -> set[str]:
        """Squad types a unit blueprint belongs to, e.g. {'farseer'}, lowercased"""
        self._scan_squads()
        return self._unit_squad_tags.get(path, set())

    def unit_squads(self, path: str) -> list:
        """Squad blueprints whose loadout contains the unit"""
        self._scan_squads()
        return self._unit_squads.get(path, [])

    def _scan_squads(self):
        if self._unit_squad_tags is not None:
            return
        self._unit_squad_tags = {}
        self._unit_squads = {}
        for sbps_path in self.blobs:
            if not sbps_path.startswith('sbps'):
                continue
            tree = self.decode(sbps_path)
            # squad_loadout_ext.squad_type is unreliable: unset squads carry a scout_marine default
            selection_group = tree.get('squad_ui_ext', {}).get('selection_group')
            tags = {selection_group.lower()} if selection_group else set()

            def register(node):
                for key, value in node.items():
                    if isinstance(value, dict):
                        register(value)
                    elif key.value == 'type':
                        self._unit_squads.setdefault(value, []).append(sbps_path)
                        self._unit_squad_tags.setdefault(value, set()).update(tags)

            register(tree.get('squad_loadout_ext', {}).get('unit_list', {}))

    def wargear_by_tag(self) -> dict[str, list[str]]:
        """Wargear blueprint paths grouped by the squad types that can equip them"""
        if self._wargear_by_tag is None:
            self._wargear_by_tag = {}
            for path in self.blobs:
                if not path.startswith('wargear'):
                    continue
                requirements = self.decode(path).get('wargear', {}).get('requirements', {})
                type_list = requirements.get('required_squad_type', {}).get('type_list', {})
                for key, tag in type_list.items():
                    if key.value == 'squad_type':
                        self._wargear_by_tag.setdefault(tag.lower(), []).append(path)
        return self._wargear_by_tag

    def untagged_wargear(self) -> dict[tuple[str, str], list[str]]:
        """Wargear with render models but no squad-type requirement, reachable only through
        game code (campaign loot pools, Last Stand loadouts), by (race, art category)"""
        if self._untagged_wargear is None:
            self._untagged_wargear = {}
            for path in self.blobs:
                if not path.startswith('wargear'):
                    continue
                wargear = self.decode(path).get('wargear', {})
                type_list = wargear.get('requirements', {}).get('required_squad_type', {}).get('type_list', {})
                if any(key.value == 'squad_type' for key in type_list):
                    continue
                race = path_race(path)
                if race is None:
                    continue
                for model in self.wargear_models(path):
                    category = art_category(model)
                    if category is not None:
                        entries = self._untagged_wargear.setdefault((race, category), [])
                        if path not in entries:
                            entries.append(path)
        return self._untagged_wargear

    def wargear_models(self, wargear_path: str) -> list[str]:
        """Render model art paths of a wargear blueprint"""
        models = []

        def walk(node):
            for key, value in node.items():
                if key.value in ('chapter_render_models', 'online_level_render_models'):
                    continue
                if isinstance(value, dict):
                    walk(value)
                elif key.value == 'render_model':
                    if value not in models:
                        models.append(value)

        walk(self.decode(wargear_path).get('wargear', {}))
        return models

    def unit_wargear_refs(self, path: str, equippable: bool = False, same_race_only: bool = True) -> list[str]:
        """Wargear blueprint paths of a unit: its default loadout, optionally everything it can equip"""
        refs = tree_wargear_refs(self.decode(path))
        for sbps_path in self.unit_squads(path):
            refs += [ref for ref in tree_wargear_refs(self.decode(sbps_path)) if ref not in refs]
        if equippable:
            for sbps_path in self.unit_squads(path):
                upgrades = []

                def collect(node):
                    for key, value in node.items():
                        if isinstance(value, dict):
                            collect(value)
                        elif key.value == 'upgrade' and value:
                            upgrades.append(value)

                collect(self.decode(sbps_path))
                for upgrade in upgrades:
                    refs += [ref for ref in tree_wargear_refs(self.decode(upgrade)) if ref not in refs]
            for tag in sorted(self.unit_squad_tags(path)):
                refs += [w for w in self.wargear_by_tag().get(tag, []) if w not in refs]
            race = path_race(path)
            animator = self.decode(path).get('entity_blueprint_ext', {}).get('animator', '')
            parts = pathlib.PureWindowsPath(animator).parts
            category = f'{parts[1]}_wargear' if len(parts) > 1 else None
            if category is not None:
                for (entry_race, entry_category), entries in self.untagged_wargear().items():
                    if entry_category != category:
                        continue
                    if same_race_only and entry_race != race:
                        continue
                    refs += [w for w in entries if w not in refs]
        return refs

    def unit_wargear(self, path: str, equippable: bool = False) -> list[str]:
        """Wargear render model art paths of a unit: its default loadout, optionally everything it can equip"""
        models = []
        for wargear_path in self.unit_wargear_refs(path, equippable):
            models += [m for m in self.wargear_models(wargear_path) if m not in models]
        return models
