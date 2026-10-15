import bpy

from .. import materials

# the mesh uv layers the badge and squad decal sample through
BADGE_UV_LAYER = 'badge.uv'
DECAL_UV_LAYER = 'decal.uv'


def create_shader_group():
    """The Dawn of War III surface, mirroring the game's dow3_unit shader: spc RGB
    is the specular colour, srgb-decoded spc alpha the gloss map, and the emissive
    mask rides in the normal map's spare channel (the reconstructed image's alpha).
    The albedo comes in already teamcolored."""
    group, created = materials._new_group(materials.MaterialType.DOW3.value)
    if not created:
        return group
    interface = group.interface

    materials._group_input(interface, 'diffuse', 'NodeSocketColor', (0.8, 0.8, 0.8, 1.0))
    materials._group_input(interface, 'opacity', 'NodeSocketFloat', 1.0)
    materials._group_input(interface, 'specularity', 'NodeSocketColor', (0.0, 0.0, 0.0, 1.0))
    materials._group_input(interface, 'gloss', 'NodeSocketFloat', 0.5)
    materials._group_input(interface, 'use_gloss', 'NodeSocketFloat', 0.0)
    materials._group_input(interface, 'normal_map', 'NodeSocketColor', (0.5, 0.5, 1.0, 1.0))
    materials._group_input(interface, 'emissive_mask', 'NodeSocketFloat', 0.0)
    materials._group_input(interface, 'emissive_factor', 'NodeSocketFloat', 1.0)
    materials._group_input(interface, 'teamcolor', 'NodeSocketColor', (0.8, 0.8, 0.8, 1.0))
    materials._group_input(interface, 'teamcolor_spec', 'NodeSocketColor', (0.0, 0.0, 0.0, 1.0))
    materials._group_input(interface, 'roughness_value', 'NodeSocketFloat', 0.5)

    for name, socket_type in [
        ('base_color', 'NodeSocketColor'),
        ('metallic', 'NodeSocketFloat'),
        ('roughness', 'NodeSocketFloat'),
        ('specular_tint', 'NodeSocketColor'),
        ('specular_ior', 'NodeSocketFloat'),
        ('emission_color', 'NodeSocketColor'),
        ('emission_strength', 'NodeSocketFloat'),
        ('alpha', 'NodeSocketFloat'),
        ('normal', 'NodeSocketVector'),
    ]:
        interface.new_socket(name=name, in_out='OUTPUT', socket_type=socket_type)

    nodes = group.nodes
    links = group.links
    inp = nodes.new('NodeGroupInput')
    inp.location = -700, 0
    out = nodes.new('NodeGroupOutput')
    out.location = 500, 0

    links.new(inp.outputs['teamcolor'], out.inputs['emission_color'])

    # the paints tint the spec map too; 'teamcolor_spec' carries the (tinted) F0

    # F0 = the (team-tinted) spec map, decoded to linear. Principled composes its
    # dielectric F0 as 0.08 * ior_level * tint, so the magnitude goes into the
    # level (max channel * 12.5, clamped) and the tint carries ONLY the chroma
    # (F0 / max channel) -- feeding the raw F0 colour as tint would square the
    # magnitude and darken every specular up to ~12x. Below the 0.08 cap the
    # composition is then the exact coloured F0.
    # The game's diffuse term is always the full albedo and its spec is the
    # coloured F0 at any strength; above the cap, fold the unrepresentable excess
    # into the base colour where spec clearly outweighs the albedo:
    # base = albedo + m*(1 - 0.08/F0)*F0_colour with
    # m = clamp((F0 - 0.08 - 2*max(albedo)) / 0.05), metallic stays 0 (no
    # environment dependence, diffuse brightness preserved).
    f0_split = nodes.new('ShaderNodeSeparateColor')
    f0_split.location = -900, 60
    links.new(inp.outputs['teamcolor_spec'], f0_split.inputs['Color'])
    f0_max = nodes.new('ShaderNodeMath')
    f0_max.operation = 'MAXIMUM'
    f0_max.location = -800, 60
    links.new(f0_split.outputs[0], f0_max.inputs[0])
    links.new(f0_split.outputs[1], f0_max.inputs[1])
    f0_luma = nodes.new('ShaderNodeMath')
    f0_luma.operation = 'MAXIMUM'
    f0_luma.label = 'F0 strength'
    f0_luma.location = -700, 60
    links.new(f0_max.outputs[0], f0_luma.inputs[0])
    links.new(f0_split.outputs[2], f0_luma.inputs[1])
    spec_level = nodes.new('ShaderNodeMath')
    spec_level.operation = 'MULTIPLY'
    spec_level.use_clamp = True
    spec_level.inputs[1].default_value = 12.5  # F0 / 0.08
    spec_level.label = 'Dielectric F0'
    spec_level.location = -550, -80
    links.new(f0_luma.outputs[0], spec_level.inputs[0])
    links.new(spec_level.outputs[0], out.inputs['specular_ior'])
    tint_floor = nodes.new('ShaderNodeMath')
    tint_floor.operation = 'MAXIMUM'
    tint_floor.inputs[1].default_value = 0.001
    tint_floor.location = -550, -220
    links.new(f0_luma.outputs[0], tint_floor.inputs[0])
    tint = nodes.new('ShaderNodeVectorMath')
    tint.operation = 'DIVIDE'
    tint.label = 'F0 chroma'
    tint.location = -400, -220
    links.new(inp.outputs['teamcolor_spec'], tint.inputs[0])
    links.new(tint_floor.outputs[0], tint.inputs[1])
    links.new(tint.outputs[0], out.inputs['specular_tint'])
    albedo_split = nodes.new('ShaderNodeSeparateColor')
    albedo_split.location = -900, 180
    links.new(inp.outputs['teamcolor'], albedo_split.inputs['Color'])
    albedo_max = nodes.new('ShaderNodeMath')
    albedo_max.operation = 'MAXIMUM'
    albedo_max.location = -750, 180
    links.new(albedo_split.outputs[0], albedo_max.inputs[0])
    links.new(albedo_split.outputs[1], albedo_max.inputs[1])
    albedo_max2 = nodes.new('ShaderNodeMath')
    albedo_max2.operation = 'MAXIMUM'
    albedo_max2.location = -620, 180
    links.new(albedo_max.outputs[0], albedo_max2.inputs[0])
    links.new(albedo_split.outputs[2], albedo_max2.inputs[1])
    spec_excess = nodes.new('ShaderNodeMath')
    spec_excess.operation = 'MULTIPLY_ADD'
    spec_excess.inputs[1].default_value = 1 / 0.05    # (F0 - 0.08) / 0.05
    spec_excess.inputs[2].default_value = -0.08 / 0.05
    spec_excess.location = -550, 130
    links.new(f0_luma.outputs[0], spec_excess.inputs[0])
    spec_wins = nodes.new('ShaderNodeMath')
    spec_wins.operation = 'MULTIPLY_ADD'
    spec_wins.use_clamp = True
    spec_wins.inputs[1].default_value = -2 / 0.05     # - 2*max(albedo) / 0.05
    spec_wins.label = 'Spec dominance'
    spec_wins.location = -420, 130
    links.new(albedo_max2.outputs[0], spec_wins.inputs[0])
    links.new(spec_excess.outputs[0], spec_wins.inputs[2])
    f0_floor = nodes.new('ShaderNodeMath')
    f0_floor.operation = 'MAXIMUM'
    f0_floor.inputs[1].default_value = 0.001
    f0_floor.location = -420, 0
    links.new(f0_luma.outputs[0], f0_floor.inputs[0])
    cap_ratio = nodes.new('ShaderNodeMath')
    cap_ratio.operation = 'DIVIDE'
    cap_ratio.inputs[0].default_value = 0.08
    cap_ratio.location = -300, 0
    links.new(f0_floor.outputs[0], cap_ratio.inputs[1])
    excess_frac = nodes.new('ShaderNodeMath')
    excess_frac.operation = 'SUBTRACT'
    excess_frac.use_clamp = True
    excess_frac.inputs[0].default_value = 1.0
    excess_frac.location = -180, 0
    links.new(cap_ratio.outputs[0], excess_frac.inputs[1])
    excess_amount = nodes.new('ShaderNodeMath')
    excess_amount.operation = 'MULTIPLY'
    excess_amount.location = -60, 60
    links.new(spec_wins.outputs[0], excess_amount.inputs[0])
    links.new(excess_frac.outputs[0], excess_amount.inputs[1])
    excess = nodes.new('ShaderNodeVectorMath')
    excess.operation = 'SCALE'
    excess.location = -60, 200
    links.new(inp.outputs['teamcolor_spec'], excess.inputs[0])
    links.new(excess_amount.outputs[0], excess.inputs['Scale'])
    base = nodes.new('ShaderNodeVectorMath')
    base.operation = 'ADD'
    base.label = 'Apply spec'
    base.location = 80, 300
    links.new(inp.outputs['teamcolor'], base.inputs[0])
    links.new(excess.outputs[0], base.inputs[1])
    links.new(base.outputs[0], out.inputs['base_color'])
    out.inputs['metallic'].default_value = 0.0


    # the game's gloss is srgb-decoded, then drives a Blinn-Phong power 2^(12g-1);
    # roughness approximates that lobe width with sqrt(2 / (power + 2))
    gloss_linear = nodes.new('ShaderNodeMath')
    gloss_linear.operation = 'POWER'
    gloss_linear.inputs[1].default_value = 2.2
    gloss_linear.location = -550, -150
    links.new(inp.outputs['gloss'], gloss_linear.inputs[0])
    power_exp = nodes.new('ShaderNodeMath')
    power_exp.operation = 'MULTIPLY_ADD'
    power_exp.inputs[1].default_value = 12.0
    power_exp.inputs[2].default_value = -1.0
    power_exp.location = -400, -150
    links.new(gloss_linear.outputs[0], power_exp.inputs[0])
    power = nodes.new('ShaderNodeMath')
    power.operation = 'POWER'
    power.inputs[0].default_value = 2.0
    power.location = -400, -280
    links.new(power_exp.outputs[0], power.inputs[1])
    lobe = nodes.new('ShaderNodeMath')
    lobe.operation = 'ADD'
    lobe.inputs[1].default_value = 2.0
    lobe.location = -250, -150
    links.new(power.outputs[0], lobe.inputs[0])
    lobe_inv = nodes.new('ShaderNodeMath')
    lobe_inv.operation = 'DIVIDE'
    lobe_inv.inputs[0].default_value = 2.0
    lobe_inv.location = -250, -280
    links.new(lobe.outputs[0], lobe_inv.inputs[1])
    gloss_roughness = nodes.new('ShaderNodeMath')
    gloss_roughness.operation = 'SQRT'
    gloss_roughness.location = -100, -150
    links.new(lobe_inv.outputs[0], gloss_roughness.inputs[0])
    roughness = nodes.new('ShaderNodeMix')
    roughness.data_type = 'FLOAT'
    roughness.location = 100, -150
    links.new(inp.outputs['use_gloss'], roughness.inputs['Factor'])
    links.new(inp.outputs['roughness_value'], roughness.inputs[2])
    links.new(gloss_roughness.outputs[0], roughness.inputs[3])
    links.new(roughness.outputs['Result'], out.inputs['roughness'])

    # emission strength = min(mask * clamp(factor, 1, 4), 4), colored by the albedo
    factor_min = nodes.new('ShaderNodeMath')
    factor_min.operation = 'MAXIMUM'
    factor_min.inputs[1].default_value = 1.0
    factor_min.location = -550, -400
    links.new(inp.outputs['emissive_factor'], factor_min.inputs[0])
    factor_max = nodes.new('ShaderNodeMath')
    factor_max.operation = 'MINIMUM'
    factor_max.inputs[1].default_value = 4.0
    factor_max.location = -400, -400
    links.new(factor_min.outputs[0], factor_max.inputs[0])
    emission_scaled = nodes.new('ShaderNodeMath')
    emission_scaled.operation = 'MULTIPLY'
    emission_scaled.location = -250, -400
    links.new(inp.outputs['emissive_mask'], emission_scaled.inputs[0])
    links.new(factor_max.outputs[0], emission_scaled.inputs[1])
    emission = nodes.new('ShaderNodeMath')
    emission.operation = 'MINIMUM'
    emission.inputs[1].default_value = 4.0
    emission.location = -100, -400
    links.new(emission_scaled.outputs[0], emission.inputs[0])
    links.new(emission.outputs[0], out.inputs['emission_strength'])

    object_info = nodes.new('ShaderNodeObjectInfo')
    object_info.location = -400, -550
    alpha = nodes.new('ShaderNodeMath')
    alpha.operation = 'MULTIPLY'
    alpha.use_clamp = True
    alpha.location = -150, -550
    links.new(inp.outputs['opacity'], alpha.inputs[0])
    links.new(object_info.outputs['Alpha'], alpha.inputs[1])
    links.new(alpha.outputs[0], out.inputs['alpha'])

    normal_map = nodes.new('ShaderNodeNormalMap')
    normal_map.location = -150, -750
    links.new(inp.outputs['normal_map'], normal_map.inputs['Color'])
    links.new(normal_map.outputs['Normal'], out.inputs['normal'])

    return group


