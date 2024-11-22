import bpy

from . import props


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
                layout.separator()
                remote_prop_owner = props.get_mesh_prop_owner(context.active_object)
                if remote_prop_owner is None:
                    layout.row().label(text='Mesh is not parented to an armature', icon='ERROR')
                else:
                    for prop in props.REMOTE_PROPS['MESH']:
                        make_prop_row(
                            layout,
                            remote_prop_owner,
                            prop_name=props.create_prop_name(prop, context.active_object.name),
                            display_name=prop,
                            driver_obj=context.active_object,
                        )
                make_prop_row(layout, context.active_object, 'xref_source')
        if context.active_pose_bone is not None:
            layout.row().prop(context.active_pose_bone, 'name')
            layout.separator()
            make_prop_row(layout, context.active_pose_bone, 'stale')
        layout.separator()
        layout.row().prop(context.scene, 'dow_update_animations')
        layout.row().operator(DOW_OT_attach_object.bl_idname)
        layout.row().operator(DOW_OT_detach_object.bl_idname)


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
                is_renamed = False # Object copied
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
    for t in [
        bpy.types.Object,
        bpy.types.Material,
        bpy.types.PoseBone,
    ]:
        t.dow_name = bpy.props.StringProperty()
    bpy.types.Scene.dow_update_animations = bpy.props.BoolProperty(
        name="Update actions on renames",
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
    delattr(bpy.types.Scene, 'dow_update_animations')
    for t in [
        bpy.types.Object,
        bpy.types.Material,
        bpy.types.PoseBone,
    ]:
        delattr(t, 'dow_name')
    bpy.utils.unregister_class(DOW_OT_detach_object)
    bpy.utils.unregister_class(DOW_OT_attach_object)
    bpy.utils.unregister_class(DOW_OT_setup_property)
    bpy.utils.unregister_class(DowMaterialTools)
    bpy.utils.unregister_class(DowTools)


if __name__ == "__main__":
    register()
