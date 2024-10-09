# Transfering a model from Gladius to DoW

Warhammer 40,000: Gladius – Relics of War is a turn-based strategy set in the Warhammer 40,000 universe.  
In addition to being a great game it has a lot of units not presented in the Dawn of War game, including AdMech and tyranid races.  
In this tutorial I'll show how you can take a model from Gladius and use it in Dow.

1. Get the model
    1. Intstall [Blender Gladius Addon](https://github.com/amorgun/blender_gladius?tab=readme-ov-file#installation)
    2. Choose your favorite unit and open its .xml file with `File -> Import -> Gladius Unit (.xml)`
2. Fix model materials
    1. Open `Scripting` tab
    2. Click `New`
    3. Copy the script from [here](https://github.com/amorgun/dow_utils/blob/main/scripts/gladius_convert.py) and paste it into the newly created script
    4. Run the script with `Alt + P` or click the `Run` button
3. Export the model to `.whm`  
 Go to `File -> Export -> Dawn of War model (.whm)`.
4. Create a `.whe` file  
 You can do it using export to `.sgm` and Object Editor as described in the [export tutorial](./export.md).  
 But I find it more convenient using [Object Tool](https://github.com/amorgun/dow_utils/tree/main/object_tool):  
    1. Copy the template from [here](./gladius_whe_template.json)
    2. Adjust `selected_ui` there to match the unit model
    3. Add needed animations
    4. Use [Object Tool](https://github.com/amorgun/dow_utils/tree/main/object_tool) to convert the resulting `.json` file to `.whe`