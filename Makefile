addon_name = blender_dow

$(addon_name).zip: __init__.py importer.py exporter.py chunky.py textures.py utils.py \
 LICENSE README.md docs/export.md
	mkdir $(addon_name); \
	cp --parents $^ $(addon_name); \
	zip -r $@ $(addon_name); \
	rm -rf $(addon_name)
