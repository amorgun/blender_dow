"""Dawn of War III .rgt texture decoder (Pillow only, no bpy).

Container (Relic Chunky V4):
  FOLDTSET > DATADATA > FOLDTXTR "<path>" > FOLDDXTC > { DATATMAN, DATATDAT }

DATATMAN (u32 array): [1], [2] full width/height, [6] block-compression format enum:
  19 -> BC1 (DXT1)                       diffuse (_dif)
  22 -> BC3 in DXT5nm packing            normal (_nrm): X in alpha, Y in green, Z reconstructed,
                                         red = emissive mask
  23 -> BC3 (DXT5)                       specular (_spc)
  28 -> BC7                              army-painter pattern mask (_ptn)

DATATDAT: u32 tag, then ONE LOGICAL BYTE STREAM of mip records, each
{u32 level, u32 width, u32 height, u32 payload_size} + payload, smallest mip first down to
level 0 = full resolution. The logical stream is stored chunked into blocks of at most
64 KB, each independently either a raw byte run or a zlib stream (zlib iff it opens with
a zlib magic); chunk boundaries do not align with record boundaries. decode_full()
reassembles the stream and returns every mip.

The BC payloads are decoded by wrapping them in a DDS header (DX10 extension for BC7)
and handing them to Pillow's bcn decoder.
"""
import enum
import io
import struct
import zlib

from PIL import Image as PilImage, ImageChops

from .. import chunky
from ..textures import build_dds_header


@enum.unique
class TextureFormat(enum.IntEnum):
    """DATATMAN[6] block-compression format"""
    BC1 = 19
    BC3_NM = 22  # DXT5nm: X in alpha, Y in green, Z reconstructed, red = emissive mask
    BC3 = 23
    BC7 = 28


ZLIB_MAGICS = (b'\x78\xda', b'\x78\x9c', b'\x78\x01')

_DXGI_FORMAT_BC7_UNORM = 98


def _find_chunks(data: bytes) -> tuple:
    man = dat = None
    for depth, typeid, version, size, name, pos in chunky.walk_chunks(data):
        if typeid == 'DATATMAN':
            man = struct.unpack_from(f'<{size // 4}I', data, pos)
        elif typeid == 'DATATDAT':
            dat = data[pos:pos + size]
    return man, dat


def decode_full(data: bytes) -> tuple:
    """Decode a .rgt -> (format enum, {level: (width, height, bc_bytes)}), full resolution included."""
    man, dat = _find_chunks(data)
    if man is None or dat is None:
        raise ValueError('not a .rgt (missing DATATMAN/DATATDAT)')

    logical = bytearray()
    mips = {}
    cur = None  # [level, w, h, size, payload buffer] of the record being filled

    def drain():
        nonlocal cur
        while True:
            if cur is None:
                if len(logical) < 16:
                    return 16 - len(logical)
                level, w, h, sz = struct.unpack_from('<4I', logical, 0)
                del logical[:16]
                cur = [level, w, h, sz, bytearray()]
            level, w, h, sz, buf = cur
            take = min(sz - len(buf), len(logical))
            buf += logical[:take]
            del logical[:take]
            if len(buf) < sz:
                return sz - len(buf)
            mips[level] = (w, h, bytes(buf))
            cur = None
            if not logical:
                return 16

    off = 4
    pending = 16
    while off < len(dat):
        if dat[off:off + 2] in ZLIB_MAGICS:
            decompressor = zlib.decompressobj()
            try:
                block = decompressor.decompress(dat[off:]) + decompressor.flush()
            except zlib.error:
                block = None
            if block is not None:
                logical += block
                off = len(dat) - len(decompressor.unused_data)
                pending = drain()
                continue
        take = min(pending, len(dat) - off)
        logical += dat[off:off + take]
        off += take
        pending = drain()
    return TextureFormat(man[6]), mips


def _dds_wrap(fmt: TextureFormat, width: int, height: int, payload: bytes) -> bytes:
    fourcc = {
        TextureFormat.BC1: b'DXT1',
        TextureFormat.BC3: b'DXT5',
        TextureFormat.BC3_NM: b'DXT5',
        TextureFormat.BC7: b'DX10',
    }[fmt]
    dxgi_format = _DXGI_FORMAT_BC7_UNORM if fmt == TextureFormat.BC7 else None
    return build_dds_header(width, height, len(payload), fourcc, dxgi_format=dxgi_format) + payload


