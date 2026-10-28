# godot_libretro

GDExtension host for running Libretro cores inside Godot. `LibretroSession`
dlopens a core, loads a game, runs frames, and hands back video frames, audio
chunks, save RAM, core options and, for hardware-rendering cores, frames
presented through a frontend-owned Vulkan device.

The release artifact is a single zip for Linux:

- `godot_libretro-linux-extension-<version>.zip`: the GDExtension for both
  architectures, debug and release. Extract it over a Godot project root and
  it lands in `addons/godot_libretro/.bin/...`.

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
- `.cache/toolchains/`: the pinned cross toolchains, downloaded by `make`.
  `make clean` leaves this cache alone.
- `third_party/`: pinned submodules (godot-cpp, libretro-common).
- `tests/`: the C callback probe (`make test-probe`), the minimal dummy core
  under `tests/dummy/` that stands in for a real core, and a minimal Godot
  project under `tests/godot/` that loads the dummy through the extension
  (`make test-godot`).

## Building

Prerequisites: the submodules at their pinned commits (the Makefile
initializes them), `scons`, `zip`, the Vulkan headers (`libvulkan-dev` on
Debian/Ubuntu; only the headers are used, the extension opens libvulkan at
runtime) and about 1 GB for the toolchains.

```
make toolchains                     # download and verify both toolchains, once
make extension ARCH=arm64 TARGET=template_release
make extension-all                  # full matrix, both architectures
make test-probe                     # drive the dummy through the raw callbacks
make test-godot                     # headless Godot harness, needs GODOT=...
make package                        # verify, then the dist/ zip
```

Both architectures cross-compile on the host, so no arm64 machine and no
emulator is involved. `make dummy ARCH=arm64` builds the dummy core for
another architecture, e.g. to run the harness on a device.

`make package` needs the matrix built first: the zip takes its artifacts from
`build/godot_libretro/` and stops with the missing path if it is not there.

Set `CCACHE=ccache` (and `CCACHE_DIR`) to put a compiler cache in front of the
toolchain, which is what CI does.

## Architecture and the glibc baseline

Objects live under `build/obj/<arch>/<variant>/` because the compiler is part
of the command line SCons signs them with: without the architecture in the
path, the x86-64 build and the arm64 cross build would otherwise pass the
identical command line and share objects.

The artifacts require the glibc of the toolchain's sysroot, not the one of the
machine that builds them. The toolchains here are Bootlin's `2022.08-1`
(GCC 11.3, glibc 2.35), and the libraries reference symbols up to
`GLIBC_2.34`; `readelf --version-info <library>` shows what a build needs.
Raising that baseline is a decision, not a side effect: the version, URL and
sha256 of each architecture's tarball are pinned in the Makefile.
