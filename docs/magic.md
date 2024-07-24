# Additional model configuration
The DoW model format contains a lot of info that's not easily translated into Blender. There is a list of magic attributes and reserved names used in export.

## Material
### Attributes
| **Name** | **Type** | **Description** |
|----------|---------|--------|
| full_path | str | Path to export this material. Useful for reusing the same `.rsh` between different models. |
| internal | bool | Do not export this material to a separate file and keep it inside the model file|
### Node names
The addon can guess what images to export to the channels of the resulting `.rsh` and `.wtp` file.  
For better control over it you can set the name of a Shader Node node to one of these values:
| **Name** | **Exported to** | **Node type** |
|----------|----------|----------|
| diffuse | .rsh | Image Texture |
| specularity | .rsh | Image Texture |
| reflection | .rsh | Image Texture |
| self_illumination | .rsh | Image Texture |
| opacity | .rsh | Image Texture |
| color_layer_default | .wtp | Image Texture |
| color_layer_dirt | .wtp | Image Texture |
| color_layer_primary | .wtp | Image Texture |
| color_layer_secondary | .wtp | Image Texture |
| color_layer_trim | .wtp | Image Texture |
| color_layer_weapons | .wtp | Image Texture |
| color_layer_eyes | .wtp | Image Texture |
| badge_position | .wtp | Combine XYZ |
| badge_display_size | .wtp | Combine XYZ |
| banner_position | .wtp | Combine XYZ |
| banner_display_size | .wtp | Combine XYZ |


## Bone
### Attributes
| **Name** | **Type** | **Animation frames** | **Description** |
|----------|-------|---------|------------|
| Stale | bool | First | Apply it to each bone you want to disable in an animation. |
### Bone collections
| **Name** | **Description** |
|----------|-----------------|
| Markers | Used to identify bones as markers. If there is no such collection bones with names starting with `marker_` are considered markers. |
| Cameras | Used to identify bones as camera positions. |

## Mesh
### Attributes
| **Name** | **Type** | **Description** |
|----------|----|-------------|
| xref_source | str | Reference this mesh from an external file instead of this model. |
 

## Armature
### Attributes
DoW mostly uses skeletal animation so if you want to animate stuff like texture UV offset [you need to use attributes of the armature](https://blenderartists.org/t/how-do-actions-with-multiple-objects-work/1525242).
| **Name** | **Animation frames** | **Type** | **Description** |
|----------|-------------|----|------------|
| force_invisible__<mesh_name> | First | bool | Force the mesh to be invisible in the current animation |
| visibility__<mesh_name> | All | float | Hack for animatiing mesh visibility. |
| uv_offset__<material_name> | All | tuple[float, float] | Hack for animatiing UV offset. Often used for tank tracks. | 
| uv_tiling__<material_name> | All | tuple[float, float] | Hack for animatiing UV tiling. I couldn't find any models using it.| 
