import enum
import io
import struct

from PIL import Image as PilImage


@enum.unique
class DdsType(enum.Enum):
    DXT1 = enum.auto()
    DXT5 = enum.auto()


def img2pil(bpy_image) -> PilImage:
    import bpy

    texture_data = None
    if bpy_image.packed_files:
        texture_data = bpy_image.packed_file.data
    else:
        packed_image = bpy_image.copy()
        try:
            packed_image.pack()
        except RuntimeError:
            return None
        if packed_image.packed_file is None:
            return None
        texture_data = packed_image.packed_file.data
        bpy.data.images.remove(packed_image)
    texture_stream = io.BytesIO(texture_data)
    texture_stream.seek(0)
    return PilImage.open(texture_stream)


def encode_dds(pil_image: PilImage, force_type: DdsType = None):
    if 'A' not in pil_image.mode:
        has_alpha = False
    else:
        alpha_hist = pil_image.getchannel('A').histogram()
        has_alpha = any([a > 0 for a in alpha_hist[:-1]])
        # TODO test for 1-bit alpha
    if force_type is DdsType.DXT1 or (not force_type and not has_alpha):
        pixel_format = 'DXT1'
    else:
        pixel_format = 'DXT5'
    result = io.BytesIO()
    pil_image.save(result, format='DDS', pixel_format=pixel_format)
    result.flush()
    result.seek(0)
    is_dds, width, height, declared_data_size, num_mips, image_format, image_type = read_dds_header(result)
    return width, height, num_mips, image_format, image_type, result

# if (imageType == 5) return FileFormats.ImgType.DXT1DDS;
# if (imageType == 6) return FileFormats.ImgType.DXT3DDS;
# if (imageType == 7) return FileFormats.ImgType.DXT5DDS;
# if (imageType == 0 || imageType == 2) return FileFormats.ImgType.TGA;

        # ((Control)label1).Text = "Texture:";
        # ((Control)label2).Text = "Reflection:";
        # ((Control)label3).Text = "Specularity Map:";
        # ((Control)label4).Text = "Self Illumination:";
        # ((Control)label5).Text = "Opacity:";

_DDSD_FLAGS = 0x00081007       # CAPS | HEIGHT | WIDTH | PIXELFORMAT | LINEARSIZE
_DDSD_MIPMAPCOUNT = 0x20000
_DDPF_FOURCC = 0x00000004
_DDSCAPS_TEXTURE = 0x1000
_DOW_DDSCAPS_FLAGS = 0x401008  # TEXTURE | COMPLEX | MIPMAP


def build_dds_header(
    width: int,
    height: int,
    data_size: int,
    fourcc: bytes,
    num_mips: int = 0,
    caps: int = _DDSCAPS_TEXTURE,
    dxgi_format: int = None,
) -> bytes:
    """A DDS file header; fourcc b'DX10' formats append the DX10 extension
    naming the dxgi_format"""
    header = struct.Struct('<4s 7l 44x 2l 4s 20x 2l 12x').pack(
        b'DDS ', 124, _DDSD_FLAGS | (_DDSD_MIPMAPCOUNT if num_mips > 0 else 0),
        height, width, data_size, 0, num_mips,
        32, _DDPF_FOURCC, fourcc,  # pixel format
        caps, 0,  # ddscaps
    )
    if dxgi_format is not None:
        header += struct.pack('<5I', dxgi_format, 3, 0, 1, 0)  # TEXTURE2D, one array slice
    return header


def write_dds(
    src: io.BufferedIOBase,
    dst: io.BufferedIOBase,
    data_size: int,
    width: int,
    height: int,
    num_mips: int,
    image_format: int,
):
    fourcc = {8: b'DXT1', 10: b'DXT3', 11: b'DXT5'}[image_format]
    dst.write(build_dds_header(width, height, data_size, fourcc, num_mips, caps=_DOW_DDSCAPS_FLAGS))
    dst.write(src.read(data_size))


def write_tga(
    src: io.BufferedIOBase,
    dst: io.BufferedIOBase,
    data_size: int,
    width: int,
    height: int,
    grayscale: bool = False
):
    # See http://www.paulbourke.net/dataformats/tga/
    header = struct.Struct('<3B 2HB 4H2B').pack(
        0,  # ID length
        0,  # file contains no color map
        3 if grayscale else 2,  # uncompressed grayscale image
        0, 0, 32,  # Color Map Specification
        0, 0, width, height, 8 if grayscale else 32, 0,  # Image Specification.
    )
    dst.write(header)
    dst.write(src.read(data_size))


def read_dds_header(src: io.BufferedIOBase):
    fmt = '< 4s 8x 3l 4x l 44x 8x 4s 20x 8x 12x'
    data = src.read(struct.calcsize(fmt))
    dds_magic, height, width, data_size, num_mips, fourCC = struct.unpack(fmt, data)

    is_dds = dds_magic == b'DDS '
    return (is_dds, width, height, data_size, num_mips,
             {b'DXT1': 8, b'DXT3': 10, b'DXT5': 11}[fourCC] if is_dds else None,
             {b'DXT1': 5, b'DXT3': 6, b'DXT5': 7}[fourCC] if is_dds else None,
    )
