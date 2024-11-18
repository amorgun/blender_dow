import functools

import bpy

from . import utils


class DOW_OT_setup_property(bpy.types.Operator):
    """Set up a new property"""

    bl_idname = 'object.dow_setup_prop'
    bl_label = 'Create property'
    bl_options = {'REGISTER'}

    name: bpy.props.StringProperty()

    def execute(self, context):
        utils.setup_property(context.obj, self.name)

        if hasattr(context, 'driver_source') and hasattr(context, 'driver_target'):
            add_driver = functools.partial(utils.add_driver, obj=context.driver_source, target_id=context.driver_target)

        if self.name.startswith(f'visibility{utils.PROP_SEP}'):
            add_driver(obj_prop_path='color', target_data_path=f'["{self.name}"]', fallback_value=1.0, index=3)
        if self.name.startswith(f'uv_offset{utils.PROP_SEP}'):
            add_driver(obj_prop_path='nodes["Mapping"].inputs[1].default_value', target_data_path=f'["{self.name}"][0]', fallback_value=0, index=0)
            add_driver(obj_prop_path='nodes["Mapping"].inputs[1].default_value', target_data_path=f'["{self.name}"][1]', fallback_value=0, index=1)
        if self.name.startswith(f'uv_tiling{utils.PROP_SEP}'):
            add_driver(obj_prop_path='nodes["Mapping"].inputs[3].default_value', target_data_path=f'["{self.name}"][0]', fallback_value=1, index=0)
            add_driver(obj_prop_path='nodes["Mapping"].inputs[3].default_value', target_data_path=f'["{self.name}"][1]', fallback_value=1, index=1)
        return {'FINISHED'}


def make_prop_row(row, obj, prop_name: str, display_name: str = None, **extra_objs: dict):
    display_name = display_name or prop_name
    if prop_name in obj:
        row.prop(obj, f'["{prop_name}"]', text=display_name)
    else:
        row.context_pointer_set(name='obj', data=obj)
        for k, v in extra_objs.items():
            row.context_pointer_set(name=k, data=v)
        row.operator('object.dow_setup_prop', text=f'Set up "{display_name}"').name = prop_name


class DowTools(bpy.types.Panel):
    bl_label = 'DoW Tools'
    bl_idname = 'VIEW3D_PT_dow_tools'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'DoW'

    @classmethod
    def poll(cls, context):
        return (
            context.active_object is not None and context.active_object.type == 'MESH'
        ) or (
            context.active_pose_bone is not None
        )

    def draw(self, context):
        layout = self.layout
        if context.active_object is not None:
            if context.active_object.type == 'MESH':
                if context.active_object.parent is None or context.active_object.parent.type != 'ARMATURE':
                    layout.row().label(text='Mesh is not parented to an armature', icon='ERROR')
                else:
                    for prop in [
                        'force_invisible',
                        'visibility',
                    ]:
                        make_prop_row(
                            layout,
                            context.active_object.parent,
                            prop_name=utils.create_prop_name(prop, context.active_object.name),
                            display_name=prop,
                            driver_source=context.active_object,
                            driver_target=context.active_object.parent,
                        )
                make_prop_row(layout, context.active_object, 'xref_source')
        if context.active_pose_bone is not None:
            make_prop_row(layout, context.active_pose_bone, 'stale')


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

    def get_armature(self, mat):
        if mat.node_tree.animation_data is None:
            return None
        for driver in mat.node_tree.animation_data.drivers:
            if driver.data_path.startswith('nodes["Mapping"].inputs'):
                try:
                    target = driver.driver.variables[0].targets[0]
                    return target.id
                except Exception:
                    continue

    def draw(self, context):
        layout = self.layout
        mat = context.active_object.active_material
        # mapping_node = mat.node_tree.nodes.get('Mapping')
        # if mapping_node is None:
        #     layout.row().label(text='Material is not parented to an armature', icon='ERROR')
        #     return
        arm = self.get_armature(mat)
        if arm is None:
            layout.row().label(text='Material is not parented to an armature', icon='ERROR')
        else:
            for prop in [
                'uv_offset',
                'uv_tiling',
            ]:
                make_prop_row(
                    layout,
                    arm,
                    prop_name=utils.create_prop_name(prop, mat.name),
                    display_name=prop,
                    driver_source=mat.node_tree,
                    driver_target=arm,
                )
        for prop in [
            'full_path',
            'internal',
        ]:
            make_prop_row(layout, mat, prop)

def register():
    bpy.utils.register_class(DowTools)
    bpy.utils.register_class(DowMaterialTools)
    bpy.utils.register_class(DOW_OT_setup_property)


def unregister():
    bpy.utils.unregister_class(DOW_OT_setup_property)
    bpy.utils.unregister_class(DowMaterialTools)
    bpy.utils.unregister_class(DowTools)


if __name__ == "__main__":
    register()
