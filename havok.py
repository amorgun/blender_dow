"""Havok binary packfile decoding shared by the Dawn of War importers.

Dawn of War II ships Havok 4.5.1 packfiles (32-bit pointers, delta- or
interleaved-compressed clips) as loose .hkx files or inside .hkanim chunky
containers. Dawn of War III ships hk_2014.2.5 packfiles (64-bit pointers) as
.rhka files holding a HavokAnimSet with an embedded hkaSkeleton and per-clip
hkaAnimationBinding + hkaQuantizedAnimation payloads. Both pointer layouts are
parsed by one Packfile class; both games' clips decode into the same
Skeleton / Animation shapes.
"""
import dataclasses
import io
import math
import pathlib
import struct

from .chunky import ChunkReader, ChunkyVersion

PACKFILE_MAGIC = b'\x57\xe0\xe0\x57\x10\xc0\xc0\x10'

TRACK_DYNAMIC = 0
TRACK_STATIC = 1
TRACK_CLEAR = 2

IDENTITY = (0.0, 0.0, 0.0), (0.0, 0.0, 0.0, 1.0), (1.0, 1.0, 1.0)


class Packfile:
    """One reader for both packfile generations, dispatching on the header's
    pointer size: 32-bit (48-byte section records) and 64-bit (64-byte records).
    All accessor offsets are data-section relative."""

    def __init__(self, data: bytes):
        assert data[:8] == PACKFILE_MAGIC, 'not a Havok packfile'
        self.data = data
        self.pointer_size = data[0x10]
        num_sections = struct.unpack_from('<l', data, 0x14)[0]
        record_size = {4: 48, 8: 64}[self.pointer_size]
        self.sections = []
        pos = 0x40
        for _ in range(num_sections):
            name = data[pos:pos + 19].split(b'\0')[0].decode()
            start, local_fix, global_fix, virtual_fix, exports, imports, end = struct.unpack_from('<7L', data, pos + 20)
            self.sections.append((name, start, local_fix, global_fix, virtual_fix, exports, imports, end))
            pos += record_size

        name, start, size, *_ = next(s for s in self.sections if s[0] == '__classnames__')
        classnames = {}
        raw = data[start:start + size]
        pos = 0
        while pos + 5 < len(raw):
            if struct.unpack_from('<L', raw, pos)[0] == 0xffffffff:
                break
            end = raw.index(b'\0', pos + 5)
            classnames[pos + 5] = raw[pos + 5:end].decode()
            pos = end + 1

        name, self.base, local_fix, global_fix, virtual_fix, exports, *_ = next(s for s in self.sections if s[0] == '__data__')
        self.pointers = {}
        pos = self.base + local_fix
        while pos < self.base + global_fix:
            src, dst = struct.unpack_from('<2L', data, pos)
            pos += 8
            if src != 0xffffffff:
                self.pointers[src] = dst
        pos = self.base + global_fix
        while pos < self.base + virtual_fix:
            src, section, dst = struct.unpack_from('<3L', data, pos)
            pos += 12
            if src != 0xffffffff:
                self.pointers[src] = dst
        self.objects = {}
        pos = self.base + virtual_fix
        stop = self.base + exports
        while pos + 12 <= stop:
            obj, section, name_offset = struct.unpack_from('<3L', data, pos)
            pos += 12
            if obj == 0xffffffff:
                break
            self.objects.setdefault(classnames[name_offset], []).append(obj)

    def object(self, classname: str) -> int:
        return self.objects[classname][0]

    def ptr(self, offset: int) -> int:
        return self.pointers.get(offset)

    def u32(self, offset: int) -> int:
        return struct.unpack_from('<L', self.data, self.base + offset)[0]

    def i32(self, offset: int) -> int:
        return struct.unpack_from('<l', self.data, self.base + offset)[0]

    def f32(self, offset: int) -> float:
        return struct.unpack_from('<f', self.data, self.base + offset)[0]

    def read(self, offset: int, fmt: str):
        return struct.unpack_from(fmt, self.data, self.base + offset)

    def cstr(self, offset: int) -> str:
        end = self.data.index(b'\0', self.base + offset)
        return self.data[self.base + offset:end].decode('utf8', 'replace')


