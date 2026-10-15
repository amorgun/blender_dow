"""Import-side helpers shared by more than one game's importer."""
import functools
import io
import pathlib
import tempfile

import bpy
import mathutils
from PIL import Image as PilImage, ImageChops

from . import materials

# DirectX (x, y, z) -> Blender (-x, -z, y)
CONV = mathutils.Matrix((
    (-1, 0, 0, 0),
    (0, 0, -1, 0),
    (0, 1, 0, 0),
    (0, 0, 0, 1),
))
CONV_INV = CONV.inverted()

DOW2_TEAM_CHANNELS = (
    materials.TeamcolorLayers.PRIMARY,
    materials.TeamcolorLayers.SECONDARY,
    materials.TeamcolorLayers.TRIM,
    materials.TeamcolorLayers.WEAPONS,
)


def bake_teamcolor_layers(name: str, team_image, diffuse_image, channel_layers=DOW2_TEAM_CHANNELS,
                          detail_scale: float = 1.0, detail_multiplier=None, invert_alpha: bool = False) -> dict:
    """Split the paint mask channels into DoW1-style teamcolor layer images.
    channel_layers maps mask channels to layers (None skips); detail_scale brightens
    the painted detail; detail_multiplier pre-cancels the DoW1 tree's gain;
    invert_alpha turns the alpha channel into a mask (dow3 tertiary)."""
    team = PilImage.open(io.BytesIO(team_image.packed_file.data)).convert('RGBA')
    diffuse = PilImage.open(io.BytesIO(diffuse_image.packed_file.data)).convert('RGB')
    if team.size != diffuse.size:
        team = team.resize(diffuse.size, PilImage.NEAREST)

    bands = team.split()
    if invert_alpha:
        bands = bands[:3] + (ImageChops.invert(bands[3]),)
    channels = {layer: mask for layer, mask in zip(channel_layers, bands) if layer is not None}
    layer_images = {materials.TeamcolorLayers.DEFAULT: diffuse_image}
    detail = diffuse
    if detail_scale != 1.0:
        detail = diffuse.point(lambda v: min(255, round(v * detail_scale)))
    if detail_multiplier is not None:
        if detail_multiplier.size != detail.size:
            detail_multiplier = detail_multiplier.resize(detail.size, PilImage.NEAREST)
        detail = ImageChops.multiply(detail, PilImage.merge('RGB', (detail_multiplier,) * 3))

    with tempfile.TemporaryDirectory() as tmpdir:
        def bake(layer, pil_image):
            file = pathlib.Path(tmpdir) / f'{name}_{layer.value}.png'
            pil_image.save(file)
            image = bpy.data.images.load(str(file))
            image.pack()
            image.use_fake_user = True
            layer_images[layer] = image

        for layer, mask in channels.items():
            bake(layer, ImageChops.multiply(detail, PilImage.merge('RGB', (mask, mask, mask))))
        painted = functools.reduce(ImageChops.add, channels.values())
        bake(materials.TeamcolorLayers.DIRT, ImageChops.invert(painted))
    return layer_images
