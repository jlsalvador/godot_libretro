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
  architecture, containing the core `.so`, its `LICENSE_<core>` file and the
  original sample ROM that core boots.
  Consumers discover cores at runtime from a `cores/` folder beside the
  executable (drop the `.so` files there, any mix of cores and platforms).

## Layout

- `src/`: the GDExtension sources (MIT, see `LICENSE`).
- `SConstruct`: builds `build/godot_libretro/<arch>/<variant>/libgodot_libretro.so`
  from objects kept per architecture and variant under `build/obj/<arch>/<variant>/`.
- `third_party/`: pinned submodules (godot-cpp, the five cores, libretro-common).
- `tools/Containerfile`: the Debian 12 (GCC 12) toolchain image, built once per
  architecture (`godot-libretro-toolchain:12-x86_64` and `:12-arm64`). Every
  build runs natively inside the image of its own architecture: newer host
  toolchains (measured GCC 15) miscompile the N64 core into a ~400x slower
  binary, and cross-compiling does not work for this stack (godot-cpp's
  SConstruct builds an explicit Environment that discards `CC`/`CXX`).
- `tests/`: the C callback probe that drives a built core without Godot
  (`make test-probe`), and a minimal Godot project
  under `tests/godot/` that loads the cores through the extension
  (`make test-godot`).
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
binaries. `libretro.h` (API header only, no code linked) is taken from
our pinned `third_party/libretro-common` submodule.

## Building

Prerequisites: podman, and the submodules at their pinned commits (single
level: the nested gitlink inside the N64 core tree is intentionally left
uninitialized). The Makefile initializes them for you.

```
make toolchain-image ARCH=x86_64   # the pinned image for one architecture
make toolchain-image ARCH=arm64    # ... and for the other one
make extension ARCH=x86_64 TARGET=template_debug
make core-fceumm ARCH=arm64        # any single core, independently
make cores ARCH=x86_64             # all five for one arch
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

## Stripping, symbols and the verification gate

Cores ship stripped (`strip --strip-unneeded`), measured savings of 2-11% per
core (mupen64plus_next 8.3 MB to 7.4 MB, gambatte 4.9 MB to 4.8 MB). Nothing is
packaged until it has booted:

- every stripped core is resolved symbol by symbol through
  `libretro_probe --symbols`, which is exactly what a bad strip would break;
- the three cores whose callbacks the bare probe can drive (FCEUmm, bsnes, mGBA)
  also run 120 real frames;
- `mupen64plus_next` and `gambatte` are loaded through the extension itself by
  `check-stripped-godot`, because they need what `LibretroSession` provides (a
  host Vulkan device and `GET_CAN_DUPE`) and the bare probe cannot boot them.

`make package` runs those checks for both architectures (arm64 under qemu) plus
the host-architecture harness. Pass `SKIP_VERIFY=1` only where the checks
already ran, which is what CI does: each architecture job verifies its own
stripped cores before anything is zipped.

The `template_debug` extension variant keeps full DWARF (`debug_symbols=yes`,
`-g2`) so a crash on the headset resolves to file and line; `template_release`
links with `-s`. Measured on x86_64: 522 KB stripped release versus 10.8 MB for
the debug variant, which is why the extension zip is around 9 MB rather than
1 MB. Building the debug variant with `debug_symbols=no` gives 1.1 MB with the
symbol table only, and no line information.

## Releases

Tagged releases build the matrix (linux x86_64 + arm64 x template_debug +
template_release) and upload the zips with an md5 manifest.
`.github/workflows/build.yml` starts the toolchain image of each architecture
and runs `make IN_CONTAINER=1 <target>` inside it, so no build command lives in
the YAML. The arm64 jobs run that image under qemu on the amd64 runner.

This repository has no remote yet, so the YAML has never been executed by a
runner. Only the commands it calls are validated locally.