def create_teamcolor_group():
    """ApplyTeamcolorDow3, mirroring the game's paint shader: the pattern's R/G/B
    masks (and its inverted alpha) each lerp the gamma-space albedo toward an
    OVERLAY of the paint colour, then the badge stencil (tinted by its own three
    paints) and the squad decal composite over the painted albedo."""
    group, created = materials._new_group('ApplyTeamcolorDow3')
    if not created:
        return group
    interface = group.interface

    # a colour whose gamma encoding is 0.5 leaves the overlay unchanged
    overlay_identity = (0.5 ** 2.2,) * 3 + (1.0,)
    materials._group_input(interface, 'diffuse', 'NodeSocketColor', (0.8, 0.8, 0.8, 1.0))
    materials._group_input(interface, 'specularity', 'NodeSocketColor', (0.0, 0.0, 0.0, 1.0))
    materials._group_input(interface, 'pattern', 'NodeSocketColor', (0.0, 0.0, 0.0, 1.0))
    materials._group_input(interface, 'pattern_alpha', 'NodeSocketFloat', 1.0)
    for layer in materials.SCHEME_PAINT_LAYERS:
        materials._group_input(interface, f'{layer.value}_color', 'NodeSocketColor', overlay_identity)
    for layer in materials.SCHEME_PAINT_LAYERS:
        materials._group_input(interface, f'{layer.value}_spec_color', 'NodeSocketColor', overlay_identity)
    # the paint's gloss overlays the spc-alpha gloss map in gamma space (raw
    # byte values on both sides, 0.5 = identity)
    materials._group_input(interface, 'gloss', 'NodeSocketFloat', 0.5)
    for layer in materials.SCHEME_PAINT_LAYERS:
        materials._group_input(interface, f'{layer.value}_gloss', 'NodeSocketFloat', 0.5)
    materials._group_input(interface, 'badge', 'NodeSocketColor', (0.0, 0.0, 0.0, 1.0))
    for c in materials.BADGE_PAINT_LAYERS:
        materials._group_input(interface, f'badge_{c.value}_color', 'NodeSocketColor', (0.5, 0.5, 0.5, 1.0))
    materials._group_input(interface, 'decal_color', 'NodeSocketColor', (0.0, 0.0, 0.0, 1.0))
    materials._group_input(interface, 'decal_alpha', 'NodeSocketFloat', 0.0)

    interface.new_socket(name='result', in_out='OUTPUT', socket_type='NodeSocketColor')
    interface.new_socket(name='specularity', in_out='OUTPUT', socket_type='NodeSocketColor')
    interface.new_socket(name='gloss', in_out='OUTPUT', socket_type='NodeSocketFloat')

    nodes = group.nodes
    links = group.links
    inp = nodes.new('NodeGroupInput')
    inp.location = -1100, 0
    out = nodes.new('NodeGroupOutput')
    out.location = 1300, 0

    # the paint math runs in gamma space, mirroring the game's pattern shader
    detail = nodes.new('ShaderNodeGamma')
    detail.inputs['Gamma'].default_value = 0.4545
    detail.location = -850, 400
    links.new(inp.outputs['diffuse'], detail.inputs['Color'])

    pattern = nodes.new('ShaderNodeSeparateColor')
    pattern.location = -850, 150
    links.new(inp.outputs['pattern'], pattern.inputs['Color'])

    tertiary_mask = nodes.new('ShaderNodeMath')
    tertiary_mask.operation = 'SUBTRACT'
    tertiary_mask.use_clamp = True
    tertiary_mask.inputs[0].default_value = 1.0
    tertiary_mask.label = 'Tertiary mask'
    tertiary_mask.location = -850, -100
    links.new(inp.outputs['pattern_alpha'], tertiary_mask.inputs[1])

    spec_detail = nodes.new('ShaderNodeGamma')
    spec_detail.inputs['Gamma'].default_value = 0.4545
    spec_detail.location = -850, 550
    links.new(inp.outputs['specularity'], spec_detail.inputs['Color'])

    masks = [pattern.outputs[0], pattern.outputs[1], pattern.outputs[2], tertiary_mask.outputs[0]]
    painted = detail.outputs[0]
    painted_spec = spec_detail.outputs[0]
    for idx, layer in enumerate(materials.SCHEME_PAINT_LAYERS):
        for suffix, source in (('color', 'painted'), ('spec_color', 'painted_spec')):
            color = nodes.new('ShaderNodeGamma')
            color.inputs['Gamma'].default_value = 0.4545
            color.location = -650, 300 - 220 * idx - (110 if suffix == 'spec_color' else 0)
            links.new(inp.outputs[f'{layer.value}_{suffix}'], color.inputs['Color'])

            tinted = nodes.new('ShaderNodeMixRGB')
            tinted.blend_type = 'OVERLAY'
            tinted.use_clamp = True
            tinted.label = f'{layer}_spec' if suffix == 'spec_color' else layer
            tinted.location = -400, 300 - 220 * idx - (110 if suffix == 'spec_color' else 0)
            links.new(masks[idx], tinted.inputs['Fac'])
            links.new(painted_spec if source == 'painted_spec' else painted, tinted.inputs['Color1'])
            links.new(color.outputs[0], tinted.inputs['Color2'])
            if source == 'painted_spec':
                painted_spec = tinted.outputs[0]
            else:
                painted = tinted.outputs[0]

    spec_linear = nodes.new('ShaderNodeGamma')
    spec_linear.inputs['Gamma'].default_value = 2.2
    spec_linear.location = 750, 550
    links.new(painted_spec, spec_linear.inputs['Color'])
    links.new(spec_linear.outputs[0], out.inputs['specularity'])

    # gloss overlay chain: scalar values through the same OVERLAY math (a float
    # into a color socket spreads to all channels; the shader group re-decodes)
    painted_gloss = inp.outputs['gloss']
    for idx, layer in enumerate(materials.SCHEME_PAINT_LAYERS):
        tinted = nodes.new('ShaderNodeMixRGB')
        tinted.blend_type = 'OVERLAY'
        tinted.use_clamp = True
        tinted.label = f'{layer}_gloss'
        tinted.location = -400, 700 + 110 * idx
        links.new(masks[idx], tinted.inputs['Fac'])
        links.new(painted_gloss, tinted.inputs['Color1'])
        links.new(inp.outputs[f'{layer.value}_gloss'], tinted.inputs['Color2'])
        painted_gloss = tinted.outputs[0]
    links.new(painted_gloss, out.inputs['gloss'])


    # badge stencil: its channels are masks for the badge's own three paints
    badge = nodes.new('ShaderNodeSeparateColor')
    badge.location = -850, -500
    links.new(inp.outputs['badge'], badge.inputs['Color'])

    badge_sum = None
    badge_mask_sum = None
    for idx, c in enumerate(materials.BADGE_PAINT_LAYERS):
        color = nodes.new('ShaderNodeGamma')
        color.inputs['Gamma'].default_value = 0.4545
        color.location = -650, -400 - 220 * idx
        links.new(inp.outputs[f'badge_{c.value}_color'], color.inputs['Color'])

        tinted = nodes.new('ShaderNodeMixRGB')
        tinted.blend_type = 'MULTIPLY'
        tinted.inputs['Fac'].default_value = 1
        tinted.label = f'badge_{c.value}'
        tinted.location = -450, -400 - 220 * idx
        links.new(color.outputs[0], tinted.inputs['Color1'])
        links.new(badge.outputs[idx], tinted.inputs['Color2'])

        if badge_sum is None:
            badge_sum = tinted.outputs[0]
        else:
            add = nodes.new('ShaderNodeMixRGB')
            add.blend_type = 'ADD'
            add.inputs['Fac'].default_value = 1
            add.location = -250, -400 - 220 * idx
            links.new(badge_sum, add.inputs['Color1'])
            links.new(tinted.outputs[0], add.inputs['Color2'])
            badge_sum = add.outputs[0]

        if badge_mask_sum is None:
            badge_mask_sum = badge.outputs[idx]
        else:
            madd = nodes.new('ShaderNodeMath')
            madd.operation = 'ADD'
            madd.location = -250, -620 - 220 * idx
            links.new(badge_mask_sum, madd.inputs[0])
            links.new(badge.outputs[idx], madd.inputs[1])
            badge_mask_sum = madd.outputs[0]

    coverage = nodes.new('ShaderNodeMath')
    coverage.operation = 'MULTIPLY'
    coverage.use_clamp = True
    coverage.inputs[1].default_value = 2.0
    coverage.label = 'Badge coverage'
    coverage.location = -50, -500
    links.new(badge_mask_sum, coverage.inputs[0])

    with_badge = nodes.new('ShaderNodeMixRGB')
    with_badge.blend_type = 'MIX'
    with_badge.location = 550, 100
    links.new(coverage.outputs[0], with_badge.inputs['Fac'])
    links.new(painted, with_badge.inputs['Color1'])
    links.new(badge_sum, with_badge.inputs['Color2'])

    to_linear = nodes.new('ShaderNodeGamma')
    to_linear.inputs['Gamma'].default_value = 2.2
    to_linear.location = 750, 100
    links.new(with_badge.outputs[0], to_linear.inputs['Color'])

    with_decal = nodes.new('ShaderNodeMixRGB')
    with_decal.blend_type = 'MIX'
    with_decal.location = 1000, 0
    links.new(inp.outputs['decal_alpha'], with_decal.inputs['Fac'])
    links.new(to_linear.outputs[0], with_decal.inputs['Color1'])
    links.new(inp.outputs['decal_color'], with_decal.inputs['Color2'])

    links.new(with_decal.outputs[0], out.inputs['result'])
    return group


