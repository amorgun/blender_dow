addon_name = blender_dow

$(addon_name).zip: __init__.py importer.py exporter.py chunky.py textures.py utils.py \
 LICENSE README.md docs/export.md docs/magic.md docs/tts_import.md default_badge.tga default_banner.tga
	mkdir $(addon_name); \
	cp --parents $^ $(addon_name); \
	zip -r $@ $(addon_name); \
	rm -rf $(addon_name)
