import json
import pathlib
import platform

import bpy
from bpy_extras.io_utils import ImportHelper, ExportHelper

from . import importer, exporter, operators, props


ADDON_LOCATION = pathlib.Path(__file__).parent


class LastCallArgsGroup(bpy.types.PropertyGroup):
    import_whm: bpy.props.StringProperty()
    import_teamcolor: bpy.props.StringProperty()
    export_whm: bpy.props.StringProperty()
    export_sgm: bpy.props.StringProperty()


class DOW_OT_setup_data_path_from_module(bpy.types.Operator):
    """Setup the Mod folder from a mod .module file"""

    bl_idname = 'object.dow_setup_with_module'
    bl_label = 'Setup using .module'
    bl_options = {'REGISTER', 'UNDO'}

    filepath: bpy.props.StringProperty(subtype="FILE_PATH")
    directory: bpy.props.StringProperty(subtype="DIR_PATH")
    filter_glob: bpy.props.StringProperty(
        default='*.module',
        options={'HIDDEN'},
        maxlen=255,
    )

    def execute(self, context):
        import configparser

        config = configparser.ConfigParser(interpolation=None, comment_prefixes=('#', ';', '--'))
        filepath = pathlib.Path(self.filepath)
        with filepath.open('r') as f:
            config.read_file(f)
            config = config['global']
            mod_folder = filepath.parent/config['modfolder']
            if not mod_folder.is_dir():
                raise Exception(f'Cannot find "{mod_folder}"')
            addon_prefs = get_preferences(context)
            addon_prefs.mod_folder = str(mod_folder)
        return {'FINISHED'}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}


def update_autoswitch_actions(self, context):
    if context.scene.dow_autoswitch_actions == 'DEFAULT':
        context.scene.dow_autoswitch_actions_view = self.autoswitch_actions
        context.scene.dow_autoswitch_actions = 'DEFAULT'


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

    autoswitch_actions: bpy.props.BoolProperty(
        name='Sync switch actions',
        description='Enable actions autoswitch all opened files',
        default=True,
        update=update_autoswitch_actions,
    )

    primary_color: bpy.props.FloatVectorProperty(
        name='Primary',
        default=(0.43, 0.08, 0.00),
        subtype='COLOR',
    )

    secondary_color: bpy.props.FloatVectorProperty(
        name='Secondary',
        default=(0.63, 0.53, 0.38),
        subtype='COLOR',
    )

    trim_color: bpy.props.FloatVectorProperty(
        name='Trim',
        default=(0.22, 0.16, 0.16),
        subtype='COLOR',
    )

    weapons_color: bpy.props.FloatVectorProperty(
        name='Weapon',
        default=(0.16, 0.16, 0.16),
        subtype='COLOR',
    )

    eyes_color: bpy.props.FloatVectorProperty(
        name='Eyes',
        default=(0.01, 0.01, 0.01),
        subtype='COLOR',
    )

    badge_path: bpy.props.StringProperty(
        name="Badge",
        description='Path to the badge image',
        subtype='FILE_PATH',
        default=str(ADDON_LOCATION/'default_badge.tga'),
    )

    banner_path: bpy.props.StringProperty(
        name="Banner",
        description='Path to the banner image',
        subtype='FILE_PATH',
        default=str(ADDON_LOCATION/'default_banner.tga'),
    )

    last_args: bpy.props.PointerProperty(type=LastCallArgsGroup)

    def draw(self, context):
        mod_folder = pathlib.Path(self.mod_folder)
        self.layout.prop(self, 'mod_folder')
        if not (mod_folder.is_dir() and any(p.name.lower() == 'data' and p.is_dir() for p in mod_folder.iterdir())):
            self.layout.label(text='''The mod folder is probably not set up correctly. It must be a directory containing a 'data ' subdirectory.''', icon='ERROR')
        op = self.layout.operator(DOW_OT_setup_data_path_from_module.bl_idname)
        if mod_folder.is_dir():
            op.directory = str(mod_folder.parent)
        self.layout.row().prop(self, 'autoswitch_actions')

        teamcolor_panel_header, teamcolor_panel = self.layout.panel('default_teamcolor')
        teamcolor_panel_header.label(text='Default teamcolor')
        if teamcolor_panel is not None:
            teamcolor_panel.row().prop(self, 'primary_color')
            teamcolor_panel.row().prop(self, 'secondary_color')
            teamcolor_panel.row().prop(self, 'trim_color')
            teamcolor_panel.row().prop(self, 'weapons_color')
            teamcolor_panel.row().prop(self, 'eyes_color')
            teamcolor_panel.prop(self, 'badge_path')
            teamcolor_panel.prop(self, 'banner_path')


