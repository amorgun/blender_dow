import json
import pathlib
import platform

import bpy
import mathutils
from bpy_extras.io_utils import ImportHelper, ExportHelper

from . import operators, materials, dds, dow2, dow3, exporting, havok, utils
from .dow1 import importer, exporter
from .dow2.attrib import path_race
from .dow_layout import DowLayout


ADDON_LOCATION = pathlib.Path(__file__).parent


class LastCallArgsGroup(bpy.types.PropertyGroup):
    import_whm: bpy.props.StringProperty()
    import_dow2_unit: bpy.props.StringProperty()
    import_dow2_model: bpy.props.StringProperty()
    import_dow3_unit: bpy.props.StringProperty()
    import_dow3_model: bpy.props.StringProperty()
    import_teamcolor: bpy.props.StringProperty()
    export_whm: bpy.props.StringProperty()
    export_sgm: bpy.props.StringProperty()


class LastCallFileArgsGroup(bpy.types.PropertyGroup):
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


def save_args(storage, op, op_id: str, *arg_names):
    args = {i: getattr(op, i) for i in arg_names if op.properties.is_property_set(i)}
    setattr(storage, op_id, json.dumps(args))


def remember_last_args(operator, context, args_location: str):
    last_args = {}
    addon_prefs = get_preferences(context)
    last_args_global = getattr(addon_prefs.last_args, args_location)
    if last_args_global:
        last_args.update(json.loads(last_args_global))
    last_args_file = getattr(context.scene.dow_last_args, args_location, None)
    if last_args_file:
        last_args.update(json.loads(last_args_file))
    for k, v in last_args.items():
        try:
            setattr(operator, k, v)
        except Exception:
            pass
    return operator


class ToolPanel:
    bl_space_type = 'FILE_BROWSER'
    bl_region_type = 'TOOL_PROPS'
    operator_idnames = set()
    use_property_split: bool = False

    @classmethod
    def poll(cls, context):
        sfile = context.space_data
        operator = sfile.active_operator

        return operator is not None and operator.bl_idname in cls.operator_idnames

    def draw(self, context):
        layout = self.layout
        layout.use_property_split = self.use_property_split
        layout.use_property_decorate = False

        sfile = context.space_data
        operator = sfile.active_operator
        self.draw_impl(context, operator)

    def draw_impl(self, context, operator): ...


class ImportPanel(ToolPanel):
    operator_idnames = {
        'IMPORT_MODEL_OT_dow_whm',
        'IMPORT_MODEL_OT_dow_whm_browse',
        'IMPORT_MODEL_OT_dow2_unit',
        'IMPORT_MODEL_OT_dow2_unit_browse',
        'IMPORT_MODEL_OT_dow2_model',
        'IMPORT_MODEL_OT_dow3_unit',
        'IMPORT_MODEL_OT_dow3_unit_browse',
        'IMPORT_MODEL_OT_dow3_model',
    }


class ImportPanelBasicOptions(ImportPanel, bpy.types.Panel):
    bl_idname = 'FILE_PT_dow_import_common'
    bl_label = 'Basic Options'

    def draw_impl(self, context, operator):
        self.layout.prop(operator, 'new_project')
        self.layout.prop(operator, 'load_teamcolor')
        if 'strict_mode' in operator.bl_rna.properties:
            self.layout.prop(operator, 'strict_mode')
        if 'dow1_materials' in operator.bl_rna.properties:
            self.layout.prop(operator, 'dow1_materials')
        if 'load_inspection_camera' in operator.bl_rna.properties:
            self.layout.prop(operator, 'load_inspection_camera')


class ImportPanelVertexMerging(ImportPanel, bpy.types.Panel):
    bl_idname = 'FILE_PT_dow_import_vertex_merging'
    bl_label = 'Vertex Merging'

    def draw_impl(self, context, operator):
        self.layout.prop(operator, 'enable_vertex_automerge')
        row = self.layout.row()
        row.enabled = operator.enable_vertex_automerge
        row.prop(operator, 'vertex_position_merge_threshold', text='Position Threshold')


class ImportOptions:
    """Options shared by every model import operator"""

    new_project: bpy.props.BoolProperty(
        name='New project',
        description='Create a new project for the imported model',
        default=True,
    )

    load_teamcolor: bpy.props.BoolProperty(
        name='Load teamcolor',
        description='Load and apply the teamcolorable layers',
        default=True,
    )

    enable_vertex_automerge: bpy.props.BoolProperty(
        name='Enable Vertex Automerge',
        description='Automatically merge close vertices',
        default=True,
    )

    vertex_position_merge_threshold: bpy.props.FloatProperty(
        name='Vertex merging position threshold',
        description='Maximum distance between merged vertices',
        default=0.001, min=0, soft_max=1, precision=3,
    )


