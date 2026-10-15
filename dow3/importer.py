import functools
import io
import math
import re
import pathlib
import tempfile

import bpy
import mathutils
import numpy
from PIL import Image as PilImage, ImageChops

from .attrib import AttribDb
from . import rgm, rgt
from . import materials as dow3_materials
from .. import havok, materials, mesh, utils
from ..dow2 import importer as dow2_importer
from ..importing import CONV, CONV_INV, bake_teamcolor_layers

# suffixes of the shader texture-path params, in materials.MaterialLayers terms
TEXTURE_VARS = ('diffusetex', 'normalmap', 'speculartex', 'glosstex', 'patterntex', 'alphatex', 'badge1tex', 'badge2tex')

# ptn masks carry the army-painter colors in RGB; the inverted alpha is the fourth
# (tertiary) paint mask, mapped onto the DoW1 tree's free EYES slot
PTN_TEAM_CHANNELS = (
    materials.TeamcolorLayers.PRIMARY,
    materials.TeamcolorLayers.SECONDARY,
    materials.TeamcolorLayers.TRIM,
    materials.TeamcolorLayers.EYES,
)

LUMA = numpy.array([0.2126, 0.7152, 0.0722])


def _to_array(pil_image) -> numpy.ndarray:
    return numpy.asarray(pil_image, dtype=numpy.float32) / 255


def _to_image(array: numpy.ndarray, mode='RGB') -> PilImage:
    return PilImage.fromarray((numpy.clip(array, 0, 1) * 255 + 0.5).astype(numpy.uint8), mode)


def _overlay(base: numpy.ndarray, color, mask: numpy.ndarray) -> numpy.ndarray:
    """The game's paint blend: gamma-space overlay of a colour, lerped by a mask"""
    color = numpy.asarray(color, dtype=numpy.float32)
    blended = numpy.where(base < 0.5, 2 * base * color, 1 - 2 * (1 - base) * (1 - color))
    return base + mask[..., None] * (numpy.clip(blended, 0, 1) - base)


def conv_matrix(values) -> mathutils.Matrix:
    """12 floats stored as 4x3 column triplets -> Blender-frame matrix (dow2 layout)"""
    v = values
    m = mathutils.Matrix((
        (v[0], v[3], v[6], v[9]),
        (v[1], v[4], v[7], v[10]),
        (v[2], v[5], v[8], v[11]),
        (0, 0, 0, 1),
    ))
    return CONV @ m @ CONV_INV


