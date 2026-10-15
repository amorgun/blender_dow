import bpy

from .. import materials

# engine scalar vars as group inputs; alpha_test gates whether dif alpha
# applies (game: opaque when off), dirt_visibility scales the dirt layer
# (engine-animated from 0 = clean); highlight/occlusion_flag drive engine-only
# effects (selection flash, behind-cover silhouette) and stay unrouted
SCALAR_VARS = (
    ('alpha_test', 0.0),
    ('highlight', 0.0),
    ('occlusion_flag', 0.0),
    ('dirt_visibility', 0.0),
)

# the campaign Blood Ravens scheme: simulation/scar/data/teamcolours.scar picks
# config/armypaintercolours.lua entries (visible ones, 1-based) 32 Blood Raven Red /
# 13 Bone / 1 Chaos Black / 83 Metallic Gold; byte values /255, fed to the shader raw
DEFAULT_TEAM_COLOURS = {
    'primary': (100 / 255, 20 / 255, 0.0),
    'secondary': (170 / 255, 150 / 255, 115 / 255),
    'trim': (10 / 255, 10 / 255, 10 / 255),
    'weapons': (110 / 255, 104 / 255, 53 / 255),
}
# teamSpecular/teamGloss of the same palette entries; the shader adds them raw to the
# raw spc sample (its spec path is linear), so no gamma conversion anywhere
DEFAULT_TEAM_SPECULARS = {
    'primary': (0.0, 0.0, 0.0),
    'secondary': (1 / 255, 1 / 255, 0.0),
    'trim': (0.0, 0.0, 0.0),
    'weapons': (85 / 255, 95 / 255, 40 / 255),
}
DEFAULT_TEAM_GLOSS = {
    'primary': 0.0,
    'secondary': 0.0,
    'trim': 0.0,
    'weapons': 0.01,
}

# bump whenever the group node graphs change shape, so blends holding groups
# built by an older addon get fresh groups instead of reusing stale ones
GROUP_VERSION = 2

# tem mask channels R/G/B/A drive these four paint slots, in shader constant order
TEAM_SLOTS = (
    materials.TeamcolorLayers.PRIMARY,
    materials.TeamcolorLayers.SECONDARY,
    materials.TeamcolorLayers.TRIM,
    materials.TeamcolorLayers.WEAPONS,
)


