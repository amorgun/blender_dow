# Additional model configuration
The DoW model format contains a lot of info that's not easily translated into Blender. There is a list of magic attributes and reserved names used in export.

## Material
### Attributes
| **Name** | **Description** |
|----------|-----------------|
| full_path | Path to export this material. Useful for reusing the same `.rsh` between different models. |
| internal | Do not export this material to a separate file and keep it inside the model file|
### Node names
| **Name** |
|----------|
| diffuse |
| specularity |
| reflection |
| self_illumination |
| opacity |


## Bone
### Attributes
| **Name** | **Animation frames** | **Description** |
|----------|-----------------|------------|
| Stale | First | Apply it to each bone you want to disable in an animation. |
### Bone collections
| **Name** | **Description** |
|----------|-----------------|
| Markers | Used to identify bones as markers. If there is no such collection bones with names starting with `marker_` are considered markers. |
| Cameras | Used to identify bones as camera positions. |

## Mesh
### Attributes
| **Name** | **Description** |
|----------|-----------------|
| xref_source | Reference this mesh from an external file instead of this model. |
 

## Armature
### Attributes
| **Name** | **Animation frames** | **Type** | **Description** |
|----------|-------------|----|------------|
| force_invisible__<mesh_name> | First | bool | Force the mesh to be invisible in the current animation |
| visibility__<mesh_name> | All | float | Hack for animatiing mesh visibility. |
| uv_offset__<material_name> | All | tuple[float, float] | Hack for animatiing UV offset. Often used for tank tracks. | 
| uv_tiling__<material_name> | All | tuple[float, float] | Hack for animatiing UV tiling. I couldn't find any models using it.| 
