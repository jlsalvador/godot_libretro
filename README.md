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
  architecture, containing the core `.so` and its `LICENSE_<core>` file.
  Consumers discover cores at runtime from a `cores/` folder beside the
  executable (drop the `.so` files there, any mix of cores and platforms).

## Layout

- `src/`: the GDExtension sources (MIT, see `LICENSE`).
- `SConstruct`: builds `build/godot_libretro/<arch>/<variant>/libgodot_libretro.so`.
- `third_party/`: pinned submodules (godot-cpp + the five cores).
- `tools/Containerfile`: the Debian 12 (GCC 12) toolchain image. Every build
  runs inside it: newer host toolchains (measured GCC 15) miscompile the
  N64 core into a ~400x slower binary, so host toolchain drift must not
  reach the artifacts.
- `tools/package.sh`: builds the release zips (Godot-extractable).
- `LICENSES/`: the upstream license files of each pinned dependency, taken
  verbatim from the repos at their pinned commits.

## Cores and licenses

| Core | Artifact | License file |
|---|---|---|
| FCEUmm | `fceumm_libretro.so` | `LICENSES/LICENSE_fceumm` |
| bsnes | `bsnes_libretro.so` | `LICENSES/LICENSE_bsnes` |
| Mupen64Plus-Next | `mupen64plus_next_libretro.so` | `LICENSES/LICENSE_mupen64plus_next` |
| Gambatte | `gambatte_libretro.so` | `LICENSES/LICENSE_gambatte` |
| mGBA | `mgba_libretro.so` | `LICENSES/LICENSE_mgba` |
| godot-cpp (linked) | `libgodot_libretro.so` | `LICENSES/LICENSE_godot_cpp` |

The cores are separate works loaded at runtime through the Libretro C API;
the extension never links them, so their licenses stay inside their own
binaries. `libretro.h` (API header only, no code linked) ships inside the
cores' libretro-common trees.

## Building

Prerequisites: podman, `git submodule update --init` (single level: the
nested gitlink inside the N64 core tree is intentionally left uninitialized).

```
make toolchain-image            # build the pinned toolchain image
make extension ARCH=x86_64 TARGET=template_debug
make core-fceumm ARCH=arm64     # any single core, independently
make cores ARCH=x86_64          # all five for one arch
make release                    # full matrix, both arches
make package                    # dist/ zips + manifest.md5
```

## Releases

Tagged releases build the matrix (linux x86_64 + arm64 x template_debug +
template_release) and upload the zips with an md5 manifest. See
`.github/workflows/` (developed and validated locally before first push).