@dataclasses.dataclass
class Skeleton:
    name: str
    bone_names: list
    parents: list
    reference_pose: list  # (translation3, rotation_xyzw4, scale3) per bone
    float_slots: list = dataclasses.field(default_factory=list)  # float track slot names
    reference_floats: list = dataclasses.field(default_factory=list)  # rest value per slot


@dataclasses.dataclass
class Animation:
    duration: float
    num_poses: int
    track_bones: list  # bone index per track
    tracks: list       # tracks[track][pose] = (translation3, rotation_xyzw4, scale3)
    float_tracks: list = dataclasses.field(default_factory=list)  # (slot name, value per pose)

    @property
    def fps(self) -> float:
        return (self.num_poses - 1) / self.duration if self.duration else 0.0


@dataclasses.dataclass
class Clip:
    name: str
    skeleton: Skeleton
    animation: Animation


# ---------------------------------------------------------------------------
# Dawn of War II: Havok 4.5.1 (.hkx packfiles, .hkanim chunky containers)
# ---------------------------------------------------------------------------

def read_hkanim_set_names(data: bytes) -> list[str]:
    """Motion set names in a .hkanim container"""
    return list(read_hkanim(data))


def read_hkanim(data: bytes) -> dict:
    """Motion sets from a .hkanim container: {set name: [(clip name, packfile bytes)]}"""
    reader = ChunkReader(io.BytesIO(data), ChunkyVersion.V3)
    reader.skip_relic_chunky()
    reader.read_header('FOLDHAAS')
    sets = {}
    for chunk in reader.iter_chunks():
        if chunk.typeid != 'DATAHAWS':
            reader.skip(chunk.size)
            continue
        blob = reader.stream.read(chunk.size)
        count = struct.unpack_from('<L', blob, 0)[0]
        pos = 4
        names = []
        for _ in range(count):
            length = struct.unpack_from('<L', blob, pos)[0]
            pos += 4
            names.append(blob[pos:pos + length].decode())
            pos += length
        clips = []
        for name in names:
            size = struct.unpack_from('<L', blob, pos)[0]
            pos += 4
            if size:
                clips.append((pathlib.PureWindowsPath(name).name, blob[pos:pos + size]))
            pos += size
        if clips:
            sets[chunk.name] = clips
    return sets


def read_hkx_cameras(pf: Packfile) -> list:
    """hkxScene cameras embedded in a DoW2 clip packfile.

    Returns (from3, focus3, up3, fov_radians, clip_a, clip_b) per instance; the two
    clip values are the near/far pair (stored order varies, take min/max)."""
    cameras = []
    for obj in pf.objects.get('hkxCamera', []):
        values = pf.read(obj, '<15f')
        cameras.append((values[0:3], values[4:7], values[8:11], values[12], values[13], values[14]))
    return cameras


def read_skeleton(pf: Packfile) -> Skeleton:
    obj = pf.object('hkSkeleton')
    name = pf.cstr(pf.ptr(obj + 0x00))
    num_bones = pf.i32(obj + 0x10)
    parents = list(pf.read(pf.ptr(obj + 0x04), f'<{num_bones}h'))
    bones_arr = pf.ptr(obj + 0x0c)
    bone_names = [pf.cstr(pf.ptr(pf.ptr(bones_arr + i * 4))) for i in range(num_bones)]
    pose_off = pf.ptr(obj + 0x14)
    reference_pose = []
    for i in range(num_bones):
        t = pf.read(pose_off + i * 48, '<4f')
        r = pf.read(pose_off + i * 48 + 16, '<4f')
        s = pf.read(pose_off + i * 48 + 32, '<4f')
        reference_pose.append((t[:3], r, s[:3]))
    return Skeleton(name, bone_names, parents, reference_pose)


