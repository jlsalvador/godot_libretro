ROOTDIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
IMAGE ?= godot-libretro-toolchain:12
HOST_ARCH := $(shell uname -m)
ARCH ?= $(HOST_ARCH)
TARGET ?= template_debug
JOBS ?= $(shell nproc)
RUN_USER := $(shell id -u):$(shell id -g)
VERSION ?= $(shell git -C $(ROOTDIR) describe --tags --exact-match 2>/dev/null || git -C $(ROOTDIR) rev-parse --short HEAD 2>/dev/null || echo local)

# Every build runs inside the pinned Debian 12 (GCC 12) container so host
# toolchain drift cannot change the artifacts (measured: GCC 15 miscompiles
# the N64 core into a ~400x slower binary).
container-run = podman run --rm --network none --user $(RUN_USER) -v $(ROOTDIR):/work -w /work $(IMAGE)

ifneq ($(filter arm64,$(ARCH)),)
GAMBATTE_FLAGS := CROSS_COMPILE=aarch64-linux-gnu- ARCHITECTURE=arm ARCHITECTURE_S=arm ARCHITECTURE_T=arm
else
GAMBATTE_FLAGS := CROSS_COMPILE=x86_64-linux-gnu- ARCHITECTURE=x86 ARCHITECTURE_S=i386 ARCHITECTURE_T=amd64
endif

.PHONY: all toolchain-image submodules extension extension-all \
	core-fceumm core-bsnes core-mupen64plus-next core-gambatte core-mgba \
	cores release package clean

all: extension-all cores

toolchain-image:
	podman build -t $(IMAGE) $(ROOTDIR)/tools

# Single level: the nested gitlink inside the N64 core tree is intentionally
# left uninitialized.
submodules:
	git -C $(ROOTDIR) submodule update --init

extension: submodules
	$(container-run) scons -j$(JOBS) target=$(TARGET) arch=$(ARCH)

extension-all:
	$(MAKE) extension ARCH=x86_64 TARGET=template_debug
	$(MAKE) extension ARCH=x86_64 TARGET=template_release
	$(MAKE) extension ARCH=arm64 TARGET=template_debug
	$(MAKE) extension ARCH=arm64 TARGET=template_release

core-fceumm: submodules
	mkdir -p build/libretro/$(ARCH)
	$(container-run) bash -c 'make -C third_party/libretro-fceumm -j$(JOBS) OBJARCH=$(ARCH) && cp third_party/libretro-fceumm/fceumm_libretro.so build/libretro/$(ARCH)/'

core-bsnes: submodules
	mkdir -p build/libretro/$(ARCH)
	$(container-run) bash -c 'make -C third_party/libretro-bsnes -j$(JOBS) OBJARCH=$(ARCH) && cp third_party/libretro-bsnes/bsnes_libretro.so build/libretro/$(ARCH)/'

core-mupen64plus-next: submodules
	mkdir -p build/libretro/$(ARCH)
	$(container-run) bash -c 'make -C third_party/libretro-mupen64plus-nx -j$(JOBS) platform=unix HAVE_THR_AL=1 LLE=1 ARCH=$(ARCH) && cp third_party/libretro-mupen64plus-nx/mupen64plus_next_libretro.so build/libretro/$(ARCH)/'

core-gambatte: submodules
	mkdir -p build/libretro/$(ARCH)
	$(container-run) bash -c 'make -C third_party/libretro-gambatte -j$(JOBS) $(GAMBATTE_FLAGS) && cp third_party/libretro-gambatte/gambatte_libretro.so build/libretro/$(ARCH)/'

core-mgba: submodules
	mkdir -p build/libretro/$(ARCH)
	$(container-run) bash -c 'make -C third_party/libretro-mgba -j$(JOBS) TARGET=libretro ARCH=$(ARCH) && cp third_party/libretro-mgba/mgba_libretro.so build/libretro/$(ARCH)/'

cores: core-fceumm core-bsnes core-mupen64plus-next core-gambatte core-mgba

# Full release matrix: extension (both arches, debug + release) and all
# cores for both arches.
release: extension-all
	$(MAKE) cores ARCH=x86_64
	$(MAKE) cores ARCH=arm64

# One Godot-extractable zip per core per arch, plus a single extension
# zip holding every platform and variant (addons/ layout).
package: release
	$(container-run) bash tools/package.sh $(VERSION)

clean:
	rm -rf build dist
