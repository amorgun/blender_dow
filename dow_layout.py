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
    for part in parts:
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
                    for p in parts:
                        root /= p
                    return root
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
            if 'engine' not in result:
                result['engine'] = {
                    'modfolder': 'engine',
                    'archivefiles': ['%LOCALE%\EnginLoc', 'Engine', 'Engine-New'],
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
