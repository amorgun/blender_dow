.PHONY: all build validate wheels
BLENDER := blender
PIP := pip
TMP_DIR := build

all: wheels build

build: __init__.py importer.py exporter.py chunky.py textures.py utils.py operators.py props.py \
 slpp.py blender_manifest.toml sga.py dow_layout.py \
 LICENSE README.md docs/export.md docs/magic.md docs/tts_import.md default_badge.tga default_banner.tga
	mkdir $(TMP_DIR); \
	cp -r ./wheels $^ $(TMP_DIR); \
	cp --parents $^ $(TMP_DIR); \
	cd $(TMP_DIR); \
	$(BLENDER) --command extension build --verbose; \
	cd ..;
	cp $(TMP_DIR)/*.zip .; \
	rm -rf $(TMP_DIR)

wheels:
	$(PIP) download quicktex==0.2.1 --dest ./wheels --only-binary=:all: --python-version=3.11 --platform=manylinux_2_17_x86_64; \
	$(PIP) download quicktex==0.2.1 --dest ./wheels --only-binary=:all: --python-version=3.11 --platform=win_amd64;

validate:
	$(BLENDER) --command extension validate