def create_teamcolor_group():
    """ApplyTeamcolorDow2, mirroring the dow2_unit pixel shader: the tem mask's
    R/G/B/A channels each lerp the gamma-space diffuse toward an OVERLAY of the
    slot colour (chained, like the shader's r0 accumulator); team specular and
    gloss are ADDED per slot (killed under dirt); badge then banner alpha-lerp
    over the painted diffuse before the ^2.2 decode."""
    group, created = materials._new_group('ApplyTeamcolorDow2', GROUP_VERSION)
    if not created:
        return group
    interface = group.interface

    materials._group_input(interface, 'diffuse', 'NodeSocketColor', (0.8, 0.8, 0.8, 1.0))
    materials._group_input(interface, 'specularity', 'NodeSocketColor', (0.0, 0.0, 0.0, 1.0))
    materials._factor_input(interface, 'gloss', 0.0)
    materials._group_input(interface, 'pattern', 'NodeSocketColor', (0.0, 0.0, 0.0, 1.0))
    materials._factor_input(interface, 'pattern_alpha', 0.0)
    materials._factor_input(interface, 'dirt_alpha', 0.0)
    materials._factor_input(interface, 'dirt_visibility', 0.0)
    for layer in TEAM_SLOTS:
        materials._group_input(interface, f'{layer.value}_color', 'NodeSocketColor',
                               (*(c ** 2.2 for c in DEFAULT_TEAM_COLOURS[layer.value]), 1.0))
    for layer in TEAM_SLOTS:
        materials._group_input(interface, f'{layer.value}_spec_color', 'NodeSocketColor',
                               (*DEFAULT_TEAM_SPECULARS[layer.value], 1.0))
    for layer in TEAM_SLOTS:
        materials._factor_input(interface, f'{layer.value}_gloss', DEFAULT_TEAM_GLOSS[layer.value])
    materials._group_input(interface, 'badge_color', 'NodeSocketColor', (0.0, 0.0, 0.0, 1.0))
    materials._factor_input(interface, 'badge_alpha', 0.0)
    materials._group_input(interface, 'banner_color', 'NodeSocketColor', (0.0, 0.0, 0.0, 1.0))
    materials._factor_input(interface, 'banner_alpha', 0.0)

    interface.new_socket(name='result', in_out='OUTPUT', socket_type='NodeSocketColor')
    interface.new_socket(name='specularity', in_out='OUTPUT', socket_type='NodeSocketColor')
    interface.new_socket(name='gloss', in_out='OUTPUT', socket_type='NodeSocketFloat')

    nodes = group.nodes
    links = group.links
    inp = nodes.new('NodeGroupInput')
    inp.location = -1100, 0
    out = nodes.new('NodeGroupOutput')
    out.location = 1100, 0

    # the paint math runs in gamma space, mirroring the game's shader; the
    # diffuse input is the raw (Non-Color) gamma-space sample
    pattern = nodes.new('ShaderNodeSeparateColor')
    pattern.location = -850, 150
    links.new(inp.outputs['pattern'], pattern.inputs['Color'])
    masks = [pattern.outputs[0], pattern.outputs[1], pattern.outputs[2], inp.outputs['pattern_alpha']]

    painted = inp.outputs['diffuse']
    for idx, layer in enumerate(TEAM_SLOTS):
        color = nodes.new('ShaderNodeGamma')
        color.inputs['Gamma'].default_value = 0.4545
        color.location = -650, 300 - 200 * idx
        links.new(inp.outputs[f'{layer.value}_color'], color.inputs['Color'])
        tinted = nodes.new('ShaderNodeMixRGB')
        tinted.blend_type = 'OVERLAY'
        tinted.use_clamp = True
        tinted.label = layer
        tinted.location = -400, 300 - 200 * idx
        links.new(masks[idx], tinted.inputs['Fac'])
        links.new(painted, tinted.inputs['Color1'])
        links.new(color.outputs[0], tinted.inputs['Color2'])
        painted = tinted.outputs[0]

    # badge then banner alpha-lerp over the painted diffuse, still in gamma space
    for name, x in (('badge', -150), ('banner', 50)):
        color = nodes.new('ShaderNodeGamma')
        color.inputs['Gamma'].default_value = 0.4545
        color.location = x, -250
        links.new(inp.outputs[f'{name}_color'], color.inputs['Color'])
        mixed = nodes.new('ShaderNodeMixRGB')
        mixed.blend_type = 'MIX'
        mixed.label = name
        mixed.location = x + 100, 100
        links.new(inp.outputs[f'{name}_alpha'], mixed.inputs['Fac'])
        links.new(painted, mixed.inputs['Color1'])
        links.new(color.outputs[0], mixed.inputs['Color2'])
        painted = mixed.outputs[0]

    to_linear = nodes.new('ShaderNodeGamma')
    to_linear.inputs['Gamma'].default_value = 2.2
    to_linear.location = 400, 100
    links.new(painted, to_linear.inputs['Color'])
    links.new(to_linear.outputs[0], out.inputs['result'])

    # team specular/gloss are additive, scaled down where dirt covers the paint
    # (dirt shows only as the engine ramps dirtVisibility, model default 0)
    dirt_amount = nodes.new('ShaderNodeMath')
    dirt_amount.operation = 'MULTIPLY'
    dirt_amount.label = 'Dirt amount'
    dirt_amount.location = -1000, -400
    links.new(inp.outputs['dirt_alpha'], dirt_amount.inputs[0])
    links.new(inp.outputs['dirt_visibility'], dirt_amount.inputs[1])
    dirt_keep = nodes.new('ShaderNodeMath')
    dirt_keep.operation = 'SUBTRACT'
    dirt_keep.inputs[0].default_value = 1.0
    dirt_keep.label = 'Dirt keep'
    dirt_keep.location = -850, -400
    links.new(dirt_amount.outputs[0], dirt_keep.inputs[1])

    spec_add = None
    gloss_add = None
    for idx, layer in enumerate(TEAM_SLOTS):
        term = nodes.new('ShaderNodeVectorMath')
        term.operation = 'SCALE'
        term.location = -650, -500 - 150 * idx
        links.new(inp.outputs[f'{layer.value}_spec_color'], term.inputs[0])
        links.new(masks[idx], term.inputs['Scale'])
        if spec_add is None:
            spec_add = term.outputs[0]
        else:
            acc = nodes.new('ShaderNodeVectorMath')
            acc.operation = 'ADD'
            acc.location = -450, -500 - 150 * idx
            links.new(spec_add, acc.inputs[0])
            links.new(term.outputs[0], acc.inputs[1])
            spec_add = acc.outputs[0]
        gterm = nodes.new('ShaderNodeMath')
        gterm.operation = 'MULTIPLY_ADD'
        gterm.location = -650, -1100 - 150 * idx
        links.new(inp.outputs[f'{layer.value}_gloss'], gterm.inputs[0])
        links.new(masks[idx], gterm.inputs[1])
        if gloss_add is None:
            gterm.inputs[2].default_value = 0.0
        else:
            links.new(gloss_add, gterm.inputs[2])
        gloss_add = gterm.outputs[0]

    spec_scaled = nodes.new('ShaderNodeVectorMath')
    spec_scaled.operation = 'SCALE'
    spec_scaled.location = -250, -500
    links.new(spec_add, spec_scaled.inputs[0])
    links.new(dirt_keep.outputs[0], spec_scaled.inputs['Scale'])
    spec_out = nodes.new('ShaderNodeVectorMath')
    spec_out.operation = 'ADD'
    spec_out.location = -50, -500
    links.new(inp.outputs['specularity'], spec_out.inputs[0])
    links.new(spec_scaled.outputs[0], spec_out.inputs[1])
    links.new(spec_out.outputs[0], out.inputs['specularity'])

    # gloss base is the spc alpha, srgb-decoded like the shader's pow 2.2
    gloss_linear = nodes.new('ShaderNodeMath')
    gloss_linear.operation = 'POWER'
    gloss_linear.inputs[1].default_value = 2.2
    gloss_linear.location = -250, -1100
    links.new(inp.outputs['gloss'], gloss_linear.inputs[0])
    gloss_out = nodes.new('ShaderNodeMath')
    gloss_out.operation = 'MULTIPLY_ADD'
    gloss_out.use_clamp = True  # the shader's mad_sat
    gloss_out.location = -50, -1100
    links.new(gloss_add, gloss_out.inputs[0])
    links.new(dirt_keep.outputs[0], gloss_out.inputs[1])
    links.new(gloss_linear.outputs[0], gloss_out.inputs[2])
    links.new(gloss_out.outputs[0], out.inputs['gloss'])
    return group


