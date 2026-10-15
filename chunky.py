import contextlib
import dataclasses
import enum
import io
import os
import struct
import typing


@enum.unique
class ChunkyVersion(enum.IntEnum):
    """Relic Chunky container version. V1 = Dawn of War, V3 = Dawn of War II, V4 = Dawn of War III."""
    V1 = 1
    V3 = 3
    V4 = 4


# per version: the relic chunky file-header size, and the chunk-header struct format
# (V3 chunk headers carry two extra u32 fields after the name length; V4 matches V1)
_FILE_HEADER_SIZE = {ChunkyVersion.V1: 24, ChunkyVersion.V3: 36, ChunkyVersion.V4: 24}
_CHUNK_HEADER_FMT = {ChunkyVersion.V1: '<8slll', ChunkyVersion.V3: '<8slllll', ChunkyVersion.V4: '<8slll'}


def walk_chunks(data: bytes, pos: int = 24, end: int = None, depth: int = 0) -> typing.Iterator[tuple]:
    """Flat walk over a V1/V4-layout chunky buffer, yielding (depth, typeid, version, size, name, payload_pos)."""
    if end is None:
        end = len(data)
    while pos + 20 <= end:
        typeid, version, size, name_length = struct.unpack_from('<8s3L', data, pos)
        typeid = typeid.decode('ascii', errors='replace')
        pos += 20
        name = data[pos:pos + name_length].split(b'\0')[0].decode('utf8', errors='replace')
        pos += name_length
        yield depth, typeid, version, size, name, pos
        if typeid.startswith('FOLD'):
            yield from walk_chunks(data, pos, pos + size, depth + 1)
        pos += size


@dataclasses.dataclass
class ChunkHeader:  # -- Structure Holding Chunk Header Data
    typeid: str = None
    version: int = None
    size: int = None
    name_length: int = None
    name_bytes: bytes = None

    @property
    def name(self):
        return str(self.name_bytes.rstrip(b'\0'), 'utf8', errors='ignore')


class ChunkReader:
    def __init__(self, stream, version: ChunkyVersion = ChunkyVersion.V1):
        self.stream = stream
        self.version = version

    def read_header(self, expected_typeid: str = None) -> ChunkHeader:
        fields = self.read_struct(_CHUNK_HEADER_FMT[self.version])
        if fields is None:
            return None
        typeid, version, size, name_length = fields[:4]
        name = self.stream.read(name_length)
        typeid = str(typeid, 'ascii')
        if expected_typeid:
            assert typeid == expected_typeid, f'Expected {expected_typeid}, got {typeid}'
        return ChunkHeader(typeid, version, size, name_length, name)

    def read_struct(self, fmt: str) -> tuple | None:
        size = struct.calcsize(fmt)
        buf = self.stream.read(size)
        if len(buf) < size:
            return None
        return struct.unpack(fmt, buf)
    
    def read_one(self, fmt: str) -> typing.Any:
        fields = self.read_struct(fmt)
        if fields is None:
            return None
        assert len(fields) == 1, 'Need to parse exactly 1 value'
        return fields[0]
    
    def read_str(self, encoding='utf8', errors: str = 'ignore'):
        str_len = self.read_one('<l')
        if str_len == 0:
            return ''
        return str(self.read_one(f'<{str_len}s'), encoding, errors=errors)
    
    def skip(self, nbytes: int) -> None:
        self.stream.seek(nbytes, os.SEEK_CUR)

    def skip_relic_chunky(self) -> None:
        return self.skip(_FILE_HEADER_SIZE[self.version])

    def read_folder(self, header: ChunkHeader) -> 'ChunkReader':
        data = self.stream.read(header.size)
        return ChunkReader(io.BytesIO(data), self.version)

    def iter_chunks(self) -> typing.Iterator[ChunkHeader]:
        while (current_chunk := self.read_header()):
            yield current_chunk

    def index_chunks(self) -> dict[str, list[tuple[ChunkHeader, int]]]:
        """Scan the remaining chunks of this folder into {typeid: [(header, payload
        offset)]}, so callers can dispatch by type with seeks instead of relying on
        sibling order — the Dawn of War chunk trees don't guarantee one."""
        positions = {}
        for chunk in self.iter_chunks():
            positions.setdefault(chunk.typeid, []).append((chunk, self.stream.tell()))
            self.skip(chunk.size)
        return positions

    def seek_chunk(self, chunk_position: tuple[ChunkHeader, int]) -> ChunkHeader:
        """Position the stream at an indexed chunk's payload and return its header"""
        chunk, position = chunk_position
        self.stream.seek(position, os.SEEK_SET)
        return chunk


