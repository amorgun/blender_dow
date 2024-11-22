import collections
import contextlib
import dataclasses
import pathlib
import struct
import typing
import zlib


def read_struct(fmt: str, stream) -> tuple:
    size = struct.calcsize(fmt)
    buf = stream.read(size)
    if len(buf) < size:
        return None
    return struct.unpack(fmt, buf)


def read_one(fmt: str, stream) -> typing.Any:
    fields = read_struct(fmt, stream)
    if fields is None:
        return None
    assert len(fields) == 1, 'Need to parse exactly 1 value'
    return fields[0]


@dataclasses.dataclass
class Header2:
    table_of_content_offset: int
    data_offset: int

    @classmethod
    def parse(cls, stream) -> 'Header2':
        checksum1, name, checksum2, table_of_content_size, data_offset = read_struct('<16s128s16sLL', stream)
        assert data_offset == table_of_content_size + 180, f'{data_offset=} {table_of_content_size=}'
        return cls(180, data_offset)


@dataclasses.dataclass
class Toc25:
    """Table of content
    """
    virtual_drive_offset: int
    virtual_drive_count: int
    folder_offset: int
    folder_count: int
    file_offset: int
    file_count: int
    name_buffer_offset: int
    name_buffer_count: int

    @classmethod
    def parse(cls, stream) -> 'Toc25':
        data = read_struct('<IHIHIHIH', stream)
        return cls(*data)


@dataclasses.dataclass
class VirtualDrive25:
    path: str
    name: str
    first_folder: int
    last_folder: int
    first_file: int
    last_file: int

    @classmethod
    def parse(cls, stream) -> 'VirtualDrive25':
        path, name, first_folder, last_folder, first_file, last_file, unk = read_struct('<64s64sHHHH2s', stream)
        return cls(str(path.rstrip(b'\0'), 'ascii'), str(name.rstrip(b'\0'), 'ascii'),
                   first_folder, last_folder, first_file, last_file)


@dataclasses.dataclass
class FolderHeader25:
    name_offset: int
    sub_folder_start_index: int
    sub_folder_end_index: int
    file_start_index: int
    file_end_index: int

    @classmethod
    def parse(cls, stream) -> 'FolderHeader25':
        data = read_struct('<L4H', stream)
        return cls(*data)


@dataclasses.dataclass
class FileHeader2:
    name_offset: int
    compression_flag: int
    data_offset: int
    compressed_size: int
    decompressed_size: int

    @classmethod
    def parse(cls, stream) -> 'FileHeader2':
        data = read_struct('<5L', stream)
        return cls(*data)


@dataclasses.dataclass
class IndexFolder(collections.abc.MutableMapping):
    name: str
    children: dict[str, 'IndexItem'] = dataclasses.field(default_factory=dict)
    parent: 'IndexFolder | None' = dataclasses.field(default=None, repr=False)

    def __getitem__(self, key: str) -> 'IndexItem':
        return self.children[key]
    
    def __setitem__(self, key: str, value: 'IndexItem'):
        self.children[key] = value
        value.parent = self

    def __delitem__(self, key: str):
        child = self.children.get(key)
        if child is not None:
            child.parent = None
        del self.children[key]

    def __iter__(self) -> typing.Iterator[str]:
        return iter(self.children)
    
    def __len__(self) -> int:
        return len(self.children)

    @staticmethod
    def is_file():
        return False
    
    @staticmethod
    def is_dir():
        return True
    
    def iterdir(self) -> typing.Iterator['IndexItem']:
        yield from self.children.values()


@dataclasses.dataclass
class IndexFile:
    name: str
    data_offset: int
    compressed_size: int
    compression_flag: int
    data_size: int
    parent: 'IndexFolder | None' = dataclasses.field(default=None, repr=False)

    @staticmethod
    def is_file():
        return True
    
    @staticmethod
    def is_dir():
        return False


IndexItem = IndexFolder | IndexFile