def create_shader_group():
    """The Dawn of War II surface, mirroring the dow2_unit pixel shader:

        final = albedo * diffuseLight + specColour * specLight
        specLight = sum((R.L)^n * lightCol * (n+2)/2) + envCube(R) * gloss * 8
        n = 200 * gloss + 10

    Only direct lights and the env cube feed specLight in the game - ambient SH
    is diffuse-only (test rigs should keep fill lights' specular_factor at 0).
    Albedo comes in already teamcoloured, dirt alpha-lerps over it in gamma space,
    occlusion multiplies it (game: ambient only). The specColour term maps to a
    Glossy BSDF the material adds to the principled one: Blender's microfacet BRDF
    carries the (n+2)/2 normalisation, world lighting stands in for the envmap
    term, and roughness = (2/(4n+2))^0.25 matches the Phong lobe width (Phong n
    = Blinn 4n = Beckmann alpha 2/(4n+2); the socket squares its input). Principled dielectric specular is turned off (specular_ior = 0) so
    the lobe isn't doubled. Emission is the emissive red x multiplier x 10,
    albedo-coloured."""
    group, created = materials._new_group(materials.MaterialType.DOW2.value, GROUP_VERSION)
    if not created:
        return group
    interface = group.interface

    materials._group_input(interface, 'diffuse', 'NodeSocketColor', (0.8, 0.8, 0.8, 1.0))
    materials._factor_input(interface, 'opacity', 1.0)
    materials._group_input(interface, 'specularity', 'NodeSocketColor', (0.0, 0.0, 0.0, 1.0))
    materials._factor_input(interface, 'gloss', 0.0)
    materials._group_input(interface, 'normal_map', 'NodeSocketColor', (0.5, 0.5, 1.0, 1.0))
    materials._factor_input(interface, 'occlusion', 1.0)
    materials._group_input(interface, 'dirt_overlay', 'NodeSocketColor', (0.0, 0.0, 0.0, 1.0))
    materials._factor_input(interface, 'dirt_alpha', 0.0)
    materials._group_input(interface, 'self_illumination_color', 'NodeSocketColor', (0.0, 0.0, 0.0, 1.0))
    materials._group_input(interface, 'emissive_multiplier', 'NodeSocketFloat', 0.0, min_value=0.0)
    materials._group_input(interface, 'pattern', 'NodeSocketColor', (0.0, 0.0, 0.0, 1.0))
    materials._group_input(interface, 'teamcolor', 'NodeSocketColor', (0.8, 0.8, 0.8, 1.0))
    materials._group_input(interface, 'teamcolor_spec', 'NodeSocketColor', (0.0, 0.0, 0.0, 1.0))
    for name, default in SCALAR_VARS:
        materials._factor_input(interface, name, default)

    for name, socket_type in [
        ('base_color', 'NodeSocketColor'),
        ('metallic', 'NodeSocketFloat'),
        ('roughness', 'NodeSocketFloat'),
        ('specular_ior', 'NodeSocketFloat'),
        ('spec_color', 'NodeSocketColor'),
        ('emission_color', 'NodeSocketColor'),
        ('emission_strength', 'NodeSocketFloat'),
        ('alpha', 'NodeSocketFloat'),
        ('normal', 'NodeSocketVector'),
    ]:
        interface.new_socket(name=name, in_out='OUTPUT', socket_type=socket_type)

    nodes = group.nodes
    links = group.links
    inp = nodes.new('NodeGroupInput')
    inp.location = -900, 0
    out = nodes.new('NodeGroupOutput')
    out.location = 700, 0

    # dirt alpha-lerps over the (team-painted) diffuse in gamma space; the dirt
    # image is a raw (Non-Color) gamma-space sample, the teamcolor input is
    # linear and re-encodes exactly (pow both ways)
    albedo_gamma = nodes.new('ShaderNodeGamma')
    albedo_gamma.inputs['Gamma'].default_value = 0.4545
    albedo_gamma.location = -650, 400
    links.new(inp.outputs['teamcolor'], albedo_gamma.inputs['Color'])
    # dirt only shows as the engine ramps dirtVisibility (model default 0 = clean)
    dirt_amount = nodes.new('ShaderNodeMath')
    dirt_amount.operation = 'MULTIPLY'
    dirt_amount.label = 'Dirt amount'
    dirt_amount.location = -650, 150
    links.new(inp.outputs['dirt_alpha'], dirt_amount.inputs[0])
    links.new(inp.outputs['dirt_visibility'], dirt_amount.inputs[1])
    dirtied = nodes.new('ShaderNodeMixRGB')
    dirtied.blend_type = 'MIX'
    dirtied.label = 'Dirt'
    dirtied.location = -450, 350
    links.new(dirt_amount.outputs[0], dirtied.inputs['Fac'])
    links.new(albedo_gamma.outputs[0], dirtied.inputs['Color1'])
    links.new(inp.outputs['dirt_overlay'], dirtied.inputs['Color2'])
    albedo_linear = nodes.new('ShaderNodeGamma')
    albedo_linear.inputs['Gamma'].default_value = 2.2
    albedo_linear.location = -250, 350
    links.new(dirtied.outputs[0], albedo_linear.inputs['Color'])

    # occlusion multiplies the albedo (game: ambient only)
    occluded = nodes.new('ShaderNodeVectorMath')
    occluded.operation = 'SCALE'
    occluded.label = 'Occlusion'
    occluded.location = -50, 350
    links.new(albedo_linear.outputs[0], occluded.inputs[0])
    links.new(inp.outputs['occlusion'], occluded.inputs['Scale'])

    links.new(occluded.outputs[0], out.inputs['base_color'])
    out.inputs['metallic'].default_value = 0.0
    out.inputs['specular_ior'].default_value = 0.0

    # the spec colour drives the added Glossy lobe; opacity kills it where the
    # surface is alpha-clipped away (the game discards the whole fragment)
    spec_color = nodes.new('ShaderNodeVectorMath')
    spec_color.operation = 'SCALE'
    spec_color.label = 'Spec x opacity'
    spec_color.location = 300, 300
    links.new(inp.outputs['teamcolor_spec'], spec_color.inputs[0])
    links.new(inp.outputs['opacity'], spec_color.inputs['Scale'])
    links.new(spec_color.outputs[0], out.inputs['spec_color'])

    # gloss (linear) -> Phong exponent p = 200*g + 10; a Phong lobe matches a
    # Blinn one of exponent 4p, so roughness = sqrt(2/(4p+2))
    power_exp = nodes.new('ShaderNodeMath')
    power_exp.operation = 'MULTIPLY_ADD'
    power_exp.inputs[1].default_value = 800.0
    power_exp.inputs[2].default_value = 42.0  # 4p + 2
    power_exp.location = -650, -550
    links.new(inp.outputs['gloss'], power_exp.inputs[0])
    lobe = nodes.new('ShaderNodeMath')
    lobe.operation = 'DIVIDE'
    lobe.inputs[0].default_value = 2.0
    lobe.location = -500, -550
    links.new(power_exp.outputs[0], lobe.inputs[1])
    # fourth root: sqrt gives the Beckmann alpha, but Blender BSDF roughness
    # sockets square their input (alpha = r^2), so the socket takes sqrt(alpha)
    roughness = nodes.new('ShaderNodeMath')
    roughness.operation = 'POWER'
    roughness.inputs[1].default_value = 0.25
    roughness.location = -350, -550
    links.new(lobe.outputs[0], roughness.inputs[0])
    links.new(roughness.outputs[0], out.inputs['roughness'])

    # emission: emissive red x multiplier x 10, coloured by the albedo
    emissive_red = nodes.new('ShaderNodeSeparateColor')
    emissive_red.location = -650, -750
    links.new(inp.outputs['self_illumination_color'], emissive_red.inputs['Color'])
    emissive_scaled = nodes.new('ShaderNodeMath')
    emissive_scaled.operation = 'MULTIPLY'
    emissive_scaled.location = -500, -750
    links.new(emissive_red.outputs[0], emissive_scaled.inputs[0])
    links.new(inp.outputs['emissive_multiplier'], emissive_scaled.inputs[1])
    emissive_boost = nodes.new('ShaderNodeMath')
    emissive_boost.operation = 'MULTIPLY'
    emissive_boost.inputs[1].default_value = 10.0
    emissive_boost.location = -350, -750
    links.new(emissive_scaled.outputs[0], emissive_boost.inputs[0])
    links.new(emissive_boost.outputs[0], out.inputs['emission_strength'])
    links.new(albedo_linear.outputs[0], out.inputs['emission_color'])

    # the game only honours dif alpha when alphaTest is on (or the _alpha blend
    # variant, which setup forces to 1); otherwise the draw is opaque
    alpha_gate = nodes.new('ShaderNodeMix')
    alpha_gate.data_type = 'FLOAT'
    alpha_gate.label = 'Alpha test gate'
    alpha_gate.location = 400, -200
    alpha_gate.inputs[2].default_value = 1.0  # A: opaque
    links.new(inp.outputs['alpha_test'], alpha_gate.inputs['Factor'])
    links.new(inp.outputs['opacity'], alpha_gate.inputs[3])
    links.new(alpha_gate.outputs[0], out.inputs['alpha'])

    normal = nodes.new('ShaderNodeNormalMap')
    normal.location = -250, -900
    links.new(inp.outputs['normal_map'], normal.inputs['Color'])
    links.new(normal.outputs[0], out.inputs['normal'])
    return group