class ModelLoader(dow2_importer.ModelLoader):
    def __init__(self, layout, dow1_materials: bool = False, **kwargs):
        """dow1_materials bakes the DoW3 shading into the stock DoW1 material tree"""
        super().__init__(layout, **kwargs)
        self.dow1_materials = dow1_materials
        self.badge_image = None
        self.decal_image = None
        # the import paint scheme (AttribDb.paint_scheme_info); the dow1-compat bake
        # tints the spec map with its paint specular colours statically
        self.scheme_info = None
        # badge-shader materials awaiting the DoW1-compatible badge/decal texture bake
        self._dow1_badge_jobs = {}

    def find_model(self, art_path: str):
        posix_path = pathlib.PureWindowsPath(art_path).as_posix()
        if not posix_path.lower().startswith('art/'):
            posix_path = f'art/{posix_path}'
        return self.layout.find(f'{posix_path}.rgm')

    def load_unit(self, unit_path: str, parts: list[str] = None):
        if self.attrib_db is None:
            self.attrib_db = AttribDb.load(self.layout)
        if self.attrib_db is None:
            self.messages.append(('ERROR', 'No attribute database found in the environment'))
            return
        animator = self.attrib_db.unit_animator(unit_path)
        root_model = self.find_model(animator) if animator else None
        if root_model is None:
            self.messages.append(('ERROR', f'Cannot find model "{animator}"'))
            return
        collections = [self.load_file(root_model)]
        loaded = {str(root_model)}
        if parts is None:
            parts = [p.art_path for p in self.attrib_db.unit_parts(unit_path)]
        for art_path in parts:
            part_model = self.find_model(art_path)
            if part_model is None:
                self.messages.append(('WARNING', f'Cannot find part model "{art_path}"'))
                continue
            if str(part_model) in loaded:
                continue
            loaded.add(str(part_model))
            collections.append(self.load_file(part_model))
        if not any(o.type == 'MESH' for c in collections for o in c.all_objects):
            self.messages.append(('INFO', f'"{unit_path}" has no geometry'))
        if self.dow1_materials:
            self.bake_badge_decal()

    def unit_model(self, unit_path: str):
        if self.attrib_db is None:
            self.attrib_db = AttribDb.load(self.layout)
        animator = self.attrib_db.unit_animator(unit_path) if self.attrib_db else None
        return self.find_model(animator) if animator else None

    def load_file(self, path):
        model = rgm.parse_rgm(path.read_bytes())
        if self.root_collection is None:
            self.root_collection = bpy.data.collections.new(path.stem)
            self.bpy_context.scene.collection.children.link(self.root_collection)
        collection = self.root_collection

        if self.armature_obj is None:
            self.armature = bpy.data.armatures.new('Armature')
            self.armature_obj = bpy.data.objects.new('Armature', self.armature)
            self.armature_obj.show_in_front = True
            collection.objects.link(self.armature_obj)

        if model.skeleton:
            self.create_skeleton([(b.name, b.parent, conv_matrix(b.matrix)) for b in model.skeleton])
            for partition, first, count in model.partitions:
                bone_collection = self.armature.collections.get(partition) or self.armature.collections.new(partition)
                for bone in model.skeleton[first:first + count]:
                    actual = self.bone_case.get(bone.name.lower())
                    if actual and actual in self.armature.bones:
                        bone_collection.assign(self.armature.bones[actual])
        created_materials = {name: self.create_material(name, material)
                             for name, material in model.materials.items()}
        has_lods = any(k.startswith('lodselectorsdef_lod_') for k in model.attributes)
        for geometry in model.geometries:
            # a model with several mesh groups keeps each in its own collection: the
            # extra groups hold state variants (wrecks, damage) toggled by the game
            geometry_collection = collection
            if len(model.geometries) > 1:
                geometry_collection = self.get_collection(geometry.material or geometry.name, collection)
            self.create_rgm_mesh(geometry, created_materials, geometry_collection, path.stem, has_lods)
        if self.import_markers and model.markers:
            self.create_markers([(m.name, m.parent, conv_matrix(m.matrix), m.params) for m in model.markers])
        if model.cameras:
            self.create_camera_objects([
                (camera.name, camera.position, camera.rotation, math.radians(camera.fov), camera.clip_start, camera.clip_end)
                for camera in model.cameras])
        # lod selector attributes are represented by the lod collections; the rest
        # are engine metadata with no scene counterpart
        for key, value in model.attributes.items():
            if not key.startswith('lodselectorsdef'):
                collection[f'attribute_{key}'] = list(value) if isinstance(value, tuple) else value
        # the base collection holds the level-0 geometry; the lower-detail levels
        # (marked by their 'lod_level' property) stay hidden by default
        for lod_col in collection.children_recursive:
            if lod_col.get('lod_level'):
                lod_col.hide_render = lod_col.hide_viewport = True
        return collection

    def load_skeleton(self, path):
        """Merge only the skeleton of a part model into the armature"""
        model = rgm.parse_rgm(path.read_bytes())
        if model.skeleton:
            self.create_skeleton([(b.name, b.parent, conv_matrix(b.matrix)) for b in model.skeleton])

    def create_rgm_mesh(self, geometry, created_materials: dict, collection, model_name: str, has_lods: bool):
        skin_bones = []
        rebind = []
        for skin_bone in geometry.skin_bones:
            actual = self.bone_case.get(skin_bone.name.lower(), skin_bone.name)
            skin_bones.append(actual)
            world = self.bone_world.get(actual)
            invbind = conv_matrix(skin_bone.inverse_bind)
            rebind.append(world @ invbind if world is not None else mathutils.Matrix.Identity(4))

        def is_identity(matrix):
            delta = matrix - mathutils.Matrix.Identity(4)
            return all(abs(value) < 1e-4 for row in delta for value in row)

        needs_rebind = not all(is_identity(matrix) for matrix in rebind)

        material = created_materials.get(geometry.material)
        if material is None and geometry.material:
            self.messages.append(('WARNING', f'Mesh "{geometry.name}": unknown material "{geometry.material}"'))

        for submesh in geometry.submeshes:
            # models that declare LOD selectors (lodselectorsdef) carry their lower
            # detail levels as extra submeshes; the level lives only in the mesh name
            lod_match = has_lods and re.search(r'(?:^|_)lod0*(\d+)', submesh.name)
            lod_level = int(lod_match.group(1)) if lod_match else 0
            if lod_level > 0 and not self.import_lods:
                continue
            submesh_collection = collection if lod_level == 0 else self.get_collection(f'lod{lod_level}', collection)
            if lod_level > 0:
                submesh_collection['lod_level'] = lod_level
            vertices = [(-x, -z, y) for x, y, z in submesh.positions]
            normals = [mathutils.Vector((-x, -z, y)).normalized() for x, y, z in submesh.normals] or None
            faces = [(a, c, b) for a, b, c in submesh.triangles]
            uv_layers = [[(u, 1 - v) for u, v in uv_set] for uv_set in submesh.uv_sets]
            colors = [(r / 255, g / 255, b / 255, a / 255) for r, g, b, a in submesh.colours] or None

            def influences(vert_skin):
                total = sum(weight for _, weight in vert_skin)
                if not total:
                    return []
                return [(idx, weight / total) for idx, weight in vert_skin if idx < len(skin_bones)]

            if needs_rebind:
                # a part skinned in its own file's bind pose, e.g. a weapon authored around its grip bone
                for vert_idx, vert_skin in enumerate(submesh.skin):
                    matrix = mathutils.Matrix.Diagonal((0, 0, 0, 0))
                    for idx, weight in influences(vert_skin):
                        matrix = matrix + rebind[idx] * weight
                    vertices[vert_idx] = (matrix @ mathutils.Vector(vertices[vert_idx])).to_tuple()
                    if normals is not None:
                        normals[vert_idx] = (matrix.to_3x3() @ normals[vert_idx]).normalized()

            skin_vert_array = None
            if geometry.skin_bones:
                skin_vert_array = []
                for vert_skin in submesh.skin:
                    vert_influences = influences(vert_skin)
                    skin_vert_array.append((
                        tuple(skin_bones[idx] for idx, _ in vert_influences),
                        mathutils.Vector([weight for _, weight in vert_influences] + [0] * (4 - len(vert_influences))),
                    ))

            mesh_data = mesh.MeshData(vertices, faces, skin_vert_array, color_array=colors)
            if self.enable_vertex_automerge and normals is not None:
                mesh_data = mesh.merge(mesh_data, normals, uv_layers, position_threshold=self.vertex_position_merge_threshold)
            else:
                mesh_data = mesh.build_corners(mesh_data, normals, uv_layers)

            obj = mesh.create_mesh(
                mesh_data, [] if material is None else [material], submesh.name or model_name,
                armature_obj=self.armature_obj,
                rigid_bone=None,
                messages=self.messages,
                # the badge samples through the second uv set, the squad decal through the third
                uv_names=['UVMap', dow3_materials.BADGE_UV_LAYER, dow3_materials.DECAL_UV_LAYER],
            )
            submesh_collection.objects.link(obj)

    def create_material(self, name: str, material: rgm.Material):
        if name in self.created_materials:
            return self.created_materials[name]
        mat = bpy.data.materials.new(name)
        mat['dow3_shader'] = material.shader
        for var in TEXTURE_VARS:
            if var in material.params:
                mat[var] = material.params[var]
        mat.use_nodes = True

        def load(var, colorspace='sRGB'):
            image = self.load_image(material.params.get(var))
            if image is not None and colorspace != 'sRGB':
                image.colorspace_settings.name = colorspace
            return image

        material_data = materials.MaterialInfo(roughness_value=0.5)
        # the game samples spc RGB through an srgb view (its shader hand-converts
        # only the alpha channel), so the spec map decodes like a colour texture
        for layer, var, colorspace in (
            (materials.MaterialLayers.DIFFUSE, 'diffusetex', 'sRGB'),
            (materials.MaterialLayers.SPECULAR_MASK, 'speculartex', 'sRGB'),
            (materials.MaterialLayers.GLOSS, 'glosstex', 'Non-Color'),
            (materials.MaterialLayers.NORMAL_MAP, 'normalmap', 'Non-Color'),
            (materials.MaterialLayers.OPACITY, 'alphatex', 'Non-Color'),
        ):
            if (image := load(var, colorspace)) is not None:
                material_data.channel_images[layer] = image
        diffuse = material_data.channel_images.get(materials.MaterialLayers.DIFFUSE)
        specular = material_data.channel_images.get(materials.MaterialLayers.SPECULAR_MASK)
        spec_has_gloss = False
        if specular is not None:
            spc_pil = PilImage.open(io.BytesIO(specular.packed_file.data))
            spec_has_gloss = 'A' in spc_pil.getbands()
        team = None
        if self.teamcolor_scheme and (pattern_path := material.params.get('patterntex')):
            team = self.load_image(pattern_path)
        force_teamcolor = diffuse is not None and team is not None
        if self.dow1_materials:
            if (normal := material_data.channel_images.get(materials.MaterialLayers.NORMAL_MAP)) is not None:
                # the game's emissive mask rides in the normal map's spare channel,
                # kept in the reconstructed image's alpha; emission = mask * factor
                # (the DoW1 strength socket tops out at 1 vs the game's 4)
                nrm_pil = PilImage.open(io.BytesIO(normal.packed_file.data))
                if 'A' in nrm_pil.getbands():
                    factor = min(max(float(material.params.get('emissive_factor', 1.0)), 1.0), 4.0)
                    emissive = numpy.asarray(nrm_pil.getchannel('A'), dtype=numpy.float32) / 255 * factor
                    if emissive.any():
                        material_data.channel_images[materials.MaterialLayers.SELF_ILLUMUNATION_MASK] = self.bake_channel_image(
                            f'{name}_ems', _to_image(emissive, 'L'))
            # Mirror the native projection: F0 = the paint-tinted spec map decoded
            # from srgb; where the F0 beyond the 0.08 cap outweighs the albedo the
            # excess folds into the baked diffuse (base = albedo + m*(1-0.08/F0)*F0,
            # no metallic - keeps the game's always-on diffuse term). The layer bake
            # reuses the baked/original diffuse ratio (halved: the teamcolor overlay's
            # sub-pivot branch is 2*value*color).
            detail_multiplier = None
            if specular is not None:
                spc_rgba = PilImage.open(io.BytesIO(specular.packed_file.data))
                scheme_gloss = self.scheme_info.get('gloss', {}) if self.scheme_info else {}
                if 'A' in spc_rgba.getbands():
                    # spc alpha is the srgb-encoded gloss; the paints overlay it
                    # in gamma space, then bake the DoW1 tree's 1-roughness with
                    # the game's Blinn lobe width: roughness = sqrt(2 / (2^(12g-1) + 2))
                    gloss_gamma = numpy.asarray(spc_rgba.getchannel('A'), dtype=numpy.float32) / 255
                    if team is not None and scheme_gloss:
                        ptn_g = PilImage.open(io.BytesIO(team.packed_file.data)).convert('RGBA')
                        if ptn_g.size != spc_rgba.size:
                            ptn_g = ptn_g.resize(spc_rgba.size, PilImage.NEAREST)
                        ptn_g = _to_array(ptn_g)
                        g_masks = [ptn_g[..., 0], ptn_g[..., 1], ptn_g[..., 2], 1 - ptn_g[..., 3]]
                        for layer, mask in zip(('primary', 'secondary', 'trim', 'tertiary'), g_masks):
                            if layer in scheme_gloss:
                                p = scheme_gloss[layer]
                                over = numpy.where(gloss_gamma < 0.5,
                                                   2 * gloss_gamma * p,
                                                   1 - 2 * (1 - gloss_gamma) * (1 - p))
                                gloss_gamma = gloss_gamma + mask * (numpy.clip(over, 0, 1) - gloss_gamma)
                    gloss_linear = gloss_gamma ** 2.2
                    roughness = numpy.sqrt(2 / (2 ** (12 * gloss_linear - 1) + 2))
                    if materials.MaterialLayers.GLOSS not in material_data.channel_images:
                        material_data.channel_images[materials.MaterialLayers.GLOSS] = self.bake_channel_image(
                            f'{name}_gls', _to_image(1 - roughness, 'L'))
                    material_data.roughness_value = float(roughness.mean())
                if diffuse is not None:
                    dif_pil = PilImage.open(io.BytesIO(diffuse.packed_file.data)).convert('RGB')
                    spc_pil = spc_rgba.convert('RGB')
                    if spc_pil.size != dif_pil.size:
                        spc_pil = spc_pil.resize(dif_pil.size, PilImage.NEAREST)
                    spc_gamma = _to_array(spc_pil)
                    spec_colors = self.scheme_info.get('colors_spec', {}) if self.scheme_info else {}
                    masks = None
                    if team is not None:
                        ptn = PilImage.open(io.BytesIO(team.packed_file.data)).convert('RGBA')
                        if ptn.size != dif_pil.size:
                            ptn = ptn.resize(dif_pil.size, PilImage.NEAREST)
                        ptn = _to_array(ptn)
                        masks = [ptn[..., 0], ptn[..., 1], ptn[..., 2], 1 - ptn[..., 3]]
                        for layer, mask in zip(('primary', 'secondary', 'trim', 'tertiary'), masks):
                            if layer in spec_colors:
                                spc_gamma = _overlay(spc_gamma, spec_colors[layer], mask)
                    f0 = spc_gamma ** 2.2
                    f = f0.max(axis=-1)
                    albedo = _to_array(dif_pil) ** 2.2
                    spec_wins = numpy.clip((f - 0.08 - 2 * albedo.max(axis=-1)) / 0.05, 0, 1)
                    excess = spec_wins * numpy.clip(1 - 0.08 / numpy.maximum(f, 1e-3), 0, 1)
                    base_t = numpy.clip(albedo + f0 * excess[..., None], 0, 1)
                    # bake the tinted F0 COLOUR into the DoW1 spec channel (the tree's
                    # spec is RGB: base = albedo + 0.5*S' and its metallic accept
                    # colour) so gold trim keeps its coloured sheen; the strength is
                    # limited to what the base can repay so the albedo solve below
                    # never clips a channel
                    repay = numpy.minimum(
                        1.0, 2 * base_t.min(axis=-1) / numpy.maximum(f, 1e-3))
                    spec_baked = f0 * repay[..., None]
                    albedo_baked = numpy.clip(base_t - 0.5 * spec_baked, 0, 1)
                    material_data.channel_images[materials.MaterialLayers.SPECULAR_MASK] = self.bake_channel_image(
                        f'{name}_spc', _to_image(spec_baked), colorspace='Non-Color')
                    dif_baked_gamma = albedo_baked ** (1 / 2.2)
                    ratio = (dif_baked_gamma @ LUMA) / numpy.maximum(_to_array(dif_pil) @ LUMA, 1e-3)
                    detail_multiplier = _to_image(numpy.clip(ratio / 2, 0, 1), 'L')
                    diffuse = self.bake_channel_image(f'{name}_dif', _to_image(dif_baked_gamma), colorspace='sRGB')
            if force_teamcolor:
                material_data.teamcolor_images = bake_teamcolor_layers(
                    name, team, material_data.channel_images[materials.MaterialLayers.DIFFUSE],
                    channel_layers=PTN_TEAM_CHANNELS, detail_scale=2.0,
                    detail_multiplier=detail_multiplier or PilImage.new('L', (1, 1), 128),
                    invert_alpha=True)
                material_data.teamcolor_images[materials.TeamcolorLayers.DEFAULT] = diffuse
                if 'badge1tex' in material.params and (self.badge_image is not None or self.decal_image is not None):
                    self._dow1_badge_jobs[name] = {
                        'material': mat,
                        'team_image': team,
                        'diffuse_orig_image': material_data.channel_images[materials.MaterialLayers.DIFFUSE],
                        'diffuse_final_image': diffuse,
                        'detail_multiplier': detail_multiplier,
                        'layer_images': material_data.teamcolor_images,
                    }
            if diffuse is not None:
                material_data.channel_images[materials.MaterialLayers.DIFFUSE] = diffuse
            materials.setup_material(mat, material_data, force_teamcolor)
        else:
            material_data.material_type = materials.MaterialType.DOW3
            if spec_has_gloss and materials.MaterialLayers.GLOSS not in material_data.channel_images:
                # the gloss map lives in the spc alpha channel; aliasing the slot to the
                # spec image makes the tree sample its alpha instead of a separate file
                material_data.channel_images[materials.MaterialLayers.GLOSS] = specular
            if team is not None:
                team.colorspace_settings.name = 'Non-Color'
                material_data.channel_images[materials.MaterialLayers.PATTERN] = team
            if 'badge1tex' in material.params:
                if self.badge_image is not None:
                    material_data.teamcolor_images[materials.TeamcolorLayers.BADGE] = self.badge_image
                if self.decal_image is not None:
                    material_data.teamcolor_images[materials.TeamcolorLayers.DECAL] = self.decal_image
                if (self.badge_image is not None or self.decal_image is not None) and not force_teamcolor and self.teamcolor_scheme:
                    self.messages.append(('WARNING', f'Material "{name}": badge material without a diffuse+pattern pair; badge not applied'))
            dow3_materials.setup_material(mat, material_data, force_teamcolor)
            group_node = materials.get_shader_group_node(mat)
            if 'emissive_factor' in material.params and 'emissive_factor' in group_node.inputs:
                group_node.inputs['emissive_factor'].default_value = float(material.params['emissive_factor'])
            offset_node = materials.get_uv_offset_node(mat)
            if offset_node is not None:
                offset_node.inputs['Location'].default_value[0] = material.params.get('diffuse_offsetu', 0.0)
                offset_node.inputs['Location'].default_value[1] = -material.params.get('diffuse_offsetv', 0.0)
        self.created_materials[name] = mat
        return mat

    def bake_badge_decal(self):
        """Reproject the badge stencil into the matching teamcolor mask layers and
        the decal into diffuse + dirt, via Cycles EMIT bakes over badge.uv/decal.uv."""
        if not self._dow1_badge_jobs:
            return
        scene = self.bpy_context.scene
        prev_engine = scene.render.engine
        scene.render.engine = 'CYCLES'
        scene.cycles.samples = 1
        try:
            for job in self._dow1_badge_jobs.values():
                objects = [
                    obj for obj in scene.objects
                    if obj.type == 'MESH'
                    and job['material'].name in obj.data.materials
                    and dow3_materials.BADGE_UV_LAYER in obj.data.uv_layers
                ]
                if not objects:
                    continue
                size = tuple(job['diffuse_final_image'].size)
                badge_overlay = None
                if self.badge_image is not None:
                    badge_overlay = self._bake_reprojection(
                        objects, job['material'], self.badge_image, dow3_materials.BADGE_UV_LAYER, size)
                decal_overlay = decal_alpha = None
                if self.decal_image is not None:
                    decal_overlay = self._bake_reprojection(
                        objects, job['material'], self.decal_image, dow3_materials.DECAL_UV_LAYER, size)
                    decal_alpha = self._bake_reprojection(
                        objects, job['material'], self.decal_image, dow3_materials.DECAL_UV_LAYER, size,
                        source_socket='Alpha').getchannel('R')
                self._compose_badge_layers(job, badge_overlay, decal_overlay, decal_alpha)
        finally:
            scene.render.engine = prev_engine

    def _bake_reprojection(self, objects, mat, source_image, uv_layer: str, size, source_socket: str = 'Color'):
        """Reproject source_image, sampled through uv_layer with a [0, 1] CLIP window,
        into uv0 texture space via a Cycles EMIT bake. Returns a PIL RGB image."""
        import numpy

        target = bpy.data.images.new('__dow_bake_target', *size, alpha=False)
        target.colorspace_settings.name = 'Non-Color'
        prev_colorspace = source_image.colorspace_settings.name
        source_image.colorspace_settings.name = 'Non-Color'

        bake_mat = bpy.data.materials.new('__dow_bake')
        bake_mat.use_nodes = True
        tree = bake_mat.node_tree
        tree.nodes.clear()
        node_uv = tree.nodes.new('ShaderNodeUVMap')
        node_uv.uv_map = uv_layer
        node_source = tree.nodes.new('ShaderNodeTexImage')
        node_source.image = source_image
        node_source.extension = 'CLIP'
        node_emit = tree.nodes.new('ShaderNodeEmission')
        node_out = tree.nodes.new('ShaderNodeOutputMaterial')
        tree.links.new(node_uv.outputs[0], node_source.inputs['Vector'])
        tree.links.new(node_source.outputs[source_socket], node_emit.inputs['Color'])
        tree.links.new(node_emit.outputs[0], node_out.inputs['Surface'])
        node_target = tree.nodes.new('ShaderNodeTexImage')
        node_target.image = target
        tree.nodes.active = node_target

        view_layer = self.bpy_context.view_layer
        prev_slots = []
        for obj in view_layer.objects:
            obj.select_set(False)
        for obj in objects:
            for slot in obj.material_slots:
                if slot.material == mat:
                    prev_slots.append(slot)
                    slot.material = bake_mat
            obj.data.uv_layers.active_index = 0
            obj.select_set(True)
        view_layer.objects.active = objects[0]
        try:
            bpy.ops.object.bake(type='EMIT', margin=0, use_clear=False)
            pixels = numpy.empty(size[0] * size[1] * 4, dtype=numpy.float32)
            target.pixels.foreach_get(pixels)
        finally:
            for slot in prev_slots:
                slot.material = mat
            for obj in objects:
                obj.select_set(False)
            source_image.colorspace_settings.name = prev_colorspace
            bpy.data.materials.remove(bake_mat)
            bpy.data.images.remove(target)
        # image rows come bottom-up; PIL wants top-down
        pixels = pixels.reshape(size[1], size[0], 4)[::-1, :, :3]
        return PilImage.fromarray((pixels * 255 + 0.5).astype(numpy.uint8), 'RGB')

    def _compose_badge_layers(self, job, badge_overlay, decal_overlay, decal_alpha):
        team = PilImage.open(io.BytesIO(job['team_image'].packed_file.data)).convert('RGBA')
        detail_source = PilImage.open(io.BytesIO(job['diffuse_orig_image'].packed_file.data)).convert('RGB')
        default_pil = PilImage.open(io.BytesIO(job['diffuse_final_image'].packed_file.data)).convert('RGB')
        size = detail_source.size
        if team.size != size:
            team = team.resize(size, PilImage.NEAREST)

        bands = team.split()
        bands = bands[:3] + (ImageChops.invert(bands[3]),)
        masks = {layer: mask for layer, mask in zip(PTN_TEAM_CHANNELS, bands) if layer is not None}
        if badge_overlay is not None:
            if badge_overlay.size != size:
                badge_overlay = badge_overlay.resize(size, PilImage.NEAREST)
            # composite the badge pre-tinted with its own badge paints into the
            # diffuse (like the native graph: stencil channel x badge colour,
            # gamma-space) -- routing it through the armour layer colours paints
            # it with the wrong scheme slots
            stencil = _to_array(badge_overlay)
            badge_colors = (self.scheme_info or {}).get('badge_colors', {})
            tint = numpy.zeros_like(stencil)
            for idx, layer in enumerate(('primary', 'secondary', 'trim')):
                color = numpy.asarray(badge_colors.get(layer, (0.5, 0.5, 0.5)), dtype=numpy.float32)
                tint += stencil[..., idx:idx + 1] * color[None, None, :]
            coverage = _to_image(numpy.clip(stencil.sum(axis=-1) * 2, 0, 1), 'L')
            default_pil = PilImage.composite(_to_image(tint), default_pil, coverage)
            keep = ImageChops.invert(coverage)
            masks = {layer: ImageChops.multiply(mask, keep) for layer, mask in masks.items()}
        if decal_overlay is not None and decal_alpha is not None:
            if decal_overlay.size != size:
                decal_overlay = decal_overlay.resize(size, PilImage.NEAREST)
                decal_alpha = decal_alpha.resize(size, PilImage.NEAREST)
            default_pil = PilImage.composite(decal_overlay, default_pil, decal_alpha)
            keep = ImageChops.invert(decal_alpha)
            masks = {layer: ImageChops.multiply(mask, keep) for layer, mask in masks.items()}

        detail = detail_source.point(lambda v: min(255, v * 2))
        detail_multiplier = job['detail_multiplier'] or PilImage.new('L', (1, 1), 128)
        if detail_multiplier.size != size:
            detail_multiplier = detail_multiplier.resize(size, PilImage.NEAREST)
        detail = ImageChops.multiply(detail, PilImage.merge('RGB', (detail_multiplier,) * 3))

        def repack(image, pil_image):
            buffer = io.BytesIO()
            pil_image.save(buffer, format='PNG', compress_level=1)
            data = buffer.getvalue()
            image.pack(data=data, data_len=len(data))
            # pack(data=...) does not invalidate the already-loaded pixel buffer;
            # without a reload renders keep sampling the pre-composite pixels
            image.reload()

        for layer, mask in masks.items():
            repack(job['layer_images'][layer], ImageChops.multiply(detail, PilImage.merge('RGB', (mask,) * 3)))
        painted = functools.reduce(ImageChops.add, masks.values())
        repack(job['layer_images'][materials.TeamcolorLayers.DIRT], ImageChops.invert(painted))
        if badge_overlay is not None or decal_overlay is not None:
            repack(job['diffuse_final_image'], default_pil)


    def create_focus_camera(self, perspective: dict | None):
        """Camera from the unit's armoury camera_perspective preset (unit faces -Y)"""
        if perspective is None:
            return None
        name = f'inspect_{perspective["name"]}'
        if name in self.created_cameras:
            return self.created_cameras[name]
        theta = perspective['theta']
        direction = mathutils.Vector((math.sin(theta), -math.cos(theta), 0.0))
        position = direction * perspective['length'] + mathutils.Vector((0.0, 0.0, perspective['camera_height']))
        target = mathutils.Vector((0.0, 0.0, perspective['lookat_height']))
        matrix = self.look_at_matrix(position, target, mathutils.Vector((0.0, 0.0, 1.0)))
        cam_obj = utils.create_camera(self.cameras_collection(), name, matrix, math.radians(60), 0.1, 1000.0)
        self.created_cameras[name] = cam_obj
        return cam_obj

    def load_badge(self, stencil_path: str | None):
        """The paint scheme's badge stencil, sampled through the badge.uv layer"""
        self.badge_image = self.load_image(stencil_path)
        if self.badge_image is not None:
            self.badge_image.colorspace_settings.name = 'Non-Color'

    def load_decal(self, decal_path: str | None):
        """The unit's squad decal, sampled through the decal.uv layer"""
        self.decal_image = self.load_image(decal_path)

    def load_image(self, texture_path: str | None):
        if not texture_path:
            return None
        key = texture_path.lower()
        if key in self.created_images:
            return self.created_images[key]
        posix_path = pathlib.PureWindowsPath(texture_path).as_posix()
        texture_data = self.layout.find(f'{posix_path}.rgt')
        if texture_data is None:
            self.created_images[key] = None
            return None
        name = pathlib.PurePath(posix_path).name
        with tempfile.TemporaryDirectory() as tmpdir:
            file = pathlib.Path(tmpdir) / f'{name}.png'
            rgt.to_pil(texture_data.read_bytes()).save(file, compress_level=1)
            image = bpy.data.images.load(str(file))
            image.pack()
            image.packed_files[-1].filepath = image.filepath_raw = str(self.unpacked_texture_folder / f'{name}.png')
            image.use_fake_user = True
        if hasattr(image, 'dow_export_path'):
            image.dow_export_path = texture_path
        self.created_images[key] = image
        return image

    def load_animations(self, unit_path: str, motion_sets: list[str] = None):
        model = self.unit_model(unit_path)
        if model is None:
            return
        rhka = self.layout.find(f'{model.parent.layout_path()}/{model.stem}.rhka')
        if rhka is None:
            self.messages.append(('INFO', f'No animation set next to "{model}"'))
            return
        data = rhka.read_bytes()
        selected = None
        if motion_sets is not None:
            selected = {name for name in havok.read_clip_names(data)
                        if name.split('\\', 1)[0] in motion_sets}
        missing_bones = set()
        for clip in havok.iter_clips(data, selected):
            action_name = pathlib.PureWindowsPath(clip.name).as_posix()
            try:
                missing_bones |= self._bake_clip(action_name, clip.skeleton, clip.animation)
            except ValueError as e:
                self.messages.append(('WARNING', f'Cannot decode clip: {e}'))
        if missing_bones:
            self.messages.append(('WARNING', f'Animations use {len(missing_bones)} bones absent from the armature: {", ".join(sorted(missing_bones))}'))
        if self.armature_obj.animation_data is not None:
            self.armature_obj.animation_data.action = None
        if self.armature_obj.pose is not None:
            for pose_bone in self.armature_obj.pose.bones:
                pose_bone.matrix_basis = mathutils.Matrix.Identity(4)