def read_animation(pf: Packfile) -> Animation:
    binding = pf.object('hkAnimationBinding')
    num_tracks_bound = pf.i32(binding + 0x08)
    track_bones = list(pf.read(pf.ptr(binding + 0x04), f'<{num_tracks_bound}h'))
    if 'hkInterleavedSkeletalAnimation' in pf.objects:
        return read_interleaved_animation(pf, track_bones)
    return read_delta_animation(pf, track_bones)


def read_interleaved_animation(pf: Packfile, track_bones: list) -> Animation:
    obj = pf.object('hkInterleavedSkeletalAnimation')
    duration = pf.f32(obj + 0x0c)
    num_tracks = pf.i32(obj + 0x10)
    transforms = pf.ptr(obj + 0x20)
    num_transforms = pf.i32(obj + 0x24)
    num_poses = num_transforms // num_tracks
    tracks = []
    for track in range(num_tracks):
        poses = []
        for pose in range(num_poses):
            offset = transforms + (pose * num_tracks + track) * 48
            translation = pf.read(offset, '<3f')
            rotation = pf.read(offset + 16, '<4f')
            scale = pf.read(offset + 32, '<3f')
            poses.append((translation, rotation, scale))
        tracks.append(poses)
    return Animation(duration, num_poses, track_bones, tracks)


def read_delta_animation(pf: Packfile, track_bones: list) -> Animation:
    obj = pf.object('hkDeltaCompressedSkeletalAnimation')
    duration = pf.f32(obj + 0x0c)
    num_tracks = pf.i32(obj + 0x10)
    num_poses = pf.i32(obj + 0x20)
    block_size = pf.i32(obj + 0x24)
    num_dynamic, offset_idx, scale_idx, bitwidth_idx = pf.read(obj + 0x2c, '<4L')
    quantized_idx, quantized_size, mask_idx = pf.read(obj + 0x3c, '<3L')
    static_idx = pf.u32(obj + 0x4c)
    total_block_size, last_block_size = pf.read(obj + 0x54, '<2L')
    buf_off = pf.ptr(obj + 0x5c)
    buf_len = pf.i32(obj + 0x60)
    buf = pf.data[pf.base + buf_off:pf.base + buf_off + buf_len]

    masks = struct.unpack_from(f'<{num_tracks}H', buf, mask_idx)
    num_static = (offset_idx - static_idx) // 4
    statics = struct.unpack_from(f'<{num_static}f', buf, static_idx)
    offsets = struct.unpack_from(f'<{num_dynamic}f', buf, offset_idx)
    scales = struct.unpack_from(f'<{num_dynamic}f', buf, scale_idx)
    bitwidths = buf[bitwidth_idx:bitwidth_idx + num_dynamic]

    # per-DOF source map, in track order: ('d', dynamic index) / ('s', static index) / ('c', identity value)
    sources = []
    dynamic_count = static_count = 0
    for track, mask in enumerate(masks):
        track_sources = []
        for component, (type_shift, flag_shift, size) in enumerate(((0, 6, 3), (2, 9, 4), (4, 13, 3))):
            track_type = mask >> type_shift & 3
            identity = IDENTITY[component]
            for dof in range(size):
                if track_type == TRACK_CLEAR:
                    track_sources.append(('c', identity[dof]))
                elif mask >> (flag_shift + size - 1 - dof) & 1:
                    track_sources.append(('d', dynamic_count))
                    dynamic_count += 1
                else:
                    track_sources.append(('s', static_count))
                    static_count += 1
        sources.append(track_sources)

    # decode dynamic DOF streams: per block, per DOF: float32 first value, then
    # (poses-1) deltas of bitwidth bits, delta = offset + scale * q / (2^bitwidth - 1)
    num_blocks = (num_poses + block_size - 1) // block_size
    dynamic = [[0.0] * num_poses for _ in range(num_dynamic)]
    for block in range(num_blocks):
        segment = quantized_idx + block * total_block_size
        pose0 = block * block_size
        poses_in_block = min(block_size, num_poses - pose0)
        for d in range(num_dynamic):
            bitwidth = bitwidths[d]
            value = struct.unpack_from('<f', buf, segment)[0]
            dynamic[d][pose0] = value
            stream = segment + 4
            quantum = scales[d] / ((1 << bitwidth) - 1)
            if bitwidth == 8:
                for i in range(poses_in_block - 1):
                    value += offsets[d] + quantum * buf[stream + i]
                    dynamic[d][pose0 + 1 + i] = value
            else:
                bitpos = 0
                for i in range(poses_in_block - 1):
                    q = 0
                    for bit in range(bitwidth):
                        q |= (buf[stream + (bitpos >> 3)] >> (bitpos & 7) & 1) << bit
                        bitpos += 1
                    value += offsets[d] + quantum * q
                    dynamic[d][pose0 + 1 + i] = value
            segment = stream + ((poses_in_block - 1) * bitwidth + 7 >> 3)

    tracks = []
    for track_sources in sources:
        poses = []
        for pose in range(num_poses):
            dofs = []
            for kind, value in track_sources:
                if kind == 'd':
                    dofs.append(dynamic[value][pose])
                elif kind == 's':
                    dofs.append(statics[value])
                else:
                    dofs.append(value)
            translation = tuple(dofs[0:3])
            rotation = tuple(dofs[3:7])
            norm = sum(x * x for x in rotation) ** 0.5
            if norm:
                rotation = tuple(x / norm for x in rotation)
            poses.append((translation, rotation, tuple(dofs[7:10])))
        tracks.append(poses)
    return Animation(duration, num_poses, track_bones, tracks)