def setup_material(mat, material_data: materials.MaterialInfo, force_teamcolor: bool):
    links = mat.node_tree.links
    shader = mat.get('dow2_shader') or ''
    # the _alpha shader variant alpha-blends instead of alpha-testing
    is_alpha_blend = shader.endswith('_alpha')
    if is_alpha_blend:
        mat.surface_render_method = 'BLENDED'
    # the _2uv variant samples diffuse/spec/normal from the second uv set
    # (team/occlusion/emissive stay on uv0) and has no dirt and no team spec
    is_2uv = shader.endswith('_2uv')
    node_final = mat.node_tree.nodes[0]
    node_final.location = 300, 400
    mat.node_tree.nodes[1].location = node_final.location[0] + 400, node_final.location[1]

    group_node = mat.node_tree.nodes.new('ShaderNodeGroup')
    group_node.node_tree = create_shader_group()
    group_node.location = node_final.location[0] - 300, node_final.location[1]
    materials.link_shader_group_to_bsdf(mat, group_node, node_final)

    # the game's specLight term: a Glossy lobe added on top of the diffuse surface
    node_output = mat.node_tree.nodes[1]
    node_glossy = mat.node_tree.nodes.new('ShaderNodeBsdfGlossy')
    node_glossy.location = node_final.location[0], node_final.location[1] - 700
    links.new(group_node.outputs['spec_color'], node_glossy.inputs['Color'])
    links.new(group_node.outputs['roughness'], node_glossy.inputs['Roughness'])
    links.new(group_node.outputs['normal'], node_glossy.inputs['Normal'])
    node_add = mat.node_tree.nodes.new('ShaderNodeAddShader')
    node_add.location = node_output.location[0] + 200, node_output.location[1]
    node_output.location[0] += 400
    links.new(node_final.outputs[0], node_add.inputs[0])
    links.new(node_glossy.outputs[0], node_add.inputs[1])
    links.new(node_add.outputs[0], node_output.inputs['Surface'])

    group_node.inputs['emissive_multiplier'].default_value = material_data.default_emission_strength
    if is_alpha_blend:
        group_node.inputs['alpha_test'].default_value = 1.0  # blending always uses alpha
    if material_data.default_color:
        group_node.inputs['diffuse'].default_value[:3] = material_data.default_color.from_srgb_to_scene_linear()

    uv_vector = materials.setup_uv_offset(mat, node_final.location[1] - 1600, node_final.location[1] - 200)
    uv2_vector = uv_vector
    if is_2uv:
        node_uv2 = mat.node_tree.nodes.new('ShaderNodeUVMap')
        node_uv2.uv_map = 'UVMap.001'
        node_uv2.location = -700, node_final.location[1] + 150
        uv2_vector = node_uv2.outputs[0]

    def new_tex(layer, image, y_index, vector=None):
        node_tex = mat.node_tree.nodes.new('ShaderNodeTexImage')
        node_tex.image = image
        node_tex.location = -430, node_final.location[1] - 320 * y_index
        node_tex.label = layer
        links.new(vector if vector is not None else uv_vector, node_tex.inputs['Vector'])
        return node_tex

    channel = material_data.channel_images
    num_tex = 0
    node_diffuse = node_spec = node_dirt = node_pattern = None
    if (image := channel.get(materials.MaterialLayers.DIFFUSE)) is not None:
        node_diffuse = new_tex(materials.MaterialLayers.DIFFUSE, image, num_tex, uv2_vector)
        num_tex += 1
        links.new(node_diffuse.outputs[0], group_node.inputs['diffuse'])
        links.new(node_diffuse.outputs['Alpha'], group_node.inputs['opacity'])
    if (image := channel.get(materials.MaterialLayers.SPECULAR_MASK)) is not None:
        node_spec = new_tex(materials.MaterialLayers.SPECULAR_MASK, image, num_tex, uv2_vector)
        num_tex += 1
        links.new(node_spec.outputs[0], group_node.inputs['specularity'])
    if (image := channel.get(materials.MaterialLayers.NORMAL_MAP)) is not None:
        node_normal = new_tex(materials.MaterialLayers.NORMAL_MAP, image, num_tex, uv2_vector)
        num_tex += 1
        links.new(node_normal.outputs[0], group_node.inputs['normal_map'])
    if (image := channel.get(materials.MaterialLayers.OCCLUSION)) is not None:
        node_occ = new_tex(materials.MaterialLayers.OCCLUSION, image, num_tex)
        num_tex += 1
        links.new(node_occ.outputs[0], group_node.inputs['occlusion'])
    if (image := channel.get(materials.MaterialLayers.DIRT)) is not None:
        node_dirt = new_tex(materials.MaterialLayers.DIRT, image, num_tex)
        num_tex += 1
        links.new(node_dirt.outputs[0], group_node.inputs['dirt_overlay'])
        links.new(node_dirt.outputs['Alpha'], group_node.inputs['dirt_alpha'])
    if (image := channel.get(materials.MaterialLayers.SELF_ILLUMUNATION_COLOR)) is not None:
        node_emissive = new_tex(materials.MaterialLayers.SELF_ILLUMUNATION_COLOR, image, num_tex)
        num_tex += 1
        links.new(node_emissive.outputs[0], group_node.inputs['self_illumination_color'])
    if force_teamcolor and (image := channel.get(materials.MaterialLayers.PATTERN)) is not None:
        node_pattern = new_tex(materials.MaterialLayers.PATTERN, image, num_tex)
        num_tex += 1

    # the shader reads albedo/spec/gloss from the teamcolor inputs; the plain
    # textures feed them when no teamcolor chain exists (replaced below); the
    # raw gamma-space diffuse decodes ^2.2 to the linear teamcolor convention
    if node_diffuse is not None:
        dif_linear = mat.node_tree.nodes.new('ShaderNodeGamma')
        dif_linear.inputs['Gamma'].default_value = 2.2
        dif_linear.label = 'Diffuse decode'
        dif_linear.location = -100, node_final.location[1] + 150
        links.new(node_diffuse.outputs[0], dif_linear.inputs['Color'])
        links.new(dif_linear.outputs[0], group_node.inputs['teamcolor'])
    else:
        group_node.inputs['teamcolor'].default_value = tuple(group_node.inputs['diffuse'].default_value)
    if node_spec is not None:
        links.new(node_spec.outputs[0], group_node.inputs['teamcolor_spec'])
        gloss_linear = mat.node_tree.nodes.new('ShaderNodeMath')
        gloss_linear.operation = 'POWER'
        gloss_linear.inputs[1].default_value = 2.2
        gloss_linear.label = 'Gloss decode'
        gloss_linear.location = -100, node_final.location[1] - 320 * num_tex
        links.new(node_spec.outputs['Alpha'], gloss_linear.inputs[0])
        links.new(gloss_linear.outputs[0], group_node.inputs['gloss'])

    if node_pattern is not None and node_diffuse is not None:
        teamcolor_node = mat.node_tree.nodes.new('ShaderNodeGroup')
        teamcolor_node.node_tree = create_teamcolor_group()
        teamcolor_node.location = group_node.location[0] - 250, group_node.location[1] + 500
        if is_2uv:
            # the 2uv shader's paint loop reads only the colour slots
            for layer in TEAM_SLOTS:
                teamcolor_node.inputs[f'{layer.value}_spec_color'].default_value = (0.0, 0.0, 0.0, 1.0)
                teamcolor_node.inputs[f'{layer.value}_gloss'].default_value = 0.0
        links.new(node_diffuse.outputs[0], teamcolor_node.inputs['diffuse'])
        links.new(node_pattern.outputs[0], teamcolor_node.inputs['pattern'])
        links.new(node_pattern.outputs['Alpha'], teamcolor_node.inputs['pattern_alpha'])
        links.new(teamcolor_node.outputs['result'], group_node.inputs['teamcolor'])
        if node_spec is not None:
            links.new(node_spec.outputs[0], teamcolor_node.inputs['specularity'])
            links.new(node_spec.outputs['Alpha'], teamcolor_node.inputs['gloss'])
            links.new(teamcolor_node.outputs['specularity'], group_node.inputs['teamcolor_spec'])
            links.new(teamcolor_node.outputs['gloss'], group_node.inputs['gloss'])
        if node_dirt is not None:
            links.new(node_dirt.outputs['Alpha'], teamcolor_node.inputs['dirt_alpha'])
        _setup_badge_nodes(mat, material_data, teamcolor_node, uv_vector)


