Here is a process of importing a model from [Tabletop Simulator](https://store.steampowered.com/app/286160/Tabletop_Simulator/)(TTS) into DoW.
I will import a space marine model from [this mod](https://steamcommunity.com/sharedfiles/filedetails/?id=3136633493).

1. **Open the model**
    - Subscribe to the mod.
    - Open TTS to load the mod data.
    - Load the mod
    - Right-click on the model, select "Custom" and copy URLs for model and texture.
    - Open the URLs in a web browser and download the mesh and image.
    Your browser may warn you about insecure download. Press "Keep".
    - Open Blender and import the mesh.  
    Go to `File - Import - Wavefromt (.obj)` and select mesh file.  
    Don't forget to remove the default cube.
    - Configure texture
        - Open "Shading" tab
        - Select the mesh
        - Add `Image Texture` node (`Right Click - Add- Image Texture`)
        - Click `Open` and select the downloaded texture
        - Connect `Color` and `Alpha` outputs to the matching inputs of the `Principled BSDF` node.
    - Rename the material
      Material name is used for exported texture name. The name `Material.001` may cause collisions if you decide to import several models.  
        Double-click on the Material name and type a new name, e.g. `tts_space_marine`
1. **Export the model to `.whm`**  
 Go to `File -> Export -> Dawn of War model (.whm)`.
 Since the model is not animated you don't need a `.whe`.

Then you can a new unit and add it to Army Painter [as described before](export.md#).
I'll copy it here just for completness:

3. **Put the `.whm` files into your mod folder**  
    I put the resulting `tts_model.whm` into `Data/art/ebps/races/space_marines/troops`.
    By default the textures are exported into the folder with the same name as the exported model.
    Copy converted `.rsh` textures into the appropriate location. Check `info.txt` in the exported textures folder for the correct locations.
9. **Configure DoW to show your model in Army Painter**
    1. Open your mod with [Corsix's Mod Studio](https://modstudio.corsix.org/)
    2. Add an entity to `Data/attrib/ebps/races/space_marines/troops`  
        You can copy an existing file and change `entity_blueprint_ext - animator` value to `Races/Space_Marines/Troops/tts_model`  
        Also set `ui_info - screen_name_id` value to `TTS test` and  the data type to `Text`
    3. Edit `space_marine_race.rgd`.  
        Find `Data/attrib/racebps/space_marine_race.rgd` and set `teamcolour_preview - entity_03` value to `tts_model`.