import bpy
from bpy_extras import anim_utils
import bmesh
import mathutils

from . import props, utils, textures


class DOW_OT_setup_property(bpy.types.Operator):
    """Set up a new property"""

    bl_idname = 'object.dow_setup_prop'
    bl_label = 'Create property'
    bl_options = {'REGISTER'}

    name: bpy.props.StringProperty()

    def execute(self, context):
        props.setup_property(context.obj, self.name)

        if hasattr(context, 'driver_obj'):
            props.setup_drivers(context.driver_obj, context.obj, self.name)
        return {'FINISHED'}


def make_prop_row(row, obj, prop_name: str, display_name: str = None, **extra_objs: dict):
    display_name = display_name or prop_name
    if prop_name in obj:
        row.prop(obj, f'["{prop_name}"]', text=display_name)
    else:
        row.context_pointer_set(name='obj', data=obj)
        for k, v in extra_objs.items():
            row.context_pointer_set(name=k, data=v)
        row.operator(DOW_OT_setup_property.bl_idname, text=f'Set up "{display_name}"').name = prop_name


class DOW_OT_setup_uv_mapping(bpy.types.Operator):
    """Set up uv_offset and uv_tiling nodes"""

    bl_idname = 'object.dow_setup_uv_mapping_node'
    bl_label = 'Create uv mapping nodes'
    bl_options = {'REGISTER'}

    def execute(self, context):
        mat = context.mat
        mat.use_nodes = True
        links = mat.node_tree.links
        uv_vector = utils.setup_uv_offset(mat, -1600, 200)
        for node_tex in mat.node_tree.nodes:
            if node_tex.bl_idname == 'ShaderNodeTexImage' and not node_tex.inputs['Vector'].links:
                links.new(uv_vector, node_tex.inputs['Vector'])
        return {'FINISHED'}


def make_prop_row(row, obj, prop_name: str, display_name: str = None, **extra_objs: dict):
    display_name = display_name or prop_name
    if prop_name in obj:
        row.prop(obj, f'["{prop_name}"]', text=display_name)
    else:
        row.context_pointer_set(name='obj', data=obj)
        for k, v in extra_objs.items():
            row.context_pointer_set(name=k, data=v)
        row.operator(DOW_OT_setup_property.bl_idname, text=f'Set up "{display_name}"').name = prop_name


class DOW_OT_attach_object(bpy.types.Operator):
    """Attach the object to the armature"""

    bl_idname = 'object.dow_attach_object'
    bl_label = 'Attach to armature'
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return (
            context.active_object is not None
            and context.active_object.type == 'ARMATURE'
            and any(o.type == 'MESH'
                    and props.get_mesh_prop_owner(o) != context.active_object
                    for o in context.selected_objects)
        )

    def execute(self, context):
        armature = context.active_object
        for obj in context.selected_objects:
            if obj.type != 'MESH':
                continue
            remote_prop_owner = props.get_mesh_prop_owner(obj)
            if remote_prop_owner == armature:
                continue
            obj.parent = armature
            for mod in obj.modifiers:
                if mod.type == 'ARMATURE':
                    mod.object = armature
            for prop in props.REMOTE_PROPS[obj.type]:
                prop_name = props.create_prop_name(prop, obj.name)
                props.clear_drivers(obj, prop_name)
                props.setup_drivers(obj, armature, prop_name)
                if remote_prop_owner is None:
                    continue
                if prop_name in remote_prop_owner:
                    armature[prop_name] = remote_prop_owner[prop_name]
            for mat in obj.data.materials:
                remote_prop_owner = props.get_material_prop_owner(mat)
                for prop in props.REMOTE_PROPS['MATERIAL']:
                    prop_name = props.create_prop_name(prop, mat.name)
                    props.clear_drivers(mat, prop_name)
                    props.setup_drivers(mat, armature, prop_name)
                    if remote_prop_owner is None or remote_prop_owner == armature:
                        continue
                    if prop_name in remote_prop_owner:
                        armature[prop_name] = remote_prop_owner[prop_name]
        return {'FINISHED'}


class DOW_OT_detach_object(bpy.types.Operator):
    """Detach the object from its armature"""

    bl_idname = 'object.dow_detach_object'
    bl_label = 'Detach from armature'
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return any(
            o.type == 'MESH' and props.get_mesh_prop_owner(o) is not None
            for o in context.selected_objects
        )

    def execute(self, context):
        for obj in context.selected_objects:
            if obj.type != 'MESH':
                continue
            remote_prop_owner = props.get_mesh_prop_owner(obj)
            for prop in props.REMOTE_PROPS[obj.type]:
                prop_name = props.create_prop_name(prop, obj.name)
                props.clear_drivers(obj, prop_name)
                if remote_prop_owner is not None:
                    remote_prop_owner.pop(prop_name, None)
            obj.parent = None
            for mod in obj.modifiers:
                if mod.type == 'ARMATURE':
                    mod.object = None
            for mat in obj.data.materials:
                remote_prop_owner = props.get_material_prop_owner(mat)
                for prop in props.REMOTE_PROPS['MATERIAL']:
                    prop_name = props.create_prop_name(prop, obj.name)
                    props.clear_drivers(mat, prop_name)
                    if remote_prop_owner is not None:
                        remote_prop_owner.pop(prop_name, None)
        return {'FINISHED'}