def get_preferences(context) -> AddonPreferences:
    return context.preferences.addons[__package__].preferences


def save_args(prefs: AddonPreferences, op, op_id: str, *arg_names):
    args = {i: getattr(op, i) for i in arg_names}
    setattr(prefs.last_args, op_id, json.dumps(args))


def remember_last_args(operator, context, args_location: str):
    addon_prefs = get_preferences(context)
    last_args = getattr(addon_prefs.last_args, args_location)
    if last_args:
        for k, v in json.loads(last_args).items():
            try:
                setattr(operator, k, v)
            except Exception:
                pass
    return operator


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

    load_wtp: bpy.props.BoolProperty(
        name='Team colourable',
        description='Load matching .wtp files',
        default=False,
    )

    strict_mode: bpy.props.BoolProperty(
        name='Strict',
        description='Fail if the model has format errors',
        default=False,
    )

    def execute(self, context):
        if self.new_project:
            bpy.ops.wm.read_homefile(app_template='')
            for mesh in bpy.data.meshes:
                bpy.data.meshes.remove(mesh)
            for material in bpy.data.materials:
                material.user_clear()
                bpy.data.materials.remove(material)
            for cam in bpy.data.cameras:
                bpy.data.cameras.remove(cam)
        addon_prefs = get_preferences(context)
        save_args(addon_prefs, self, 'import_whm',
                  'filepath', 'new_project', 'load_wtp', 'strict_mode')
        if not context.scene.dow_export_filename:
            context.scene.dow_export_filename = pathlib.Path(self.filepath).stem
        with open(self.filepath, 'rb') as f:
            reader = importer.ChunkReader(f)
            loader = importer.WhmLoader(
                pathlib.Path(addon_prefs.mod_folder),
                load_wtp=self.load_wtp,
                stric_mode=self.strict_mode,
                context=context,
            )
            window = context.window_manager.windows[0]
            with context.temp_override(window=window):
                try:
                    loader.load(reader)
                    if self.load_wtp:
                        loader.apply_teamcolor({
                            **{k: getattr(addon_prefs, f'{k}_color') for k in loader.TEAMCOLORABLE_LAYERS},
                            **{k: getattr(addon_prefs, f'{k}_path') for k in loader.TEAMCOLORABLE_IMAGES},
                        })
                    for area in context.screen.areas:
                        if area.type == 'VIEW_3D':
                            space = area.spaces.active
                            if space.type == 'VIEW_3D':
                                space.shading.type = 'MATERIAL'
                    operators.init_dow_props()
                    context.scene.dow_update_animations = True
                finally:
                    for message_lvl, message in loader.messages:
                        self.report({message_lvl}, message)
        return {'FINISHED'}


