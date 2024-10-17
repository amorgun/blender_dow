import contextlib
import dataclasses
import io
import os
import struct
import typing


@dataclasses.dataclass
class ChunkHeader:  # -- Structure Holding Chunk Header Data
    typeid: str = None
    version: int = None
    size: int = None
    name_length: int = None
    name_bytes: bytes = None

    @property
    def name(self):
        return str(self.name_bytes.rstrip(b'\0'), 'utf8')


class ChunkReader:
    def __init__(self, stream):
        self.stream = stream
        
    def read_header(self, expected_typeid: str = None) -> ChunkHeader:
        fields = self.read_struct('<8slll')
        if fields is None:
            return None
        typeid, version, size, name_length = fields
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
    
    def read_str(self, encoding='utf8'):
        str_len = self.read_one('<l')
        if str_len == 0:
            return ''
        return str(self.read_one(f'<{str_len}s'), encoding)
    
    def skip(self, nbytes: int) -> None:
        self.stream.seek(nbytes, os.SEEK_CUR)

    def skip_relic_chunky(self) -> None:
        return self.skip(24)
    
    def read_folder(self, header: ChunkHeader) -> 'ChunkReader':
        data = self.stream.read(header.size)
        return ChunkReader(io.BytesIO(data))
    
    def iter_chunks(self) -> typing.Iterator[ChunkHeader]:
        while (current_chunk := self.read_header()):
            yield current_chunk


class ChunkWriter:
    def __init__(self, stream, chunk_versions: dict):
        self.stream = stream
        self.curr_data_size = 0
        self.curr_typeid = None
        self.chunk_versions = chunk_versions

    @contextlib.contextmanager
    def start_chunk(
        self,
        typeid: str,
        name: str = '',
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
        header_fmt = f'<8slll{len(name_bytes)}s'
        prev_chunk_version = self.chunk_versions
        assert typeid in self.chunk_versions, typeid
        self.chunk_versions = self.chunk_versions[typeid]
        version = self.chunk_versions['version']
        self.stream.write(struct.pack(header_fmt, typeid_bytes, version, 0, len(name_bytes), name_bytes))
        current_pos = self.stream.tell()
        yield self
        self.stream.seek(current_pos - struct.calcsize(f'<ll{len(name_bytes)}s'), os.SEEK_SET)
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
