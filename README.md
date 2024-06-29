# Blender Dawn of War Addon
Blender addon for import and export of Dawn of War models.  

![blender_screenshot](images/dred_render1.png)

## Key features
- **Import and Export Functionality**: Supports the Dawn of War model format(`.whm`), including meshes, textures, bones, markers and animations.
* **Object Editor support**: Allows export to Object Editor model format(`.sgm`) so you can configure the unit properties, e.g. motions and actions.
- **Built-in conversion of textures**: Automatically imports `.rsh` texture files, including all additional layers, and exports all of it back.

## Installation
1. Download the latest release from the [Releases page](https://github.com/amorgun/blender_dow/releases/).
2. In Blender go to `Edit -> Preferences`
3. Go to the `Addons` tab, click `Install..` and select `blender_dow.zip`
4. Set up the `Mod folder` option to the path to your mod.

## Import
1. Open your mod with [Corsix's Mod Studio](https://modstudio.corsix.org/)
2. Find and unpack the model `.whm` file (usually located at `Data/art/ebps/races/<race>/troops`)
3. Unpack `.rsh` files with model textures  (usually located at `Data/art/ebps/races/<race>/texture_share`)  
  If you missed some textures you can unpack them later following error messages from the addon.
4. In Blender go to `File -> Import -> Dawn of War model (.whm)` and select your `.whm` file.

## Export
Often a simple `File -> Export -> Dawn of War model (.whm)` is enough.  
There is a [detailed page](docs/export.md) with a full process of exporting and adding a new model into DoW.

## Troubleshooting
The addon reports some messages that you can find in [Info Editor](https://docs.blender.org/manual/en/latest/editors/info_editor.html).  
In case it doesn't help feel free to [file an issue](https://github.com/amorgun/blender_dow/issues).


## Acknowledgments
- [Santos Tools](https://web.archive.org/web/20140916035249/http://forums.relicnews.com/showthread.php?76791-Santos-Tools) - original import script for 3ds Max.
- [Relic-Game-Tool](https://github.com/ModernMAK/Relic-Game-Tool) - another script for parsing relic chunky files and importing models to Blender.
- [Dawn of War Texture Tool](https://skins.hiveworldterra.co.uk/Downloads/detail_DawnOfWarTextureTool.html) - detailed info on RSH file structure.
- [Mudflaps WHM Model Converter Tool](https://web.archive.org/web/20140914165503/http://forums.relicnews.com/showthread.php?116040-WHM-Model-Converter-Tool)

## Disclaimer
Not affiliated with Sega, Relic Entertainment, or THQ.