# ---------------------------------------------------------------------------
# Dawn of War III: hk_2014 (.rhka HavokAnimSet packfiles)
# ---------------------------------------------------------------------------

def read_48bit_quat(buf: bytes, pos: int) -> list:
    """QT_48bit smallest-three packing: 3x uint16 = 45 value bits + 3 control bits.
    The three stored components are the smallest three (range +-1/sqrt2); the largest
    is reconstructed, its slot given by two control bits and its sign by a third.
    Result is (x, y, z, w). Ported from PredatorCZ/HavokLib Read48Quat."""
    x, y, z = struct.unpack_from('<3H', buf, pos)
    mask = 0x7FFF
    frac = 0.000043161  # 1 / (2^15 - 1) * 2/sqrt2; maps [0, 0x7FFF] -> [-1/r2, 1/r2]
    v = [((x & mask) - 0x3FFF) * frac,
         ((y & mask) - 0x3FFF) * frac,
         ((z & mask) - 0x3FFF) * frac]
    w2 = 1.0 - (v[0] * v[0] + v[1] * v[1] + v[2] * v[2])
    big = math.sqrt(w2) if w2 > 0.0 else 0.0
    result_shift = ((y >> 14) & 2) | ((x >> 15) & 1)
    big *= -1.0 if (z >> 15) else 1.0
    c = (v[0], v[1], v[2], big)
    return {0: [c[3], c[0], c[1], c[2]],
            1: [c[0], c[3], c[1], c[2]],
            2: [c[0], c[1], c[3], c[2]],
            3: [c[0], c[1], c[2], c[3]]}[result_shift]


def align16(n: int) -> int:
    return (n + 15) & ~15


