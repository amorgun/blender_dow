from __future__ import annotations

import struct
from typing import IO

from PIL import Image, ImageFile
from PIL.DdsImagePlugin import DDSD, D3DFMT, DXGI_FORMAT, DDPF, DDS_MAGIC, DDSCAPS
from PIL._binary import i32le as i32, o8, o32le as o32


def mip_sizes(dimensions: tuple[int, int], mip_count: int | None = None) -> list[tuple[int, int]]:
    chain = []

    while all(i > 1 for i in dimensions):
        chain.append(dimensions)
        dimensions = tuple([max(dim // 2, 1) for dim in dimensions])

    return chain


def resize_no_premultiply(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    if image.mode == 'RGBA':
        rgb = image.convert('RGB').resize(size, Image.BILINEAR)
        a = image.getchannel('A').resize(size, Image.BILINEAR)
        rgb.putalpha(a)
        return rgb
    else:
        return image.resize(size, Image.BILINEAR)


def save_with_mipmaps(im: Image.Image, fp: IO[bytes], filename: str | bytes) -> None:
    if im.mode not in ("RGB", "RGBA", "L", "LA"):
        msg = f"cannot write mode {im.mode} as DDS"
        raise OSError(msg)

    flags = DDSD.CAPS | DDSD.HEIGHT | DDSD.WIDTH | DDSD.PIXELFORMAT
    bitcount = len(im.getbands()) * 8
    pixel_format = im.encoderinfo.get("pixel_format")
    args: tuple[int] | str
    if pixel_format:
        codec_name = "bcn"
        flags |= DDSD.LINEARSIZE
        # pitch = (im.width + 3) * 4
        rgba_mask = [0, 0, 0, 0]
        pixel_flags = DDPF.FOURCC
        if pixel_format == "DXT1":
            fourcc = D3DFMT.DXT1
            args = (1,)
        elif pixel_format == "DXT3":
            fourcc = D3DFMT.DXT3
            args = (2,)
        elif pixel_format == "DXT5":
            fourcc = D3DFMT.DXT5
            args = (3,)
        else:
            fourcc = D3DFMT.DX10
            if pixel_format == "BC2":
                args = (2,)
                dxgi_format = DXGI_FORMAT.BC2_TYPELESS
            elif pixel_format == "BC3":
                args = (3,)
                dxgi_format = DXGI_FORMAT.BC3_TYPELESS
            elif pixel_format == "BC5":
                args = (5,)
                dxgi_format = DXGI_FORMAT.BC5_TYPELESS
                if im.mode != "RGB":
                    msg = "only RGB mode can be written as BC5"
                    raise OSError(msg)
            else:
                msg = f"cannot write pixel format {pixel_format}"
                raise OSError(msg)
    else:
        codec_name = "raw"
        flags |= DDSD.PITCH
        # pitch = (im.width * bitcount + 7) // 8

        alpha = im.mode[-1] == "A"
        if im.mode[0] == "L":
            pixel_flags = DDPF.LUMINANCE
            args = im.mode
            if alpha:
                rgba_mask = [0x000000FF, 0x000000FF, 0x000000FF]
            else:
                rgba_mask = [0xFF000000, 0xFF000000, 0xFF000000]
        else:
            pixel_flags = DDPF.RGB
            args = im.mode[::-1]
            rgba_mask = [0x00FF0000, 0x0000FF00, 0x000000FF]

            if alpha:
                r, g, b, a = im.split()
                im = Image.merge("RGBA", (a, r, g, b))
        if alpha:
            pixel_flags |= DDPF.ALPHAPIXELS
        rgba_mask.append(0xFF000000 if alpha else 0)

        fourcc = D3DFMT.UNKNOWN

    caps = DDSCAPS.TEXTURE
    mip_chain = mip_sizes(im.size)
    if len(mip_chain) > 1:
        flags |= DDSD.MIPMAPCOUNT
        caps |= DDSCAPS.MIPMAP | DDSCAPS.COMPLEX

    fp.write(
        o32(DDS_MAGIC)
        + struct.pack(
            "<4I",
            124,  # header size
            flags,  # flags
            im.height,
            im.width,
        )
    )
    pitch_pos = fp.tell()
    fp.write(
        struct.pack(
            "<3I",
            0,  # pitch placeholder
            1,  # depth
            len(mip_chain),  # mipmaps
        )
        + struct.pack("11I", *((0,) * 11))  # reserved
        # pfsize, pfflags, fourcc, bitcount
        + struct.pack("<4I", 32, pixel_flags, fourcc, bitcount)
        + struct.pack("<4I", *rgba_mask)  # dwRGBABitMask
        + struct.pack("<5I", caps, 0, 0, 0, 0)
    )
    assert fp.tell() == 4 + 124, 'error writing file: incorrect header size'
    if fourcc == D3DFMT.DX10:
        fp.write(
            # dxgi_format, 2D resource, misc, array size, straight alpha
            struct.pack("<5I", dxgi_format, 3, 0, 0, 1)
        )
    start_pos = fp.tell()
    ImageFile._save(im, fp, [ImageFile._Tile(codec_name, (0, 0) + im.size, 0, args)])
    pitch = fp.tell() - start_pos
    for mip_size in mip_chain[1:]:
        ImageFile._save(resize_no_premultiply(im, mip_size), fp, [ImageFile._Tile(codec_name, (0, 0) + mip_size, 0, args)])
    fp.seek(pitch_pos)
    fp.write(struct.pack("<I", pitch))


def register():
    Image.register_save("DDS", save_with_mipmaps)


def unregister():
    from PIL.DdsImagePlugin import _save
    Image.register_save("DDS", _save)
