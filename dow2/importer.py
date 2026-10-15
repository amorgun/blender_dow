import io
import math
import pathlib
import struct
import tempfile

import bpy
import mathutils
from PIL import Image as PilImage

from ..chunky import ChunkReader, ChunkyVersion
from ..dow_layout import DowLayout
from .attrib import AttribDb
from .. import animations, havok, materials, mesh, props, utils
from . import materials as dow2_materials
from ..importing import CONV, CONV_INV, DOW2_TEAM_CHANNELS, bake_teamcolor_layers

# display rolls appended to bone/marker matrices so the Blender Y stick points
# along the skeleton's bone axis (file-frame -X) / the marker's forward (file-frame +Y)
BONE_ROLL = mathutils.Matrix.Rotation(math.radians(-90), 4, 'Z')
BONE_ROLL_INV = BONE_ROLL.inverted()
MARKER_ROLL = mathutils.Matrix.Rotation(math.radians(90), 4, 'X')

# file frame -> a display-rolled bone's local frame
CONV_BONE = BONE_ROLL_INV @ CONV
CONV_BONE_INV = CONV_BONE.inverted()

ELEMENT_SIZES = {2: 4, 3: 8, 4: 12, 14: 4}

TEXTURE_VARS = ('diffuseTex', 'normalMap', 'specularTex', 'glossTex', 'occlusionTex', 'emissiveTex', 'teamTex', 'dirtTex', 'badge1Tex', 'badge2Tex')


def unpack_snorm(b: int) -> float:
    return (b - 128) / 127 if b >= 128 else (b - 127) / 127


def read_conv_matrix(reader) -> mathutils.Matrix:
    v = reader.read_struct('<12f')
    m = mathutils.Matrix((
        (v[0], v[3], v[6], v[9]),
        (v[1], v[4], v[7], v[10]),
        (v[2], v[5], v[8], v[11]),
        (0, 0, 0, 1),
    ))
    return CONV @ m @ CONV_INV