# See https://github.com/ModernMAK/Relic-Game-Tool/wiki/SGA-Archive
class SgaArchive:
    def __init__(self, index: dict, path: pathlib.Path):
        self.index = index
        self.path = path
        self.stream = None

    @classmethod
    def parse(cls, sga_path: str | pathlib.PurePath) -> 'SgaArchive':
        with open(sga_path, 'rb') as f:
            magic, version_major, version_minor = read_struct('<8s2H', f)
            assert magic == b'_ARCHIVE'
            header_cls = {
                (2, 0): Header2
            }[version_major, version_minor]
            header = header_cls.parse(f)
            
            toc_cls = {
                (2, 0): Toc25,
                (5, 0): Toc25,
            }[version_major, version_minor]
            f.seek(header.table_of_content_offset)
            toc = toc_cls.parse(f)

            virtual_drive_cls = {
                (2, 0): VirtualDrive25,
                (5, 0): VirtualDrive25,
            }[version_major, version_minor]
            f.seek(header.table_of_content_offset + toc.virtual_drive_offset)
            drives = [virtual_drive_cls.parse(f) for _ in range(toc.virtual_drive_count)]

            folder_cls = {
                (2, 0): FolderHeader25,
                (5, 0): FolderHeader25,
            }[version_major, version_minor]
            f.seek(header.table_of_content_offset + toc.folder_offset)
            folders = [folder_cls.parse(f) for _ in range(toc.folder_count)]

            file_cls = {
                (2, 0): FileHeader2,
            }[version_major, version_minor]
            f.seek(header.table_of_content_offset + toc.file_offset)
            files = [file_cls.parse(f) for _ in range(toc.file_count)]

            max_name_offset = max(i.name_offset for s in [folders, files] for i in s)
            f.seek(header.table_of_content_offset + toc.name_buffer_offset + max_name_offset + 1)
            last_name_size = 1
            while True:
                last_name_size += 1
                if f.read(1) in (b'\0', b''):
                    break
            f.seek(header.table_of_content_offset + toc.name_buffer_offset)
            name_buffer = f.read(max_name_offset + last_name_size)
            name_buffer_splits = [idx for idx, c in enumerate(name_buffer) if c == 0]

            def find_name(start: int):
                l, r = 0, len(name_buffer_splits)
                if name_buffer_splits[l] == start:
                    return ''
                while l + 1 != r:
                    m = (l + r) // 2
                    if name_buffer_splits[m] <= start:
                        l = m
                    else:
                        r = m
                return str(name_buffer[start: name_buffer_splits[r]], 'utf8')

            index = {}
            folder_infos = [IndexFolder(name=find_name(f.name_offset)) for f in folders]
            file_infos = [IndexFile(
                    name=find_name(f.name_offset),
                    data_offset=header.data_offset + f.data_offset,
                    compressed_size=f.compressed_size,
                    compression_flag=f.compression_flag,
                    data_size=f.decompressed_size,
                ) for f in files]
            for drive in drives:
                drive_index = index.setdefault(drive.name, {})
                drive_folders = folders[drive.first_folder:drive.last_folder]

                def create_path(path: str) -> pathlib.PurePosixPath:
                    return pathlib.PurePosixPath(pathlib.PureWindowsPath(path.lower()).as_posix())

                folder_paths = [create_path(i.name) for i in folder_infos]
                for folder, info, path in zip(drive_folders, folder_infos, folder_paths):
                    for idx in range(folder.sub_folder_start_index, folder.sub_folder_end_index):
                        child_info = folder_infos[idx]
                        child_path = folder_paths[idx]
                        child_info.name = str(child_path.relative_to(path))
                        info[child_info.name] = child_info
                    for idx in range(folder.file_start_index, folder.file_end_index):
                        child_info = file_infos[idx]
                        info[child_info.name] = child_info
                for info in folder_infos:
                    if info.parent is None:
                        drive_index[info.name] = info

            return cls(index, pathlib.Path(sga_path))

    @contextlib.contextmanager
    def open(self):
        if self.stream is not None:
            yield self
            return

        with self.path.open('rb') as f:
            self.stream = f
            try:
                yield self
            finally:
                self.stream = None

    def resolve_path(self, path: str | pathlib.PurePath) -> IndexItem | None:
        path_str = str(path).lower()
        vdrive = None
        if ':' in path_str:
            vdrive, path_str = path_str.split(':', 1)
        path = pathlib.PureWindowsPath(path_str)
        normalized_path = pathlib.PurePosixPath(path.as_posix())
        parts = normalized_path.parts
        if parts and parts[0] == '/':
            parts = parts[1:]
        for drive, index in self.index.items():
            index = index['']
            for part in parts:
                index = index.get(part)
                if index is None:
                    break
            else:
                return index
        return None

    def read_file(self, file: IndexFile, retries: int = 3) -> bytes:
        with self.open():
            for retry in range(retries):
                try:
                    self.stream.seek(file.data_offset)
                    data = self.stream.read(file.compressed_size)
                    if file.compression_flag != 0:
                        data = zlib.decompress(data)
                    return data
                except zlib.error:
                    if retry + 1 == retries:
                        raise

    def read_bytes(self, path: str | pathlib.PurePath) -> bytes:
        file = self.resolve_path(path)
        if file is None:
            raise FileNotFoundError(path)
        if file.is_dir():
            raise IsADirectoryError(path)
        return self.read_file(file)
    
    def make_path(self, path: str | pathlib.PurePath = '') -> 'SgaPath':
        return SgaPath(self, path)


