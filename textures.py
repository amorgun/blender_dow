import enum
import io
import struct

import bpy
from PIL import Image as PilImage


@enum.unique
class MaterialLayers(str, enum.Enum):
    DIFFUSE = 'diffuse'
    SPECULAR_MASK = 'specularity'
    SPECULAR_REFLECTION = 'reflection'
    SELF_ILLUMUNATION_MASK = 'self_illumination'
    SELF_ILLUMUNATION_COLOR = 'self_illumination_color'
    OPACITY = 'opacity'


@enum.unique
class TeamcolorLayers(str, enum.Enum):
    PRIMARY = 'primary'
    SECONDARY = 'secondary'
    TRIM = 'trim'
    WEAPONS = 'weapons'
    EYES = 'eyes'
    DIRT = 'dirt'
    DEFAULT = 'default'
    BADGE = 'badge'
    BANNER = 'banner'


@enum.unique
class DdsType(enum.Enum):
    DXT1 = enum.auto()
    DXT5 = enum.auto()


def img2pil(bpy_image) -> PilImage:
    texture_data = None
    if bpy_image.packed_files:
        texture_data = bpy_image.packed_file.data
    else:
        packed_image = bpy_image.copy()
        packed_image.pack()
        if packed_image.packed_file is None:
            return None
        texture_data = packed_image.packed_file.data
        bpy.data.images.remove(packed_image)
    texture_stream = io.BytesIO(texture_data)
    texture_stream.seek(0)
    return PilImage.open(texture_stream)


def encode_dds(pil_image: PilImage, path: str, force_type: DdsType = None):
    import quicktex.dds
    import quicktex.s3tc.bc1
    import quicktex.s3tc.bc3

    level = 10
    color_mode = quicktex.s3tc.bc1.BC1Encoder.ColorMode
    mode = color_mode.ThreeColor
    bc1_encoder = quicktex.s3tc.bc1.BC1Encoder(level, mode)
    bc3_encoder = quicktex.s3tc.bc3.BC3Encoder(level)
    if 'A' not in pil_image.mode:
        has_alpha = False
    else:
        alpha_hist = pil_image.getchannel('A').histogram()
        has_alpha = any([a > 0 for a in alpha_hist[:-1]])
        # TODO test for 1-bit alpha
    if force_type is DdsType.DXT1 or (not force_type and not has_alpha):
        quicktex.dds.encode(pil_image, bc1_encoder, 'DXT1').save(path)
    else:
        quicktex.dds.encode(pil_image, bc3_encoder, 'DXT5').save(path)

# if (imageType == 5) return FileFormats.ImgType.DXT1DDS;
# if (imageType == 6) return FileFormats.ImgType.DXT3DDS;
# if (imageType == 7) return FileFormats.ImgType.DXT5DDS;
# if (imageType == 0 || imageType == 2) return FileFormats.ImgType.TGA;

        # ((Control)label1).Text = "Texture:";
        # ((Control)label2).Text = "Reflection:";
        # ((Control)label3).Text = "Specularity Map:";
        # ((Control)label4).Text = "Self Illumination:";
        # ((Control)label5).Text = "Opacity:";

def write_dds(
    src: io.BufferedIOBase,
    dst: io.BufferedIOBase,
    data_size: int,
    width: int,
    height: int,
    num_mips: int,
    image_format: int,
):
    _DOW_DXT_FLAGS = 0x000A1007  # _DEFAULT_FLAGS | _dwF_MIPMAP | _dwF_LINEAR
    _ddsF_FOURCC = 0x00000004
    _DOW_DDSCAPS_FLAGS = 0x401008 # _ddscaps_F_TEXTURE | _ddscaps_F_COMPLEX | _ddscaps_F_MIPMAP_S
    fourCC = {8: b'DXT1', 10: b'DXT3', 11: b'DXT5'}[image_format]
    header = struct.Struct('<4s 7l 44x 2l 4s 20x 2l 12x').pack(
        b'DDS ', 124, _DOW_DXT_FLAGS, height, width, data_size, 0, num_mips, 
        32, _ddsF_FOURCC, fourCC,  # pixel format
        _DOW_DDSCAPS_FLAGS, 0,  # ddscaps
    )
    dst.write(header)
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