def _read_hka_skeleton(pf: Packfile, obj: int) -> Skeleton:
    name = pf.cstr(pf.ptr(obj + 0x10))
    n = pf.u32(obj + 0x20)
    parents = list(pf.read(pf.ptr(obj + 0x18), f'<{n}h'))
    bones = pf.ptr(obj + 0x28)  # hkArray<hkaBone>, 16 bytes/elem
    bone_names = [pf.cstr(pf.ptr(bones + i * 16)) for i in range(n)]
    pose = pf.ptr(obj + 0x38)   # hkArray<hkQsTransform>, 48 bytes/elem
    reference_pose = []
    for i in range(n):
        t = pf.read(pose + i * 48, '<4f')
        r = pf.read(pose + i * 48 + 16, '<4f')
        s = pf.read(pose + i * 48 + 32, '<4f')
        reference_pose.append((t[:3], r, s[:3]))
    num_slots = pf.u32(obj + 0x60)
    slots = pf.ptr(obj + 0x58)  # hkArray<hkStringPtr>
    float_slots = [pf.cstr(pf.ptr(slots + i * 8)) for i in range(num_slots)]
    num_refs = pf.u32(obj + 0x50)
    reference_floats = list(pf.read(pf.ptr(obj + 0x48), f'<{num_refs}f')) if num_refs else []
    return Skeleton(name, bone_names, parents, reference_pose, float_slots, reference_floats)


def _animset_entries(pf: Packfile) -> list:
    """HavokAnimSet root -> [(clip name, binding obj, skeleton obj)]"""
    root = pf.object('HavokAnimSet')
    arr = pf.ptr(root + 0x18)
    count = pf.u32(root + 0x20)
    return [(pf.cstr(pf.ptr(arr + i * 24)), pf.ptr(arr + i * 24 + 8), pf.ptr(arr + i * 24 + 16))
            for i in range(count)]