def _reconstruct_dxt5nm(image: PilImage) -> PilImage:
    """DXT5nm -> standard RGB tangent-space normal map: X from alpha, Y from green,
    Z = sqrt(1 - x² - y²). The stored red channel is the game's emissive mask and
    survives in the reconstructed image's alpha."""
    x = image.getchannel('A')
    y = image.getchannel('G')
    square = [round(min(255, (v / 127.5 - 1.0) ** 2 * 255)) for v in range(256)]
    # 255 - x² - y², clipped at 0 by subtract(); then z scaled back to a signed byte
    z_squared = ImageChops.subtract(ImageChops.invert(x.point(square)), y.point(square))
    z = z_squared.point([round(((v / 255) ** 0.5 + 1.0) * 127.5) for v in range(256)])
    return PilImage.merge('RGBA', (x, y, z, image.getchannel('R')))


def _encode_bc(image: PilImage, fmt: TextureFormat) -> bytes:
    if fmt == TextureFormat.BC1:
        pixel_format = 'DXT1'
        image = image.convert('RGB')
    else:
        pixel_format = 'DXT5'
        image = image.convert('RGBA')
        if fmt == TextureFormat.BC3_NM:
            r, g, b, a = image.split()
            zero = PilImage.new('L', image.size, 0)
            image = PilImage.merge('RGBA', (a, g, zero, r))
    out = io.BytesIO()
    image.save(out, format='DDS', pixel_format=pixel_format)
    payload = out.getvalue()[128:]  # strip the DDS header
    blocks = ((image.width + 3) // 4) * ((image.height + 3) // 4)
    expected = blocks * (8 if pixel_format == 'DXT1' else 16)
    if len(payload) < expected:
        raise ValueError(f'Pillow produced no {pixel_format} data (needs Pillow >= 11)')
    return payload[:expected]  # some Pillow builds append a mip chain


def encode(image: PilImage, fmt: TextureFormat, source_path: str = '', declared_path: str = '') -> bytes:
    """Encode a Pillow image into a .rgt file (BC7 sources are written as BC3)."""
    if fmt == TextureFormat.BC7:
        fmt = TextureFormat.BC3
    width, height = image.size
    levels = []
    mip = image
    while True:
        levels.append((mip.width, mip.height, _encode_bc(mip, fmt)))
        if mip.width == 1 and mip.height == 1:
            break
        mip = mip.resize((max(1, mip.width // 2), max(1, mip.height // 2)), PilImage.LANCZOS)

    stored = io.BytesIO()
    chunk_counts = []
    pairs = []
    for level, (w, h, payload) in reversed(list(enumerate(levels))):
        record = struct.pack('<4I', level, w, h, len(payload)) + payload
        chunk_counts.append((len(record) + 65535) // 65536)
        for pos in range(0, len(record), 65536):
            piece = record[pos:pos + 65536]
            packed = zlib.compress(piece)
            pairs.append((len(piece), len(packed)))
            stored.write(packed)

    tman = struct.pack(
        f'<{8 + len(chunk_counts) + 2 * len(pairs)}I',
        4, width, height, 1, 1, 2, int(fmt), (len(levels) << 8) | 1,
        *(count << 8 for count in chunk_counts),
        *(value << 8 for pair in pairs for value in pair))
    tdat = struct.pack('<I', 4) + stored.getvalue()

    out = io.BytesIO()
    writer = chunky.ChunkWriter(out, {
        'FOLDTSET': {'version': 1, 'DATADATA': {'version': 3}, 'FOLDTXTR': {
            'version': 1, 'FOLDDXTC': {'version': 4, 'DATATMAN': {'version': 1}, 'DATATDAT': {'version': 1}}}},
    }, chunky.ChunkyVersion.V4)
    writer.write_relic_chunky()
    with writer.start_chunk('FOLDTSET', name=declared_path):
        with writer.start_chunk('DATADATA'):
            writer.write_str(source_path)
            writer.write_struct('<L', 0)
        with writer.start_chunk('FOLDTXTR', name=declared_path):
            with writer.start_chunk('FOLDDXTC'):
                with writer.start_chunk('DATATMAN'):
                    writer.write(tman)
                with writer.start_chunk('DATATDAT'):
                    writer.write(tdat)
    return out.getvalue()


def to_pil(data: bytes) -> PilImage:
    """Decode a .rgt file's bytes to an RGBA Pillow image at full resolution."""
    fmt, mips = decode_full(data)
    if not mips:
        raise ValueError('no mip records in .rgt')
    width, height, payload = mips[min(mips)]
    image = PilImage.open(io.BytesIO(_dds_wrap(fmt, width, height, payload))).convert('RGBA')
    if fmt == TextureFormat.BC3_NM:
        image = _reconstruct_dxt5nm(image)
    return image
