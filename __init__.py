bl_info = {
    'name': 'Dawn of War .WHM format',
    'description': 'Import and export of Dawn of War models',
    'author': 'amorgun',
    'license': 'GPL',
    'version': (0, 4),
    'blender': (4, 0, 0),
    'doc_url': 'https://github.com/amorgun/blender_dow',
    'tracker_url': 'https://github.com/amorgun/blender_dow/issues',
    'support': 'COMMUNITY',
    'category': 'Import-Export',
}

import pathlib
import platform

import bpy
from bpy_extras.io_utils import ImportHelper

from . import importer


class AddonPreferences(bpy.types.AddonPreferences):
    bl_idname = __package__

    mod_folder: bpy.props.StringProperty(
        name="Mod folder",
        description='Directory containing your mod data. Used for locating textures and other linked data.',
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
        default="*.whm",
        options={'HIDDEN'},
    )

    def execute(self, context):        
        for action in bpy.data.actions:
            bpy.data.actions.remove(action)

        for material in bpy.data.materials:
            material.user_clear()
            bpy.data.materials.remove(material)
        
        for image in bpy.data.images:
            bpy.data.images.remove(image)

        for mesh in bpy.data.meshes:
            bpy.data.meshes.remove(mesh)

        preferences = context.preferences
        addon_prefs = preferences.addons[__package__].preferences

        with open(self.filepath, 'rb') as f:
            reader = importer.ChunkReader(f)
            loader = importer.WhmLoader(pathlib.Path(addon_prefs.mod_folder))
            loader.load(reader)
        for message_lvl, message in loader.messages:
            self.report({message_lvl}, message)

        return {'FINISHED'}


def import_menu_func(self, context):
    self.layout.operator(ImportWhm.bl_idname, text='Dawn of War model (.whm)')


def register():
    bpy.utils.register_class(ImportWhm)
    bpy.utils.register_class(AddonPreferences)
    bpy.types.TOPBAR_MT_file_import.append(import_menu_func)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(import_menu_func)
    bpy.utils.unregister_class(AddonPreferences)
    bpy.utils.unregister_class(ImportWhm)
