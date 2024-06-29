plugin_name = blender_dow

$(plugin_name).zip: __init__.py importer.py exporter.py chunky.py textures.py utils.py \
 LICENSE README.md docs/export.md
	mkdir $(plugin_name); \
	cp --parents $^ $(plugin_name); \
	zip -r $@ $(plugin_name); \
	rm -rf $(plugin_name)
