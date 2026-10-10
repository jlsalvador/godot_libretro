#!/usr/bin/env bash
# Packages the full matrix into Godot-extractable zips:
#   godot_libretro-linux-extension-<version>.zip
#     all platforms + variants, addon layout (extract over a project root)
#   godot_libretro-linux-<arch>-<core>-<version>.zip
#     one core per architecture, with its LICENSE_<core>
set -eu
cd "$(cd "$(dirname "$0")/.." && pwd)"
V="$1"
CORES="fceumm bsnes mupen64plus_next gambatte mgba"

rm -rf dist
mkdir -p dist

# Extension: every arch x variant in a single zip, addons/ layout.
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
for arch in x86_64 arm64; do
	for pair in "template_debug:debug" "template_release:release"; do
		target="${pair%%:*}"
		variant="${pair##*:}"
		out_dir="addons/godot_libretro/.bin/linux/${target}/${arch}"
		mkdir -p "${stage}/${out_dir}"
		cp "build/godot_libretro/${arch}/${variant}/libgodot_libretro.so" "${stage}/${out_dir}/"
	done
done
cp LICENSE "${stage}/addons/godot_libretro/"
(cd "${stage}" && zip -9 -rX "/work/dist/godot_libretro-linux-extension-${V}.zip" addons >/dev/null)

# Cores: one zip per core per arch, .so + its own license file.
for arch in x86_64 arm64; do
	for core in ${CORES}; do
		(
			cd "build/libretro/${arch}"
			zip -9 -X "/work/dist/godot_libretro-linux-${arch}-${core}-${V}.zip" \
				"${core}_libretro.so" -j "/work/LICENSES/LICENSE_${core}" >/dev/null
		)
	done
done

(cd dist && md5sum *.zip > manifest.md5)
echo "dist/ contents:"
ls -1 dist/