class ImportWhm(ImportOptions, bpy.types.Operator, ImportHelper):
    """Import a Dawn of War .whm model file, or select a .module file and browse its models"""
    bl_idname = 'import_model.dow_whm'
    bl_label = 'Import .whm file'
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = '.whm'

    filter_glob: bpy.props.StringProperty(
        default='*.whm;*.module',
        options={'HIDDEN'},
        maxlen=255,
    )

    strict_mode: bpy.props.BoolProperty(
        name='Strict',
        description='Fail if the model has format errors',
        default=False,
    )

    def execute(self, context):
        if pathlib.Path(self.filepath).suffix.lower() == '.module':
            return self.browse_module(context)
        if self.new_project:
            utils.reset_scene()
        addon_prefs = get_preferences(context)
        save_args(addon_prefs.last_args, self, 'import_whm',
                  'filepath', 'new_project', 'load_teamcolor', 'strict_mode',
                  'enable_vertex_automerge', 'vertex_position_merge_threshold',
        )
        if not context.scene.dow_export_filename:
            context.scene.dow_export_filename = pathlib.Path(self.filepath).stem
        with open(self.filepath, 'rb') as f:
            reader = importer.ChunkReader(f)
            loader = importer.WhmLoader(
                pathlib.Path(addon_prefs.mod_folder),
                load_wtp=self.load_teamcolor,
                stric_mode=self.strict_mode,
                enable_vertex_automerge=self.enable_vertex_automerge,
                vertex_position_merge_threshold=self.vertex_position_merge_threshold,
                context=context,
            )
            window = context.window_manager.windows[0]
            with context.temp_override(window=window):
                try:
                    loader.load(reader)
                    if self.load_teamcolor:
                        loader.apply_teamcolor({
                            **{k: getattr(addon_prefs, f'{k.value}_color') for k in materials.TEAMCOLORABLE_LAYERS},
                            **{k: getattr(addon_prefs, f'{k.value}_path') for k in materials.TEAMCOLOR_IMAGES},
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

    def browse_module(self, context):
        import configparser

        config = configparser.ConfigParser(interpolation=None, comment_prefixes=('#', ';', '--'))
        module_path = pathlib.Path(self.filepath)
        with module_path.open('r') as f:
            config.read_file(f)
        mod_folder = module_path.parent / config['global']['modfolder']
        if not mod_folder.is_dir():
            raise Exception(f'Cannot find "{mod_folder}"')
        layout = DowLayout.from_mod_folder(mod_folder)

        def populate(parts: tuple[str, ...]) -> dict[str, bool]:
            folder = str(pathlib.PurePosixPath(*parts)) if parts else ''
            return {
                entry.name: entry.is_dir()
                for entry in layout.iterdir(folder)
                if entry.is_dir() or entry.suffix.lower() == '.whm'
            }

        return utils.browse_tree(
            populate, 'import_model.dow_whm_browse',
            mod_folder=str(mod_folder),
            new_project=self.new_project,
            load_teamcolor=self.load_teamcolor,
            strict_mode=self.strict_mode,
            enable_vertex_automerge=self.enable_vertex_automerge,
            vertex_position_merge_threshold=self.vertex_position_merge_threshold,
        )

    def draw(self, context):
        pass


class ImportWhmBrowse(ImportOptions, utils.TreeBrowseOperator, bpy.types.Operator):
    """Browse the mod's .whm models and import the selected one"""
    bl_idname = 'import_model.dow_whm_browse'
    bl_label = 'Import .whm file'
    bl_options = {'INTERNAL'}

    filter_glob: bpy.props.StringProperty(
        default='*.whm',
        options={'HIDDEN'},
        maxlen=255,
    )

    strict_mode: bpy.props.BoolProperty(
        name='Strict',
        description='Fail if the model has format errors',
        default=False,
    )

    mod_folder: bpy.props.StringProperty(options={'HIDDEN', 'SKIP_SAVE'})

    def on_select(self, relative: pathlib.PurePath):
        save_args(get_preferences(bpy.context).last_args, self, 'import_whm',
                  'new_project', 'load_teamcolor', 'strict_mode',
                  'enable_vertex_automerge', 'vertex_position_merge_threshold',
        )
        args = dict(
            filepath=str(relative),
            mod_folder=self.mod_folder,
            new_project=self.new_project,
            load_wtp=self.load_teamcolor,
            strict_mode=self.strict_mode,
            enable_vertex_automerge=self.enable_vertex_automerge,
            vertex_position_merge_threshold=self.vertex_position_merge_threshold,
        )
        utils.run_in_view3d(lambda: bpy.ops.import_model.dow_whm_cli('EXEC_DEFAULT', **args))

    def draw(self, context):
        pass


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

    new_project: bpy.props.BoolProperty(options={'HIDDEN'}, default=True)
    load_wtp: bpy.props.BoolProperty(options={'HIDDEN'}, default=True)
    strict_mode: bpy.props.BoolProperty(options={'HIDDEN'}, default=False)
    enable_vertex_automerge: bpy.props.BoolProperty(options={'HIDDEN'}, default=True)
    vertex_position_merge_threshold: bpy.props.FloatProperty(options={'HIDDEN'}, default=0.001, min=0, soft_max=1, precision=3)

    def execute(self, context):
        if self.new_project:
            utils.reset_scene()
        addon_prefs = get_preferences(context)
        if not context.scene.dow_export_filename:
            context.scene.dow_export_filename = pathlib.Path(self.filepath).stem
        loader = importer.WhmLoader(
            pathlib.Path(self.mod_folder),
            load_wtp=self.load_wtp,
            stric_mode=self.strict_mode,
            enable_vertex_automerge=self.enable_vertex_automerge,
            vertex_position_merge_threshold=self.vertex_position_merge_threshold,
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
                    if self.load_wtp:
                        loader.apply_teamcolor({
                            **{k: getattr(addon_prefs, f'{k.value}_color') for k in materials.TEAMCOLORABLE_LAYERS},
                            **{k: getattr(addon_prefs, f'{k.value}_path') for k in materials.TEAMCOLOR_IMAGES},
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


_dow2_unit_items = {}


_dow2_attrib_dbs = {}


def dow2_attrib_db(module_path: str) -> 'dow2.AttribDb':
    db = _dow2_attrib_dbs.get(module_path)
    if db is None:
        db = _dow2_attrib_dbs[module_path] = dow2.AttribDb.load(DowLayout.from_dow2_module(module_path))
    return db


def dow2_units(module_path: str) -> list[tuple[str, str, str]]:
    items = _dow2_unit_items.get(module_path)
    if items is None:
        db = dow2_attrib_db(module_path)
        units = db.units() if db is not None else {}
        items = _dow2_unit_items[module_path] = [
            (path, f'{path}  [{attachment_count} parts]', animator)
            for path, (animator, attachment_count) in sorted(units.items())
        ]
    return items


def dow2_unit_items(self, context):
    module_path = context.window_manager.dow2_last_module
    if not module_path:
        return []
    return dow2_units(module_path)


def dow2_unit_trie(module_path: str) -> dict:
    db = dow2_attrib_db(module_path)
    trie = {}
    for path in db.blobs if db is not None else ():
        if not path.startswith('ebps'):
            continue
        *folders, unit = pathlib.PureWindowsPath(path).parts
        node = trie
        for part in folders:
            node = node.setdefault(part, {})
        node[f'{unit}.unit'] = None
    return trie


def import_dow2_unit(op, context, unit_path: str, attachments: list[str] = None, wargear: list[str] = None, import_wargear: str = 'DEFAULT', motion_sets: list[str] = None):
    module_path = context.window_manager.dow2_last_module
    if op.new_project:
        utils.reset_scene()
        context.window_manager.dow2_last_module = module_path
    loader = dow2.ModelLoader(
        DowLayout.from_dow2_module(module_path),
        import_lods=op.import_lods,
        import_wargear=import_wargear,
        dow1_materials=getattr(op, 'dow1_materials', False),
        teamcolor_scheme=int(op.teamcolor_scheme) if op.load_teamcolor else 0,
        enable_vertex_automerge=op.enable_vertex_automerge,
        vertex_position_merge_threshold=op.vertex_position_merge_threshold,
        context=context,
    )
    loader.attrib_db = dow2_attrib_db(module_path)
    window = context.window_manager.windows[0]
    with context.temp_override(window=window):
        try:
            loader.load_unit(unit_path, attachments=attachments, wargear=wargear)
            loader.load_animations(unit_path, motion_sets=motion_sets)
            # the game-default team colours (config/teamcolours.lua) live in the
            # ApplyTeamcolorDow2 socket defaults and the badge/banner images come
            # from the material's own badge1/2Tex - addon-pref overrides would
            # repaint every import away from the game look. The dow1-compat tree
            # has no defaults of its own, so paint it with the game colours.
            if getattr(op, 'dow1_materials', False):
                loader.apply_teamcolor({
                    materials.TeamcolorLayers(layer): mathutils.Color(color)
                    for layer, color in dow2.materials.DEFAULT_TEAM_COLOURS.items()
                })
            utils.set_viewport_shading(context)
        finally:
            for message_lvl, message in loader.messages:
                op.report({message_lvl}, message)
    return {'FINISHED'}


class Dow2ImportOptions:
    """Parameters specific to DoW2 import operators"""

    import_lods: bpy.props.BoolProperty(
        name='Import LODs',
        description='Import lower levels of detail in addition to the main geometry',
        default=False,
    )

    teamcolor_scheme: bpy.props.EnumProperty(
        name='Teamcolor scheme',
        description="Which of the model's teamcolor schemes (tem mask patterns) to apply",
        items=[
            ('1', 'Scheme 1', "The model's primary teamcolor pattern"),
            ('2', 'Scheme 2', 'Second pattern; falls back to the primary when the model has no variants'),
            ('3', 'Scheme 3', 'Third pattern; falls back to the primary when the model has no variants'),
            ('4', 'Scheme 4', 'Fourth pattern; falls back to the primary when the model has no variants'),
        ],
    )

    dow1_materials: bpy.props.BoolProperty(
        name='DoW1-compatible materials',
        description='Bake the DoW2 shading into the stock DoW1 material tree '
                    '(recolorable layers; dirt, occlusion and team spec are frozen at import)',
        default=False,
    )


class Dow2LoadoutPart(bpy.types.PropertyGroup):
    art_path: bpy.props.StringProperty()
    enabled: bpy.props.BoolProperty(default=True)


class Dow2LoadoutWargear(bpy.types.PropertyGroup):
    ref: bpy.props.StringProperty()
    group: bpy.props.StringProperty()
    race: bpy.props.StringProperty()
    dlc: bpy.props.StringProperty()
    enabled: bpy.props.BoolProperty(default=False)


class DOW_OT_dow2_loadout_select_all(bpy.types.Operator):
    """Change selection for every visible item in a loadout list"""

    bl_idname = 'import_model.dow2_loadout_select_all'
    bl_label = 'Select all loadout items'
    bl_options = {'INTERNAL'}

    collection: bpy.props.StringProperty()
    status: bpy.props.BoolProperty()

    def execute(self, context):
        operator = context.popup_operator
        unit_race = path_race(operator.unit)
        for item in getattr(operator, self.collection):
            if self.collection in ('wargear', 'dlc_models') and operator.same_race_wargear and item.race and item.race != unit_race:
                continue
            item.enabled = self.status
        return {'FINISHED'}


class DOW_UL_dow2_loadout(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname):
        row = layout.row()
        text = item.name
        group = getattr(item, 'group', '')
        if group:
            race = item.race.removeprefix('race_')
            prefix = ' '.join(part for part in (race if race and not data.same_race_wargear else '', item.dlc) if part)
            if prefix:
                text = f'{prefix}: {text}'
            text = f'{group}:  {text}'
        row.prop(item, 'enabled', text=text)

    def filter_items(self, context, data, propname):
        items = getattr(data, propname)
        if self.filter_name:
            flags = bpy.types.UI_UL_list.filter_items_by_name(self.filter_name, self.bitflag_filter_item, items, 'name')
        else:
            flags = [self.bitflag_filter_item] * len(items)
        if propname in ('wargear', 'dlc_models') and data.same_race_wargear:
            unit_race = path_race(data.unit)
            flags = [
                flag if not item.race or item.race == unit_race else 0
                for flag, item in zip(flags, items)
            ]
        return flags, []


class ImportDow2Unit(ImportOptions, bpy.types.Operator, ImportHelper):
    """Import a Dawn of War II unit: select the game .module file, then browse its units"""
    bl_idname = 'import_model.dow2_unit'
    bl_label = 'Import DoW2 unit'
    bl_options = {'REGISTER'}

    filename_ext = '.module'

    filter_glob: bpy.props.StringProperty(
        default='*.module',
        options={'HIDDEN'},
        maxlen=255,
    )

    def execute(self, context):
        context.window_manager.dow2_last_module = self.filepath
        save_args(get_preferences(context).last_args, self, 'import_dow2_unit',
                  'filepath', 'new_project', 'load_teamcolor',
                  'enable_vertex_automerge', 'vertex_position_merge_threshold',
        )
        trie = dow2_unit_trie(self.filepath)

        def populate(parts: tuple[str, ...]) -> dict[str, bool]:
            node = trie
            for part in parts:
                node = node[part]
            return {name: child is not None for name, child in node.items()}

        return utils.browse_tree(
            populate, 'import_model.dow2_unit_browse',
            new_project=self.new_project,
            load_teamcolor=self.load_teamcolor,
            enable_vertex_automerge=self.enable_vertex_automerge,
            vertex_position_merge_threshold=self.vertex_position_merge_threshold,
        )

    def draw(self, context):
        pass


class ImportDow2UnitBrowse(ImportOptions, utils.TreeBrowseOperator, bpy.types.Operator):
    """Browse the module's units like folders and import the selected one with its attachment and wargear models"""
    bl_idname = 'import_model.dow2_unit_browse'
    bl_label = 'Import DoW2 unit'
    bl_options = {'INTERNAL'}

    filter_glob: bpy.props.StringProperty(
        default='*.unit',
        options={'HIDDEN'},
        maxlen=255,
    )

    def on_select(self, relative: pathlib.PurePath):
        save_args(get_preferences(bpy.context).last_args, self, 'import_dow2_unit',
                  'new_project', 'load_teamcolor',
                  'enable_vertex_automerge', 'vertex_position_merge_threshold',
        )
        unit = str(pathlib.PureWindowsPath(relative.with_suffix('')))
        args = dict(
            unit=unit,
            new_project=self.new_project,
            load_teamcolor=self.load_teamcolor,
            enable_vertex_automerge=self.enable_vertex_automerge,
            vertex_position_merge_threshold=self.vertex_position_merge_threshold,
        )
        utils.run_in_view3d(lambda: bpy.ops.import_model.dow2_unit_loadout('INVOKE_DEFAULT', True, **args))

    def draw(self, context):
        pass


class ImportDow2UnitLoadout(ImportOptions, Dow2ImportOptions, bpy.types.Operator):
    """Import a Dawn of War II unit with a configurable loadout of attachment and wargear models"""
    bl_idname = 'import_model.dow2_unit_loadout'
    bl_label = 'Import DoW2 unit'
    bl_options = {'REGISTER', 'UNDO'}

    unit: bpy.props.StringProperty(options={'HIDDEN'})
    populated_for: bpy.props.StringProperty(options={'HIDDEN'})

    same_race_wargear: bpy.props.BoolProperty(
        name='Same race only',
        description="Show only wargear and DLC models of the unit's own race in the loadout",
        default=True,
    )

    attachments: bpy.props.CollectionProperty(type=Dow2LoadoutPart)
    attachments_index: bpy.props.IntProperty(options={'HIDDEN'})
    wargear: bpy.props.CollectionProperty(type=Dow2LoadoutWargear)
    wargear_index: bpy.props.IntProperty(options={'HIDDEN'})
    dlc_models: bpy.props.CollectionProperty(type=Dow2LoadoutWargear)
    dlc_models_index: bpy.props.IntProperty(options={'HIDDEN'})
    motion_sets: bpy.props.CollectionProperty(type=Dow2LoadoutPart)
    motion_sets_index: bpy.props.IntProperty(options={'HIDDEN'})

    def populate_loadout(self, db, layout):
        chapter_attachments = {art for arts in db.unit_chapter_attachments(self.unit).values() for art in arts}
        for art, rank in db.unit_attachment_levels(self.unit).items():
            if art in chapter_attachments:
                continue
            item = self.attachments.add()
            item.name = pathlib.PureWindowsPath(art).name + (f' (rank {rank})' if rank > 1 else '')
            item.art_path = art
            item.enabled = rank <= 1
        default_refs = set(db.unit_wargear_refs(self.unit))
        equippable_refs = db.unit_wargear_refs(self.unit, equippable=True, same_race_only=False)
        groups = {}
        for ref in equippable_refs:
            wargear = db.decode(ref).get('wargear', {})
            groups.setdefault((str(wargear.get('type', '')), wargear.get('slot', 0)), []).append(ref)
        for (wargear_type, slot), refs in sorted(groups.items()):
            for ref in refs:
                item = self.wargear.add()
                models = ', '.join(pathlib.PureWindowsPath(model).stem for model in db.wargear_models(ref))
                item.name = f'{pathlib.PureWindowsPath(ref).name} ({models})' if models else pathlib.PureWindowsPath(ref).name
                item.ref = ref
                item.group = f'{wargear_type} {slot}'
                item.race = path_race(ref) or ''
                item.dlc = next(iter(db.wargear_chapter_models(ref)), '')
                item.enabled = ref in default_refs
        unit_race = path_race(self.unit) or ''
        chapters = {}
        for chapter, arts in db.unit_chapter_attachments(self.unit).items():
            entries = chapters.setdefault(chapter, {})
            for art in arts:
                entries.setdefault(art, unit_race)
        for ref in equippable_refs:
            for chapter, models in db.wargear_chapter_models(ref).items():
                entries = chapters.setdefault(chapter, {})
                for model in models:
                    entries.setdefault(model, path_race(ref) or '')
        for chapter in sorted(chapters):
            for art, race in chapters[chapter].items():
                item = self.dlc_models.add()
                item.name = pathlib.PureWindowsPath(art).name
                item.ref = art
                item.group = chapter
                item.race = race
        loader = dow2.ModelLoader(layout)
        loader.attrib_db = db
        model = loader.unit_model(self.unit)
        if model is not None:
            set_names = []
            animations = model.parent / 'animations'
            if animations.is_dir():
                set_names += [entry.name for entry in animations.iterdir() if entry.is_dir()]
            hkanim = model.parent / f'{model.stem}.hkanim'
            if hkanim.is_file():
                set_names += [name for name in havok.read_hkanim_set_names(hkanim.read_bytes()) if name not in set_names]
            for set_name in sorted(set_names):
                item = self.motion_sets.add()
                item.name = set_name

    def repopulate(self, context):
        module_path = context.window_manager.dow2_last_module
        self.attachments.clear()
        self.wargear.clear()
        self.dlc_models.clear()
        self.motion_sets.clear()
        self.populate_loadout(dow2_attrib_db(module_path), DowLayout.from_dow2_module(module_path))
        self.populated_for = self.unit

    def draw(self, context):
        layout = self.layout
        layout.prop(self, 'import_lods')
        layout.prop(self, 'load_teamcolor')
        row = layout.row()
        row.enabled = self.load_teamcolor
        row.prop(self, 'teamcolor_scheme')
        race_filter_anchor = 'dlc_models' if len(self.dlc_models) else 'wargear'
        for title, collection, index_prop in (
            ('Attachments', 'attachments', 'attachments_index'),
            ('Wargear', 'wargear', 'wargear_index'),
            ('DLC models', 'dlc_models', 'dlc_models_index'),
            ('Motion sets', 'motion_sets', 'motion_sets_index'),
        ):
            if not len(getattr(self, collection)):
                continue
            layout.separator()
            layout.label(text=title)
            layout.template_list('DOW_UL_dow2_loadout', collection, self, collection, self, index_prop)
            row = layout.row()
            row.context_pointer_set(name='popup_operator', data=self)
            deselect = row.operator(DOW_OT_dow2_loadout_select_all.bl_idname, text='Deselect All')
            deselect.collection, deselect.status = collection, False
            select = row.operator(DOW_OT_dow2_loadout_select_all.bl_idname, text='Select All')
            select.collection, select.status = collection, True
            if collection == race_filter_anchor:
                layout.prop(self, 'same_race_wargear')

    def invoke(self, context, event):
        if self.populated_for != self.unit:
            self.repopulate(context)
        return context.window_manager.invoke_props_dialog(self, width=500)

    def execute(self, context):
        db = dow2_attrib_db(context.window_manager.dow2_last_module)
        if self.populated_for != self.unit:
            self.repopulate(context)
        attachments = [item.art_path for item in self.attachments if item.enabled]
        unit_race = path_race(self.unit)
        attachments += [
            item.ref for item in self.dlc_models
            if item.enabled and item.ref not in attachments
            and not (self.same_race_wargear and item.race and item.race != unit_race)
        ]
        wargear = []
        for item in self.wargear:
            if not item.enabled:
                continue
            if self.same_race_wargear and item.race and item.race != unit_race:
                continue
            wargear += [m for m in db.wargear_models(item.ref) if m not in wargear]
        motion_sets = [item.name for item in self.motion_sets if item.enabled]
        return import_dow2_unit(self, context, self.unit, attachments=attachments, wargear=wargear, motion_sets=motion_sets)


class ImportDow2UnitSelect(ImportOptions, Dow2ImportOptions, bpy.types.Operator):
    """Pick a unit from the mounted Dawn of War II environment and import it with all its attachment models"""
    bl_idname = 'import_model.dow2_unit_select'
    bl_label = 'Select DoW2 unit'
    bl_options = {'REGISTER', 'UNDO'}
    bl_property = 'unit'

    unit: bpy.props.EnumProperty(items=dow2_unit_items)

    import_wargear: bpy.props.EnumProperty(
        name='Wargear',
        description='Which wargear render models to import alongside the unit',
        items=[
            ('NONE', 'None', 'Only the unit model and its attachments'),
            ('DEFAULT', 'Default loadout', 'Wargear the unit carries by default'),
            ('ALL', 'All equippable', 'Every wargear item the unit can equip'),
        ],
        default='DEFAULT',
    )

    def invoke(self, context, event):
        context.window_manager.invoke_search_popup(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        if not self.unit:
            self.report({'ERROR'}, 'No unit selected')
            return {'CANCELLED'}
        return import_dow2_unit(self, context, self.unit, import_wargear=self.import_wargear)


class ImportDow2Model(ImportOptions, Dow2ImportOptions, bpy.types.Operator, ImportHelper):
    """Import a single Dawn of War II .model file"""
    bl_idname = 'import_model.dow2_model'
    bl_label = 'Import .model file'
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = '.model'

    filter_glob: bpy.props.StringProperty(
        default='*.model',
        options={'HIDDEN'},
        maxlen=255,
    )

    def execute(self, context):
        save_args(get_preferences(context).last_args, self, 'import_dow2_model', 'dow1_materials',
                  'new_project', 'load_teamcolor', 'import_lods', 'teamcolor_scheme',
                  'enable_vertex_automerge', 'vertex_position_merge_threshold',
        )
        module_path = context.window_manager.dow2_last_module or None
        if self.new_project:
            utils.reset_scene()
            if module_path:
                context.window_manager.dow2_last_module = module_path
        filepath = pathlib.Path(self.filepath)
        loader = dow2.ModelLoader(
            DowLayout.from_dow2_module(module_path, loose_folder=filepath.parent),
            import_lods=self.import_lods,
            dow1_materials=self.dow1_materials,
            teamcolor_scheme=int(self.teamcolor_scheme) if self.load_teamcolor else 0,
            enable_vertex_automerge=self.enable_vertex_automerge,
            vertex_position_merge_threshold=self.vertex_position_merge_threshold,
            context=context,
        )
        window = context.window_manager.windows[0]
        with context.temp_override(window=window):
            try:
                collection = loader.load_file(loader.layout.find(filepath.name))
                if not any(o.type == 'MESH' for o in collection.all_objects):
                    loader.messages.append(('INFO', f'"{filepath.name}" has no geometry. Import its wargear part models to see the unit'))
                if self.load_teamcolor:
                    addon_prefs = get_preferences(context)
                    loader.apply_teamcolor({
                        **{k: getattr(addon_prefs, f'{k.value}_color') for k in materials.TEAMCOLORABLE_LAYERS},
                        **{k: getattr(addon_prefs, f'{k.value}_path') for k in materials.TEAMCOLOR_IMAGES},
                    })
                utils.set_viewport_shading(context)
            finally:
                for message_lvl, message in loader.messages:
                    self.report({message_lvl}, message)
        return {'FINISHED'}

    def draw(self, context):
        pass


_dow3_attrib_dbs = {}


def dow3_attrib_db(folder: str) -> 'dow3.AttribDb':
    db = _dow3_attrib_dbs.get(folder)
    if db is None:
        db = _dow3_attrib_dbs[folder] = dow3.AttribDb.load(DowLayout.from_dow3_folder(folder))
    return db


def dow3_unit_trie(folder: str) -> dict:
    db = dow3_attrib_db(folder)
    trie = {}
    for path in db.blueprint_paths if db is not None else ():
        if not path.startswith('attrib/ebps'):
            continue
        *folders, unit = pathlib.PurePosixPath(path).parts
        node = trie
        for part in folders:
            node = node.setdefault(part, {})
        node[f'{pathlib.PurePosixPath(unit).stem}.unit'] = None
    return trie


def dow3_teamcolor_dict(context, db: 'dow3.AttribDb | None', race: str | None, scheme_info: dict = None) -> dict:
    """Teamcolor layers for a DoW3 import: the paint scheme's colors (the race
    default when no scheme is given), addon-preference colors and badge images
    otherwise"""
    addon_prefs = get_preferences(context)
    # no image paths here: the dow3 badge stencil is loaded once by the loader and
    # wired at material build; the dow1-style pre-colored badge images never apply
    result = {
        **{k: getattr(addon_prefs, f'{k.value}_color') for k in materials.TEAMCOLORABLE_LAYERS},
    }
    if db is not None:
        if scheme_info is None:
            scheme_info = db.paint_scheme_info(race)
        for layer, color in scheme_info['colors'].items():
            result[materials.TeamcolorLayers(layer)] = mathutils.Color(color)
        if 'tertiary' in scheme_info['colors']:
            # the dow1-compat bake maps the tertiary mask onto the EYES layer
            result[materials.TeamcolorLayers.EYES] = mathutils.Color(scheme_info['colors']['tertiary'])
        for layer, color in scheme_info.get('colors_spec', {}).items():
            result[f'spec_{layer}'] = mathutils.Color(color)
        for layer, gloss in scheme_info.get('gloss', {}).items():
            result[f'gloss_{layer}'] = gloss
        for layer, color in scheme_info['badge_colors'].items():
            result[f'badge_{layer}'] = mathutils.Color(color)
    return result


BADGE_FROM_SCHEME = 'SCHEME_DEFAULT'
BADGE_NONE = 'NO_BADGE'


def import_dow3_unit(op, context, unit_path: str, parts: list[str] = None):
    folder = context.window_manager.dow3_last_folder
    if op.new_project:
        utils.reset_scene()
        context.window_manager.dow3_last_folder = folder
    loader = dow3.ModelLoader(
        DowLayout.from_dow3_folder(folder),
        dow1_materials=op.dow1_materials,
        teamcolor_scheme=1 if op.load_teamcolor else 0,
        enable_vertex_automerge=op.enable_vertex_automerge,
        vertex_position_merge_threshold=op.vertex_position_merge_threshold,
        context=context,
    )
    loader.attrib_db = db = dow3_attrib_db(folder)
    race = dow3.attrib.unit_race(unit_path)
    scheme_info = None
    if db is not None and op.load_teamcolor:
        scheme = getattr(op, 'paint_scheme', '') or None
        scheme_info = db.paint_scheme_info(race, scheme)
        badge = getattr(op, 'badge', BADGE_FROM_SCHEME)
        if badge == BADGE_NONE:
            scheme_info['badge'] = scheme_info['badge_stencil'] = None
            scheme_info['badge_colors'] = {}
        elif badge and badge != BADGE_FROM_SCHEME and (badge_info := db.badge_info(badge)) is not None:
            scheme_info['badge'] = badge
            scheme_info['badge_stencil'] = badge_info['stencil']
            scheme_info['badge_colors'] = badge_info['colors']
        loader.load_badge(scheme_info['badge_stencil'])
        loader.load_decal(db.unit_decal(unit_path))
        loader.scheme_info = scheme_info
    window = context.window_manager.windows[0]
    with context.temp_override(window=window):
        try:
            loader.load_unit(unit_path, parts=parts)
            loader.load_animations(unit_path)
            if db is not None and getattr(op, 'load_inspection_camera', False):
                loader.create_focus_camera(db.unit_focus_perspective(unit_path))
            if op.load_teamcolor:
                loader.apply_teamcolor(dow3_teamcolor_dict(context, db, race, scheme_info))
            utils.set_viewport_shading(context)
        finally:
            for message_lvl, message in loader.messages:
                op.report({message_lvl}, message)
    return {'FINISHED'}


_dow3_enum_items = {}


def dow3_paint_scheme_items(self, context):
    folder = context.window_manager.dow3_last_folder
    race = dow3.attrib.unit_race(self.unit) if getattr(self, 'unit', '') else None
    key = ('paint_scheme', folder, race)
    items = _dow3_enum_items.get(key)
    if items is None:
        db = dow3_attrib_db(folder)
        schemes = db.paint_schemes(race) if db is not None else []
        items = _dow3_enum_items[key] = [
            (scheme, pathlib.PurePosixPath(scheme).name, scheme)
            for scheme in schemes
        ] or [('', 'None', '')]
    return items


def dow3_badge_items(self, context):
    folder = context.window_manager.dow3_last_folder
    key = ('badge', folder)
    items = _dow3_enum_items.get(key)
    if items is None:
        db = dow3_attrib_db(folder)
        badges = db.badges() if db is not None else []
        items = _dow3_enum_items[key] = [
            (BADGE_FROM_SCHEME, 'Paint scheme badge', 'Use the badge of the selected paint scheme'),
            (BADGE_NONE, 'No badge', 'Do not apply a badge'),
        ] + [(badge, badge, badge) for badge in badges]
    return items


class Dow3ImportOptions:
    """Parameters specific to DoW3 import operators"""

    dow1_materials: bpy.props.BoolProperty(
        name='DoW1-compatible materials',
        description='Bake the DoW3 shader particularities (dark albedo, gloss in the specular alpha, '
                    'emissive pattern mask) into standard texture layers and build the unchanged DoW1 '
                    'material tree, so material tooling and exporters work on the imported models',
        default=False,
    )

    load_inspection_camera: bpy.props.BoolProperty(
        name='Inspection camera',
        description="Create the armoury inspection camera: the unit's camera_perspective "
                    'preset the game orbits around it in the army painter and unit views',
        default=False,
    )


class ImportDow3Unit(ImportOptions, Dow3ImportOptions, bpy.types.Operator, ImportHelper):
    """Import a Dawn of War III unit: select the game's relicgame.module file (or its archives folder), then browse its units"""
    bl_idname = 'import_model.dow3_unit'
    bl_label = 'Import DoW3 unit'
    bl_options = {'REGISTER'}

    filename_ext = '.module'

    filter_glob: bpy.props.StringProperty(
        default='*.module',
        options={'HIDDEN'},
        maxlen=255,
    )

    def execute(self, context):
        module = DowLayout.resolve_dow3_module(self.filepath)
        if module is None:
            try:
                module = DowLayout.resolve_dow3_archives_folder(self.filepath)
            except FileNotFoundError as e:
                self.report({'ERROR'}, str(e))
                return {'CANCELLED'}
        context.window_manager.dow3_last_folder = str(module)
        save_args(get_preferences(context).last_args, self, 'import_dow3_unit',
                  'filepath', 'new_project', 'load_teamcolor', 'dow1_materials',
                  'load_inspection_camera', 'enable_vertex_automerge', 'vertex_position_merge_threshold',
        )
        trie = dow3_unit_trie(str(module))

        def populate(parts: tuple[str, ...]) -> dict[str, bool]:
            node = trie
            for part in parts:
                node = node[part]
            return {name: child is not None for name, child in node.items()}

        return utils.browse_tree(
            populate, 'import_model.dow3_unit_browse',
            new_project=self.new_project,
            load_teamcolor=self.load_teamcolor,
            dow1_materials=self.dow1_materials,
            load_inspection_camera=self.load_inspection_camera,
            enable_vertex_automerge=self.enable_vertex_automerge,
            vertex_position_merge_threshold=self.vertex_position_merge_threshold,
        )

    def draw(self, context):
        pass


class ImportDow3UnitBrowse(ImportOptions, Dow3ImportOptions, utils.TreeBrowseOperator, bpy.types.Operator):
    """Browse the game's units like folders and import the selected one with its part models"""
    bl_idname = 'import_model.dow3_unit_browse'
    bl_label = 'Import DoW3 unit'
    bl_options = {'INTERNAL'}

    filter_glob: bpy.props.StringProperty(
        default='*.unit',
        options={'HIDDEN'},
        maxlen=255,
    )

    def on_select(self, relative: pathlib.PurePath):
        save_args(get_preferences(bpy.context).last_args, self, 'import_dow3_unit',
                  'new_project', 'load_teamcolor', 'dow1_materials',
                  'load_inspection_camera', 'enable_vertex_automerge', 'vertex_position_merge_threshold',
        )
        unit = str(relative.with_suffix('.rgd').as_posix())
        args = dict(
            unit=unit,
            new_project=self.new_project,
            load_teamcolor=self.load_teamcolor,
            dow1_materials=self.dow1_materials,
            load_inspection_camera=self.load_inspection_camera,
            enable_vertex_automerge=self.enable_vertex_automerge,
            vertex_position_merge_threshold=self.vertex_position_merge_threshold,
        )
        utils.run_in_view3d(lambda: bpy.ops.import_model.dow3_unit_loadout('INVOKE_DEFAULT', True, **args))

    def draw(self, context):
        pass


class ImportDow3UnitLoadout(ImportOptions, Dow3ImportOptions, bpy.types.Operator):
    """Import a Dawn of War III unit with a configurable loadout of part models"""
    bl_idname = 'import_model.dow3_unit_loadout'
    bl_label = 'Import DoW3 unit'
    bl_options = {'REGISTER', 'UNDO'}

    unit: bpy.props.StringProperty(options={'HIDDEN'})
    populated_for: bpy.props.StringProperty(options={'HIDDEN'})

    paint_scheme: bpy.props.EnumProperty(
        name='Paint scheme',
        description='The army paint scheme for the teamcolor layers and the badge',
        items=dow3_paint_scheme_items,
    )
    badge: bpy.props.EnumProperty(
        name='Badge',
        description='Override the paint scheme badge',
        items=dow3_badge_items,
    )

    parts: bpy.props.CollectionProperty(type=Dow2LoadoutWargear)
    parts_index: bpy.props.IntProperty(options={'HIDDEN'})

    def populate_loadout(self, db, layout):
        rows = []
        seen = set()
        for part in db.unit_parts(self.unit):
            if dow3.attrib.INVISIBLE_ANIMATOR not in part.art_path:
                rows.append(('default', part.art_path))
            for skin, art_path in part.skins.items():
                rows.append((skin, art_path))
        for group, art_path in sorted(rows, key=lambda row: (row[0] != 'default', row[0])):
            if (group, art_path) in seen:
                continue
            seen.add((group, art_path))
            stem = pathlib.PureWindowsPath(art_path).stem
            item = self.parts.add()
            item.name = stem
            item.ref = art_path
            item.group = group
            item.enabled = group == 'default' and '_damage_' not in stem

    def repopulate(self, context):
        folder = context.window_manager.dow3_last_folder
        self.parts.clear()
        self.populate_loadout(dow3_attrib_db(folder), DowLayout.from_dow3_folder(folder))
        self.populated_for = self.unit

    def draw(self, context):
        layout = self.layout
        layout.prop(self, 'load_teamcolor')
        if self.load_teamcolor:
            layout.prop(self, 'paint_scheme')
            layout.prop(self, 'badge')
        layout.prop(self, 'dow1_materials')
        layout.prop(self, 'load_inspection_camera')
        if len(self.parts):
            layout.separator()
            layout.label(text='Parts')
            layout.template_list('DOW_UL_dow2_loadout', 'parts', self, 'parts', self, 'parts_index')
            row = layout.row()
            row.context_pointer_set(name='popup_operator', data=self)
            deselect = row.operator(DOW_OT_dow2_loadout_select_all.bl_idname, text='Deselect All')
            deselect.collection, deselect.status = 'parts', False
            select = row.operator(DOW_OT_dow2_loadout_select_all.bl_idname, text='Select All')
            select.collection, select.status = 'parts', True

    def invoke(self, context, event):
        if self.populated_for != self.unit:
            self.repopulate(context)
        return context.window_manager.invoke_props_dialog(self, width=500)

    def execute(self, context):
        if self.populated_for != self.unit:
            self.repopulate(context)
        parts = list(dict.fromkeys(item.ref for item in self.parts if item.enabled))
        return import_dow3_unit(self, context, self.unit, parts=parts)


class ImportDow3Model(ImportOptions, Dow3ImportOptions, bpy.types.Operator, ImportHelper):
    """Import a single Dawn of War III .rgm model file"""
    bl_idname = 'import_model.dow3_model'
    bl_label = 'Import .rgm file'
    bl_options = {'REGISTER', 'UNDO'}

    filename_ext = '.rgm'

    filter_glob: bpy.props.StringProperty(
        default='*.rgm',
        options={'HIDDEN'},
        maxlen=255,
    )

    def execute(self, context):
        save_args(get_preferences(context).last_args, self, 'import_dow3_model',
                  'new_project', 'load_teamcolor', 'dow1_materials',
                  'enable_vertex_automerge', 'vertex_position_merge_threshold',
        )
        folder = context.window_manager.dow3_last_folder or None
        if self.new_project:
            utils.reset_scene()
            if folder:
                context.window_manager.dow3_last_folder = folder
        filepath = pathlib.Path(self.filepath)
        loader = dow3.ModelLoader(
            DowLayout.from_dow3_folder(folder, loose_folder=filepath.parent),
            dow1_materials=self.dow1_materials,
            teamcolor_scheme=1 if self.load_teamcolor else 0,
            enable_vertex_automerge=self.enable_vertex_automerge,
            vertex_position_merge_threshold=self.vertex_position_merge_threshold,
            context=context,
        )
        window = context.window_manager.windows[0]
        with context.temp_override(window=window):
            try:
                db = dow3.AttribDb.load(loader.layout) if self.load_teamcolor else None
                scheme_info = None
                if db is not None:
                    scheme_info = db.paint_scheme_info(None)
                    loader.load_badge(scheme_info['badge_stencil'])
                    loader.scheme_info = scheme_info
                collection = loader.load_file(loader.layout.find(filepath.name))
                if not any(o.type == 'MESH' for o in collection.all_objects):
                    loader.messages.append(('INFO', f'"{filepath.name}" has no geometry. Archetype models carry only the skeleton; import the unit or its part models to see the meshes'))
                if self.dow1_materials:
                    loader.bake_badge_decal()
                if self.load_teamcolor:
                    loader.apply_teamcolor(dow3_teamcolor_dict(context, db, None, scheme_info))
                utils.set_viewport_shading(context)
            finally:
                for message_lvl, message in loader.messages:
                    self.report({message_lvl}, message)
        return {'FINISHED'}

    def draw(self, context):
        pass


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
        save_args(addon_prefs.last_args, self, 'import_teamcolor', 'filepath', 'set_as_defaul')
        loader = importer.WhmLoader(pathlib.Path(addon_prefs.mod_folder), context=context)
        try:
            teamcolor = loader.load_teamcolor(self.filepath)
            loader.apply_teamcolor(teamcolor)
            if self.set_as_defaul:
                for c in materials.TEAMCOLORABLE_LAYERS:
                    setattr(addon_prefs, f'{c.value}_color', teamcolor.get(c, getattr(addon_prefs, f'{c.value}_color')))
                for c in materials.TEAMCOLOR_IMAGES:
                    setattr(addon_prefs, f'{c.value}_path', str(teamcolor.get(c, getattr(addon_prefs, f'{c.value}_path'))))
        finally:
            for message_lvl, message in loader.messages:
                self.report({message_lvl}, message)
        return {'FINISHED'}


class ExportPanel(ToolPanel):
    operator_idnames = {'EXPORT_MODEL_OT_dow_whm', 'EXPORT_MODEL_OT_dow_sgm'}


class ExportPanelMeta(ExportPanel, bpy.types.Panel):
    bl_idname = 'FILE_PT_dow_export_meta'
    bl_label = 'Metadata'

    def draw_impl(self, context, operator):
        self.layout.prop(operator, 'object_name')
        self.layout.prop(operator, 'meta', text='Custom data')


class ExportPanelTextures(ExportPanel, bpy.types.Panel):
    bl_idname = 'FILE_PT_dow_export_textures'
    bl_label = 'Textures'

    def draw_impl(self, context, operator):
        is_whm = operator.bl_idname == 'EXPORT_MODEL_OT_dow_whm'
        self.layout.prop(operator, 'convert_textures')
        if is_whm:
            self.layout.prop(operator, 'texture_format')
        else:
            operator.texture_format = exporter.MaterialExportFormat.RSH
        self.layout.prop(operator, 'max_texture_size')
        self.layout.prop(operator, 'default_texture_path')
        self.layout.prop(operator, 'ignore_material_paths')
        if is_whm:
            if operator.texture_format == exporter.MaterialExportFormat.RSH:
                self.layout.prop(operator, 'export_teamcolored_rtx')
                if operator.export_teamcolored_rtx:
                    self.layout.prop(operator, 'teamcolored_rtx_suffix')
        else:
            operator.teamcolored_rtx_suffix = ''
        self.layout.prop(operator, 'data_location')
        if operator.data_location == 'custom_folder':
            self.layout.prop(operator, 'custom_data_folder')
        self.layout.prop(operator, 'store_layout')
        self.layout.prop(operator, 'override_files')


class ExportPanelVertexMerging(ExportPanel, bpy.types.Panel):
    bl_idname = 'FILE_PT_dow_export_vertex_merging'
    bl_label = 'Vertex Merging'

    def draw_impl(self, context, operator):
        self.layout.prop(operator, 'vertex_position_merge_threshold', text='Position Threshold')
        self.layout.prop(operator, 'vertex_normal_merge_threshold', text='Normal Threshold')


class ExportPanelOther(ExportPanel, bpy.types.Panel):
    bl_idname = 'FILE_PT_dow_export_other'
    bl_label = 'Other'
    bl_options = {'DEFAULT_CLOSED'}

    def draw_impl(self, context, operator):
        self.layout.prop(operator, 'ignore_xrefs')
        self.layout.prop(operator, 'use_legacy_marker_orientation')


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

    texture_format: bpy.props.EnumProperty(
        name='Texture format',
        description='How to store textures',
        items=(
            (exporter.MaterialExportFormat.RSH, 'Vanilla', 'Export textures to rsh files, compatible with both AE and DE'),
            (exporter.MaterialExportFormat.RTX, 'Definitive Edition', 'Export textures to the DE rtx format, supporting new lighting'),
        ),
        default='rsh',
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

    ignore_material_paths: bpy.props.BoolProperty(
        default=False,
        name='Ignore per-material paths',
        description='Force-put all exported textures into the Default texture folder',
    )

    export_teamcolored_rtx: bpy.props.BoolProperty(
        default=False,
        name='Export teamcolored rtx',
        description='Export rtx files with teamcolor baked in',
    )


    teamcolored_rtx_suffix: bpy.props.StringProperty(
        default='_default_0',
        name='Teamcolored rtx suffix',
        description='The suffix used for exported rtx files with teamcolor baked in',
    )

    data_location: bpy.props.EnumProperty(
        name='Data store location',
        description='How to store textures',
        items=(
            ('mod_root', 'Mod root', 'Put new filed into the mod folder'),
            ('nearby', 'Standalone folder', 'Create a directory with data alongside the exported file'),
            ('custom_folder', 'Custom folder', 'Manually set the result folder'),
        ),
        default='nearby',
    )

    custom_data_folder: bpy.props.StringProperty(
        default='',
        name='Custom data folder',
        description='The folder to save textures',
        # subtype='FILE_PATH',  # Blender cannot have nested fileselectors
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

    override_files: bpy.props.BoolProperty(
        name='Override files',
        description='Override existing files',
        default=False,
    )

    vertex_position_merge_threshold: bpy.props.FloatProperty(
        name='Vertex merging position threshold',
        description='Maximum distance between merged vertices. Use 0 to disable proximity merging',
        default=0, min=0, soft_max=1, precision=3,
    )

    vertex_normal_merge_threshold: bpy.props.FloatProperty(
        name='Vertex merging normal threshold',
        description='Maximum corner normal difference between merged vertices',
        default=0.01, min=0, soft_max=1, precision=3,
    )

    ignore_xrefs: bpy.props.BoolProperty(
        default=False,
        name='Force copy xrefs',
        description='Copy the data of xrefed meshes instead of creating a reference to another model',
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
        saved_args = [
            'filepath', 'object_name', 'meta', 'convert_textures', 'texture_format', 'max_texture_size',
            'default_texture_path', 'ignore_material_paths', 'export_teamcolored_rtx', 'teamcolored_rtx_suffix',
            'data_location', 'store_layout', 'custom_data_folder', 'override_files', 'ignore_xrefs',
            'vertex_position_merge_threshold', 'vertex_normal_merge_threshold',
        ]
        save_args(addon_prefs.last_args, self, f'export_{self.filename_ext[1:]}', *saved_args)
        save_args(context.scene.dow_last_args, self, f'export_{self.filename_ext[1:]}', *saved_args)
        filepath = pathlib.Path(self.filepath)
        context.scene.dow_last_export_filename = filepath.stem
        data_folder = filepath.with_suffix('')
        data_subfolder = None
        if self.data_location == 'mod_root':
            data_folder = addon_prefs.mod_folder
            data_subfolder = 'data'
        elif self.data_location == 'custom_folder' and self.custom_data_folder.strip():
            data_folder = self.custom_data_folder.strip()
        paths = exporter.FileDispatcher(data_folder, layout={
            'flat': exporter.FileDispatcher.Layout.FLAT,
            'flat_folders': exporter.FileDispatcher.Layout.FLAT_FOLDERS,
            'full_path': exporter.FileDispatcher.Layout.FULL_PATH,
        }[self.store_layout], subfolder=data_subfolder)
        object_name = self.object_name if self.object_name else filepath.stem
        with open(self.filepath, 'wb') as f:
            writer = exporter.ChunkWriter(f, exporter.get_chunk_versions(self.FORMAT, self.texture_format))
            ex = exporter.Exporter(paths,
                                   override_files=self.override_files,
                                   format=self.FORMAT,
                                   default_texture_path=self.default_texture_path,
                                   ignore_material_paths=self.ignore_material_paths,
                                   convert_textures=self.convert_textures,
                                   material_export_format=self.texture_format,
                                   export_teamcolored_rtx=(
                                       self.texture_format == exporter.MaterialExportFormat.RSH
                                       and self.export_teamcolored_rtx
                                       and self.teamcolored_rtx_suffix.strip() != ''
                                   ),
                                   teamcolored_rtx_suffix=self.teamcolored_rtx_suffix,
                                   max_texture_size=self.max_texture_size,
                                   vertex_position_merge_threshold=self.vertex_position_merge_threshold,
                                   vertex_normal_merge_threshold=self.vertex_normal_merge_threshold,
                                   ignore_xrefs=self.ignore_xrefs,
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
        if context.scene.dow_last_export_filename:
            blend_filename = context.scene.dow_last_export_filename
        elif blend_filepath:
            blend_filename = pathlib.Path(blend_filepath).stem
        elif context.scene.dow_export_filename:
            blend_filename = context.scene.dow_export_filename
        else:
            blend_filename = 'untitled'
        self.filepath = str(pathlib.Path(self.filepath).parent / f'{blend_filename}{self.filename_ext}')
        return super().invoke(context, _event)

    def draw(self, context):
        pass


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


class ExportGame2Model:
    object_name: bpy.props.StringProperty(
        default='',
        name='Object name',
    )

    convert_textures: bpy.props.BoolProperty(
        name='Convert textures',
        description='Export the model textures next to it',
        default=True,
    )

    data_location: bpy.props.EnumProperty(
        name='Data store location',
        description='Where to store textures',
        items=(
            ('nearby', 'Standalone folder', 'Create a directory with data alongside the exported file'),
            ('custom_folder', 'Custom folder', 'Manually set the result folder'),
        ),
        default='nearby',
    )

    custom_data_folder: bpy.props.StringProperty(
        default='',
        name='Custom data folder',
        description='The folder to save textures',
    )

    store_layout: bpy.props.EnumProperty(
        name='Texture store layout',
        description='How to organize textures',
        items=(
            ('flat', 'Flat', 'Put everything in the root folder'),
            ('full_path', 'Full path', 'Create nested directories according to data paths, mergeable over the game files'),
        ),
        default='flat',
    )

    def run_export(self, filepath: str, paths, object_name: str, context) -> list:
        raise NotImplementedError

    def execute(self, context):
        addon_prefs = get_preferences(context)
        saved_args = ['filepath', 'object_name', 'convert_textures', 'data_location', 'custom_data_folder', 'store_layout']
        save_args(addon_prefs.last_args, self, f'export_{self.filename_ext[1:]}', *saved_args)
        save_args(context.scene.dow_last_args, self, f'export_{self.filename_ext[1:]}', *saved_args)
        filepath = pathlib.Path(self.filepath)
        context.scene.dow_last_export_filename = filepath.stem
        data_folder = filepath.with_suffix('')
        if self.data_location == 'custom_folder' and self.custom_data_folder.strip():
            data_folder = self.custom_data_folder.strip()
        paths = exporting.FileDispatcher(data_folder, layout={
            'flat': exporting.FileDispatcher.Layout.FLAT,
            'full_path': exporting.FileDispatcher.Layout.FULL_PATH,
        }[self.store_layout])
        object_name = self.object_name if self.object_name else filepath.stem
        messages = self.run_export(self.filepath, paths, object_name, context)
        if self.data_location == 'nearby':
            paths.dump_info()
        for message_lvl, message in messages:
            self.report({message_lvl}, message)
        return {'FINISHED'}

    def invoke(self, context, _event):
        blend_filepath = context.blend_data.filepath
        if context.scene.dow_last_export_filename:
            blend_filename = context.scene.dow_last_export_filename
        elif blend_filepath:
            blend_filename = pathlib.Path(blend_filepath).stem
        elif context.scene.dow_export_filename:
            blend_filename = context.scene.dow_export_filename
        else:
            blend_filename = 'untitled'
        self.filepath = str(pathlib.Path(self.filepath).parent / f'{blend_filename}{self.filename_ext}')
        return super().invoke(context, _event)

    def draw(self, context):
        layout = self.layout
        layout.prop(self, 'object_name')
        layout.prop(self, 'convert_textures')
        layout.prop(self, 'data_location')
        if self.data_location == 'custom_folder':
            layout.prop(self, 'custom_data_folder')
        layout.prop(self, 'store_layout')


class ExportDow2Model(bpy.types.Operator, ExportGame2Model, ExportHelper):
    """Export Dawn of War II .model file"""
    bl_idname = 'export_model.dow2_model'
    bl_label = 'Export .model file'
    bl_options = {'REGISTER'}

    filename_ext = '.model'

    filter_glob: bpy.props.StringProperty(
        default='*.model',
        options={'HIDDEN'},
        maxlen=255,
    )

    def run_export(self, filepath, paths, object_name, context):
        return dow2.exporter.export_model(filepath, paths, object_name, convert_textures=self.convert_textures, context=context)


class DOW_MT_import(bpy.types.Menu):
    bl_idname = 'DOW_MT_import'
    bl_label = 'Dawn of War'

    def draw(self, context):
        layout = self.layout
        op = layout.operator(ImportWhm.bl_idname, text='Dawn of War model (.whm)')
        remember_last_args(op, context, 'import_whm')
        op = layout.operator(ImportTeamcolor.bl_idname, text='Dawn of War color scheme (.teamcolour)')
        remember_last_args(op, context, 'import_teamcolor')
        layout.separator()
        op = layout.operator(ImportDow2Unit.bl_idname, text='Dawn of War II unit')
        remember_last_args(op, context, 'import_dow2_unit')
        op = layout.operator(ImportDow2Model.bl_idname, text='Dawn of War II model (.model)')
        remember_last_args(op, context, 'import_dow2_model')
        layout.separator()
        op = layout.operator(ImportDow3Unit.bl_idname, text='Dawn of War III unit')
        remember_last_args(op, context, 'import_dow3_unit')
        op = layout.operator(ImportDow3Model.bl_idname, text='Dawn of War III model (.rgm)')
        remember_last_args(op, context, 'import_dow3_model')


class DOW_MT_export(bpy.types.Menu):
    bl_idname = 'DOW_MT_export'
    bl_label = 'Dawn of War'

    def draw(self, context):
        layout = self.layout
        op = layout.operator(ExportWhm.bl_idname, text='Dawn of War model (.whm)')
        remember_last_args(op, context, 'export_whm')
        op = layout.operator(ExportSgm.bl_idname, text='Dawn of War Object Editor model (.sgm)')
        remember_last_args(op, context, 'export_sgm')
        layout.separator()
        op = layout.operator(ExportDow2Model.bl_idname, text='Dawn of War II model (.model)')
        remember_last_args(op, context, 'export_model')


def import_menu_func(self, context):
    self.layout.menu(DOW_MT_import.bl_idname)


def export_menu_func(self, context):
    self.layout.menu(DOW_MT_export.bl_idname)


class DOW_FH_whm_import(bpy.types.FileHandler):
    bl_idname = "OBJECT_FH_whm_import"
    bl_label = "File handler for DoW .whm model import"
    bl_import_operator = "import_model.dow_whm"
    bl_file_extensions = ".whm"

    @classmethod
    def poll_drop(cls, context):
        return (context.area and context.area.type == 'VIEW_3D')


class DOW_FH_rgm_import(bpy.types.FileHandler):
    bl_idname = "OBJECT_FH_rgm_import"
    bl_label = "File handler for DoW3 .rgm model import"
    bl_import_operator = "import_model.dow3_model"
    bl_file_extensions = ".rgm"

    @classmethod
    def poll_drop(cls, context):
        return (context.area and context.area.type == 'VIEW_3D')


def register():
    dds.register()
    bpy.utils.register_class(ImportPanelBasicOptions)
    bpy.utils.register_class(ImportPanelVertexMerging)
    bpy.utils.register_class(ImportWhm)
    bpy.utils.register_class(ImportWhmCli)
    bpy.utils.register_class(Dow2LoadoutPart)
    bpy.utils.register_class(Dow2LoadoutWargear)
    bpy.utils.register_class(DOW_OT_dow2_loadout_select_all)
    bpy.utils.register_class(DOW_UL_dow2_loadout)
    bpy.utils.register_class(ImportDow2Unit)
    bpy.utils.register_class(ImportWhmBrowse)
    bpy.utils.register_class(ImportDow2UnitBrowse)
    bpy.utils.register_class(ImportDow2UnitLoadout)
    bpy.utils.register_class(ImportDow2UnitSelect)
    bpy.utils.register_class(ImportDow2Model)
    bpy.utils.register_class(ImportDow3Unit)
    bpy.utils.register_class(ImportDow3UnitBrowse)
    bpy.utils.register_class(ImportDow3UnitLoadout)
    bpy.utils.register_class(ImportDow3Model)
    bpy.utils.register_class(ImportTeamcolor)
    bpy.utils.register_class(ExportPanelMeta)
    bpy.utils.register_class(ExportPanelTextures)
    bpy.utils.register_class(ExportPanelVertexMerging)
    bpy.utils.register_class(ExportPanelOther)
    bpy.utils.register_class(ExportWhm)
    bpy.utils.register_class(ExportSgm)
    bpy.utils.register_class(ExportDow2Model)
    bpy.utils.register_class(LastCallArgsGroup)
    bpy.utils.register_class(LastCallFileArgsGroup)
    bpy.utils.register_class(DOW_OT_setup_data_path_from_module)
    bpy.utils.register_class(AddonPreferences)
    bpy.utils.register_class(DOW_MT_import)
    bpy.utils.register_class(DOW_MT_export)
    bpy.types.TOPBAR_MT_file_import.append(import_menu_func)
    bpy.types.TOPBAR_MT_file_export.append(export_menu_func)
    bpy.types.WindowManager.dow2_last_module = bpy.props.StringProperty()
    bpy.types.WindowManager.dow3_last_folder = bpy.props.StringProperty()
    bpy.types.Scene.dow_export_filename = bpy.props.StringProperty()
    bpy.types.Scene.dow_last_export_filename = bpy.props.StringProperty()
    bpy.types.Scene.dow_last_args = bpy.props.PointerProperty(type=LastCallFileArgsGroup)
    bpy.types.Object.dow_shadow_mesh = bpy.props.PointerProperty(
        type=bpy.types.Object,
        name='shadow',
        description='Shape used to cast shadows',
    )
    bpy.types.Image.dow_export_path = bpy.props.StringProperty(
        default='',
        name='Image Export Path',
        description='Path to where the game will look for this image',
    )
    bpy.utils.register_class(DOW_FH_whm_import)
    bpy.utils.register_class(DOW_FH_rgm_import)
    operators.register()


def unregister():
    operators.unregister()
    bpy.utils.unregister_class(DOW_FH_rgm_import)
    bpy.utils.unregister_class(DOW_FH_whm_import)
    del bpy.types.Image.dow_export_path
    del bpy.types.Object.dow_shadow_mesh
    del bpy.types.Scene.dow_last_args
    del bpy.types.Scene.dow_last_export_filename
    del bpy.types.Scene.dow_export_filename
    del bpy.types.WindowManager.dow3_last_folder
    del bpy.types.WindowManager.dow2_last_module
    bpy.types.TOPBAR_MT_file_export.remove(export_menu_func)
    bpy.types.TOPBAR_MT_file_import.remove(import_menu_func)
    bpy.utils.unregister_class(DOW_MT_export)
    bpy.utils.unregister_class(DOW_MT_import)
    bpy.utils.unregister_class(AddonPreferences)
    bpy.utils.unregister_class(DOW_OT_setup_data_path_from_module)
    bpy.utils.unregister_class(LastCallFileArgsGroup)
    bpy.utils.unregister_class(LastCallArgsGroup)
    bpy.utils.unregister_class(ExportDow2Model)
    bpy.utils.unregister_class(ExportSgm)
    bpy.utils.unregister_class(ExportWhm)
    bpy.utils.unregister_class(ExportPanelOther)
    bpy.utils.unregister_class(ExportPanelVertexMerging)
    bpy.utils.unregister_class(ExportPanelTextures)
    bpy.utils.unregister_class(ExportPanelMeta)
    bpy.utils.unregister_class(ImportTeamcolor)
    bpy.utils.unregister_class(ImportDow3Model)
    bpy.utils.unregister_class(ImportDow3UnitLoadout)
    bpy.utils.unregister_class(ImportDow3UnitBrowse)
    bpy.utils.unregister_class(ImportDow3Unit)
    bpy.utils.unregister_class(ImportDow2Model)
    utils.cleanup_tree()
    bpy.utils.unregister_class(ImportDow2UnitSelect)
    bpy.utils.unregister_class(ImportDow2UnitLoadout)
    bpy.utils.unregister_class(ImportDow2UnitBrowse)
    bpy.utils.unregister_class(ImportWhmBrowse)
    bpy.utils.unregister_class(ImportDow2Unit)
    bpy.utils.unregister_class(DOW_UL_dow2_loadout)
    bpy.utils.unregister_class(DOW_OT_dow2_loadout_select_all)
    bpy.utils.unregister_class(Dow2LoadoutWargear)
    bpy.utils.unregister_class(Dow2LoadoutPart)
    bpy.utils.unregister_class(ImportWhmCli)
    bpy.utils.unregister_class(ImportWhm)
    bpy.utils.unregister_class(ImportPanelVertexMerging)
    bpy.utils.unregister_class(ImportPanelBasicOptions)
    dds.unregister()