@dataclasses.dataclass(repr=False)
class SgaPath:
    SENTINEL = object()

    sga: SgaArchive = dataclasses.field(repr=False)
    impl: pathlib.PurePath
    _cached_item: IndexItem = dataclasses.field(default=SENTINEL, init=False)

    def __post_init__(self):
        self.impl = pathlib.PurePath(self.impl)

    @property
    def _sga_item(self):
        if self._cached_item is SgaPath.SENTINEL:
            self._cached_item = self.sga.resolve_path(self.impl)
        return self._cached_item

    def read_bytes(self) -> bytes:
        item = self._sga_item
        if item is None:
            raise FileNotFoundError(self.impl)
        if item.is_dir():
            raise IsADirectoryError(self.impl)
        return self.sga.read_file(item)
    
    def read_text(self, encoding: str = 'utf-8', errors: str = 'strict') -> str:
        data = self.read_bytes()
        return str(data, encoding=encoding, errors=errors)

    def exists(self) -> None:
        return self._sga_item is not None

    def is_file(self) -> bool:
        item = self._sga_item
        return item is not None and item.is_file()

    def is_dir(self) -> bool:
        item = self._sga_item
        return item is not None and item.is_dir()
    
    def iterdir(self) -> 'typing.Iterator[SgaPath]':
        item = self._sga_item
        if item is None:
            raise FileNotFoundError(self.impl)
        if not item.is_dir():
            raise NotADirectoryError(self.impl)
        for key, child_data in item.children.items():
            child = SgaPath(self.sga, self.impl / key)
            child._cached_item = child_data
            yield child

    def __str__(self) -> str:
        return str(self.impl)

    def __repr__(self):
        return f'''SgaPath("{self.impl}", "{self.sga.path}")'''

    def __hash__(self):
        return hash(self.impl)
    
    def __lt__(self, other: 'SgaPath') -> bool:
        return self.impl < other.impl

    def __truediv__(self, other) -> 'SgaPath':
        return SgaPath(self.sga, self.impl / other)

    def __rtruediv__(self, other) -> 'SgaPath':
        return SgaPath(self.sga, other / self.impl)
    
    @property
    def name(self) -> str:
        return self.impl.name

    @property
    def suffix(self) -> str:
        return self.impl.suffix
    
    @property
    def stem(self) -> str:
        return self.impl.stem

    @property
    def data_size(self):
        return self._sga_item.data_size

    def layout_path(self)-> pathlib.PurePosixPath:
        return self.impl