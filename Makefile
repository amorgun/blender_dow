.PHONY: all build validate wheels
BLENDER := blender
PIP := pip
TMP_DIR := build

all: wheels build

build: __init__.py animations.py chunky.py havok.py mesh.py textures.py dds.py materials.py utils.py operators.py props.py \
 slpp.py blender_manifest.toml sga.py dow_layout.py exporting.py importing.py \
 dow1/__init__.py dow1/importer.py dow1/exporter.py dow1/materials.py \
 dow2/__init__.py dow2/importer.py dow2/exporter.py dow2/attrib.py dow2/materials.py \
 dow3/__init__.py dow3/importer.py dow3/attrib.py dow3/materials.py dow3/rgm.py dow3/rgt.py \
 LICENSE README.md docs/export.md docs/magic.md docs/tts_import.md docs/faq.md docs/first_steps.md \
 default_badge.tga default_banner.tga
	mkdir $(TMP_DIR); \
	cp -r ./wheels $(TMP_DIR); \
	cp --parents $^ $(TMP_DIR); \
	cd $(TMP_DIR); \
	$(BLENDER) --command extension build --verbose; \
	cd ..;
	cp $(TMP_DIR)/*.zip .; \
	rm -rf $(TMP_DIR)

wheels:
	$(PIP) download pillow==12.1.1 --dest ./wheels --only-binary=:all: --python-version=3.13 --platform=manylinux_2_17_x86_64; \
	$(PIP) download pillow==12.1.1 --dest ./wheels --only-binary=:all: --python-version=3.13 --platform=win_amd64;

validate:
	$(BLENDER) --command extension validate