class DOW_OT_create_shadow(bpy.types.Operator):
    """Create a shadow mesh for the selected object"""

    bl_idname = 'object.dow_create_shadow'
    bl_label = 'Create shadow'
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and any(
            o.type == 'MESH' and utils.can_be_force_skinned(o)
            for o in context.selected_objects
        )

    def execute(self, context):
        for obj in context.selected_objects:
            if obj.type != 'MESH' or not utils.can_be_force_skinned(obj):
                continue
            data = obj.data.copy()
            data.materials.clear()
            shadow_obj = bpy.data.objects.new(f'{obj.name}_shadow', data)
            shadow_obj.parent = obj.parent
            shadow_obj.parent_type = obj.parent_type
            shadow_obj.parent_bone = obj.parent_bone
            for m in obj.modifiers:
                if m.type == 'ARMATURE':
                    armature_mod = shadow_obj.modifiers.new('Skeleton', 'ARMATURE')
                    armature_mod.object = m.object
                    break
            shadow_obj.modifiers.new('Weld', 'WELD')
            context.scene.collection.objects.link(shadow_obj)
            obj.dow_shadow_mesh = shadow_obj
        return {'FINISHED'}


class DOW_OT_autosplit_mesh(bpy.types.Operator):
    """Find and extract mesh parts that can be parented to a single bone"""

    bl_idname = 'object.dow_autosplit_mesh'
    bl_label = 'Autosplit mesh'
    bl_options = {'REGISTER', 'UNDO'}

    min_poly: bpy.props.IntProperty(
        name='Min poly count',
        description='Minimum size of an extracted mesh',
        default=16,
    )

    tolerance: bpy.props.FloatProperty(
        name='Tolerance',
        description='The maximum difference for the vertex weight to be considered fully attached to a bone',
        default=1e-4,
        min=0,
        soft_max=1,
        precision=4,
    )

    @classmethod
    def poll(cls, context):
        return context.mode == 'OBJECT' and any(
            o.type == 'MESH' and not utils.can_be_force_skinned(o)
            for o in context.selected_objects
        )

    def execute(self, context):
        for obj in list(context.selected_objects):
            if not (obj.type == 'MESH' and not utils.can_be_force_skinned(obj)):
                continue
            bones = None
            armature = utils.get_armature(obj)
            if armature is not None:
                bones = {b.name for b in armature.data.bones}
            vert2group = {}
            for v in obj.data.vertices:
                groups = v.groups
                if bones is not None:
                    groups = [g for g in groups if obj.vertex_groups[g.group].name in bones]
                groups = [g for g in groups if g.weight > 1e-4]
                groups.sort(key=lambda x: x.weight, reverse=True)
                if len(groups) == 0:
                    continue
                if groups[0].weight < 1 - self.tolerance:
                    continue
                vert2group[v.index] = groups[0].group
            poly_groups = {}
            extracted_groups = {}
            for p in obj.data.polygons:
                g = vert2group.get(p.vertices[0])
                if g is None:
                    continue
                if any(vert2group.get(v) != g for v in p.vertices):
                    continue
                poly_groups.setdefault(g, []).append(p.index)
                extracted_groups.setdefault(g, set()).update(p.vertices)

            def remove_unused(bm):
                verts = [v for v in bm.verts if not v.link_faces]
                for v in verts:
                    bm.verts.remove(v)

            removed_faces = set()
            for g, poly in poly_groups.items():
                vertex_group = obj.vertex_groups[g]
                if len(poly) < self.min_poly:
                    continue
                obj_copy = obj.copy()
                obj_copy.data = obj.data.copy()
                obj_copy.name = vertex_group.name
                if obj.animation_data is not None:
                    obj_copy.animation_data_clear()
                    obj_copy.animation_data_create()
                    orig_action = obj.animation_data.action
                    for action in bpy.data.actions:
                        obj.animation_data.action = action
                        obj_copy.animation_data.action = action
                        channelbag_orig = anim_utils.action_get_channelbag_for_slot(action, obj.animation_data.action_slot)
                        if channelbag_orig is None:
                            continue
                        channelbag_new = anim_utils.action_get_channelbag_for_slot(action, obj_copy.animation_data.action_slot)
                        obj_copy.animation_data.action_slot = action.slots.new(id_type='OBJECT', name=obj_copy.name)
                        channelbag_new = action.layers[0].strips[0].channelbags.new(obj_copy.animation_data.action_slot)
                        new_fcurves = channelbag_new.fcurves
                        for fcurve in list(channelbag_orig.fcurves):
                            if fcurve.is_empty:
                                continue
                            new_fcurve = new_fcurves.new(fcurve.data_path, index=fcurve.array_index)
                            if fcurve.group is not None:
                                group_data = channelbag_new.groups.get(fcurve.group.name)
                                if group_data is None:
                                    group_data = channelbag_new.groups.new(fcurve.group.name)
                                new_fcurve.group = group_data
                            for k in fcurve.keyframe_points:
                                new_fcurve.keyframe_points.insert(*k.co)
                    obj.animation_data.action = orig_action
                    obj_copy.animation_data.action = orig_action
                for c in obj.users_collection:
                    c.objects.link(obj_copy)
                bm = bmesh.new()
                bm.from_mesh(obj_copy.data)
                bm.verts.ensure_lookup_table()
                bm.faces.ensure_lookup_table()
                keep = set(poly)
                for p in bm.faces:
                    if p.index not in keep:
                        bm.faces.remove(p)
                    else:
                        removed_faces.add(p.index)
                remove_unused(bm)
                bm.to_mesh(obj_copy.data)
                if bones is not None:
                    vertex_group_copy = obj_copy.vertex_groups[vertex_group.name]
                    for g in obj_copy.vertex_groups:
                        if g.name in bones and g != vertex_group_copy:
                            obj_copy.vertex_groups.remove(g)
                        vertex_group_copy.add([v.index for v in obj_copy.data.vertices], 1, 'REPLACE')
            if bones is not None:
                for group_idx, verts in extracted_groups.items():
                    for group in obj.vertex_groups:
                        if group.index == group_idx:
                            group.add(list(verts), 1, 'REPLACE')
                        elif group.name in bones:
                            group.remove(list(verts))
            bm = bmesh.new()
            bm.from_mesh(obj.data)
            bm.verts.ensure_lookup_table()
            bm.faces.ensure_lookup_table()
            for p in bm.faces:
                if p.index in removed_faces:
                    bm.faces.remove(p)
            remove_unused(bm)
            bm.to_mesh(obj.data)
        return {'FINISHED'}