class ImportWhmCli(bpy.types.Operator):
    """Import Dawn of War .whm model file"""
    bl_idname = 'import_model.dow_whm_cli'
    bl_label = ''
    bl_options = {'REGISTER'}

    filepath: bpy.props.StringProperty(
        name='Path to the model',
        options={'HIDDEN'},
        default='',
    )

    mod_folder: bpy.props.StringProperty(
        name='Mod folder',
        options={'HIDDEN'},
        default='',
    )

    def execute(self, context):
        bpy.ops.wm.read_homefile(app_template='')
        for mesh in bpy.data.meshes:
            bpy.data.meshes.remove(mesh)
        for material in bpy.data.materials:
            material.user_clear()
            bpy.data.materials.remove(material)
        for cam in bpy.data.cameras:
            bpy.data.cameras.remove(cam)
        addon_prefs = get_preferences(context)
        if not context.scene.dow_export_filename:
            context.scene.dow_export_filename = pathlib.Path(self.filepath).stem
        loader = importer.WhmLoader(
            pathlib.Path(self.mod_folder),
            load_wtp=True,
            stric_mode=False,
            context=context,
        )

        import io
        content = loader.layout.find(self.filepath).read_bytes()
        with io.BytesIO(content) as f:
            reader = importer.ChunkReader(f)
            window = context.window_manager.windows[0]
            with context.temp_override(window=window):
                try:
                    loader.load(reader)
                    loader.apply_teamcolor({
                        **{k: getattr(addon_prefs, f'{k}_color') for k in loader.TEAMCOLORABLE_LAYERS},
                        **{k: getattr(addon_prefs, f'{k}_path') for k in loader.TEAMCOLORABLE_IMAGES},
                    })
                    for area in context.screen.areas:
                        if area.type == 'VIEW_3D':
                            space = area.spaces.active
                            if space.type == 'VIEW_3D':
                                space.shading.type = 'MATERIAL'
                    operators.init_dow_props()
                    context.scene.dow_update_animations = True
                finally:
                    for message_lvl, message in loader.messages:
                        self.report({message_lvl}, message)
        return {'FINISHED'}


class ImportTeamcolor(bpy.types.Operator, ImportHelper):
    """Import Dawn of War .teamcolour file"""
    bl_idname = 'import_model.dow_teamcolour'
    bl_label = 'Import .teamcolour file'
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = '.teamcolour'

    filter_glob: bpy.props.StringProperty(
        default='*.teamcolour',
        options={'HIDDEN'},
        maxlen=255,
    )

    set_as_defaul: bpy.props.BoolProperty(
        name='Save as default',
        description='Keep this color scheme as the default for other models',
        default=False,
    )

    def execute(self, context):
        addon_prefs = get_preferences(context)
        save_args(addon_prefs, self, 'import_teamcolor', 'filepath', 'set_as_defaul')
        loader = importer.WhmLoader(pathlib.Path(addon_prefs.mod_folder), context=context)
        try:
            teamcolor = loader.load_teamcolor(self.filepath)
            loader.apply_teamcolor(teamcolor)
            if self.set_as_defaul:
                for c in loader.TEAMCOLORABLE_LAYERS:
                    setattr(addon_prefs, f'{c}_color', teamcolor.get(c, getattr(addon_prefs, f'{c}_color')))
                for c in loader.TEAMCOLORABLE_IMAGES:
                    setattr(addon_prefs, f'{c}_path', str(teamcolor.get(c, getattr(addon_prefs, f'{c}_path'))))
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

    max_texture_size: bpy.props.IntProperty(
        name='Max texture size',
        description='Resize exported textures to the given max size.',
        default=768,
    )

    default_texture_path: bpy.props.StringProperty(
        default='art/ebps/races/space_marines/texture_share',
        name='Default texture folder',
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

    vertex_position_merge_threshold: bpy.props.FloatProperty(
        name='Vertex merging position threshold',
        description='Maximum distance between merged vertices. Use 0 to disable proximity merging',
        default=0, min=0, soft_max=1,
    )

    vertex_normal_merge_threshold: bpy.props.FloatProperty(
        name='Vertex merging normal threshold',
        description='Maximum corner normal difference between merged vertices',
        default=0.01, min=0, soft_max=1,
    )

    use_legacy_marker_orientation: bpy.props.BoolProperty(
        name='Legacy markers',
        description='Use legacy marker orientation',
        default=False,
    )

    FORMAT: exporter.ExportFormat = None

    def execute(self, context):
        assert self.FORMAT is not None
        addon_prefs = get_preferences(context)
        save_args(addon_prefs, self, f'export_{self.filename_ext[1:]}',
                  'filepath', 'object_name', 'meta', 'convert_textures', 'max_texture_size',
                  'default_texture_path', 'data_location', 'store_layout',
                  'vertex_position_merge_threshold', 'vertex_normal_merge_threshold',
                  )
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
                                   max_texture_size=self.max_texture_size,
                                   vertex_position_merge_threshold=self.vertex_position_merge_threshold,
                                   vertex_normal_merge_threshold=self.vertex_normal_merge_threshold,
                                   use_legacy_marker_orientation=self.use_legacy_marker_orientation,
                                   context=context)
            try:
                ex.export(writer, object_name=object_name, meta=self.meta)
                if self.data_location == 'nearby':
                    paths.dump_info()
            finally:
                for message_lvl, message in ex.messages:
                    self.report({message_lvl}, message)
        return {'FINISHED'}

    def invoke(self, context, _event):
        blend_filepath = context.blend_data.filepath
        if blend_filepath:
            blend_filename = pathlib.Path(blend_filepath).stem
        elif context.scene.dow_export_filename:
            blend_filename = context.scene.dow_export_filename
        else:
            blend_filename = 'untitled'
        self.filepath = str(pathlib.Path(self.filepath).parent / f'{blend_filename}{self.filename_ext}')
        return super().invoke(context, _event)


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


