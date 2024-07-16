bl_info = {
    'name': 'Dawn of War .WHM and .SGM formats',
    'description': 'Import and export of Dawn of War models',
    'author': 'amorgun',
    'license': 'GPL',
    'version': (0, 11),
    'blender': (4, 1, 0),
    'doc_url': 'https://github.com/amorgun/blender_dow',
    'tracker_url': 'https://github.com/amorgun/blender_dow/issues',
    'support': 'COMMUNITY',
    'category': 'Import-Export',
}

import pathlib
import platform
import sys

import bpy
from bpy_extras.io_utils import ImportHelper, ExportHelper

from . import importer, exporter, utils


PACKAGES_LOCATION = utils.get_addon_packages_location(bl_info['name'])


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
    bl_idname = 'import_model.dow_whm'
    bl_label = 'Import .whm file'
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = '.whm'

    filter_glob: bpy.props.StringProperty(
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


class ExportModel:
    object_name: bpy.props.StringProperty(
        default='',
        name='Object name',
    )

    meta: bpy.props.StringProperty(
        default='',
        name='Metadata',
        description='Custom metadata, e.g. username',
    )

    convert_textures: bpy.props.BoolProperty(
        name='Convert textures',
        description='Convert textures from .dds to .rsh',
        default=True,
    )

    data_location: bpy.props.EnumProperty(
        name='Data store location',
        description='How to store textures',
        items=(
            ('mod_root', 'Mod root', 'Put new filed into the mod folder'),
            ('nearby', 'Standalone folder', 'Create a directory with data alongside the exported file'),
        ),
        default='nearby',
    )

    store_layout: bpy.props.EnumProperty(
        name='Texture store layout',
        description='How to organize textures',
        items=(
            ('flat', 'Flat', 'Put everything in the root folder'),
            ('flat_folders', 'Flat folders', 'Create a directory for each texture'),
            ('full_path', 'Full path', 'Create nested directories according to data paths'),
        ),
        default='flat',
    )

    default_texture_path: bpy.props.StringProperty(
        default='art/ebps/races/space_marines/texture_share',
        name='Default texture folder',
    )

    install_requirements: bpy.props.BoolProperty(
        name='Install requirements',
        description='Automatically install the required packages when they are needed. Requires an internet connection.',
        default=True,
    )

    max_texture_size: bpy.props.IntProperty(
        name='Max texture size',
        description='Resize exported textures to the given max size.',
        default=768,
    )

    FORMAT: exporter.ExportFormat = None

    def execute(self, context):
        assert self.FORMAT is not None
        preferences: AddonPreferences = context.preferences
        addon_prefs = preferences.addons[__package__].preferences
        filepath = pathlib.Path(self.filepath)
        data_folder = addon_prefs.mod_folder if self.data_location == 'mod_root' else filepath.with_suffix('')
        paths = exporter.FileDispatcher(data_folder, layout={
            'flat': exporter.FileDispatcher.Layout.FLAT,
            'flat_folders': exporter.FileDispatcher.Layout.FLAT_FOLDERS,
            'full_path': exporter.FileDispatcher.Layout.FULL_PATH,
        }[self.store_layout])
        object_name = self.object_name if self.object_name else filepath.stem
        with open(self.filepath, 'wb') as f:
            writer = exporter.ChunkWriter(f, exporter.CHUNK_VERSIONS[self.FORMAT])
            ex = exporter.Exporter(paths,
                                   format=self.FORMAT,
                                   default_texture_path=self.default_texture_path,
                                   convert_textures=self.convert_textures,
                                   install_requirements=self.install_requirements,
                                   packages_location=PACKAGES_LOCATION,
                                   max_texture_size=self.max_texture_size,
                                   context=context)
            try:
                ex.export(writer, object_name=object_name, meta=self.meta)
                if self.data_location == 'nearby':
                    paths.dump_info()
            finally:
                for message_lvl, message in ex.messages:
                    self.report({message_lvl}, message)
        return {'FINISHED'}


class ExportWhm(bpy.types.Operator, ExportModel, ExportHelper):
    """Export Dawn of War .whm model file"""
    bl_idname = 'export_model.dow_whm'
    bl_label = 'Export .whm file'
    bl_options = {'REGISTER'}

    filename_ext = ".whm"

    filter_glob: bpy.props.StringProperty(
        default='*.whm',
        options={'HIDDEN'},
        maxlen=255,
    )

    FORMAT = exporter.ExportFormat.WHM


class ExportSgm(bpy.types.Operator, ExportModel, ExportHelper):
    """Export Dawn of War Object Editor .sgm model"""
    bl_idname = 'export_model.dow_sgm'
    bl_label = 'Export .sgm file'
    bl_options = {'REGISTER'}

    filename_ext = ".sgm"

    filter_glob: bpy.props.StringProperty(
        default='*.sgm',
        options={'HIDDEN'},
        maxlen=255,
    )

    FORMAT = exporter.ExportFormat.SGM


def import_menu_func(self, context):
    self.layout.operator(ImportWhm.bl_idname, text='Dawn of War model (.whm)')


def export_menu_whm_func(self, context):
    self.layout.operator(ExportWhm.bl_idname, text='Dawn of War model (.whm)')

def export_menu_sgm_func(self, context):
    self.layout.operator(ExportSgm.bl_idname, text='Dawn of War Object Editor model (.sgm)')


def register():
    bpy.utils.register_class(ImportWhm)
    bpy.utils.register_class(ExportWhm)
    bpy.utils.register_class(ExportSgm)
    bpy.utils.register_class(AddonPreferences)
    bpy.types.TOPBAR_MT_file_import.append(import_menu_func)
    bpy.types.TOPBAR_MT_file_export.append(export_menu_whm_func)
    bpy.types.TOPBAR_MT_file_export.append(export_menu_sgm_func)
    sys.path.append(str(PACKAGES_LOCATION))


def unregister():
    bpy.types.TOPBAR_MT_file_export.remove(export_menu_sgm_func)
    bpy.types.TOPBAR_MT_file_export.remove(export_menu_whm_func)
    bpy.types.TOPBAR_MT_file_import.remove(import_menu_func)
    bpy.utils.unregister_class(AddonPreferences)
    bpy.utils.unregister_class(ExportSgm)
    bpy.utils.unregister_class(ExportWhm)
    bpy.utils.unregister_class(ImportWhm)
