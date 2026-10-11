ROOTDIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
IMAGE ?= godot-libretro-toolchain:12
HOST_ARCH := $(shell uname -m)
ARCH ?= $(HOST_ARCH)
TARGET ?= template_debug
JOBS ?= $(shell nproc)
VERSION ?= $(shell git -C $(ROOTDIR) describe --tags --exact-match 2>/dev/null || git -C $(ROOTDIR) rev-parse --short HEAD 2>/dev/null || echo local)

# Every build runs inside the pinned Debian 12 (GCC 12) container so host
# toolchain drift cannot change the artifacts (measured: GCC 15 miscompiles
# the N64 core into a ~400x slower binary).
container-run = podman run --rm --network none -v $(ROOTDIR):/work -w /work $(IMAGE)

# Compiler selection mirrors the roomvr recipe: native cc/c++ on the host
# arch, the Debian cross toolchain otherwise.
ifeq ($(ARCH),x86_64)
MUPEN_ARCH := x86_64
else ifeq ($(ARCH),arm64)
MUPEN_ARCH := aarch64
else
$(error Unsupported target architecture: $(ARCH))
endif
ifeq ($(ARCH),$(HOST_ARCH))
CC_FOR_TARGET := cc
CXX_FOR_TARGET := c++
else ifeq ($(ARCH),arm64)
CC_FOR_TARGET := aarch64-linux-gnu-gcc
CXX_FOR_TARGET := aarch64-linux-gnu-g++
else
CC_FOR_TARGET := x86_64-linux-gnu-gcc
CXX_FOR_TARGET := x86_64-linux-gnu-g++
endif

.PHONY: all toolchain-image submodules extension extension-all \
	core-fceumm core-bsnes core-mupen64plus-next core-gambatte core-mgba \
	cores roms test-godot release package clean

GODOT ?= $(shell command -v godot 2>/dev/null)

all: extension-all cores roms
toolchain-image:
	podman build -t $(IMAGE) $(ROOTDIR)/tools

# Single level: the nested gitlink inside the N64 core tree is intentionally
# left uninitialized.
submodules:
	git -C $(ROOTDIR) submodule update --init

extension: submodules
	$(container-run) scons -j$(JOBS) target=$(TARGET) arch=$(ARCH) api_version=4.7

extension-all:
	$(MAKE) extension ARCH=x86_64 TARGET=template_debug
	$(MAKE) extension ARCH=x86_64 TARGET=template_release
	$(MAKE) extension ARCH=arm64 TARGET=template_debug
	$(MAKE) extension ARCH=arm64 TARGET=template_release

core-fceumm: submodules
	mkdir -p build/libretro/$(ARCH)
	$(container-run) bash -c 'make -C third_party/libretro-fceumm -f Makefile.libretro platform=unix CC=$(CC_FOR_TARGET) CXX=$(CXX_FOR_TARGET) -j$(JOBS) && cp third_party/libretro-fceumm/fceumm_libretro.so build/libretro/$(ARCH)/'

core-bsnes: submodules
	mkdir -p build/libretro/$(ARCH)
	$(container-run) bash -c 'make -C third_party/libretro-bsnes -f Makefile platform=unix CC=$(CC_FOR_TARGET) CXX=$(CXX_FOR_TARGET) -j$(JOBS) && cp third_party/libretro-bsnes/bsnes_libretro.so build/libretro/$(ARCH)/'

core-mupen64plus-next: submodules
	mkdir -p build/libretro/$(ARCH)
	$(container-run) bash -c 'make -C third_party/libretro-mupen64plus-nx -f Makefile platform=unix ARCH=$(MUPEN_ARCH) CC=$(CC_FOR_TARGET) CXX=$(CXX_FOR_TARGET) HAVE_THR_AL=1 HAVE_PARALLEL_RSP=1 HAVE_PARALLEL_RDP=1 LLE=1 -j$(JOBS) && cp third_party/libretro-mupen64plus-nx/mupen64plus_next_libretro.so build/libretro/$(ARCH)/'

core-gambatte: submodules
	mkdir -p build/libretro/$(ARCH)
	$(container-run) bash -c 'make -C third_party/libretro-gambatte -f Makefile.libretro platform=unix CC=$(CC_FOR_TARGET) CXX=$(CXX_FOR_TARGET) -j$(JOBS) && cp third_party/libretro-gambatte/gambatte_libretro.so build/libretro/$(ARCH)/'

core-mgba: submodules
	mkdir -p build/libretro/$(ARCH)
	$(container-run) bash -c 'cmake -S third_party/libretro-mgba -B third_party/libretro-mgba/build-roomvr-$(ARCH) -DBUILD_LIBRETRO=ON -DBUILD_QT=OFF -DBUILD_SDL=OFF -DBUILD_TEST=OFF -DBUILD_SUITE=OFF -DBUILD_PERF=OFF -DBUILD_PYTHON=OFF -DBUILD_DOCGEN=OFF -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_COMPILER=$(CC_FOR_TARGET) -DCMAKE_CXX_COMPILER=$(CXX_FOR_TARGET) && cmake --build third_party/libretro-mgba/build-roomvr-$(ARCH) --target mgba_libretro -j$(JOBS) && cp third_party/libretro-mgba/build-roomvr-$(ARCH)/mgba_libretro.so build/libretro/$(ARCH)/'

cores: core-fceumm core-bsnes core-mupen64plus-next core-gambatte core-mgba

# One original sample ROM per core, compiled from tools/sample_roms.
roms:
	python3 tools/sample_roms/make_sample_roms.py build/roms

# Headless Godot tests of the extension and the cores, run against the
# host-arch debug extension and the host-arch cores.
test-godot: extension roms
	@test -n "$(GODOT)" || { echo "No godot binary found; set GODOT=/path/to/godot"; exit 1; }
	$(MAKE) cores ARCH=$(HOST_ARCH)
	mkdir -p tests/godot/addons/godot_libretro/.bin/linux/template_debug/$(HOST_ARCH) \
		tests/godot/roms tests/godot/build/libretro/$(HOST_ARCH)
	cp build/godot_libretro/$(HOST_ARCH)/debug/libgodot_libretro.so \
		tests/godot/addons/godot_libretro/.bin/linux/template_debug/$(HOST_ARCH)/
	cp build/roms/sample.* tests/godot/roms/
	cp build/libretro/$(HOST_ARCH)/*.so tests/godot/build/libretro/$(HOST_ARCH)/
	@$(GODOT) --headless --xr-mode off --path tests/godot --import || true
	@for t in tests/godot/tests/test_*.gd; do \
		echo "== $$t"; \
		$(GODOT) --headless --xr-mode off --path tests/godot --script res://tests/$$(basename $$t) || exit 1; \
	done

# Full release matrix: extension (both arches, debug + release), all
# cores for both arches, and the sample ROMs.
release: extension-all roms
	$(MAKE) cores ARCH=x86_64
	$(MAKE) cores ARCH=arm64
# One Godot-extractable zip per core per arch, plus a single extension
# zip holding every platform and variant (addons/ layout).
package: release
	$(container-run) bash tools/package.sh $(VERSION)

clean:
	rm -rf build dist