def setup_material(mat, material_data: materials.MaterialInfo, force_teamcolor: bool):
    """The Dawn of War III material tree: raw game textures feed the Dow3Shader group;
    teamcolor (pattern tint + badge + decal) runs live through ApplyTeamcolorDow3."""
    mat.blend_method = 'CLIP'
    mat.alpha_threshold = 48 / 255
    mat.show_transparent_back = False

    links = mat.node_tree.links
    node_final = mat.node_tree.nodes[0]
    node_final.location = 300, 400
    mat.node_tree.nodes[1].location = node_final.location[0] + 400, node_final.location[1]

    group_node = mat.node_tree.nodes.new('ShaderNodeGroup')
    group_node.node_tree = create_shader_group()
    group_node.location = node_final.location[0] - 300, node_final.location[1]
    materials.link_shader_group_to_bsdf(mat, group_node, node_final)

    group_node.inputs['roughness_value'].default_value = material_data.roughness_value
    if material_data.default_color:
        group_node.inputs['diffuse'].default_value[:3] = material_data.default_color.from_srgb_to_scene_linear()

    uv_vector = materials.setup_uv_offset(mat, node_final.location[1] - 1600, node_final.location[1] - 200)

    def new_tex(layer, image, y_index):
        node_tex = mat.node_tree.nodes.new('ShaderNodeTexImage')
        node_tex.image = image
        node_tex.location = -430, node_final.location[1] - 320 * y_index
        node_tex.label = layer
        links.new(uv_vector, node_tex.inputs['Vector'])
        return node_tex

    channel = material_data.channel_images
    num_tex = 0
    node_diffuse = None
    if (image := channel.get(materials.MaterialLayers.DIFFUSE)) is not None:
        node_diffuse = new_tex(materials.MaterialLayers.DIFFUSE, image, num_tex)
        num_tex += 1
        links.new(node_diffuse.outputs[0], group_node.inputs['diffuse'])
        if materials.MaterialLayers.OPACITY not in channel:
            links.new(node_diffuse.outputs['Alpha'], group_node.inputs['opacity'])
    node_spec = None
    if (image := channel.get(materials.MaterialLayers.SPECULAR_MASK)) is not None:
        node_spec = new_tex(materials.MaterialLayers.SPECULAR_MASK, image, num_tex)
        num_tex += 1
        links.new(node_spec.outputs[0], group_node.inputs['specularity'])
        # the gloss map lives in the spc alpha; the importer marks it by aliasing
        # the GLOSS slot to the spec image
        if channel.get(materials.MaterialLayers.GLOSS) == image:
            links.new(node_spec.outputs['Alpha'], group_node.inputs['gloss'])
            group_node.inputs['use_gloss'].default_value = 1.0
    if (image := channel.get(materials.MaterialLayers.GLOSS)) is not None and image != channel.get(materials.MaterialLayers.SPECULAR_MASK):
        node_gloss = new_tex(materials.MaterialLayers.GLOSS, image, num_tex)
        num_tex += 1
        links.new(node_gloss.outputs[0], group_node.inputs['gloss'])
        group_node.inputs['use_gloss'].default_value = 1.0
    if (image := channel.get(materials.MaterialLayers.NORMAL_MAP)) is not None:
        node_normal = new_tex(materials.MaterialLayers.NORMAL_MAP, image, num_tex)
        num_tex += 1
        links.new(node_normal.outputs[0], group_node.inputs['normal_map'])
        # the reconstructed normal's alpha carries the game's emissive mask
        links.new(node_normal.outputs['Alpha'], group_node.inputs['emissive_mask'])
    if (image := channel.get(materials.MaterialLayers.OPACITY)) is not None:
        node_opacity = new_tex(materials.MaterialLayers.OPACITY, image, num_tex)
        num_tex += 1
        links.new(node_opacity.outputs['Alpha'], group_node.inputs['opacity'])

    node_pattern = None
    if force_teamcolor and (image := channel.get(materials.MaterialLayers.PATTERN)) is not None:
        node_pattern = new_tex(materials.MaterialLayers.PATTERN, image, num_tex)
        num_tex += 1

    # the shader group reads albedo from 'teamcolor' and F0 from 'teamcolor_spec';
    # without a teamcolor chain the plain textures feed them (the chain below
    # replaces these links when it exists)
    if node_diffuse is not None:
        links.new(node_diffuse.outputs[0], group_node.inputs['teamcolor'])
    else:
        group_node.inputs['teamcolor'].default_value = tuple(group_node.inputs['diffuse'].default_value)
    if node_spec is not None:
        links.new(node_spec.outputs[0], group_node.inputs['teamcolor_spec'])

    if force_teamcolor and node_diffuse is not None and node_pattern is not None:
        teamcolor_node = mat.node_tree.nodes.new('ShaderNodeGroup')
        teamcolor_node.node_tree = create_teamcolor_group()
        teamcolor_node.location = group_node.location[0] - 250, group_node.location[1] + 500
        links.new(node_diffuse.outputs[0], teamcolor_node.inputs['diffuse'])
        links.new(node_pattern.outputs[0], teamcolor_node.inputs['pattern'])
        links.new(node_pattern.outputs['Alpha'], teamcolor_node.inputs['pattern_alpha'])
        if node_spec is not None:
            links.new(node_spec.outputs[0], teamcolor_node.inputs['specularity'])
            links.new(teamcolor_node.outputs['specularity'], group_node.inputs['teamcolor_spec'])
            if channel.get(materials.MaterialLayers.GLOSS) == channel.get(materials.MaterialLayers.SPECULAR_MASK):
                # paints overlay the gloss too; reroute the spc-alpha gloss
                # through the teamcolor group (replaces the direct link)
                links.new(node_spec.outputs['Alpha'], teamcolor_node.inputs['gloss'])
                links.new(teamcolor_node.outputs['gloss'], group_node.inputs['gloss'])

        common_y = teamcolor_node.location[1] + 400
        if (image := material_data.teamcolor_images.get(materials.TeamcolorLayers.BADGE)) is not None:
            uv_node = mat.node_tree.nodes.new('ShaderNodeUVMap')
            uv_node.uv_map = BADGE_UV_LAYER
            uv_node.location = -900, common_y
            node_badge = mat.node_tree.nodes.new('ShaderNodeTexImage')
            node_badge.image = image
            node_badge.extension = 'CLIP'
            node_badge.label = materials.TeamcolorLayers.BADGE
            node_badge.location = -650, common_y
            links.new(uv_node.outputs[0], node_badge.inputs['Vector'])
            links.new(node_badge.outputs[0], teamcolor_node.inputs['badge'])
        if (image := material_data.teamcolor_images.get(materials.TeamcolorLayers.DECAL)) is not None:
            uv_node = mat.node_tree.nodes.new('ShaderNodeUVMap')
            uv_node.uv_map = DECAL_UV_LAYER
            uv_node.location = -900, common_y - 300
            node_decal = mat.node_tree.nodes.new('ShaderNodeTexImage')
            node_decal.image = image
            node_decal.extension = 'CLIP'
            node_decal.label = materials.TeamcolorLayers.DECAL
            node_decal.location = -650, common_y - 300
            links.new(uv_node.outputs[0], node_decal.inputs['Vector'])
            links.new(node_decal.outputs[0], teamcolor_node.inputs['decal_color'])
            links.new(node_decal.outputs['Alpha'], teamcolor_node.inputs['decal_alpha'])

        links.new(teamcolor_node.outputs['result'], group_node.inputs['teamcolor'])