class ChunkWriter:
    def __init__(self, stream, chunk_versions: dict, version: ChunkyVersion = ChunkyVersion.V1):
        self.stream = stream
        self.curr_data_size = 0
        self.curr_typeid = None
        self.chunk_versions = chunk_versions
        self.version = version

    def write_relic_chunky(self):
        magic = b'Relic Chunky\x0d\x0a\x1a\x00'
        if self.version == ChunkyVersion.V3:
            self.stream.write(struct.pack('<16s5L', magic, 3, 1, 36, 28, 1))
        else:
            self.stream.write(struct.pack('<16s2L', magic, int(self.version), 1))

    @contextlib.contextmanager
    def start_chunk(
        self,
        typeid: str,
        name: str = '',
        version: int = None,
    ):
        assert len(typeid) == 8, f'Incorrect typeid {repr(typeid)}'
        assert typeid[:4] in ('FOLD', 'DATA'), f'Incorrect typeid {repr(typeid)}'
        assert self.curr_typeid is None or self.curr_typeid[:4] == 'FOLD', f'Chunk of type {self.curr_typeid} cannot have children'
        parent_data_size = self.curr_data_size
        parent_typeid = self.curr_typeid
        self.curr_typeid = typeid
        self.curr_data_size = 0
        typeid_bytes = bytes(typeid, 'ascii')
        name_bytes = bytes(name, 'utf8')
        if name and not name_bytes.endswith(b'\0'):
            name_bytes += b'\0'
        prev_chunk_version = self.chunk_versions
        assert typeid in self.chunk_versions, typeid
        self.chunk_versions = self.chunk_versions[typeid]
        if version is None:
            version = self.chunk_versions['version']
        if self.version == ChunkyVersion.V3:
            header_fmt = f'<8slllll{len(name_bytes)}s'
            tail_fmt = f'<llll{len(name_bytes)}s'
            self.stream.write(struct.pack(header_fmt, typeid_bytes, version, 0, len(name_bytes), 0, 0, name_bytes))
        else:
            header_fmt = f'<8slll{len(name_bytes)}s'
            tail_fmt = f'<ll{len(name_bytes)}s'
            self.stream.write(struct.pack(header_fmt, typeid_bytes, version, 0, len(name_bytes), name_bytes))
        current_pos = self.stream.tell()
        yield self
        self.stream.seek(current_pos - struct.calcsize(tail_fmt), os.SEEK_SET)
        self.stream.write(struct.pack('<l', self.curr_data_size))
        self.curr_data_size = parent_data_size + struct.calcsize(header_fmt) + self.curr_data_size
        self.curr_typeid = parent_typeid
        self.chunk_versions = prev_chunk_version
        self.stream.seek(0, os.SEEK_END)

    def write(self, data: bytes, safe: bool = False):
        if safe:
            assert self.curr_typeid is None or self.curr_typeid[:4] == 'DATA', f'Cannot write bytes to {self.curr_typeid}'
        self.curr_data_size += len(data)
        return self.stream.write(data)

    def write_struct(self, fmt: str, *args):
        self.write(struct.pack(fmt, *args))
    
    def write_str(self, s: str, encoding: str = 'utf8'):
        assert s is not None
        data = bytes(s, encoding)
        self.write_struct('<l', len(data))
        self.write(data)
