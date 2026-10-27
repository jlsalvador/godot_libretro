# godot_libretro

GDExtension host for running Libretro cores inside Godot. The extension
exposes the `LibretroSession` class (dlopen a core, load a game, run frames,
read back video/audio, save RAM, core options, frontend-owned Vulkan device
for hardware-rendering cores).

This project ships one precompiled Linux artifact:

- `godot_libretro-linux-extension-<version>.zip`: the GDExtension for every
  platform and variant (extract over a Godot project root; lands in
  `addons/godot_libretro/.bin/...`).

## Cores

The extension is core-agnostic: it loads any Libretro core binary. Prebuilt
Linux cores are available from the official Libretro buildbot, one
`<core>_libretro.so.zip` per architecture under
`https://buildbot.libretro.com/nightly/linux/<arch>/latest/`, with `<arch>`
as `x86_64` or `aarch64`: download the zip for your architecture, unzip it,
and hand the resulting `<core>_libretro.so` to
`LibretroSession.load_core()`. The cores are separate works loaded at
runtime through the Libretro C API; the extension never links them, so their
licenses stay inside their own binaries.

## Layout

- `src/`: the GDExtension sources (MIT, see `LICENSE`).
- `SConstruct`: builds `build/godot_libretro/<arch>/<variant>/libgodot_libretro.so`
  from objects kept per architecture and variant under `build/obj/<arch>/<variant>/`.
- `third_party/`: pinned submodules (godot-cpp, libretro-common).
- `tools/Containerfile`: the Debian 12 (GCC 12) toolchain image, built once
  per architecture (`godot-libretro-toolchain:12-x86_64` and `:12-arm64`).
  Every build runs natively inside the image of its own architecture: newer
  host toolchains change the artifacts, and cross-compiling does not work
  for this stack (godot-cpp's SConstruct builds an explicit Environment that
  discards `CC`/`CXX`).
- `tests/`: the C callback probe (`make test-probe`), the minimal dummy core
  under `tests/dummy/` that stands in for a real core, and a minimal Godot
  project under `tests/godot/` that loads the dummy through the extension
  (`make test-godot`).

## BIOS files


## Building

Prerequisites: podman, and the submodules at their pinned commits. The
Makefile initializes them for you.

```
make toolchain-image ARCH=x86_64   # the pinned image for one architecture
make toolchain-image ARCH=arm64    # ... and for the other one
make extension ARCH=x86_64 TARGET=template_debug
make test-probe                    # drive the dummy through the raw callbacks
make test-godot                    # headless Godot harness, needs GODOT=...
make release                       # full matrix, both arches
make package                       # verify, then the dist/ zip
```

`make release` must run before `make package`: the zip rule takes its
artifacts from the already-built matrix and stops with the missing path if
it is not there.

For CI, `IN_CONTAINER=1` runs the same recipes in the current shell, for
the container CI has started around `make`.

## Why builds are isolated by architecture

The extension keeps its objects under `build/obj/<arch>/<variant>/` for the
same reason the core builds used to: SCons decides by the signature of the
command line, and two native toolchain containers (one per architecture)
pass the identical one, so an x86-64 object would be reused for the arm64
library.
