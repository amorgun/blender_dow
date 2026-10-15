import abc
import configparser
import contextlib
import dataclasses
import enum
import pathlib
import typing

from .sga import SgaArchive, SgaPath


@enum.unique
class TextureLevel(str, enum.Enum):
    HIGH = 'Full'


@enum.unique
class SoundLevel(str, enum.Enum):
    HIGH = 'Full'
    MEDIUM = 'Med'
    LOW = 'Low'


@enum.unique
class ModelLevel(str, enum.Enum):
    HIGH = 'High'
    MEDIUM = 'Medium'
    LOW = 'Low'


T = typing.TypeVar('T')


class AbstractSource(abc.ABC):
    @abc.abstractmethod
    def make_path(self, path: str | pathlib.PurePath) -> 'LayoutPath':
        raise NotImplementedError

    @abc.abstractmethod
    def exists(self) -> bool:
        raise NotImplementedError
    

@dataclasses.dataclass
class DirectoryPath:
    full_path: pathlib.Path
    root: pathlib.PurePosixPath

    def __getattr__(self, key):
        return getattr(self.full_path, key)

    def __truediv__(self, other) -> 'DirectoryPath':
        return DirectoryPath(self.full_path / other, self.root)

    def __rtruediv__(self, other) -> 'DirectoryPath':
        return DirectoryPath(other / self.full_path, self.root)

    def iterdir(self) -> 'typing.Generator[DirectoryPath, None, None]':
        for c in self.full_path.iterdir():
            yield DirectoryPath(c, self.root)

    def __str__(self) -> str:
        return str(self.layout_path())

    @property
    def data_size(self):
        return self.full_path.stat().st_size

    def layout_path(self) -> pathlib.PurePosixPath:
        return self.full_path.relative_to(self.root)

    def __getstate__(self):
        return vars(self)

    def __setstate__(self, state):
        vars(self).update(state)

LayoutPath = SgaPath | DirectoryPath


class DirectorySource(AbstractSource):
    def __init__(self, root: str | pathlib.Path, name: str):
        self.root = pathlib.Path(root)
        self.name = name

    def make_path(self, path: str | pathlib.PurePath) -> DirectoryPath:
        path = pathlib.PurePath(path)
        if path.is_absolute():
            path = path.relative_to('/')
        return DirectoryPath(self.root / path, self.root)

    def exists(self) -> bool:
        return self.root.exists()

    @contextlib.contextmanager
    def open(self):
        yield self

    def __repr__(self) -> str:
        return f'DirectorySource({self.root})'


class SgaSource(AbstractSource):
    def __init__(self, path: str | pathlib.Path, name: str):
        self.path = path
        self.name = name
        self._archive = None

    @property
    def archive(self):
        if self._archive is None and self.path.exists():
            self._archive = SgaArchive.parse(self.path)
        return self._archive

    def make_path(self, path: str | pathlib.PurePath) -> SgaPath:
        return self.archive.make_path(path)

    def exists(self) -> bool:
        return self.path.exists()

    @contextlib.contextmanager
    def open(self):
        archive = self.archive
        if archive is not None:
            with archive.open():
                yield self
                return
        yield self

    def __repr__(self) -> str:
        return f'SgaSource({self.path})'


def iter_path_candidates(part: str) -> typing.Generator[str, None, None]:
    yield part
    yield part.lower()
    yield part.upper()
    yield part.title()


def try_find_path(root: pathlib.Path, *parts: str) -> pathlib.Path:
    curr = root
    iter_parts = iter(parts)
    if not curr.is_dir():
        for p in iter_parts:
            curr /= p
        return curr
    for part_idx, part in enumerate(iter_parts):
        if part_idx == 0 and part.startswith('data:'):
            part = part[len('data:'):]
        for part_case in iter_path_candidates(part):
            if (candidate := curr / part_case).exists():
                curr = candidate
                break
        else:
            if curr.is_dir():
                for c in curr.iterdir():
                    if c.name.lower() == part.lower():
                        curr = c
                        break
                else:
                    curr /= part
                    for p in iter_parts:
                        curr /= p
                    return curr
    return curr