def _setup_badge_nodes(mat, material_data: materials.MaterialInfo, teamcolor_node, uv_vector):
    """Badge/banner image slots placed by the material's decoded matrix data,
    sampling uv0 through a Flip + position/size Mapping (dow2 convention)."""
    links = mat.node_tree.links
    placehoder_image = materials.create_placeholder_image()
    common_x, common_y = teamcolor_node.location[0] - 900, teamcolor_node.location[1] + 700

    img_size_node = mat.node_tree.nodes.new('ShaderNodeCombineXYZ')
    diffuse_img = material_data.channel_images.get(materials.MaterialLayers.DIFFUSE)
    if diffuse_img is not None:
        img_size_node.inputs['X'].default_value = diffuse_img.size[0]
        img_size_node.inputs['Y'].default_value = diffuse_img.size[1]
    img_size_node.label = 'color_layer_size'
    img_size_node.location = common_x - 300, common_y + 200

    flip_texture_node = mat.node_tree.nodes.new('ShaderNodeMapping')
    flip_texture_node.label = 'Flip'
    flip_texture_node.location = common_x - 450, common_y + 200
    flip_texture_node.inputs['Location'].default_value = (0, 1, 0)
    flip_texture_node.inputs['Scale'].default_value = (1, -1, 1)
    links.new(uv_vector, flip_texture_node.inputs['Vector'])

    for idx, (layer_name, position, size) in enumerate([
        ('badge', material_data.badge_position, material_data.badge_size),
        ('banner', material_data.banner_position, material_data.banner_size),
    ]):
        if position is None:
            node_name = f'UNUSED_{layer_name}'
            position = 0, 0
            default_image = placehoder_image
        else:
            node_name = layer_name
            default_image = None
        node_pos_x, node_pos_y = common_x, common_y - 290 * idx
        data_pos_node = mat.node_tree.nodes.new('ShaderNodeCombineXYZ')
        data_pos_node.inputs['X'].default_value = position[0]
        data_pos_node.inputs['Y'].default_value = position[1]
        data_pos_node.location = node_pos_x - 300, node_pos_y
        data_pos_node.label = f'{layer_name}_position'

        data_size_node = mat.node_tree.nodes.new('ShaderNodeCombineXYZ')
        data_size_node.inputs['X'].default_value = size[0]
        data_size_node.inputs['Y'].default_value = size[1]
        data_size_node.location = node_pos_x - 300, node_pos_y - 150
        data_size_node.label = f'{layer_name}_display_size'

        calc_pos_node = mat.node_tree.nodes.new('ShaderNodeVectorMath')
        calc_pos_node.operation = 'DIVIDE'
        calc_pos_node.location = node_pos_x - 150, node_pos_y
        links.new(data_pos_node.outputs[0], calc_pos_node.inputs[0])
        links.new(img_size_node.outputs[0], calc_pos_node.inputs[1])

        calc_scale_node = mat.node_tree.nodes.new('ShaderNodeVectorMath')
        calc_scale_node.operation = 'DIVIDE'
        calc_scale_node.location = node_pos_x - 150, node_pos_y - 150
        links.new(data_size_node.outputs[0], calc_scale_node.inputs[0])
        links.new(img_size_node.outputs[0], calc_scale_node.inputs[1])

        scale_node = mat.node_tree.nodes.new('ShaderNodeMapping')
        scale_node.vector_type = 'TEXTURE'
        scale_node.location = node_pos_x, node_pos_y
        scale_node.inputs['Rotation'].default_value = (0, 0, getattr(material_data, f'{layer_name}_rotation'))
        links.new(flip_texture_node.outputs[0], scale_node.inputs['Vector'])
        links.new(calc_pos_node.outputs[0], scale_node.inputs['Location'])
        links.new(calc_scale_node.outputs[0], scale_node.inputs['Scale'])

        node_tex = mat.node_tree.nodes.new('ShaderNodeTexImage')
        node_tex.extension = 'CLIP'
        node_tex.location = node_pos_x + 200, node_pos_y
        node_tex.label = node_name
        if default_image is not None:
            node_tex.image = default_image
            node_tex.hide = True
        links.new(scale_node.outputs[0], node_tex.inputs['Vector'])
        links.new(node_tex.outputs[0], teamcolor_node.inputs[f'{layer_name}_color'])
        links.new(node_tex.outputs[1], teamcolor_node.inputs[f'{layer_name}_alpha'])