class DOW_OT_convert_to_marker(bpy.types.Operator):
    """Convert bone to marker"""

    bl_idname = 'object.dow_convert_to_marker'
    bl_label = 'Convert Bone to Marker'
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return (
            context.mode in ('POSE', 'EDIT_ARMATURE')
            and (
                context.selected_editable_bones
                or context.selected_pose_bones_from_active_object
            )
        )

    def execute(self, context):
        armature_obj = context.active_object
        armature = armature_obj.data
        if context.mode =='POSE':
            bone_names = [b.name for b in context.selected_pose_bones_from_active_object]
            bpy.ops.object.mode_set(mode='EDIT')
            orig_mode = 'POSE'
        else:
            bone_names = [b.name for b in context.selected_editable_bones]
            orig_mode = 'EDIT'
        custom_shape_template = bpy.data.objects.get('marker_custom_shape_template')
        marker_collection = None
        for collection in armature.collections:
            if collection.name.lower() != 'markers':
                continue
            marker_collection = collection
            break
        else:
            marker_collection = armature.collections.new('Markers')
        for bone_name in bone_names:
            bone = armature.edit_bones[bone_name]
            if custom_shape_template is None:
                custom_shape_template = bpy.data.objects.new('marker_custom_shape_template', None)
                custom_shape_template.empty_display_type = 'ARROWS'
                custom_shape_template.use_fake_user = True
            bone = armature.edit_bones[bone_name]
            bone.length = 0.15
            bone.color.palette = 'CUSTOM'
            bone.color.custom.normal = mathutils.Color([14, 255, 2]) / 255
            bone.color.custom.active = mathutils.Color([255, 98, 255]) / 255
            marker_collection.assign(bone)
        bpy.ops.object.mode_set(mode='OBJECT')
        for bone_name in bone_names:
            pose_bone = armature_obj.pose.bones[bone_name]
            pose_bone.custom_shape = custom_shape_template
            pose_bone.custom_shape_scale_xyz = -1, 1, 1
        bpy.ops.object.mode_set(mode=orig_mode)
        return {'FINISHED'}


class DOW_OT_convert_to_bone(bpy.types.Operator):
    """Convert marker to bone"""

    bl_idname = 'object.dow_convert_to_bone'
    bl_label = 'Convert Marker to Bone'
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return (
            context.mode in ('POSE', 'EDIT_ARMATURE')
            and (
                context.selected_editable_bones
                or context.selected_pose_bones_from_active_object
            )
        )

    def execute(self, context):
        armature_obj = context.active_object
        armature = armature_obj.data
        if context.mode =='POSE':
            bone_names = [b.name for b in context.selected_pose_bones_from_active_object]
            bpy.ops.object.mode_set(mode='EDIT')
            orig_mode = 'POSE'
        else:
            bone_names = [b.name for b in context.selected_editable_bones]
            orig_mode = 'EDIT'
        for bone_name in bone_names:
            bone = armature.edit_bones[bone_name]
            for collection in armature.collections:
                if collection.name.lower() != 'markers':
                    continue
                collection.unassign(bone)
            bone.color.palette = 'DEFAULT'
            if len(bone.children) == 1:
                new_length = (bone.children[0].head - bone.head).length
                if new_length > 1e-3:
                    bone.length = new_length
            else:
                bone.length = 0.5
        bpy.ops.object.mode_set(mode='OBJECT')
        for bone_name in bone_names:
            pose_bone = armature_obj.pose.bones[bone_name]
            pose_bone.custom_shape = None
        bpy.ops.object.mode_set(mode=orig_mode)
        return {'FINISHED'}


class DOW_OT_select_all_actions(bpy.types.Operator):
    """Change selection for multiple actions"""

    bl_idname = 'object.dow_select_all_actions'
    bl_label = 'Select all actions'
    bl_options = {'REGISTER'}

    status: bpy.props.BoolProperty()

    def execute(self, context):
        for i in context.popup_operator.actions:
            i.selected = self.status
        return {'FINISHED'}