@dataclasses.dataclass
class DowLayout:
    default_lang: str = 'english'
    default_texture_level: TextureLevel = TextureLevel.HIGH
    default_sound_level: SoundLevel = SoundLevel.HIGH
    default_model_level: ModelLevel = ModelLevel.HIGH
    sources: list[AbstractSource] = dataclasses.field(default_factory=list)

    @classmethod
    def from_mod_folder(cls, path: str | pathlib.Path, include_movies: bool = True, include_locale: bool = True) -> 'DowLayout':
        path = pathlib.Path(path)
        dow_folder = path.parent
        res = cls._initilize_defaults(dow_folder)
        mod_configs = cls.load_mod_configs_options(dow_folder)
        required_mods = [mod_configs.get(path.name.lower(), cls._make_default_mod_config(path.name))]
        for required_mod_name in required_mods[0].get('requiredmods', ['dxp2', 'w40k']) + ['engine']:
            required_mods.append(mod_configs.get(required_mod_name.lower(), cls._make_default_mod_config(required_mod_name)))
        for mod in required_mods:
            res.sources.append(DirectorySource(try_find_path(dow_folder, mod['modfolder'], 'Data'), name=mod['modfolder']))
            for folder in mod.get('datafolders', []):
                folder = res.interpolate_path(folder)
                res.sources.append(SgaSource(try_find_path(dow_folder, mod['modfolder'], f'{folder}.sga'), name=mod['modfolder']))
            for file in mod.get('archivefiles', []):
                file = res.interpolate_path(file)
                res.sources.append(SgaSource(try_find_path(dow_folder, mod['modfolder'], f'{file}.sga'), name=mod['modfolder']))
        for mod in required_mods:
            if include_movies:
                res.sources.append(DirectorySource(try_find_path(dow_folder, mod['modfolder'], 'Movies'), name=mod['modfolder']))
            if include_locale:
                res.sources.append(DirectorySource(try_find_path(dow_folder, mod['modfolder'], res.interpolate_path('%LOCALE%')), name=mod['modfolder']))
        return res

    @classmethod
    def from_dow2_module(cls, path: str | pathlib.Path = None, loose_folder: str | pathlib.Path = None, lang: str = None) -> 'DowLayout':
        res = cls()
        if loose_folder is not None:
            res.sources.append(DirectorySource(loose_folder, name='loose'))
        if path is None:
            return res
        path = pathlib.Path(path)
        root = path.parent
        config = configparser.ConfigParser(interpolation=None, comment_prefixes=('#', ';', '--'), strict=False)
        config.read(path)
        lang = (lang or res.default_lang).lower()
        for section in config.sections():
            if ':' not in section:
                continue
            variant = section.split(':', 1)[1].lower()
            if variant not in ('common', lang):
                continue
            for key, value in config[section].items():
                source_path = try_find_path(root, *pathlib.PureWindowsPath(value).parts)
                if key.startswith('folder.'):
                    res.sources.append(DirectorySource(source_path, name=section))
                elif key.startswith('archive.'):
                    res.sources.append(SgaSource(source_path, name=section))
        return res

    @classmethod
    def from_dow3_folder(cls, path: str | pathlib.Path = None, loose_folder: str | pathlib.Path = None, model_level: ModelLevel = None) -> 'DowLayout':
        res = cls()
        if loose_folder is not None:
            res.sources.append(DirectorySource(loose_folder, name='loose'))
        if path is None:
            return res
        module = cls.resolve_dow3_module(path)
        if module is not None:
            return cls.from_dow3_module(module, loose_folder=loose_folder, model_level=model_level)
        archives = cls.resolve_dow3_archives_folder(path)
        model_level = model_level or res.default_model_level
        detail_archive = 'artlow.sga' if model_level == ModelLevel.LOW else 'arthigh.sga'
        mount_order = [detail_archive, 'art.sga']
        mount_order += sorted(
            p.name for p in archives.iterdir()
            if p.name.startswith('art') and p.suffix.lower() == '.sga'
            and not p.name.startswith(('artenvironment', 'artscenarios'))
            and p.name not in ('arthigh.sga', 'artlow.sga', 'art.sga')
        )
        mount_order.append('attrib.sga')
        for name in mount_order:
            res.sources.append(SgaSource(archives / name, name=name))
        return res

    @classmethod
    def from_dow3_module(cls, path: str | pathlib.Path, loose_folder: str | pathlib.Path = None, model_level: ModelLevel = None) -> 'DowLayout':
        """Mount the [data] and [attrib] archives of a Dawn of War III module descriptor
        (share/data/relicgame.module; archiveRoot paths are relative to the module's folder)"""
        res = cls()
        if loose_folder is not None:
            res.sources.append(DirectorySource(loose_folder, name='loose'))
        path = pathlib.Path(path)
        root = path.parent
        config = configparser.ConfigParser(interpolation=None, comment_prefixes=('#', ';'), strict=False)
        config.read(path)
        model_level = model_level or res.default_model_level
        detail_variant = 'art_low' if model_level == ModelLevel.LOW else 'art_high'

        def section_archives(section):
            archive_root = config[section].get('archiveroot')
            if archive_root is None:
                return
            names = [value for key, value in sorted(config[section].items()) if key.startswith('archive.')]
            for name in names:
                source_path = try_find_path(root, *pathlib.PureWindowsPath(archive_root).parts, f'{name}.sga')
                res.sources.append(SgaSource(source_path, name=section))

        def matching_sections(kind, variant):
            # the game's own archives (numbered subsections) mount before the engine's (base section)
            base = f'{kind}:{variant}'
            sections = [s for s in config.sections() if s == base or s.startswith(f'{base}:')]
            return sorted(sections, key=lambda s: (s == base, s))

        for section in matching_sections('data', detail_variant):
            section_archives(section)
        for section in matching_sections('data', 'common'):
            section_archives(section)
        for section in matching_sections('attrib', 'common'):
            section_archives(section)
        return res

    @classmethod
    def resolve_dow3_module(cls, path: str | pathlib.Path) -> pathlib.Path | None:
        """The module descriptor for a selection: the .module file itself, or one found
        under a game/data folder; None when only a bare archives folder was selected"""
        path = pathlib.Path(path)
        if path.is_file():
            return path if path.suffix.lower() == '.module' else None
        for candidate in (path, try_find_path(path, 'share', 'data')):
            if candidate.is_dir():
                for entry in candidate.iterdir():
                    if entry.suffix.lower() == '.module' and entry.is_file():
                        return entry
        return None

    @classmethod
    def resolve_dow3_archives_folder(cls, path: str | pathlib.Path) -> pathlib.Path:
        path = pathlib.Path(path)
        if path.is_file():
            path = path.parent
        if (path / 'arthigh.sga').is_file():
            return path
        for parts in (('share', 'data', 'dow3', 'archives'), ('dow3', 'archives')):
            candidate = try_find_path(path, *parts)
            if (candidate / 'arthigh.sga').is_file():
                return candidate
        raise FileNotFoundError(f'No Dawn of War III archives found under {path}')

    @classmethod
    def _initilize_defaults(cls, root: pathlib.Path) -> 'DowLayout':
        lang_config = cls.load_lang(root)
        game_config = cls.load_game_options(root)
        res = cls()
        res.default_lang = lang_config.get('default', res.default_lang)
        res.default_texture_level = game_config.get('texture_level', res.default_texture_level)
        res.default_sound_level = game_config.get('texture_level', res.default_sound_level)
        res.default_model_level = game_config.get('model_level', res.default_model_level)
        return res

    @classmethod
    def _make_default_mod_config(cls, folder_name: str) -> dict:
        return {
            'modfolder': folder_name,
            'datafolders': ['Data']
        }

    @classmethod
    def load_lang(cls, path: pathlib.Path) -> dict:
        conf_path = path / 'regions.ini'
        if not conf_path.is_file():
            return {}
        try:
            config = configparser.ConfigParser()
            config.read(conf_path)
            return {
                **{k.lower(): v for k, v in config['mods'].items()},
                'default': config['global']['lang'],
            }
        except Exception:
            return {}

    @classmethod
    def load_game_options(cls, path: pathlib.Path) -> dict:
        return {}  # TODO

    @classmethod
    def load_mod_configs_options(cls, path: pathlib.Path) -> dict:
        result = {}
        for file in path.iterdir():
            if file.suffix.lower() != '.module' or not file.is_file():
                continue
            config = configparser.ConfigParser(interpolation=None, comment_prefixes=('#', ';', '--'))
            try:
                config.read(file)
                config = config['global']
                result[file.stem.lower()] = {
                    **{k: config[k] for k in ('uiname', 'description', 'modfolder')},
                    **{
                        f'{key}s': [
                            i for _, i in sorted([(k, v)
                                for k, v in config.items()
                                if k.startswith(f'{key}.')
                            ], key=lambda x: int(x[0].rsplit('.')[1]))
                        ]
                        for key in ('datafolder', 'archivefile', 'requiredmod')
                    },
                }
            except Exception as e:
                print(f'Config pasing error: {e}')
        if 'engine' not in result:
            result['engine'] = {
                'modfolder': 'engine',
                'archivefiles': ['%LOCALE%\\EnginLoc', 'Engine', 'Engine-New'],
            }
        return result

    def interpolate_path(
            self,
            path: str,
            lang: str = None,
            texture_level: TextureLevel = None,
            sound_level: SoundLevel = None,
            model_level: ModelLevel = None,
        ) -> str:
        path = path.replace('%LOCALE%', 'Locale/' + (lang or self.default_lang).title())
        path = path.replace('%TEXTURE-LEVEL%', texture_level or self.default_texture_level)
        path = path.replace('%SOUND-LEVEL%', sound_level or self.default_sound_level)
        path = path.replace('%MODEL-LEVEL%', model_level or self.default_model_level)
        return pathlib.PureWindowsPath(path).as_posix()

    def iter_paths(self, path: str | pathlib.PurePath, return_missing: bool = False) -> typing.Generator[LayoutPath, None, None]:
        path = pathlib.PurePath(path)
        for source in self.sources:
            if not source.exists():
                continue
            source_path = try_find_path(source.make_path('.'), *path.parts)
            if return_missing or source_path.exists():
                yield source_path

    def find(self, path: str | pathlib.PurePath, default: T = None) -> LayoutPath | T:
        for p in self.iter_paths(path):
            return p
        return default

    def iterdir(self, path: str | pathlib.PurePath) -> typing.Generator[LayoutPath, None, None]:
        seen_files = set()
        for source in self.sources:
            if not source.exists():
                continue
            source_path = source.make_path(path)
            if source_path.exists():
                for i in source_path.iterdir():
                    if i.name.lower() not in seen_files:
                        seen_files.add(i.name.lower())
                        yield i

    @contextlib.contextmanager
    def open(self):
        with contextlib.ExitStack() as stack:
            for source in self.sources:
                if source.exists():
                    stack.enter_context(source.open())
            yield self
