# godot_libretro

GDExtension host for running Libretro cores inside Godot, plus the pinned
Libretro cores it runs. The extension exposes the `LibretroSession` class
(dlopen a core, load a game, run frames, read back video/audio, save RAM,
core options, frontend-owned Vulkan device for hardware-rendering cores).

This project ships precompiled Linux artifacts:

- `godot_libretro-linux-extension-<version>.zip`: the GDExtension for every
  platform and variant (extract over a Godot project root; lands in
  `addons/godot_libretro/.bin/...`).
- `godot_libretro-linux-<arch>-<core>-<version>.zip`: one core per
  architecture, containing the core `.so`, its `LICENSE` (the license file
  of its pinned submodule tree), a `SOURCE` file naming the upstream
  repository and the exact commit it was built from, and the original
  sample ROM that core boots.

## Layout

- `src/`: the GDExtension sources (MIT, see `LICENSE`).
- `SConstruct`: builds `build/godot_libretro/<arch>/<variant>/libgodot_libretro.so`
  from objects kept per architecture and variant under `build/obj/<arch>/<variant>/`.
- `third_party/`: pinned submodules (godot-cpp, the six cores, libretro-common).
- `tools/Containerfile`: the Debian 12 (GCC 12) toolchain image, built once per
  architecture (`godot-libretro-toolchain:12-x86_64` and `:12-arm64`). Every
  build runs natively inside the image of its own architecture: newer host
  toolchains miscompile the Mupen64Plus-Next core badly, and
  cross-compiling does not work for this stack (godot-cpp's
  SConstruct builds an explicit Environment that discards `CC`/`CXX`).
- `tests/`: the C callback probe that drives a built core without Godot
  (`make test-probe`), and a minimal Godot project
  under `tests/godot/` that loads the cores through the extension
  (`make test-godot`).

## Cores and licenses

| Core | Artifact | License file |
|---|---|---|
| FCEUmm | `fceumm_libretro.so` | `third_party/libretro-fceumm/Copying` |
| bsnes | `bsnes_libretro.so` | `third_party/libretro-bsnes/LICENSE.txt` |
| Mupen64Plus-Next | `mupen64plus_next_libretro.so` | `third_party/libretro-mupen64plus-nx/LICENSE` |
| Gambatte | `gambatte_libretro.so` | `third_party/libretro-gambatte/COPYING` |
| mGBA | `mgba_libretro.so` | `third_party/libretro-mgba/LICENSE` |
| pcsx_rearmed | `pcsx_rearmed_libretro.so` | `third_party/libretro-pcsx_rearmed/COPYING` |
| godot-cpp (linked) | `libgodot_libretro.so` | `third_party/godot-cpp/LICENSE.md` |

The cores are separate works loaded at runtime through the Libretro C API;
the extension never links them, so their licenses stay inside their own
binaries. `libretro.h` (API header only, no code linked) is taken from
our pinned `third_party/libretro-common` submodule.
Five of the six cores are GPLv2 and mGBA is MPL-2.0; each core zip's
SOURCE file names the upstream repository and the exact pinned commit, so
the complete corresponding source of every binary is available there; the
trees are built unmodified.

## Building

Prerequisites: podman, and the submodules at their pinned commits (single
level: the nested gitlink inside the Mupen64Plus-Next core tree is
uninitialized). The Makefile initializes them for you.

```
make toolchain-image ARCH=x86_64   # the pinned image for one architecture
make toolchain-image ARCH=arm64    # ... and for the other one
make extension ARCH=x86_64 TARGET=template_debug
make core-fceumm ARCH=arm64        # any single core, independently
make cores ARCH=x86_64             # all six for one arch
make release                       # full matrix, both arches
make package                       # verify, then dist/ zips + manifest.md5
```

`make release` must run before `make package`: the zip rules take their
artifacts from the already-built matrix and stop with the missing path if it is
not there.

For CI, `IN_CONTAINER=1` runs the same recipes in the current shell, for the
container CI has started around `make`.

## Why builds are isolated by architecture

Cores are built inside a per-architecture copy of their pinned tree
(`build/src/<arch>/`), never in `third_party/` itself: those Makefiles put their
`.o` files next to the sources with no architecture in the name, so a second
architecture in the same tree would report `Nothing to be done for 'all'` and
publish the first architecture's `.so` under the second one's folder.

The extension keeps its objects under `build/obj/<arch>/<variant>/` for the same
reason: SCons decides by the signature of the command line, and two native
toolchain containers pass the identical one.

