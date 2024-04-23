bl_info = {
    'name': 'Dawn of War .WHM format',
    'description': 'Import and export of Dawn of War models',
    'author': 'amorgun',
    'license': 'GPL',
    'version': (0, 6),
    'blender': (4, 1, 0),
    'doc_url': 'https://github.com/amorgun/blender_dow',
    'tracker_url': 'https://github.com/amorgun/blender_dow/issues',
    'support': 'COMMUNITY',
    'category': 'Import-Export',
}

import pathlib
import platform

import bpy
from bpy_extras.io_utils import ImportHelper, ExportHelper

from . import importer, exporter


class AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    mod_folder: bpy.props.StringProperty(
        name="Mod folder",
        description='Directory containing your mod data. Used for locating textures and other linked data',
        subtype='DIR_PATH',
        default=str((pathlib.Path(
            'C:/Program Files' if platform.system() == 'Windows' else '~/.local/share/Steam/steamapps/common'
        ) / 'Dawn of War/My_Mod').expanduser()),
    )

    def draw(self, context):
        self.layout.prop(self, 'mod_folder')


class ImportWhm(bpy.types.Operator, ImportHelper):
    """Import Dawn of War .whm model file"""
    bl_idname = 'import_model.whm'
    bl_label = 'Import .whm file'
    bl_options = {'REGISTER', 'UNDO'}

    filter_glob = bpy.props.StringProperty(
        default='*.whm',
        options={'HIDDEN'},
        maxlen=255,
    )

    new_project: bpy.props.BoolProperty(
        name='New project',
        description='Create a new project for the imported model',
        default=True,
    )

    def execute(self, context):
        if self.new_project:
            bpy.ops.wm.read_homefile(app_template='')
            for mesh in bpy.data.meshes:
                bpy.data.meshes.remove(mesh)
        preferences = context.preferences
        addon_prefs = preferences.addons[__package__].preferences
        with open(self.filepath, 'rb') as f:
            reader = importer.ChunkReader(f)
            loader = importer.WhmLoader(pathlib.Path(addon_prefs.mod_folder), context=context)
            window = context.window_manager.windows[0]
            with context.temp_override(window=window):
                try:
                    loader.load(reader)
                    for area in context.screen.areas:
                        if area.type == 'VIEW_3D':
                            space = area.spaces.active
                            if space.type == 'VIEW_3D':
                                space.shading.type = 'MATERIAL'
                finally:
                    for message_lvl, message in loader.messages:
                        self.report({message_lvl}, message)
        return {'FINISHED'}


class ExportWhm(bpy.types.Operator, ExportHelper):
    """Export Dawn of War .whm model file"""
    bl_idname = 'export_model.whm'
    bl_label = 'Export .whm file'
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = ".whm"

    object_name: bpy.props.StringProperty(
        default='Unit',
        name='Object name',
    )

    meta: bpy.props.StringProperty(
        default='',
        name='Custom metadata, e.g. username',
    )

    filter_glob: bpy.props.StringProperty(
        default='*.whm',
        options={'HIDDEN'},
        maxlen=255,
    )

    convert_textures: bpy.props.BoolProperty(
        name='Convert textures',
        description='Convert textures from .dds to .rsh',
        default=True,
    )

    data_location: bpy.props.EnumProperty(
        name='Data store location',
        description='How to store textures and other external data',
        items=(
            ('Mod_root', 'Mod root', 'Put new filed into the mod folder'),
            ('Nearby', 'Standalone folder', 'Create a directory with data alongside the exported file'),
        ),
        default='Nearby',
    )

    use_nested: bpy.props.BoolProperty(
        name='Use nested folders',
        description='Create nested directories according to data paths',
        default=True,
    )

    def execute(self, context):
        preferences = context.preferences
        addon_prefs = preferences.addons[__package__].preferences
        data_folder = addon_prefs.mod_folder if self.data_location == 'Mod_root' else pathlib.Path(self.filepath).with_suffix('')
        paths = exporter.FileDispatcher(data_folder, is_flat=not self.use_nested)
        with open(self.filepath, 'wb') as f:
            writer = exporter.ChunkWriter(f)
            ex = exporter.WhmExporter(paths, self.convert_textures)
            try:
                ex.export(writer, object_name=self.object_name, meta=self.meta)
                if self.data_location == 'Nearby' and not self.use_nested:
                    paths.dump_info()
            finally:
                for message_lvl, message in ex.messages:
                    self.report({message_lvl}, message)
        return {'FINISHED'}


def import_menu_func(self, context):
    self.layout.operator(ImportWhm.bl_idname, text='Dawn of War model (.whm)')


def export_menu_func(self, context):
    self.layout.operator(ExportWhm.bl_idname, text='Dawn of War model (.whm)')


def register():
    bpy.utils.register_class(ImportWhm)
    bpy.utils.register_class(ExportWhm)
    bpy.utils.register_class(AddonPreferences)
    bpy.types.TOPBAR_MT_file_import.append(import_menu_func)
    bpy.types.TOPBAR_MT_file_export.append(export_menu_func)


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(export_menu_func)
    bpy.types.TOPBAR_MT_file_import.remove(import_menu_func)
    bpy.utils.unregister_class(AddonPreferences)
    bpy.utils.unregister_class(ExportWhm)
    bpy.utils.unregister_class(ImportWhm)