class ModelLoader:
    def __init__(
            self,
            layout: DowLayout,
            import_lods: bool = False,
            import_markers: bool = True,
            import_wargear: str = 'DEFAULT',
            teamcolor_scheme: int = 1,
            enable_vertex_automerge: bool = True,
            vertex_position_merge_threshold: float = 0.001,
            dow1_materials: bool = False,
            context=None,
        ):
        self.layout = layout
        self.attrib_db = None
        self.import_lods = import_lods
        self.import_markers = import_markers
        self.enable_vertex_automerge = enable_vertex_automerge
        self.vertex_position_merge_threshold = vertex_position_merge_threshold
        self.import_wargear = import_wargear
        self.teamcolor_scheme = teamcolor_scheme
        self.dow1_materials = dow1_materials
        self.bpy_context = context or bpy.context
        self.messages = []
        self.root_collection = None
        self.armature = None
        self.armature_obj = None
        self.bone_world = {}
        self.bone_case = {}  # lowercase -> actual armature bone name
        self.created_materials = {}
        self.created_images = {}
        self.created_actions = {}
        self.created_cameras = {}
        self._seen_scene_cameras = set()
        self.unpacked_texture_folder = pathlib.Path('//texture_share')

    def load_unit(self, unit_path: str, attachments: list[str] = None, wargear: list[str] = None):
        if self.attrib_db is None:
            self.attrib_db = AttribDb.load(self.layout)
        if self.attrib_db is None:
            self.messages.append(('ERROR', 'No attribute database found in the environment'))
            return
        blueprint = self.attrib_db.decode(unit_path).get('entity_blueprint_ext', {})
        animator = blueprint.get('animator')
        root_model = self.find_model(animator) if animator else None
        if root_model is None:
            self.messages.append(('ERROR', f'Cannot find model "{animator}"'))
            return
        collections = [self.load_file(root_model)]
        loaded = {str(root_model)}
        if attachments is None:
            attachments = self.attrib_db.unit_attachments(unit_path)
        if wargear is None:
            wargear = self.attrib_db.unit_wargear(unit_path, equippable=self.import_wargear == 'ALL') if self.import_wargear != 'NONE' else []
        parts = [('attachment', a) for a in attachments] + [('wargear', w) for w in wargear]
        for kind, art_path in parts:
            part_model = self.find_model(art_path)
            if part_model is None:
                self.messages.append(('WARNING', f'Cannot find {kind} model "{art_path}"'))
                continue
            if str(part_model) in loaded:
                continue
            loaded.add(str(part_model))
            collections.append(self.load_file(part_model))
        for art_path in self.attrib_db.unit_attachments(unit_path) + self.attrib_db.unit_wargear(unit_path, equippable=True):
            part_model = self.find_model(art_path)
            if part_model is None or str(part_model) in loaded:
                continue
            loaded.add(str(part_model))
            self.load_skeleton(part_model)
        if not any(o.type == 'MESH' for c in collections for o in c.all_objects):
            self.messages.append(('INFO', f'"{unit_path}" has no geometry. Try importing it with all equippable wargear'))

    def apply_teamcolor(self, teamcolor_dict: dict):
        materials.apply_teamcolor_scheme(self.layout, self.unpacked_texture_folder, teamcolor_dict)

    def find_model(self, art_path: str):
        posix_path = pathlib.PureWindowsPath(art_path).as_posix()
        if not posix_path.lower().startswith('art/'):
            posix_path = f'art/{posix_path}'
        return self.layout.find(f'{posix_path}.model')

    def unit_model(self, unit_path: str):
        if self.attrib_db is None:
            self.attrib_db = AttribDb.load(self.layout)
        animator = self.attrib_db.decode(unit_path).get('entity_blueprint_ext', {}).get('animator')
        return self.find_model(animator) if animator else None

    def load_animations(self, unit_path: str, motion_sets: list[str] = None):
        model = self.unit_model(unit_path)
        if model is None:
            return
        missing_bones = set()
        loaded_sets = set()
        animations = model.parent / 'animations'
        if animations.is_dir():
            for motion_set in animations.iterdir():
                loaded_sets.add(motion_set.name)
                if motion_sets is not None and motion_set.name not in motion_sets:
                    continue
                for file in motion_set.iterdir():
                    missing_bones |= self.load_animation(file)
        hkanim = model.parent / f'{model.stem}.hkanim'
        if hkanim.is_file():
            for set_name, clips in havok.read_hkanim(hkanim.read_bytes()).items():
                if set_name in loaded_sets:
                    continue
                if motion_sets is not None and set_name not in motion_sets:
                    continue
                for clip_name, data in clips:
                    missing_bones |= self.load_animation_data(f'{set_name}/{pathlib.PurePath(clip_name).stem}', data)
        if missing_bones:
            self.messages.append(('WARNING', f'Animations use {len(missing_bones)} bones absent from the armature: {", ".join(sorted(missing_bones))}'))
        if self.armature_obj.animation_data is not None:
            self.armature_obj.animation_data.action = None
        for material in self.created_materials.values():
            if material.node_tree is not None and material.node_tree.animation_data is not None:
                material.node_tree.animation_data.action = None
        if self.armature_obj.pose is not None:  # pose data only materializes once edit mode was entered
            for pose_bone in self.armature_obj.pose.bones:
                pose_bone.matrix_basis = mathutils.Matrix.Identity(4)

    def load_animation(self, file) -> set:
        return self.load_animation_data(f'{file.parent.name}/{file.stem}', file.read_bytes())

    def load_animation_data(self, name: str, data: bytes) -> set:
        if not data.startswith(havok.PACKFILE_MAGIC):
            self.messages.append(('WARNING', f'"{name}" is not a Havok packfile'))
            return set()
        packfile = havok.Packfile(data)
        if 'hkAnimationBinding' not in packfile.objects:
            self.messages.append(('INFO', f'"{name}" contains no animation'))
            return set()
        scene_cameras = havok.read_hkx_cameras(packfile)
        if scene_cameras:
            self.create_scene_cameras(name, scene_cameras)
        skeleton = havok.read_skeleton(packfile)
        animation = havok.read_animation(packfile)
        return self._bake_clip(name, skeleton, animation)

    def _bake_clip(self, name: str, skeleton: 'havok.Skeleton', animation: 'havok.Animation') -> set:
        """Bake a decoded clip into an action with pose-bone fcurves; returns the
        clip's bone names absent from the armature"""
        missing_bones = set()
        action = self.created_actions.get(name)
        if action is None:
            action = self.created_actions[name] = bpy.data.actions.new(name=name)
        action.use_fake_user = True
        frame_scale = self._frame_scale(animation.fps)
        action.frame_range = 0, (animation.num_poses - 1) * frame_scale
        props.setup_property(action, 'fps', animation.fps)

        slot = utils.get_or_create_slot(action, 'OBJECT', 'Skeleton', identifier='OBSkeleton')
        utils.bind_action_slot(self.armature_obj, action, slot)
        channelbag = utils.ensure_channelbag_exists(action, slot)

        armature_bones = {bone.name.lower(): bone.name for bone in self.armature.bones}

        def ensure_bone(idx):
            existing = armature_bones.get(skeleton.bone_names[idx].lower())
            if existing is not None:
                return existing
            parent_idx = skeleton.parents[idx]
            parent_name = ensure_bone(parent_idx) if parent_idx >= 0 else None
            translation, (x, y, z, w), scale = skeleton.reference_pose[idx]
            local = CONV @ mathutils.Matrix.LocRotScale(mathutils.Vector(translation), mathutils.Quaternion((w, x, y, z)), mathutils.Vector(scale)) @ CONV_INV
            parent_world = self.armature.edit_bones[parent_name].matrix @ BONE_ROLL_INV if parent_name else mathutils.Matrix.Identity(4)
            new_bone = self.armature.edit_bones.new(skeleton.bone_names[idx])
            new_bone.head = (0, 0, 0)
            new_bone.tail = (0.5, 0, 0)
            new_bone.inherit_scale = 'NONE'
            bone_collection.assign(new_bone)
            if parent_name is not None:
                new_bone.parent = self.armature.edit_bones[parent_name]
            new_bone.matrix = parent_world @ local @ BONE_ROLL
            armature_bones[new_bone.name.lower()] = new_bone.name
            return new_bone.name

        if any(skeleton.bone_names[idx].lower() not in armature_bones for idx in animation.track_bones):
            self.bpy_context.view_layer.objects.active = self.armature_obj
            bpy.ops.object.mode_set(mode='EDIT', toggle=True)
            bone_collection = self.armature.collections.get('Skeleton') or self.armature.collections.new('Skeleton')
            for bone_idx in animation.track_bones:
                ensure_bone(bone_idx)
            bpy.ops.object.mode_set(mode='EDIT', toggle=True)

        hkx_index = {name.lower(): idx for idx, name in enumerate(skeleton.bone_names)}
        track_of = {bone_idx: track_idx for track_idx, bone_idx in enumerate(animation.track_bones)}

        def local_matrix(idx, pose):
            if idx in track_of:
                translation, (x, y, z, w), scale = animation.tracks[track_of[idx]][pose]
            else:
                translation, (x, y, z, w), scale = skeleton.reference_pose[idx]
            return mathutils.Matrix.LocRotScale(mathutils.Vector(translation), mathutils.Quaternion((w, x, y, z)), mathutils.Vector(scale))

        worlds = []
        for pose in range(animation.num_poses):
            frame_worlds = {}

            def world(idx):
                if idx not in frame_worlds:
                    parent_idx = skeleton.parents[idx]
                    matrix = local_matrix(idx, pose)
                    frame_worlds[idx] = (world(parent_idx) @ matrix) if parent_idx >= 0 else matrix
                return frame_worlds[idx]

            for idx in range(len(skeleton.bone_names)):
                world(idx)
            worlds.append(frame_worlds)

        armature_worlds = [{} for _ in range(animation.num_poses)]

        def armature_world(bone, pose):
            """The armature bone's world transform in the clip's frame, resting bones the clip's skeleton lacks"""
            memo = armature_worlds[pose]
            matrix = memo.get(bone.name)
            if matrix is None:
                idx = hkx_index.get(bone.name.lower())
                if idx is not None:
                    matrix = worlds[pose][idx]
                elif bone.parent is None:
                    matrix = CONV_INV @ bone.matrix_local @ BONE_ROLL_INV @ CONV
                else:
                    rest_local = bone.parent.matrix_local.inverted() @ bone.matrix_local
                    matrix = armature_world(bone.parent, pose) @ CONV_BONE_INV @ rest_local @ CONV_BONE
                memo[bone.name] = matrix
            return matrix

        for track_idx, bone_idx in enumerate(animation.track_bones):
            bone_name = armature_bones.get(skeleton.bone_names[bone_idx].lower())
            if bone_name is None:
                missing_bones.add(skeleton.bone_names[bone_idx])
                continue
            bone = self.armature.bones[bone_name]
            rest_local = bone.matrix_local if bone.parent is None else bone.parent.matrix_local.inverted() @ bone.matrix_local
            rest_local_inv = rest_local.inverted()
            key_scale = any(abs(c - 1.0) > 1e-3 for pose in animation.tracks[track_idx] for c in pose[2])
            locations, quaternions, basis_scales = [], [], []
            previous = None
            for pose in range(animation.num_poses):
                pose_world = worlds[pose][bone_idx]
                if bone.parent is not None:
                    relative = armature_world(bone.parent, pose).inverted() @ pose_world
                    basis = rest_local_inv @ CONV_BONE @ relative @ CONV_BONE_INV
                else:
                    basis = rest_local_inv @ CONV @ pose_world @ CONV_INV @ BONE_ROLL
                location, quaternion, basis_scale = basis.decompose()
                if previous is not None:
                    quaternion.make_compatible(previous)
                previous = quaternion
                locations.append(location)
                quaternions.append(quaternion)
                basis_scales.append(basis_scale)

            def write_channel(channel, samples):
                for axis in range(len(samples[0])):
                    animations.insert_keyframes(
                        channelbag, f'pose.bones["{bone_name}"].{channel}', axis,
                        [(frame * frame_scale, sample[axis]) for frame, sample in enumerate(samples)], group=bone_name)

            write_channel('location', locations)
            write_channel('rotation_quaternion', quaternions)
            if key_scale:
                write_channel('scale', basis_scales)

        uvoffset_tracks = [
            (track_idx, bone_idx) for track_idx, bone_idx in enumerate(animation.track_bones)
            if 'uvoffset' in skeleton.bone_names[bone_idx].lower()
        ]
        if uvoffset_tracks:
            # tread clips: stretch the wheels' full revolution to 180 frames and turn the uv
            # proxy bones' translation into DoW1-style uv offset keys on the tread materials
            stretch = 180 / ((animation.num_poses - 1) * frame_scale)
            for fcurve in channelbag.fcurves:
                for keyframe in fcurve.keyframe_points:
                    keyframe.co.x *= stretch
                fcurve.update()
            action.frame_range = 0, 180
            props.setup_property(action, 'fps', animation.fps * frame_scale * stretch)
            for track_idx, bone_idx in uvoffset_tracks:
                proxy_name = skeleton.bone_names[bone_idx]
                side = proxy_name.lower().rsplit('uvoffset', 1)[-1].strip('_ ')
                targets = [mat for mat_name, mat in self.created_materials.items() if side and side in mat_name.lower()]
                if not targets:
                    self.messages.append(('WARNING', f'"{name}": no material matches the uv offset bone "{proxy_name}"'))
                    continue
                translations = [animation.tracks[track_idx][pose][0] for pose in range(animation.num_poses)]
                base = translations[0]
                pose_frame = frame_scale * stretch
                for mat in targets:
                    animations.bake_material_uv_offset(action, mat, {
                        0: [(pose * pose_frame, translation[0] - base[0]) for pose, translation in enumerate(translations)],
                        1: [(pose * pose_frame, -(translation[1] - base[1])) for pose, translation in enumerate(translations)],
                    })

        # a clip animating a bone that shares a camera's name (NIS rigs) drives that
        # camera: reattach the static camera object onto the bone
        for bone_idx in animation.track_bones:
            track_bone = skeleton.bone_names[bone_idx]
            for camera_name, cam_obj in self.created_cameras.items():
                if camera_name.lower() == track_bone.lower() and cam_obj.parent_type != 'BONE':
                    actual = self.bone_case.get(track_bone.lower())
                    if actual and actual in self.armature.bones:
                        self.attach_camera_to_bone(cam_obj, actual)

        # DoW3 clips drive material uv offsets and NIS camera lenses through float
        # tracks named material:<material>:channel:<param> / camera:<camera>:<param>
        skipped_slots = []
        camera_tracks = {}
        for slot_name, values in animation.float_tracks:
            parts = slot_name.split(':')
            if len(parts) == 3 and parts[0] == 'camera':
                camera_tracks.setdefault(parts[1], {})[parts[2]] = values
                continue
            if slot_name in ('fov', 'nearplane', 'farplane'):
                # some NIS rigs name the lens slots bare; they belong to the rig's
                # bone-riding camera
                camera_tracks.setdefault('', {})[slot_name] = values
                continue
            if len(parts) != 4 or parts[0] != 'material' or parts[2] != 'channel':
                skipped_slots.append(slot_name)
                continue
            mat_name, param = parts[1], parts[3]
            mat = self.created_materials.get(mat_name)
            if mat is None:
                self.messages.append(('DEBUG', f'"{name}": material "{mat_name}" not loaded for uv animation'))
                continue
            if param == 'diffuse_offsetu':
                samples = {0: [(frame * frame_scale, value) for frame, value in enumerate(values)]}
            elif param == 'diffuse_offsetv':
                # the importer stores v flipped (1 - v)
                samples = {1: [(frame * frame_scale, -value) for frame, value in enumerate(values)]}
            else:
                skipped_slots.append(slot_name)
                continue
            animations.bake_material_uv_offset(action, mat, samples)
        for camera_name, tracks in camera_tracks.items():
            skipped_slots.extend(self._bake_camera_tracks(action, camera_name, tracks, animation.fps))
        if skipped_slots:
            self.messages.append(('INFO', f'"{name}": skipped float tracks: {", ".join(sorted(set(skipped_slots)))}'))
        return missing_bones

    def _frame_scale(self, fps: float) -> float:
        """Scene frames per clip sample, keeping a clip's wall-clock duration at any scene frame rate"""
        render = self.bpy_context.scene.render
        return render.fps / render.fps_base / fps if fps else 1.0

    def _bake_camera_tracks(self, action, camera_name: str, tracks: dict, fps: float) -> list:
        """Key the lens of the NIS bone-riding camera from the clip's float slots;
        returns the slots that could not be applied."""
        frame_scale = self._frame_scale(fps)
        prefix = f'camera:{camera_name}:' if camera_name else ''
        cam_obj = self.created_cameras.get(camera_name)
        if cam_obj is None and not camera_name:
            # bare lens slots target the rig's single bone-riding camera
            cam_obj = next((obj for obj in self.created_cameras.values() if obj.parent_type == 'BONE'), None)
        if cam_obj is None:
            suffix = ':' + camera_name.lower()
            bone_name = next(
                (bone.name for bone in self.armature.bones
                 if bone.name.lower() == camera_name.lower() or bone.name.lower().endswith(suffix)),
                None) if camera_name else None
            if bone_name is None:
                return [f'{prefix}{param}' for param in tracks]
            cam_obj = utils.create_camera(
                self.cameras_collection(), camera_name, mathutils.Matrix.Identity(4),
                math.radians(40), 0.01, 1000)
            self.attach_camera_to_bone(cam_obj, bone_name)
            self.created_cameras[camera_name] = cam_obj
        cam = cam_obj.data
        slot = utils.get_or_create_slot(action, 'CAMERA', cam.name, identifier='CA' + cam.name)
        utils.bind_action_slot(cam, action, slot)
        channelbag = utils.ensure_channelbag_exists(action, slot)
        skipped = []
        for param, values in tracks.items():
            if param == 'fov' and 1e-3 < max(values) < math.pi:
                # the camera angle isn't animatable; key the focal length instead
                samples = [(frame * frame_scale, cam.sensor_width / (2 * math.tan(max(value, 1e-3) / 2)))
                           for frame, value in enumerate(values)]
                animations.insert_keyframes(channelbag, 'lens', 0, samples)
            elif param == 'nearplane' and min(values) > 1e-4:
                animations.insert_keyframes(channelbag, 'clip_start', 0, [(frame * frame_scale, value) for frame, value in enumerate(values)])
            elif param == 'farplane' and min(values) > 1.0:
                animations.insert_keyframes(channelbag, 'clip_end', 0, [(frame * frame_scale, value) for frame, value in enumerate(values)])
            else:
                # authored-zero / degenerate values: the engine overrides them at runtime
                skipped.append(f'{prefix}{param}')
        return skipped

    def load_file(self, path):
        reader = ChunkReader(io.BytesIO(path.read_bytes()), ChunkyVersion.V3)
        reader.skip_relic_chunky()
        header = reader.read_header('FOLDMODL')
        if self.root_collection is None:
            self.root_collection = bpy.data.collections.new(header.name or path.stem)
            self.bpy_context.scene.collection.children.link(self.root_collection)
        collection = self.root_collection

        if self.armature_obj is None:
            self.armature = bpy.data.armatures.new('Armature')
            self.armature_obj = bpy.data.objects.new('Armature', self.armature)
            self.armature_obj.show_in_front = True
            collection.objects.link(self.armature_obj)

        chunk_positions = reader.index_chunks()

        def chunks(typeid: str):
            for chunk_position in chunk_positions.get(typeid, ()):
                yield reader.seek_chunk(chunk_position)

        for chunk in chunks('FOLDSKEL'):
            self.CH_FOLDSKEL(reader.read_folder(chunk))
        materials = {}
        for chunk in chunks('FOLDMTRL'):
            materials[chunk.name] = self.CH_FOLDMTRL(reader.read_folder(chunk), chunk.name)
        for chunk in chunks('FOLDMESH'):
            self.CH_FOLDMESH(reader.read_folder(chunk), materials, collection, path.stem)
        if self.import_markers:
            for chunk in chunks('DATAMRKS'):
                self.CH_DATAMRKS(reader.read_folder(chunk))
        for chunk in chunks('DATADTBP'):
            self.CH_DATADTBP(reader, chunk, collection)
        self._apply_state_lod_visibility(collection)
        return collection

    def _apply_state_lod_visibility(self, collection):
        """Show only the model's default state and its most detailed loaded LOD;
        the other variants render superimposed otherwise. Both decisions read
        the custom properties written from the chunk data ('lod_level' on the
        lod collections, 'dow_state' + the model's 'state_default'), and every
        state mesh gets an 'is_wreck' property"""
        lod_cols = [c for c in collection.children_recursive if c.get('lod_level') is not None]
        if lod_cols:
            best = min(c['lod_level'] for c in lod_cols)
            for c in lod_cols:
                hide = c['lod_level'] != best
                c.hide_render = c.hide_viewport = hide
        default_state = collection.get('state_default')
        for c in collection.children_recursive:
            state = c.get('dow_state')
            if state is None:
                continue
            wreck = bool(default_state) and state != default_state
            if wreck:
                c.hide_render = c.hide_viewport = True
            for obj in c.all_objects:
                if obj.type == 'MESH':
                    obj['is_wreck'] = wreck

    def CH_DATADTBP(self, reader: ChunkReader, chunk, collection):
        end = reader.stream.tell() + chunk.size
        reader.skip(4)
        for _ in range(reader.read_one('<L')):
            name = reader.read_str()
            states = [reader.read_str() for _ in range(reader.read_one('<L'))]
            collection[f'states_{name}'] = states
        collection['state_default'] = reader.read_str()
        # the variables section is absent in some files
        if reader.stream.tell() < end:
            for _ in range(reader.read_one('<L')):
                name = reader.read_str()
                values = list(reader.read_struct('<3f'))
                flag = reader.read_one('<B')
                collection[f'variable_{name}'] = values + [float(flag)]

    def load_skeleton(self, path):
        """Merge only the skeleton of a part model into the armature"""
        reader = ChunkReader(io.BytesIO(path.read_bytes()), ChunkyVersion.V3)
        reader.skip_relic_chunky()
        reader.read_header('FOLDMODL')
        for chunk in reader.iter_chunks():
            if chunk.typeid == 'FOLDSKEL':
                self.CH_FOLDSKEL(reader.read_folder(chunk))
            else:
                reader.skip(chunk.size)

    def CH_FOLDSKEL(self, reader: ChunkReader):
        bones = []
        for chunk in reader.iter_chunks():
            match chunk.typeid:
                case 'DATAINFO':
                    num_bones = reader.read_one('<L')
                case 'DATABONE':
                    chunk_end = reader.stream.tell() + chunk.size
                    parent_idx, _ = reader.read_struct('<lL')
                    bones.append((chunk.name, parent_idx, read_conv_matrix(reader)))
                    reader.skip(chunk_end - reader.stream.tell())
                case _:
                    reader.skip(chunk.size)
        self.create_skeleton(bones)

    def create_skeleton(self, bones: list):
        """Merge (name, parent index, local matrix) bones into the shared armature.
        Bones merge by name case-insensitively: part models may spell a shared
        bone differently than the file that created it."""
        self.bpy_context.view_layer.objects.active = self.armature_obj
        bpy.ops.object.mode_set(mode='EDIT', toggle=True)
        bone_collection = self.armature.collections.get('Skeleton') or self.armature.collections.new('Skeleton')
        world_transforms = []
        actual_names = []
        for name, parent_idx, local in bones:
            existing_name = self.bone_case.get(name.lower())
            existing = self.armature.edit_bones.get(existing_name) if existing_name else None
            if existing is not None:
                actual_names.append(existing.name)
                world_transforms.append(existing.matrix @ BONE_ROLL_INV)
                self.bone_world[existing.name] = world_transforms[-1]
                continue
            parent_world = world_transforms[parent_idx] if parent_idx != -1 else mathutils.Matrix()
            world = parent_world @ local
            world_transforms.append(world)
            self.bone_world[name] = world
            new_bone = self.armature.edit_bones.new(name)
            new_bone.head = (0, 0, 0)
            new_bone.tail = (0.5, 0, 0)
            new_bone.inherit_scale = 'NONE'
            bone_collection.assign(new_bone)
            if parent_idx != -1:
                new_bone.parent = self.armature.edit_bones[actual_names[parent_idx]]
            new_bone.matrix = world @ BONE_ROLL
            actual_names.append(new_bone.name)
            self.bone_case[name.lower()] = new_bone.name
        for name in actual_names:
            bone = self.armature.edit_bones[name]
            if len(bone.children) == 1:
                new_length = (bone.children[0].head - bone.head).length
                if new_length > 1e-3:
                    bone.length = new_length
        bpy.ops.object.mode_set(mode='EDIT', toggle=True)

    def CH_DATAMRKS(self, reader: ChunkReader):
        num_markers = reader.read_one('<L')
        markers = []
        for _ in range(num_markers):
            name = reader.read_str()
            parent_name = reader.read_str()
            local = read_conv_matrix(reader)
            params = {}
            for _ in range(reader.read_one('<L')):
                key = reader.read_str()
                reader.read_one('<L')
                params[key] = reader.read_str()
            markers.append((name, parent_name, local, params))
        self.create_markers(markers)

    def create_markers(self, markers: list):
        """Attach (name, parent name, local matrix, params) markers to the armature as bones"""
        if not markers:
            return
        self.bpy_context.view_layer.objects.active = self.armature_obj
        bpy.ops.object.mode_set(mode='EDIT', toggle=True)
        bone_collection = self.armature.collections.get('Markers') or self.armature.collections.new('Markers')
        created = []
        for name, parent_name, local, params in markers:
            existing_name = self.bone_case.get(name.lower())
            if existing_name and existing_name in self.armature.edit_bones:
                continue
            marker = self.armature.edit_bones.new(name)
            marker.head = (0, 0, 0)
            marker.tail = (0.15, 0, 0)
            bone_collection.assign(marker)
            marker.color.palette = 'CUSTOM'
            marker.color.custom.normal = mathutils.Color([14, 255, 2]) / 255
            marker.color.custom.active = mathutils.Color([255, 98, 255]) / 255
            parent_world = mathutils.Matrix()
            if parent_name:
                parent_actual = self.bone_case.get(parent_name.lower(), parent_name)
                parent = self.armature.edit_bones.get(parent_actual)
                if parent is None:
                    self.messages.append(('WARNING', f'Marker "{name}" is attached to non-existent bone "{parent_name}"'))
                else:
                    marker.parent = parent
                    parent_world = self.bone_world.get(parent_actual, mathutils.Matrix())
            self.bone_world[name] = parent_world @ local
            self.bone_case[name.lower()] = marker.name
            marker.matrix = self.bone_world[name] @ MARKER_ROLL
            created.append((marker.name, params))
        bpy.ops.object.mode_set(mode='EDIT', toggle=True)

        custom_shape = bpy.data.objects.get('marker_custom_shape_template')
        if custom_shape is None:
            custom_shape = bpy.data.objects.new('marker_custom_shape_template', None)
            custom_shape.empty_display_type = 'ARROWS'
            custom_shape.use_fake_user = True
        for name, params in created:
            pose_bone = self.armature_obj.pose.bones[name]
            pose_bone.custom_shape = custom_shape
            for key, value in params.items():
                pose_bone[key] = value

    def get_collection(self, name: str, parent):
        for child in parent.children:
            if child.name.split('.')[0] == name:
                return child
        collection = bpy.data.collections.new(name)
        parent.children.link(collection)
        return collection

    # file cameras look along their local -Z (GL convention); the frame conjugation
    # maps Blender's -Z onto file -Y, so cameras need an extra +90 X roll
    CAMERA_ROLL = mathutils.Matrix.Rotation(math.radians(90), 4, 'X')

    def cameras_collection(self):
        collection = self.root_collection or self.bpy_context.scene.collection
        return self.get_collection('Cameras', collection)

    def create_camera_objects(self, cameras: list):
        """cameras: (name, file position3, file quaternion xyzw, angle, clip_start, clip_end)"""
        collection = self.cameras_collection()
        for name, position, (x, y, z, w), angle, clip_start, clip_end in cameras:
            if name in self.created_cameras:
                continue
            local = mathutils.Matrix.LocRotScale(mathutils.Vector(position), mathutils.Quaternion((w, x, y, z)), None)
            matrix = CONV @ local @ CONV_INV @ self.CAMERA_ROLL
            self.created_cameras[name] = utils.create_camera(collection, name, matrix, angle, clip_start, clip_end)

    def attach_camera_to_bone(self, cam_obj, bone_name: str):
        bone = self.armature.bones[bone_name]
        cam_obj.parent = self.armature_obj
        cam_obj.parent_type = 'BONE'
        cam_obj.parent_bone = bone_name
        # bone-parent space sits at the bone tail with the display roll applied
        cam_obj.matrix_basis = (
            mathutils.Matrix.Translation((0, -bone.length, 0)) @ BONE_ROLL_INV @ self.CAMERA_ROLL)

    @staticmethod
    def look_at_matrix(position, target, up):
        forward = (target - position).normalized()
        right = forward.cross(up.normalized()).normalized()
        true_up = right.cross(forward)
        rotation = mathutils.Matrix((right, true_up, -forward)).transposed().to_4x4()
        return mathutils.Matrix.Translation(position) @ rotation

    def create_scene_cameras(self, clip_name: str, cameras: list):
        """Cameras from a clip packfile's hkxScene, deduplicated across clips"""
        collection = None
        for index, (position, focus, up, fov, clip_a, clip_b) in enumerate(cameras):
            key = tuple(round(value, 5) for vector in (position, focus, up) for value in vector) + (round(fov, 5),)
            if key in self._seen_scene_cameras:
                continue
            self._seen_scene_cameras.add(key)
            if collection is None:
                collection = self.cameras_collection()
            name = pathlib.PurePosixPath(clip_name).name if len(cameras) == 1 else f'{pathlib.PurePosixPath(clip_name).name}_{index}'
            name = f'camera_{name}'
            position = CONV @ mathutils.Vector(position)
            target = CONV @ mathutils.Vector(focus)
            up_vector = CONV.to_3x3() @ mathutils.Vector(up)
            matrix = self.look_at_matrix(position, target, up_vector)
            focus_obj = bpy.data.objects.new(f'{name}_focus', None)
            collection.objects.link(focus_obj)
            focus_obj.matrix_basis = mathutils.Matrix.Translation(target)
            focus_obj.empty_display_type = 'SPHERE'
            self.created_cameras[name] = utils.create_camera(
                collection, name, matrix, fov, min(clip_a, clip_b), max(clip_a, clip_b), focus_object=focus_obj)

    def CH_FOLDMESH(self, reader: ChunkReader, materials: dict, collection, model_name: str):
        for chunk in reader.iter_chunks():
            match chunk.typeid:
                case 'FOLDMGRP' | 'FOLDMESH':
                    self.CH_FOLDMESH(reader.read_folder(chunk), materials, collection, model_name)
                case 'FOLDIMDG':
                    self.CH_FOLDIMDG(reader.read_folder(chunk), materials, collection, model_name, chunk.name)
                case 'FOLDIMOD':
                    # rubble/vista world objects place the mesh container directly here
                    self.CH_FOLDIMOD(reader.read_folder(chunk), materials, collection, model_name, chunk.name or model_name)
                case _:
                    reader.skip(chunk.size)

    def CH_FOLDIMDG(self, reader: ChunkReader, materials: dict, collection, model_name: str, state_name: str):
        for chunk in reader.iter_chunks():
            if chunk.typeid != 'FOLDMESH':
                reader.skip(chunk.size)
                continue
            imod_reader = reader.read_folder(chunk)
            for imod in imod_reader.iter_chunks():
                if imod.typeid != 'FOLDIMOD':
                    imod_reader.skip(imod.size)
                    continue
                self.CH_FOLDIMOD(imod_reader.read_folder(imod), materials, collection, model_name, state_name)

    def CH_FOLDIMOD(self, reader: ChunkReader, materials: dict, collection, model_name: str, state_name: str):
        # the DATADATA lod record decides the collection the sibling meshes land
        # in, so dispatch it first regardless of where it sits in the folder
        positions = reader.index_chunks()
        mesh_collection = collection
        for chunk_position in positions.get('DATADATA', ()):
            reader.seek_chunk(chunk_position)
            lod_level = reader.read_one('<L')
            if lod_level > 0 and not self.import_lods:
                return
            parent = self.get_collection(f'lod{lod_level}', collection)
            parent['lod_level'] = lod_level
            parent = self.get_collection(state_name, parent)
            parent['dow_state'] = state_name
            mesh_collection = self.get_collection(model_name, parent)
        for chunk_position in positions.get('FOLDMESH', ()):
            chunk = reader.seek_chunk(chunk_position)
            mesh_name = chunk.name or model_name
            if mesh_name.startswith('merged material-['):
                mesh_name = mesh_name[:-1].split(',', 1)[-1].rsplit('.', 1)[-1]
            mesh_reader = reader.read_folder(chunk)
            for trim in mesh_reader.iter_chunks():
                if trim.typeid != 'FOLDTRIM':
                    mesh_reader.skip(trim.size)
                    continue
                self.CH_FOLDTRIM(mesh_reader.read_folder(trim), mesh_name, materials, mesh_collection, f'rb_{model_name}')

    def CH_FOLDTRIM(self, reader: ChunkReader, mesh_name: str, materials: dict, collection, rigid_bone: str):
        for chunk in reader.iter_chunks():
            if chunk.typeid == 'DATADATA':
                self.create_mesh(reader, mesh_name, materials, collection, rigid_bone)
                return
            reader.skip(chunk.size)

    def create_mesh(self, reader: ChunkReader, mesh_name: str, materials: dict, collection, rigid_bone: str):
        num_elements = reader.read_one('<L')
        elements = [reader.read_struct('<3L') for _ in range(num_elements)]
        num_vertices, stride = reader.read_struct('<2L')
        offsets = {}
        pos = 0
        for slot, _, data_type in elements:
            offsets[slot] = (pos, data_type)
            size = ELEMENT_SIZES.get(data_type)
            if size is None:
                self.messages.append(('WARNING', f'Mesh "{mesh_name}": unknown vertex element type {data_type} in slot {slot}'))
                return
            pos += size
        if pos != stride:
            self.messages.append(('WARNING', f'Mesh "{mesh_name}": vertex stride mismatch ({pos} != {stride})'))
            return
        data = reader.stream.read(num_vertices * stride)

        def vert_bytes(slot):
            start, _ = offsets[slot]
            return (data[i * stride + start: i * stride + start + 4] for i in range(num_vertices))

        def vert_floats(slot, count):
            start, _ = offsets[slot]
            return (struct.unpack_from(f'<{count}f', data, i * stride + start) for i in range(num_vertices))

        vertices = [(-x, -z, y) for x, y, z in vert_floats(0, 3)]
        normals = None
        if 3 in offsets:
            normals = [
                mathutils.Vector((-unpack_snorm(b[2]), -unpack_snorm(b[0]), unpack_snorm(b[1]))).normalized()
                for b in vert_bytes(3)
            ]
        uv_layers = []
        for slot in (8, 9):
            if slot in offsets:
                uv_layers.append([(u, 1 - v) for u, v in vert_floats(slot, 2)])
        colors = None
        if 6 in offsets:
            colors = [(b[2] / 255, b[1] / 255, b[0] / 255, b[3] / 255) for b in vert_bytes(6)]

        reader.skip(8)
        verts_per_face, num_indices = reader.read_struct('<2L')
        indices = reader.read_struct(f'<{num_indices}H')
        faces = [(indices[i], indices[i + 2], indices[i + 1]) for i in range(0, num_indices, verts_per_face)]

        material_name = reader.read_str()
        num_skin_bones = reader.read_one('<L')
        skin_bones = []
        rebind = []
        for _ in range(num_skin_bones):
            reader.skip(48)  # the bone's bind pose; only its inverse is needed
            invbind = read_conv_matrix(reader)
            skin_bones.append(reader.read_str())
            world = self.bone_world.get(skin_bones[-1])
            rebind.append(world @ invbind if world is not None else mathutils.Matrix.Identity(4))
        reader.skip(8)

        if num_skin_bones:
            weight_offset = offsets[2][0]
            index_offset = offsets[1][0]

            def skin_influences(vert_idx):
                base = vert_idx * stride
                bone_indices = data[base + index_offset: base + index_offset + 4]
                wb = data[base + weight_offset: base + weight_offset + 4]
                weights = [wb[2] / 255, wb[1] / 255, wb[0] / 255, wb[3] / 255]
                total = sum(weights)
                weights = [w / total for w in weights] if total else [1, 0, 0, 0]
                return [(idx, weight) for idx, weight in zip(bone_indices, weights) if weight > 0 and idx < len(skin_bones)]

            def is_identity(matrix):
                delta = matrix - mathutils.Matrix.Identity(4)
                return all(abs(value) < 1e-4 for row in delta for value in row)

            if not all(is_identity(matrix) for matrix in rebind):
                # a part skinned in its own file's bind pose, e.g. a weapon authored around its grip bone
                for vert_idx in range(num_vertices):
                    matrix = mathutils.Matrix.Diagonal((0, 0, 0, 0))
                    for idx, weight in skin_influences(vert_idx):
                        matrix = matrix + rebind[idx] * weight
                    vertices[vert_idx] = (matrix @ mathutils.Vector(vertices[vert_idx])).to_tuple()
                    if normals is not None:
                        normals[vert_idx] = (matrix.to_3x3() @ normals[vert_idx]).normalized()

        skin_vert_array = None
        if num_skin_bones:
            skin_vert_array = []
            for vert_idx in range(num_vertices):
                influences = skin_influences(vert_idx)
                skin_vert_array.append((
                    tuple(skin_bones[idx] for idx, _ in influences),
                    mathutils.Vector([weight for _, weight in influences] + [0] * (4 - len(influences))),
                ))
        mesh_data = mesh.MeshData(vertices, faces, skin_vert_array, color_array=colors)
        if self.enable_vertex_automerge and normals is not None:
            mesh_data = mesh.merge(mesh_data, normals, uv_layers, position_threshold=self.vertex_position_merge_threshold)
        else:
            mesh_data = mesh.build_corners(mesh_data, normals, uv_layers)

        material = materials.get(material_name)
        if material is None and material_name:
            self.messages.append(('WARNING', f'Mesh "{mesh_name}": unknown material "{material_name}"'))
        obj = mesh.create_mesh(
            mesh_data, [] if material is None else [material], mesh_name,
            armature_obj=self.armature_obj,
            rigid_bone=rigid_bone if rigid_bone in self.armature.bones else None,
            messages=self.messages,
        )
        collection.objects.link(obj)

    def CH_FOLDMTRL(self, reader: ChunkReader, name: str):
        if name in self.created_materials:
            return self.created_materials[name]
        shader = None
        shader_params = {}
        for chunk in reader.iter_chunks():
            match chunk.typeid:
                case 'DATAINFO':
                    shader = reader.read_str()
                case 'DATA\x00VAR':
                    var_name = reader.read_str()
                    var_type, var_size = reader.read_struct('<2L')
                    var_data = reader.stream.read(var_size)
                    if var_type == 9:
                        shader_params[var_name] = str(var_data.rstrip(b'\0'), 'utf8', errors='ignore')
                    elif var_type == 1:
                        shader_params[var_name] = struct.unpack('<f', var_data[:4])[0]
                    elif var_type == 5:
                        shader_params[var_name] = struct.unpack(f'<{var_size // 4}f', var_data[:var_size // 4 * 4])
                    elif var_type == 10:
                        shader_params[var_name] = bool(var_data[0])
                    elif var_type == 0:
                        shader_params[var_name] = struct.unpack('<L', var_data[:4])[0]
                case _:
                    reader.skip(chunk.size)

        mat = bpy.data.materials.new(name)
        mat['dow2_shader'] = shader or ''
        for var in TEXTURE_VARS:
            if var in shader_params:
                mat[var] = shader_params[var]
        mat.use_nodes = True

        def load(var, colorspace='sRGB'):
            image = self.load_image(shader_params.get(var))
            if image is not None and colorspace != 'sRGB':
                image.colorspace_settings.name = colorspace
            return image

        material_data = materials.MaterialInfo(material_type=materials.MaterialType.DOW2, roughness_value=0.5)
        # the game samples every unit texture raw (DiffuseSamplerGamma included -
        # the shader itself does the ^2.2 decode after the gamma-space paint math),
        # so everything loads Non-Color
        for layer, var, colorspace in (
            (materials.MaterialLayers.DIFFUSE, 'diffuseTex', 'Non-Color'),
            (materials.MaterialLayers.SPECULAR_MASK, 'specularTex', 'Non-Color'),
            (materials.MaterialLayers.GLOSS, 'glossTex', 'Non-Color'),
            (materials.MaterialLayers.NORMAL_MAP, 'normalMap', 'Non-Color'),
            (materials.MaterialLayers.OCCLUSION, 'occlusionTex', 'Non-Color'),
            (materials.MaterialLayers.DIRT, 'dirtTex', 'Non-Color'),
        ):
            if (image := load(var, colorspace)) is not None:
                material_data.channel_images[layer] = image
        if shader_params.get('emissiveMultiplier', 0) and (image := load('emissiveTex', 'Non-Color')) is not None:
            material_data.channel_images[materials.MaterialLayers.SELF_ILLUMUNATION_COLOR] = image
            material_data.default_emission_strength = shader_params['emissiveMultiplier']
        diffuse = material_data.channel_images.get(materials.MaterialLayers.DIFFUSE)
        team = None
        if self.teamcolor_scheme and (team_path := shader_params.get('teamTex')):
            if self.teamcolor_scheme > 1:
                team = self.load_image(f'{team_path}_{self.teamcolor_scheme}')
            if team is None:
                team = self.load_image(team_path)
        force_teamcolor = diffuse is not None and team is not None
        if team is not None:
            # the tem channels are paint masks consumed raw by the shader
            team.colorspace_settings.name = 'Non-Color'
            material_data.channel_images[materials.MaterialLayers.PATTERN] = team
        if force_teamcolor:
            for slot, prefix in (('badge1', 'badge'), ('badge2', 'banner')):
                matrix_values = shader_params.get(f'{slot}MatrixRow1Row2')
                translate = shader_params.get(f'{slot}Translate')
                if not matrix_values or not translate:
                    continue
                # badge_uv = matrix @ wrapped file uv + translate (the shader var stores matrix rows transposed)
                a, c, b, d = matrix_values
                matrix = mathutils.Matrix(((a, b), (c, d)))
                if not matrix.determinant():
                    continue
                inverse = matrix.inverted()
                position = -(inverse @ mathutils.Vector(translate[:2]))
                position.y -= 1  # the file's negated v coordinates wrap into [-1, 0]
                # the image axes in file uv space are the columns of the inverted matrix
                u_axis, v_axis = inverse.col[0], inverse.col[1]
                # the slots sample their images v-down while Blender samples v-up; flip within the rect
                position += v_axis
                width, height = diffuse.size
                setattr(material_data, f'{prefix}_position', (position.x * width, position.y * height))
                setattr(material_data, f'{prefix}_size', (width * u_axis.length, -height * v_axis.length))
                setattr(material_data, f'{prefix}_rotation', math.atan2(u_axis.y, u_axis.x))
        if self.dow1_materials and diffuse is not None:
            # Bake the decoded dow2 shading into the stock DoW1 material tree:
            # occlusion and dirt pre-composite into the diffuse; the spec map
            # bakes as the game's full spec colour (spc.rgb + tem-masked team
            # spec, dirt-suppressed) that the tree treats DoW1-style (0.5*S'
            # into base, metalness, specular tint); gloss follows the Blinn
            # lobe p = 200*g + 10 as 1 - sqrt(2/(4p+2)); the tem masks split
            # into recolorable DoW1 layer value images (diffuse only - team
            # spec has no DoW1 path, so it bakes with the default scheme).
            import numpy
            dif_pil = PilImage.open(io.BytesIO(diffuse.packed_file.data)).convert('RGB')
            albedo = (numpy.asarray(dif_pil, numpy.float32) / 255) ** 2.2
            if (occ := material_data.channel_images.pop(materials.MaterialLayers.OCCLUSION, None)) is not None:
                occ_pil = PilImage.open(io.BytesIO(occ.packed_file.data)).convert('L').resize(dif_pil.size, PilImage.NEAREST)
                albedo = albedo * (numpy.asarray(occ_pil, numpy.float32) / 255)[..., None]
            dirt_keep = 1.0
            if (dirt := material_data.channel_images.pop(materials.MaterialLayers.DIRT, None)) is not None:
                # dirt bakes at the model's dirtVisibility (engine-animated
                # battle grime; the model default is 0 = clean)
                dirt_vis = float(shader_params.get('dirtVisibility', 0.0))
                if dirt_vis > 0:
                    dirt_arr = numpy.asarray(
                        PilImage.open(io.BytesIO(dirt.packed_file.data)).convert('RGBA').resize(dif_pil.size, PilImage.NEAREST),
                        numpy.float32) / 255
                    dirt_a = dirt_arr[..., 3:] * dirt_vis
                    gamma = numpy.clip(albedo, 0, 1) ** (1 / 2.2)
                    gamma = gamma + dirt_a * (dirt_arr[..., :3] - gamma)
                    albedo = numpy.clip(gamma, 0, 1) ** 2.2
                    dirt_keep = 1 - dirt_a[..., 0]
            if (spec := material_data.channel_images.get(materials.MaterialLayers.SPECULAR_MASK)) is not None:
                spc_pil = PilImage.open(io.BytesIO(spec.packed_file.data))
                spec_baked = numpy.asarray(spc_pil.convert('RGB').resize(dif_pil.size, PilImage.NEAREST),
                                           numpy.float32) / 255
                gloss_linear = 0.0
                if 'A' in spc_pil.getbands():
                    gloss_alpha = spc_pil.getchannel('A').resize(dif_pil.size, PilImage.NEAREST)
                    gloss_linear = (numpy.asarray(gloss_alpha, numpy.float32) / 255) ** 2.2
                if team is not None:
                    tem = numpy.asarray(
                        PilImage.open(io.BytesIO(team.packed_file.data)).convert('RGBA').resize(dif_pil.size, PilImage.NEAREST),
                        numpy.float32) / 255
                    # the vanilla DoW1 tree adds 0.5*S' into base, which grays out
                    # the live paint overlay; under the paint masks the raw spc
                    # yields to the paint and only the (coloured) team spec stays
                    paint_cov = numpy.clip(tem.sum(axis=-1), 0, 1)
                    spec_baked = spec_baked * (1 - paint_cov[..., None])
                    for idx, layer in enumerate(dow2_materials.TEAM_SLOTS):
                        mask = tem[..., idx] * dirt_keep
                        spec_baked = spec_baked + mask[..., None] * numpy.asarray(
                            dow2_materials.DEFAULT_TEAM_SPECULARS[layer.value], numpy.float32)
                        gloss_linear = gloss_linear + mask * dow2_materials.DEFAULT_TEAM_GLOSS[layer.value]
                # repay-limit the spec and fold the above-cap excess into the
                # base: the tree's base = albedo + 0.5*S' then reconstructs the
                # intended unpainted response
                spc_rgb = numpy.clip(spec_baked, 0, 1)
                f = spc_rgb.max(axis=-1)
                spec_wins = numpy.clip((f - 0.08 - 2 * albedo.max(axis=-1)) / 0.05, 0, 1)
                excess = spec_wins * numpy.clip(1 - 0.08 / numpy.maximum(f, 1e-3), 0, 1)
                base_t = numpy.clip(albedo + spc_rgb * excess[..., None], 0, 1)
                repay = numpy.minimum(1.0, 2 * base_t.min(axis=-1) / numpy.maximum(f, 1e-3))
                spec_baked = spc_rgb * repay[..., None]
                albedo = numpy.clip(base_t - 0.5 * spec_baked, 0, 1)
                material_data.channel_images[materials.MaterialLayers.SPECULAR_MASK] = self.bake_channel_image(
                    f'{name}_spc', PilImage.fromarray((spec_baked * 255 + 0.5).astype(numpy.uint8), 'RGB'))
                # (2/(4p+2))^0.25: the sqrt is the Beckmann alpha, and BSDF
                # roughness sockets square their input (alpha = r^2)
                roughness = (2 / (800 * numpy.clip(gloss_linear, 0, 1) + 42)) ** 0.25
                if isinstance(roughness, numpy.ndarray):
                    material_data.channel_images[materials.MaterialLayers.GLOSS] = self.bake_channel_image(
                        f'{name}_gls', PilImage.fromarray(((1 - roughness) * 255 + 0.5).astype(numpy.uint8), 'L'))
                    material_data.roughness_value = float(roughness.mean())
                else:
                    material_data.roughness_value = float(roughness)
            if (emissive := material_data.channel_images.pop(materials.MaterialLayers.SELF_ILLUMUNATION_COLOR, None)) is not None:
                emi = numpy.asarray(PilImage.open(io.BytesIO(emissive.packed_file.data)).convert('RGB'),
                                    numpy.float32)[..., 0] / 255
                strength = numpy.clip(emi * material_data.default_emission_strength * 10, 0, 1)
                if strength.any():
                    material_data.channel_images[materials.MaterialLayers.SELF_ILLUMUNATION_MASK] = self.bake_channel_image(
                        f'{name}_ems', PilImage.fromarray((strength * 255 + 0.5).astype(numpy.uint8), 'L'))
                material_data.default_emission_strength = 0.0
            dif_baked_gamma = numpy.clip(albedo, 0, 1) ** (1 / 2.2)
            dif_baked = self.bake_channel_image(
                f'{name}_dif', PilImage.fromarray((dif_baked_gamma * 255 + 0.5).astype(numpy.uint8), 'RGB'),
                colorspace='sRGB')
            if force_teamcolor:
                lum = numpy.array([0.2126, 0.7152, 0.0722])
                ratio = (dif_baked_gamma @ lum) / numpy.maximum(
                    (numpy.asarray(dif_pil, numpy.float32) / 255) @ lum, 1e-3)
                detail_multiplier = PilImage.fromarray(
                    (numpy.clip(ratio / 2, 0, 1) * 255 + 0.5).astype(numpy.uint8), 'L')
                material_data.teamcolor_images = bake_teamcolor_layers(
                    name, team, material_data.channel_images[materials.MaterialLayers.DIFFUSE],
                    detail_scale=2.0, detail_multiplier=detail_multiplier)
                material_data.teamcolor_images[materials.TeamcolorLayers.DEFAULT] = dif_baked
            material_data.channel_images[materials.MaterialLayers.DIFFUSE] = dif_baked
            material_data.channel_images.pop(materials.MaterialLayers.PATTERN, None)
            material_data.material_type = materials.MaterialType.DOW1
        materials.setup_material(mat, material_data, force_teamcolor)
        group_node = materials.get_shader_group_node(mat)
        for var, socket in (('alphaTest', 'alpha_test'), ('bHighlight', 'highlight'),
                            ('unitOcclusionFlag', 'occlusion_flag'), ('dirtVisibility', 'dirt_visibility')):
            if var == 'alphaTest' and (shader or '').endswith('_alpha'):
                continue  # blend variant always uses alpha; setup forced the gate on
            if var in shader_params and socket in group_node.inputs:
                group_node.inputs[socket].default_value = float(shader_params[var])
        if 'dirtVisibility' in shader_params:
            # the teamcolor group scales its team-spec dirt suppression by it too
            for node in mat.node_tree.nodes:
                if (node.bl_idname == 'ShaderNodeGroup' and node.node_tree is not None
                        and node.node_tree.name.startswith('ApplyTeamcolorDow2')
                        and 'dirt_visibility' in node.inputs):
                    node.inputs['dirt_visibility'].default_value = float(shader_params['dirtVisibility'])
        offset_node = materials.get_uv_offset_node(mat)
        if offset_node is not None:
            offset_node.inputs['Location'].default_value[0] = shader_params.get('uOffset', 0.0)
            offset_node.inputs['Location'].default_value[1] = shader_params.get('vOffset', 0.0)
        for var, label in (('badge1Tex', materials.TeamcolorLayers.BADGE), ('badge2Tex', materials.TeamcolorLayers.BANNER)):
            image = load(var)
            if image is None:
                continue
            for node in mat.node_tree.nodes:
                if node.bl_idname == 'ShaderNodeTexImage' and node.label in (label, f'UNUSED_{label.value}'):
                    node.image = image
        self.created_materials[name] = mat
        return mat

    def bake_channel_image(self, name: str, pil_image, colorspace: str = 'Non-Color') -> 'bpy.types.Image':
        with tempfile.TemporaryDirectory() as tmpdir:
            file = pathlib.Path(tmpdir) / f'{name}.png'
            pil_image.save(file, compress_level=1)
            image = bpy.data.images.load(str(file))
            image.colorspace_settings.name = colorspace
            image.pack()
            image.packed_files[-1].filepath = image.filepath_raw = str(self.unpacked_texture_folder / f'{name}.png')
            image.use_fake_user = True
        return image

    def load_image(self, texture_path: str | None):
        if not texture_path:
            return None
        key = texture_path.lower()
        if key in self.created_images:
            return self.created_images[key]
        posix_path = pathlib.PureWindowsPath(texture_path).as_posix()
        texture_data = self.layout.find(f'{posix_path}.dds')
        if texture_data is None:
            self.created_images[key] = None
            return None
        name = pathlib.PurePath(posix_path).name
        with tempfile.TemporaryDirectory() as tmpdir:
            file = pathlib.Path(tmpdir) / f'{name}.png'
            PilImage.open(io.BytesIO(texture_data.read_bytes())).save(file, compress_level=1)
            image = bpy.data.images.load(str(file))
            image.pack()
            image.packed_files[-1].filepath = image.filepath_raw = str(self.unpacked_texture_folder / f'{name}.png')
            image.use_fake_user = True
        if hasattr(image, 'dow_export_path'):
            image.dow_export_path = texture_path
        self.created_images[key] = image
        return image