class DOW_UL_action_settings(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        row = layout.row()
        row.prop(item, 'selected', text=item.name)


class ActionSettings(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty()
    selected: bpy.props.BoolProperty()


class DOW_OT_batch_configure_invisible(bpy.types.Operator):
    """Configure force_invisible for multiple animations"""

    bl_idname = 'object.dow_batch_configure_force_invisible'
    bl_label = 'Batch configure force_invisible'
    bl_options = {'REGISTER'}

    mesh_name: bpy.props.StringProperty()
    actions: bpy.props.CollectionProperty(type=ActionSettings)
    selected_index: bpy.props.IntProperty()

    @classmethod
    def poll(cls, context):
        return (
            context.active_object.type == 'MESH'
            and (
                mesh_use_slotted_actions(context.active_object, 'force_invisible')
                or props.get_mesh_prop_owner(context.active_object) is not None
            )
        )

    def execute(self, context):
        for obj in context.selected_objects:
            if obj.type != 'MESH':
                continue
            for d in self.actions:
                if d.name not in bpy.data.actions:
                    continue
                action = bpy.data.actions.get(d.name)
                _set_force_invisible_inner(obj, d.selected, action)
        return {'FINISHED'}

    def invoke(self, context, event):
        wm = context.window_manager
        self.actions.clear()
        if mesh_use_slotted_actions(context.active_object, 'force_invisible'):
            animated_obj = context.active_object
            prop_name = 'force_invisible'
        else:
            animated_obj = props.get_mesh_prop_owner(context.active_object)
            prop_name = props.create_prop_name('force_invisible', context.active_object.name)
        anim_data = get_animation_data(animated_obj)
        fcurve_data_paths = [f'["{prop_name}"]', f"['{prop_name}']"]
        for action in bpy.data.actions:
            it = self.actions.add()
            it.name = action.name
            value = False
            if anim_data is not None:
                orig_action = anim_data.action
                anim_data.action = action
                value = bool(get_fcurve_flag(anim_data, fcurve_data_paths, default=False))
                anim_data.action = orig_action
            it.selected = value
        return wm.invoke_props_dialog(self, width=500)

    def draw(self, context):
        layout = self.layout
        layout.row().label(text='Actions')
        layout.template_list('DOW_UL_action_settings', '', self, 'actions', self, 'selected_index')
        row = layout.row()
        row.context_pointer_set(name='popup_operator', data=self)
        row.operator(DOW_OT_select_all_actions.bl_idname, text='Deselect All').status=False
        row.operator(DOW_OT_select_all_actions.bl_idname, text='Select All').status=True


class DOW_OT_batch_bake_actions(bpy.types.Operator):
    """Bake multiple animations"""

    bl_idname = 'object.dow_batch_bake_actions'
    bl_label = 'Batch bake actions'
    bl_options = {'REGISTER'}

    actions: bpy.props.CollectionProperty(type=ActionSettings)
    step: bpy.props.IntProperty(default=1)
    clear_constraints: bpy.props.BoolProperty(default=True)
    selected_index: bpy.props.IntProperty()

    @classmethod
    def poll(cls, context):
        return (
            context.active_object is not None
            and context.active_object.type == 'ARMATURE'
        )

    def execute(self, context):
        actions_to_bake = [bpy.data.actions.get(d.name) for d in self.actions if d.name in bpy.data.actions and d.selected]
        anim_data = context.active_object.animation_data
        orig_action = anim_data.action
        for action_idx, action in enumerate(actions_to_bake):
            anim_data.action = action
            last_frame = int(action.frame_end)
            frames = list(range(int(action.frame_start), last_frame + 1, self.step))
            if frames[-1] != last_frame:
                frames.append(last_frame)
            baked = anim_utils.bake_action(
                bpy.context.active_object,
                action=None,
                frames=frames,
                bake_options=anim_utils.BakeOptions(
                    only_selected=False,
                    do_pose=True,
                    do_object=False,
                    do_visual_keying=True,
                    do_constraint_clear=self.clear_constraints and action_idx == len(actions_to_bake) - 1,
                    do_parents_clear=False,
                    do_clean=True,
                    do_location=True,
                    do_rotation=True,
                    do_scale=True,
                    do_bbone=False,
                    do_custom_props=False),
            )
            orig_channelbag = anim_utils.action_get_channelbag_for_slot(action, anim_data.action_slot)
            baked_channelbag = anim_utils.action_get_channelbag_for_slot(baked, anim_data.action_slot)
            if orig_channelbag.fcurves is None and baked_channelbag.fcurves is not None:
                orig_channelbag = action.layers[0].strips[0].channelbags.new(anim_data.action_slot)
            for baked_fcurve in baked_channelbag.fcurves or []:
                orig_fcurve = orig_channelbag.fcurves.find(baked_fcurve.data_path, index=baked_fcurve.array_index)
                if orig_fcurve is not None:
                    orig_fcurve.keyframe_points.clear()
                else:
                    orig_fcurve = orig_channelbag.fcurves.new(baked_fcurve.data_path, index=baked_fcurve.array_index)
                for k in baked_fcurve.keyframe_points:
                    orig_fcurve.keyframe_points.insert(*k.co)
            bpy.data.actions.remove(baked, do_unlink=True)
        anim_data.action = orig_action
        return {'FINISHED'}

    def invoke(self, context, event):
        wm = context.window_manager
        self.actions.clear()
        anim_data = context.active_object.animation_data
        for action in bpy.data.actions:
            it = self.actions.add()
            it.name = action.name
            it.selected = anim_data is not None and anim_data.action == action
        return wm.invoke_props_dialog(self, width=500)

    def draw(self, context):
        layout = self.layout
        layout.row().prop(self, 'step', text='Step')
        layout.row().prop(self, 'clear_constraints', text='Clear Constraints')
        layout.row().label(text='Actions')
        layout.template_list('DOW_UL_action_settings', '', self, 'actions', self, 'selected_index')
        row = layout.row()
        row.context_pointer_set(name='popup_operator', data=self)
        row.operator(DOW_OT_select_all_actions.bl_idname, text='Deselect All').status=False
        row.operator(DOW_OT_select_all_actions.bl_idname, text='Select All').status=True


class DOW_OT_make_material_animated(bpy.types.Operator):
    """Make material animated"""

    bl_idname = 'object.dow_make_animated_material'
    bl_label = 'Make animated'
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return (
            context.material is not None
            and getattr(context, 'current_action', None) is not None
        )

    def execute(self, context):
        context.material.use_nodes = True
        node_tree = context.material.node_tree
        if node_tree.animation_data is None:
            node_tree.animation_data_create()
        node_tree.animation_data.action = context.current_action
        return {'FINISHED'}


def get_animation_data(obj):
    if obj.animation_data is not None:
        return obj.animation_data
    return None


def get_animation_data_with_action(obj):
    data = get_animation_data(obj)
    if data is not None and data.action is not None:
        return data
    return None


def get_fcurve_flag(anim_data, data_paths, default):
    channelbag = anim_utils.action_get_channelbag_for_slot(anim_data.action, anim_data.action_slot)
    if channelbag is None:
        return default
    for fcurve in channelbag.fcurves:
        if fcurve.is_empty:
            continue
        if any(fcurve.data_path == p for p in data_paths):
            return fcurve.keyframe_points[0].co[1]
    return default


def set_fcurve_flag(anim_data, data_paths, value, default, group: str, obj_name: str):
    action = anim_data.action
    utils.ensure_channelbag_exists(action, anim_data.action_slot)
    # slot = action.slots.new(id_type='OBJECT', name="Suzanne")
    channelbag = anim_utils.action_get_channelbag_for_slot(action, anim_data.action_slot)
    fcurves = channelbag.fcurves
    for fcurve in list(fcurves):
        if fcurve.is_empty:
            continue
        if any(fcurve.data_path == p for p in data_paths):
            fcurves.remove(fcurve)
    if value != default:
        fcurve = fcurves.new(data_paths[0])
        fcurve.keyframe_points.insert(0, float(value))
        if group is not None:
            group_data = channelbag.groups.get(group)
            if group_data is None:
                group_data = action.groups.new(group)
            fcurve.group = group_data


def mesh_use_slotted_actions(mesh, prop_name: str):
    remote_prop_owner = props.get_mesh_prop_owner(mesh)
    if remote_prop_owner is None:
        return True
    if prop_name == 'force_invisible':
        prop_name = props.create_prop_name(prop_name, mesh.name)
        return prop_name not in remote_prop_owner
    else:
        if mesh.animation_data is None:
            return True
        for d in mesh.animation_data.drivers:
            if (d.data_path, d.array_index) == ('color', 3):
                return False
        return True


def get_force_invisible(self):
    if mesh_use_slotted_actions(self, 'force_invisible'):
        animated_obj = self
        prop_name = 'force_invisible'
    else:
        animated_obj = props.get_mesh_prop_owner(self)
        prop_name = props.create_prop_name('force_invisible', self.name)
    anim_data = get_animation_data_with_action(animated_obj)
    if anim_data is None:
        return False
    return bool(get_fcurve_flag(anim_data, [f'["{prop_name}"]', f"['{prop_name}']"], default=False))


def set_force_invisible(self, val):
    _set_force_invisible_inner(self, val)


def _set_force_invisible_inner(obj, val, action=None):
    if mesh_use_slotted_actions(obj, 'force_invisible'):
        animated_obj = obj
        prop_name = 'force_invisible'
        obj[prop_name] = val
        fcurve_group = None
    else:
        animated_obj = remote_prop_owner = props.get_mesh_prop_owner(obj)
        prop_name = props.create_prop_name('force_invisible', obj.name)
        remote_prop_owner[prop_name] = val
        fcurve_group = obj.name
    anim_data = get_animation_data(animated_obj)
    if anim_data is None or anim_data.action is None:
        parent = props.get_mesh_prop_owner(animated_obj)
        parent_anim_data = get_animation_data_with_action(parent)
        if action is None and parent_anim_data is None:
            return
        if anim_data is None:
            anim_data = animated_obj.animation_data_create()
        final_action = anim_data.action = action if action is not None else parent_anim_data.action
    else:
        final_action = anim_data.action
        if action is not None:
            anim_data.action = action
    if anim_data.action_slot is None:
        anim_data.action_slot = anim_data.action.slots.new(id_type='OBJECT', name=obj.name)
    set_fcurve_flag(anim_data, [f'["{prop_name}"]', f"['{prop_name}']"], val, default=False, group=fcurve_group, obj_name=obj.name)
    anim_data.action = final_action


def get_stale(self):
    anim_data = get_animation_data_with_action(bpy.context.active_object)
    if anim_data is None:
        return False
    return bool(get_fcurve_flag(anim_data, [f'pose.bones["{self.name}"]["stale"]', f'pose.bones["{self.name}"]["Stale"]'], default=False))


def set_stale(self, val):
    prop = None
    for k in ['stale', 'Stale']:
        if k in self:
            prop = k
            break
    if prop is None:
        prop = 'stale'
        props.setup_property(self, prop, True)
    self[prop] = val
    anim_data = get_animation_data_with_action(bpy.context.active_object)
    if anim_data is None:
        return
    if anim_data.action_slot is None:
        anim_data.action_slot = anim_data.action.slots.new(id_type='OBJECT', name='Skeleton')
    set_fcurve_flag(anim_data, [f'pose.bones["{self.name}"]["stale"]', f'pose.bones["{self.name}"]["Stale"]'], val, default=False, group=self.name, obj_name=self.name)


def get_autoswitch_actions(self):
    res = self.get('autoswitch_actions')
    if res is not None:
        return res
    prefs = bpy.context.preferences.addons[__package__].preferences
    return prefs.autoswitch_actions


def set_autoswitch_actions(self, val):
    self['autoswitch_actions'] = val
    bpy.context.scene.dow_autoswitch_actions = 'TRUE' if val else 'FALSE'


class DowTools(bpy.types.Panel):
    bl_label = 'DoW Tools'
    bl_idname = 'VIEW3D_PT_dow_tools'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'DoW'


    def draw(self, context):
        layout = self.layout
        if context.active_object is not None:
            if context.active_object.type == 'MESH':
                layout.row().prop(context.active_object, 'name')

                if utils.can_be_force_skinned(context.active_object):
                    layout.row().label(text='Force Skinning: Yes')
                    layout.row().prop(context.active_object, 'dow_shadow_mesh')
                else:
                    layout.row().label(text='Force Skinning: No')
                    layout.row().label(text='Cannot have a shadow', icon='ERROR')
                make_prop_row(layout, context.active_object, 'xref_source')
                layout.separator()
                current_action = None
                current_anim_data = get_animation_data_with_action(context.active_object)
                remote_prop_owner = props.get_mesh_prop_owner(context.active_object)
                if current_anim_data is not None:
                    current_action = current_anim_data.action
                else:
                    parent = context.active_object.parent
                    if parent is not None:
                        parent_anim_data = get_animation_data_with_action(parent)
                        if parent_anim_data is not None:
                            current_action = parent_anim_data.action
                if current_action is not None:
                    layout.row().prop(current_action, 'name', text='Action')
                    row = layout.row()
                    row.prop(context.active_object, 'dow_force_invisible')
                    op = row.operator(DOW_OT_batch_configure_invisible.bl_idname, text='', icon='OPTIONS')
                    op.mesh_name = context.active_object.name
                else:
                    row = layout.row()
                    op = row.operator(DOW_OT_batch_configure_invisible.bl_idname, text='Batch set force_invisible', icon='OPTIONS')
                    op.mesh_name = context.active_object.name
                if mesh_use_slotted_actions(context.active_object, 'visibility'):
                    layout.row().prop(context.active_object, 'color', index=3, text='visibility')
                elif current_action:
                    make_prop_row(
                        layout.row(),
                        remote_prop_owner,
                        prop_name=props.create_prop_name('visibility', context.active_object.name),
                        display_name='visibility',
                        driver_obj=context.active_object,
                    )
        if context.active_pose_bone is not None:
            layout.row().prop(context.active_pose_bone, 'name')
            current_anim_data = get_animation_data_with_action(context.active_object)
            if current_anim_data is not None:
                current_action = current_anim_data.action
                layout.row().prop(current_action, 'name', text='Action')
                layout.row().prop(context.active_pose_bone, 'dow_stale')
        layout.separator()
        layout.row().prop(context.scene, 'dow_update_animations')
        layout.row().prop(context.scene, 'dow_autoswitch_actions_view')
        layout.row().operator(DOW_OT_autosplit_mesh.bl_idname)
        layout.row().operator(DOW_OT_create_shadow.bl_idname)
        layout.row().operator(DOW_OT_batch_bake_actions.bl_idname)
        layout.row().operator(DOW_OT_convert_to_marker.bl_idname)
        layout.row().operator(DOW_OT_convert_to_bone.bl_idname)


class DowMaterialTools(bpy.types.Panel):
    bl_label = 'DoW Tools'
    bl_idname = 'NODEEDITOR_PT_dow_tools'
    bl_space_type = 'NODE_EDITOR'
    bl_region_type = 'UI'
    bl_category = 'DoW'

    @classmethod
    def poll(cls, context):
        return (
            context.area.ui_type == 'ShaderNodeTree'
            and context.active_object is not None
            and context.active_object.active_material is not None
            and context.active_object.active_material.node_tree is not None
        )

    def draw(self, context):
        layout = self.layout
        mat = context.material
        layout.use_property_decorate = mat.node_tree.animation_data is not None
        if context.active_node is not None and hasattr(context.active_node, 'dow_image_label'):
            layout.row().prop(context.active_node, 'dow_image_label', text='Layer')
            try:
                textures.MaterialLayers(context.active_node.dow_image_label.lower())
                if context.active_node.image is None:
                    layout.row().label(text='No image selected')
                else:
                    layout.row().prop(context.active_node.image, 'dow_export_path', text="Single Image Path")
            except ValueError:
                pass
            layout.separator()
        remote_prop_owner = props.get_material_prop_owner(mat)
        if remote_prop_owner is None:
            node = None
            if mat.node_tree is not None:
                node = utils.get_uv_offset_node(mat)
                for shader_node in mat.node_tree.nodes:
                    if shader_node.bl_idname == 'ShaderNodeBsdfPrincipled':
                        layout.row().prop(shader_node.inputs['Metallic'], 'default_value', text='Default Reflections')
                        layout.row().prop(shader_node.inputs['Roughness'], 'default_value', text='Roughness')
                        break
            if node is not None:
                row = layout.row()
                row.label(text='uv_offset')
                row.prop(node.inputs[1], 'default_value', text='', index=0)
                row.prop(node.inputs[1], 'default_value', text='', index=1)
                row = layout.row()
                row.label(text='uv_tiling')
                row.prop(node.inputs[3], 'default_value', text='', index=0)
                row.prop(node.inputs[3], 'default_value', text='', index=1)
            else:
                row = layout.row()
                row.context_pointer_set(name='mat', data=mat)
                row.operator(DOW_OT_setup_uv_mapping.bl_idname, text=f'Set up "uv_offset"')
        else:
            for prop in props.REMOTE_PROPS['MATERIAL']:
                make_prop_row(
                    layout,
                    remote_prop_owner,
                    prop_name=props.create_prop_name(prop, mat.name),
                    display_name=prop,
                    driver_obj=mat,
                )
        for prop in [
            'full_path',
            'internal',
        ]:
            make_prop_row(layout, mat, prop)
        if (
            mat.node_tree is None
            or mat.node_tree.animation_data is None
        ):
            if context.active_object is not None:
                obj = context.active_object
                current_actions = []
                if obj.type == 'MESH':
                    for m in obj.modifiers:
                        if m.type == 'ARMATURE':
                            if m.object is not None and m.object.animation_data is not None:
                                try:
                                    current_actions.append(m.object.animation_data.action)
                                    break
                                except AttributeError:
                                    pass
                    try:
                        current_actions.append(obj.parent.animation_data.action)
                    except AttributeError:
                        pass
                try:
                    current_actions.append(obj.animation_data.action)
                except AttributeError:
                    pass
            row = layout.row()
            if current_actions:
                row.context_pointer_set(name='current_action', data=current_actions[0])
            row.operator(DOW_OT_make_material_animated.bl_idname)
        else:
            layout.row().label(text='Material is animated')


IMAGE_LAYERS = [
    ('NONE', 'None', ''),
    *[(k, k.replace('_', ' ').capitalize(), k) for k in textures.MaterialLayers],
    *[(k, f'Teamcolor {k.capitalize()}', f'color_layer_{k.value}') for k in [
        textures.TeamcolorLayers.DEFAULT,
        textures.TeamcolorLayers.DIRT,
        textures.TeamcolorLayers.PRIMARY,
        textures.TeamcolorLayers.SECONDARY,
        textures.TeamcolorLayers.TRIM,
        textures.TeamcolorLayers.WEAPONS,
        textures.TeamcolorLayers.EYES
    ]],
    *[(k, k.capitalize(), k) for k in [
        textures.TeamcolorLayers.BADGE,
        textures.TeamcolorLayers.BANNER,
    ]],
]


def get_image_label(self):
    layers = {i[2]: idx for idx, i in enumerate(IMAGE_LAYERS)}
    return layers.get(self.label.lower(), 0)


def set_image_label(self, val):
    label = IMAGE_LAYERS[val][2]
    if val != 0:
        for node in bpy.context.material.node_tree.nodes:
            if hasattr(node, 'dow_image_label') and node.label.lower() == label.lower():
                node.label = ''
    self.label = label


@bpy.app.handlers.persistent
def rename_listener(scene, depsgraph):
    if not depsgraph.id_type_updated('OBJECT'):
        return

    for update in depsgraph.updates:
        if isinstance(update.id, bpy.types.Armature):
            collection = bpy.data.objects
            arm = collection.get(update.id.name)
            if not arm or arm.type != 'ARMATURE':
                continue
            objs = collection = arm.pose.bones
            remote_prop_owner = None
            obj_type = 'ARMATURE'
        elif isinstance(update.id, bpy.types.Material):
            collection = bpy.data.materials
            obj = collection.get(update.id.name)
            objs = [obj] if obj else []
            remote_prop_owner = props.get_material_prop_owner(obj)
            obj_type = 'MATERIAL'
        elif isinstance(update.id, bpy.types.Object):
            collection = bpy.data.objects
            obj = collection.get(update.id.name)
            objs = [obj] if obj else []
            remote_prop_owner = props.get_mesh_prop_owner(obj)
            obj_type = 'MESH'
        else:
            continue

        for obj in objs:
            if not (obj and hasattr(obj, 'dow_name')):
                continue
            old_name = obj.dow_name
            if old_name == obj.name:
                continue
            obj.dow_name = obj.name
            is_renamed = True
            if old_name in collection:
                is_renamed = False  # Object copied
            rename_props = []
            if obj_type == 'ARMATURE' and is_renamed:
                rename_props.append((f'bones["{old_name}"]', f'bones["{obj.name}"]'))
            if remote_prop_owner is not None:
                for prop_prefix in props.REMOTE_PROPS.get(obj_type, []):
                    old_prop_name = props.create_prop_name(prop_prefix, old_name)
                    new_prop_name = props.create_prop_name(prop_prefix, obj.name)
                    if old_prop_name in remote_prop_owner:
                        props.setup_property(remote_prop_owner, new_prop_name, remote_prop_owner[old_prop_name])
                        if is_renamed:
                            remote_prop_owner.pop(old_prop_name)
                            rename_props.append((f'["{old_prop_name}"]', f'["{new_prop_name}"]'))
                        props.clear_drivers(obj, old_prop_name)
                        props.setup_drivers(obj, remote_prop_owner, new_prop_name)
            if not scene.dow_update_animations:
                continue
            if not is_renamed:
                continue
            for rename_from, rename_to in rename_props:
                for action in bpy.data.actions:
                    for fcurve in action.fcurves:
                        if rename_from in fcurve.data_path:
                            fcurve.data_path = fcurve.data_path.replace(rename_from, rename_to)


@bpy.app.handlers.persistent
def action_change_listener(scene, depsgraph):
    if not depsgraph.id_type_updated('OBJECT'):
        return

    for update in depsgraph.updates:
        obj = update.id.original
        if not hasattr(obj, 'dow_last_action'):
            continue
        if (animation_data := get_animation_data_with_action(obj)) is None:
            continue
        action = animation_data.action
        if action.session_uid == obj.dow_last_action:
            continue
        obj.dow_last_action = action.session_uid
        if not scene.dow_autoswitch_actions_view:
            continue

        for d in utils.iter_animatable():
            if (animation_data := get_animation_data_with_action(d)) is None:
                continue
            if action.session_uid == d.dow_last_action:
                continue
            animation_data.action = action
            d.dow_last_action = action.session_uid


@bpy.app.handlers.persistent
def init_dow_props(filename: str = ''):
    if bpy.context.scene.dow_autoswitch_actions == 'DEFAULT':
        prefs = bpy.context.preferences.addons[__package__].preferences
        bpy.context.scene.dow_autoswitch_actions_view = prefs.autoswitch_actions
        bpy.context.scene.dow_autoswitch_actions = 'DEFAULT'
    for obj in bpy.data.objects:
        if obj.type == 'ARMATURE' and obj.pose:
            for b in obj.pose.bones:
                b.dow_name = b.name
        if obj.type == 'MESH':
            obj.dow_name = obj.name
        if (animation_data := get_animation_data_with_action(obj)) is not None:
            obj.dow_last_action = animation_data.action.session_uid
    for obj in bpy.data.materials:
        obj.dow_name = obj.name


def register():
    bpy.utils.register_class(DowTools)
    bpy.utils.register_class(DowMaterialTools)
    bpy.utils.register_class(DOW_OT_setup_property)
    bpy.utils.register_class(DOW_OT_setup_uv_mapping)
    bpy.utils.register_class(DOW_OT_attach_object)
    bpy.utils.register_class(DOW_OT_detach_object)
    bpy.utils.register_class(DOW_OT_create_shadow)
    bpy.utils.register_class(DOW_OT_autosplit_mesh)
    bpy.utils.register_class(DOW_OT_convert_to_marker)
    bpy.utils.register_class(DOW_OT_convert_to_bone)
    bpy.utils.register_class(DOW_OT_select_all_actions)
    bpy.utils.register_class(DOW_UL_action_settings)
    bpy.utils.register_class(ActionSettings)
    bpy.utils.register_class(DOW_OT_batch_configure_invisible)
    bpy.utils.register_class(DOW_OT_batch_bake_actions)
    bpy.utils.register_class(DOW_OT_make_material_animated)
    for t in [
        bpy.types.Object,
        bpy.types.Material,
        bpy.types.PoseBone,
    ]:
        t.dow_name = bpy.props.StringProperty()
    for t in [
        bpy.types.Object,
        bpy.types.Armature,
        bpy.types.Material,
        bpy.types.ShaderNodeTree,
    ]:
        t.dow_last_action = bpy.props.IntProperty()
    bpy.types.Object.dow_force_invisible = bpy.props.BoolProperty(
        name='force_invisible',
        description=props.ARGS[bpy.types.Object, 'force_invisible']['description'],
        get=get_force_invisible,
        set=set_force_invisible,
    )
    bpy.types.PoseBone.dow_stale = bpy.props.BoolProperty(
        name='stale',
        description=props.ARGS[bpy.types.PoseBone, 'stale']['description'],
        get=get_stale,
        set=set_stale,
    )
    bpy.types.Scene.dow_update_animations = bpy.props.BoolProperty(
        name='Update actions on renames',
        description='Automatically update all actions on mesh and bone renames',
        default=False,
    )
    bpy.types.Scene.dow_autoswitch_actions = bpy.props.EnumProperty(
        items=[
            ('DEFAULT', 'Default', 'Use Addon config'),
            ('TRUE', 'True', ''),
            ('FALSE', 'False', ''),
        ],
        default='DEFAULT',
    )
    bpy.types.Scene.dow_autoswitch_actions_view = bpy.props.BoolProperty(
        name='Sync switch actions',
        description='Automatically  switch actions for all animated objects simultaneously',
        get=get_autoswitch_actions,
        set=set_autoswitch_actions,
    )
    bpy.types.ShaderNodeTexImage.dow_image_label = bpy.props.EnumProperty(
        items=[(i[0], i[1], '') for i in IMAGE_LAYERS],
        default='NONE',
        get=get_image_label,
        set=set_image_label,
    )
    bpy.app.handlers.depsgraph_update_post.append(rename_listener)
    bpy.app.handlers.depsgraph_update_post.append(action_change_listener)
    bpy.app.handlers.load_post.append(init_dow_props)


def unregister():
    bpy.app.handlers.load_post[:] = [
        h for h in bpy.app.handlers.load_post
        if h is not init_dow_props
    ]
    bpy.app.handlers.depsgraph_update_post[:] = [
        h for h in bpy.app.handlers.depsgraph_update_post
        if h not in (rename_listener, action_change_listener)
    ]
    del bpy.types.ShaderNodeTexImage.dow_image_label
    del bpy.types.Scene.dow_autoswitch_actions
    del bpy.types.Scene.dow_update_animations
    del bpy.types.PoseBone.dow_stale
    del bpy.types.Object.dow_force_invisible
    for t in [
        bpy.types.Object,
        bpy.types.Armature,
        bpy.types.Material,
        bpy.types.ShaderNodeTree,
    ]:
        del t.dow_last_action
    for t in [
        bpy.types.Object,
        bpy.types.Material,
        bpy.types.PoseBone,
    ]:
        del t.dow_name
    bpy.utils.unregister_class(DOW_OT_make_material_animated)
    bpy.utils.unregister_class(DOW_OT_batch_bake_actions)
    bpy.utils.unregister_class(DOW_OT_batch_configure_invisible)
    bpy.utils.unregister_class(ActionSettings)
    bpy.utils.unregister_class(DOW_UL_action_settings)
    bpy.utils.unregister_class(DOW_OT_select_all_actions)
    bpy.utils.unregister_class(DOW_OT_convert_to_bone)
    bpy.utils.unregister_class(DOW_OT_convert_to_marker)
    bpy.utils.unregister_class(DOW_OT_autosplit_mesh)
    bpy.utils.unregister_class(DOW_OT_create_shadow)
    bpy.utils.unregister_class(DOW_OT_detach_object)
    bpy.utils.unregister_class(DOW_OT_attach_object)
    bpy.utils.unregister_class(DOW_OT_setup_uv_mapping)
    bpy.utils.unregister_class(DOW_OT_setup_property)
    bpy.utils.unregister_class(DowMaterialTools)
    bpy.utils.unregister_class(DowTools)


if __name__ == '__main__':
    register()
