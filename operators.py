import bpy
import mathutils

from . import props, utils


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


def can_have_shadow(obj):
    for m in obj.modifiers:
        if m.type == 'ARMATURE' and m.object is not None:
            bone_names = {b.name for b in m.object.data.bones}
            break
    else:
        bone_names = set()
    vertex_groups = utils.get_weighted_vertex_groups(obj)
    return utils.get_single_bone_name(obj, vertex_groups, bone_names) is not None or len(vertex_groups) == 0


class DOW_OT_create_shadow(bpy.types.Operator):
    """Create a shadow mesh for the selected object"""

    bl_idname = 'object.dow_create_shadow'
    bl_label = 'Create shadow'
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return any(
            o.type == 'MESH' and can_have_shadow(o)
            for o in context.selected_objects
        )

    def execute(self, context):
        for obj in context.selected_objects:
            if obj.type != 'MESH' or not can_have_shadow(obj):
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


class DOW_OT_convert_to_marker(bpy.types.Operator):
    """Convert the bone to marker"""

    bl_idname = 'object.dow_convert_to_marker'
    bl_label = 'Convert to marker'
    bl_options = {'REGISTER'}

    @classmethod
    def poll(cls, context):
        return (
            context.selected_editable_bones
            or context.selected_pose_bones_from_active_object
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
        bone_collection = armature.collections.get('Markers')
        for bone_name in bone_names:
            if custom_shape_template is None:
                custom_shape_template = bpy.data.objects.new('marker_custom_shape_template', None)
                custom_shape_template.empty_display_type = 'ARROWS'
                custom_shape_template.use_fake_user = True
            if bone_collection is None:
                bone_collection = armature.collections.new('Markers')
            bone = armature.edit_bones[bone_name]
            bone.length = 0.15
            bone.color.palette = 'CUSTOM'
            bone.color.custom.normal = mathutils.Color([14, 255, 2]) / 255
            bone.color.custom.active = mathutils.Color([255, 98, 255]) / 255
            bone_collection.assign(bone)
        bpy.ops.object.mode_set(mode='OBJECT')
        for bone_name in bone_names:
            pose_bone = armature_obj.pose.bones[bone_name]
            pose_bone.custom_shape = custom_shape_template
            pose_bone.custom_shape_scale_xyz = -1, 1, 1
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
            i.force_invisible = self.status
        return {'FINISHED'}


class DOW_UL_action_settings(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        row = layout.row()
        row.prop(item, 'force_invisible', text=item.name)


class ActionSettings(bpy.types.PropertyGroup):
    name: bpy.props.StringProperty()
    force_invisible: bpy.props.BoolProperty()


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
            and props.get_mesh_prop_owner(context.active_object) is not None
        )

    def execute(self, context):
        for obj in context.selected_objects:
            if obj.type != 'MESH':
                continue
            prop_name = props.create_prop_name('force_invisible', obj.name)
            fcurve_data_paths = [f'["{prop_name}"]', f"['{prop_name}']"]
            for d in self.actions:
                if d.name not in bpy.data.actions:
                    continue
                action = bpy.data.actions.get(d.name)
                set_fcurve_flag(action, fcurve_data_paths, d.force_invisible, default=False)
        return {'FINISHED'}

    def invoke(self, context, event):
        wm = context.window_manager
        prop_name = props.create_prop_name('force_invisible', context.active_object.name)
        fcurve_data_paths = [f'["{prop_name}"]', f"['{prop_name}']"]
        for action in bpy.data.actions:
            it = self.actions.add()
            it.name = action.name
            it.force_invisible = bool(get_fcurve_flag(action, fcurve_data_paths, default=False))
        return wm.invoke_props_dialog(self, width=500)

    def draw(self, context):
        layout = self.layout
        layout.row().label(text='Actions')
        layout.template_list('DOW_UL_action_settings', '', self, 'actions', self, 'selected_index')
        row = layout.row()
        row.context_pointer_set(name='popup_operator', data=self)
        row.operator(DOW_OT_select_all_actions.bl_idname, text='Deselect All').status=False
        row.operator(DOW_OT_select_all_actions.bl_idname, text='Select All').status=True


def get_current_action(obj):
    if obj.animation_data is not None and obj.animation_data.action is not None:
        return obj.animation_data.action
    return None


def get_fcurve_flag(action, data_paths, default):
    for fcurve in action.fcurves:
        if fcurve.is_empty:
            continue
        if any(fcurve.data_path == p for p in data_paths):
            return fcurve.keyframe_points[0].co[1]
    return default


def set_fcurve_flag(action, data_paths, value, default):
    for fcurve in action.fcurves:
        if fcurve.is_empty:
            continue
        if any(fcurve.data_path == p for p in data_paths):
            action.fcurves.remove(fcurve)
    if value != default:
        fcurve = action.fcurves.new(data_paths[0])
        fcurve.keyframe_points.insert(0, float(value))


def get_force_invisible(self):
    remote_prop_owner = props.get_mesh_prop_owner(self)
    action = get_current_action(remote_prop_owner)
    if action is None:
        return False
    prop_name = props.create_prop_name('force_invisible', self.name)
    return bool(get_fcurve_flag(action, [f'["{prop_name}"]', f"['{prop_name}']"], default=False))


def set_force_invisible(self, val):
    remote_prop_owner = props.get_mesh_prop_owner(self)
    prop_name = props.create_prop_name('force_invisible', self.name)
    remote_prop_owner[prop_name] = val
    action = get_current_action(remote_prop_owner)
    if action is None:
        return
    set_fcurve_flag(action, [f'["{prop_name}"]', f"['{prop_name}']"], val, default=False)


def get_stale(self):
    action = get_current_action(bpy.context.active_object)
    if action is None:
        return False
    return bool(get_fcurve_flag(action, [f'pose.bones["{self.name}"]["stale"]'], default=False))


def set_stale(self, val):
    self['stale'] = val
    action = get_current_action(bpy.context.active_object)
    if action is None:
        return
    set_fcurve_flag(action, [f'pose.bones["{self.name}"]["stale"]'], val, default=False)


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
                remote_prop_owner = props.get_mesh_prop_owner(context.active_object)

                make_prop_row(layout, context.active_object, 'xref_source')
                if can_have_shadow(context.active_object):
                    layout.row().prop(context.active_object, 'dow_shadow_mesh')
                else:
                    layout.row().label(text='Cannot have a shadow', icon='ERROR')
                layout.separator()
                if remote_prop_owner is None:
                    layout.row().label(text='Mesh is not parented to an armature', icon='ERROR')
                else:
                    current_action = get_current_action(remote_prop_owner)
                    if current_action is not None:
                        layout.row().prop(current_action, 'name', text='Action')
                        row = layout.row()
                        row.prop(context.active_object, 'dow_force_invisible')
                        op = row.operator(DOW_OT_batch_configure_invisible.bl_idname, text='', icon='OPTIONS')
                        op.mesh_name = context.active_object.name
                        make_prop_row(
                            layout.row(),
                            remote_prop_owner,
                            prop_name=props.create_prop_name('visibility', context.active_object.name),
                            display_name='visibility',
                            driver_obj=context.active_object,
                        )
        if context.active_pose_bone is not None:
            layout.row().prop(context.active_pose_bone, 'name')
            current_action = get_current_action(context.active_object)
            if current_action is not None:
                layout.row().prop(current_action, 'name', text='Action')
                layout.row().prop(context.active_pose_bone, 'dow_stale')
        layout.separator()
        layout.row().prop(context.scene, 'dow_update_animations')
        if context.mode == 'OBJECT':
            layout.row().operator(DOW_OT_attach_object.bl_idname)
            layout.row().operator(DOW_OT_detach_object.bl_idname)
            layout.row().operator(DOW_OT_create_shadow.bl_idname)
        if context.mode in ('POSE', 'EDIT_ARMATURE'):
            layout.row().operator(DOW_OT_convert_to_marker.bl_idname)


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
        mat = context.active_object.active_material
        # mapping_node = mat.node_tree.nodes.get('Mapping')
        # if mapping_node is None:
        #     layout.row().label(text='Material is not parented to an armature', icon='ERROR')
        #     return
        remote_prop_owner = props.get_material_prop_owner(mat)
        if remote_prop_owner is None:
            layout.row().label(text='Material is not parented to an armature', icon='ERROR')
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
def init_nameprops(filename: str = ''):
    for obj in bpy.data.objects:
        if obj.type == 'ARMATURE' and obj.pose:
            for b in obj.pose.bones:
                b.dow_name = b.name
        if obj.type == 'MESH':
            obj.dow_name = obj.name
    for obj in bpy.data.materials:
        obj.dow_name = obj.name


def register():
    bpy.utils.register_class(DowTools)
    bpy.utils.register_class(DowMaterialTools)
    bpy.utils.register_class(DOW_OT_setup_property)
    bpy.utils.register_class(DOW_OT_attach_object)
    bpy.utils.register_class(DOW_OT_detach_object)
    bpy.utils.register_class(DOW_OT_create_shadow)
    bpy.utils.register_class(DOW_OT_convert_to_marker)
    bpy.utils.register_class(DOW_OT_select_all_actions)
    bpy.utils.register_class(DOW_UL_action_settings)
    bpy.utils.register_class(ActionSettings)
    bpy.utils.register_class(DOW_OT_batch_configure_invisible)
    for t in [
        bpy.types.Object,
        bpy.types.Material,
        bpy.types.PoseBone,
    ]:
        t.dow_name = bpy.props.StringProperty()
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
    bpy.app.handlers.depsgraph_update_post.append(rename_listener)
    bpy.app.handlers.load_post.append(init_nameprops)


def unregister():
    bpy.app.handlers.load_post[:] = [
        h for h in bpy.app.handlers.depsgraph_update_post
        if h is not init_nameprops
    ]
    bpy.app.handlers.depsgraph_update_post[:] = [
        h for h in bpy.app.handlers.depsgraph_update_post
        if h is not rename_listener
    ]
    del bpy.types.Scene.dow_update_animations
    del bpy.types.PoseBone.dow_stale
    del bpy.types.Object.dow_force_invisible
    for t in [
        bpy.types.Object,
        bpy.types.Material,
        bpy.types.PoseBone,
    ]:
        del t.dow_name
    bpy.utils.unregister_class(DOW_OT_batch_configure_invisible)
    bpy.utils.unregister_class(ActionSettings)
    bpy.utils.unregister_class(DOW_UL_action_settings)
    bpy.utils.unregister_class(DOW_OT_select_all_actions)
    bpy.utils.unregister_class(DOW_OT_convert_to_marker)
    bpy.utils.unregister_class(DOW_OT_create_shadow)
    bpy.utils.unregister_class(DOW_OT_detach_object)
    bpy.utils.unregister_class(DOW_OT_attach_object)
    bpy.utils.unregister_class(DOW_OT_setup_property)
    bpy.utils.unregister_class(DowMaterialTools)
    bpy.utils.unregister_class(DowTools)


if __name__ == '__main__':
    register()