def import_menu_whm_func(self, context):
    op = self.layout.operator(ImportWhm.bl_idname, text='Dawn of War model (.whm)')
    remember_last_args(op, context, 'import_whm')


def import_menu_teamcolor_func(self, context):
    op = self.layout.operator(ImportTeamcolor.bl_idname, text='Dawn of War color scheme (.teamcolour)')
    remember_last_args(op, context, 'import_teamcolor')


def export_menu_whm_func(self, context):
    op = self.layout.operator(ExportWhm.bl_idname, text='Dawn of War model (.whm)')
    remember_last_args(op, context, 'export_whm')


def export_menu_sgm_func(self, context):
    op = self.layout.operator(ExportSgm.bl_idname, text='Dawn of War Object Editor model (.sgm)')
    remember_last_args(op, context, 'export_sgm')


class DOW_FH_whm_import(bpy.types.FileHandler):
    bl_idname = "CURVE_FH_whm_import"
    bl_label = "File handler for DoW .whm model import"
    bl_import_operator = "import_model.dow_whm"
    bl_file_extensions = ".whm"

    @classmethod
    def poll_drop(cls, context):
        return (context.area and context.area.type == 'VIEW_3D')


def register():
    bpy.utils.register_class(ImportWhm)
    bpy.utils.register_class(ImportWhmCli)
    bpy.utils.register_class(ImportTeamcolor)
    bpy.utils.register_class(ExportWhm)
    bpy.utils.register_class(ExportSgm)
    bpy.utils.register_class(LastCallArgsGroup)
    bpy.utils.register_class(DOW_OT_setup_data_path_from_module)
    bpy.utils.register_class(AddonPreferences)
    bpy.types.TOPBAR_MT_file_import.append(import_menu_whm_func)
    bpy.types.TOPBAR_MT_file_import.append(import_menu_teamcolor_func)
    bpy.types.TOPBAR_MT_file_export.append(export_menu_whm_func)
    bpy.types.TOPBAR_MT_file_export.append(export_menu_sgm_func)
    bpy.types.Scene.dow_export_filename = bpy.props.StringProperty()
    bpy.types.Object.dow_shadow_mesh = bpy.props.PointerProperty(
        type=bpy.types.Object,
        name='shadow',
        description='Shape used to cast shadows',
    )
    bpy.utils.register_class(DOW_FH_whm_import)
    operators.register()


def unregister():
    operators.unregister()
    bpy.utils.unregister_class(DOW_FH_whm_import)
    del bpy.types.Object.dow_shadow_mesh
    del bpy.types.Scene.dow_export_filename
    bpy.types.TOPBAR_MT_file_export.remove(export_menu_sgm_func)
    bpy.types.TOPBAR_MT_file_export.remove(export_menu_whm_func)
    bpy.types.TOPBAR_MT_file_import.remove(import_menu_teamcolor_func)
    bpy.types.TOPBAR_MT_file_import.remove(import_menu_whm_func)
    bpy.utils.unregister_class(AddonPreferences)
    bpy.utils.unregister_class(DOW_OT_setup_data_path_from_module)
    bpy.utils.unregister_class(LastCallArgsGroup)
    bpy.utils.unregister_class(ExportSgm)
    bpy.utils.unregister_class(ExportWhm)
    bpy.utils.unregister_class(ImportTeamcolor)
    bpy.utils.unregister_class(ImportWhmCli)
    bpy.utils.unregister_class(ImportWhm)