def _decode_quantized(pf: Packfile, binding_obj: int, skeleton: Skeleton, clip_name: str) -> Animation:
    """hkaAnimationBinding + hkaQuantizedAnimation -> fully sampled Animation.
    Uniformly sampled quantized frames; components without a channel rest at the
    clip skeleton's reference pose."""
    anim_obj = pf.ptr(binding_obj + 0x18)
    n = pf.u32(binding_obj + 0x28)
    track_bones = list(pf.read(pf.ptr(binding_obj + 0x20), f'<{n}h')) if n else []

    dptr = pf.ptr(anim_obj + 0x38)
    dsz = pf.u32(anim_obj + 0x40)
    blob = pf.data[pf.base + dptr:pf.base + dptr + dsz]
    h = struct.unpack_from('<20H', blob, 0)
    num_tracks, num_frames = h[1], h[3]
    duration = struct.unpack_from('<f', blob, 8)[0]
    num_a, num_b, num_d, num_c = h[6], h[7], h[10], h[11]
    frame_stride = h[14]
    channels_a = struct.unpack_from(f'<{num_a}H', blob, h[15])
    channels_b = struct.unpack_from(f'<{num_b}H', blob, h[15] + num_a * 2)
    channels_d = struct.unpack_from(f'<{num_d}H', blob, h[16])
    channels_c = struct.unpack_from(f'<{num_c}H', blob, h[16] + num_d * 2)

    if frame_stride < align16(num_d * 2 + num_c * 6):
        raise ValueError(f'"{clip_name}": frame stride {frame_stride} < align16({num_d}*2 + {num_c}*6)')
    frames_off = h[19] + (h[19] - h[18])
    if num_frames and len(blob) < frames_off + num_frames * frame_stride:
        raise ValueError(f'"{clip_name}": blob too small for {num_frames} frames of {frame_stride} bytes')

    if not track_bones:
        track_bones = list(range(num_tracks))

    def reference(track):
        translation, rotation, scale = skeleton.reference_pose[track_bones[track]]
        return translation[:3], rotation[:4], scale[:3]

    tracks = [[list(map(list, reference(t))) for _ in range(num_frames)] for t in range(num_tracks)]

    def apply_scalar(channel_id, value, frame=None):
        track, dof = divmod(channel_id, 12)
        if track >= num_tracks:
            return  # float-track channel
        part, axis = (0, dof) if dof < 3 else (2, dof - 8)
        if not (0 <= axis < 3):
            raise ValueError(f'"{clip_name}": unsupported scalar dof {dof}')
        for pose in tracks[track] if frame is None else (tracks[track][frame],):
            pose[part][axis] = value

    def apply_rotation(channel_id, quat, frame=None):
        track = channel_id // 3
        if track >= num_tracks:
            return
        for pose in tracks[track] if frame is None else (tracks[track][frame],):
            pose[1][:] = quat

    for i, channel_id in enumerate(channels_a):
        apply_scalar(channel_id, struct.unpack_from('<f', blob, h[17] + 4 * i)[0])
    static_quats = h[17] + 4 * num_a
    for i, channel_id in enumerate(channels_b):
        apply_rotation(channel_id, read_48bit_quat(blob, static_quats + 6 * i))

    num_float = h[2]
    # float tracks only carry data when the dequantization region includes their
    # params AND the frame records have room for their u16s; otherwise the whole
    # clip rests at the skeleton's reference floats
    floats_animated = (
        num_float
        and h[19] - h[18] >= (num_d + num_float) * 4
        and frame_stride >= num_d * 2 + num_c * 6 + num_float * 2
    )
    num_params = num_d + (num_float if floats_animated else 0)
    bases = struct.unpack_from(f'<{num_params}f', blob, h[18])
    scales = struct.unpack_from(f'<{num_params}f', blob, h[19])
    for frame in range(num_frames):
        record = frames_off + frame * frame_stride
        if num_d:
            quantized = struct.unpack_from(f'<{num_d}H', blob, record)
            for j, channel_id in enumerate(channels_d):
                apply_scalar(channel_id, bases[j] + scales[j] * quantized[j], frame)
        quats = record + num_d * 2
        for i, channel_id in enumerate(channels_c):
            apply_rotation(channel_id, read_48bit_quat(blob, quats + 6 * i), frame)

    float_tracks = []
    if num_float:
        num_map = pf.u32(binding_obj + 0x38)
        slot_map = list(pf.read(pf.ptr(binding_obj + 0x30), f'<{num_map}h')) if num_map else list(range(num_float))
        for float_track in range(num_float):
            slot = slot_map[float_track] if float_track < len(slot_map) else float_track
            name = skeleton.float_slots[slot] if 0 <= slot < len(skeleton.float_slots) else f'float_slot_{slot}'
            if floats_animated:
                base, scale = bases[num_d + float_track], scales[num_d + float_track]
                offset = num_d * 2 + num_c * 6 + float_track * 2
                values = [
                    base + scale * struct.unpack_from('<H', blob, frames_off + frame * frame_stride + offset)[0]
                    for frame in range(num_frames)
                ]
            else:
                rest = skeleton.reference_floats[slot] if 0 <= slot < len(skeleton.reference_floats) else 0.0
                values = [rest] * num_frames
            float_tracks.append((name, values))

    tracks = [[tuple(map(tuple, pose)) for pose in track] for track in tracks]
    return Animation(duration, num_frames, track_bones, tracks, float_tracks)


def read_clip_names(data: bytes) -> list[str]:
    """Clip names in a .rhka animation set, without decoding any curves.
    The motion set is a clip name's first backslash-separated component."""
    pf = Packfile(data)
    return [name for name, binding, skeleton in _animset_entries(pf)]


def iter_clips(data: bytes, names: set = None):
    """Decode clips of a .rhka animation set, optionally only those in `names`."""
    pf = Packfile(data)
    skeletons = {}
    for clip_name, binding_obj, skeleton_obj in _animset_entries(pf):
        if names is not None and clip_name not in names:
            continue
        skeleton = skeletons.get(skeleton_obj)
        if skeleton is None:
            skeleton = skeletons[skeleton_obj] = _read_hka_skeleton(pf, skeleton_obj)
        yield Clip(clip_name, skeleton, _decode_quantized(pf, binding_obj, skeleton, clip_name))
