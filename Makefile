ROOTDIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
IMAGE ?= godot-libretro-toolchain
IMAGE_TAG ?= 12
HOST_ARCH := $(shell uname -m)
ARCH ?= $(HOST_ARCH)
TARGET ?= template_debug
# Debug variants keep full DWARF (-g2 from godot-cpp) so a crash on the headset
# resolves to file and line; the release variant links with -s. Measured on the
# x86_64 addon: 522 KB stripped release, 10.8 MB with DWARF, 1.1 MB with the
# symbol table only.
ifeq ($(TARGET),template_release)
DEBUG_SYMBOLS ?= no
else
DEBUG_SYMBOLS ?= yes
endif
JOBS ?= $(shell nproc)
VERSION ?= $(shell git -C $(ROOTDIR) describe --tags --exact-match 2>/dev/null || git -C $(ROOTDIR) rev-parse --short HEAD 2>/dev/null || echo local)

# One pinned Debian 12 (GCC 12) toolchain image per architecture, and every
# build runs natively inside the image of its own architecture. Two reasons,
# both measured: host toolchain drift changes the artifacts (GCC 15 compiles
# the N64 core into a ~400x slower binary), and cross-compiling from the host
# does not work for this stack (godot-cpp's SConstruct builds an explicit
# Environment that discards CC/CXX overrides, so scons passed -march=armv8-a
# to the x86-64 assembler: "bad value 'armv8-a' for '-march=' switch").
ifeq ($(ARCH),x86_64)
CONTAINER_UNAME := x86_64
OCI_PLATFORM := amd64
else ifeq ($(ARCH),arm64)
CONTAINER_UNAME := aarch64
OCI_PLATFORM := arm64
else
$(error Unsupported target architecture: $(ARCH))
endif

IMAGE_NAME := $(IMAGE):$(IMAGE_TAG)-$(ARCH)
HOST_IMAGE_NAME := $(IMAGE):$(IMAGE_TAG)-$(HOST_ARCH)

PODMAN ?= podman

# IN_CONTAINER=1 runs the recipes in the current shell: CI starts the toolchain
# container itself and calls make inside it, so the Makefile must not nest a
# podman run there. Packaging always uses the host-architecture image: zipping
# is architecture-neutral and the image is the only place guaranteed to carry
# the same zip and md5sum as every other build.
ifeq ($(IN_CONTAINER),1)
container-run = bash -c
package-run = bash -c
image-uname = uname -m
else
container-run = $(PODMAN) run --rm --platform linux/$(OCI_PLATFORM) --network none -v $(ROOTDIR):/work -w /work $(IMAGE_NAME) bash -c
package-run = $(PODMAN) run --rm --network none -v $(ROOTDIR):/work -w /work $(HOST_IMAGE_NAME) bash -c
image-uname = $(PODMAN) run --rm --platform linux/$(OCI_PLATFORM) $(IMAGE_NAME) uname -m
endif

ARCHES := x86_64 arm64
CORE_IDS := fceumm bsnes mupen64plus_next gambatte mgba
CORE_SOS := $(addsuffix _libretro.so,$(CORE_IDS))
# The sample ROM each core boots, from tools/sample_roms.
CORE_ROMS := sample.nes sample.sfc sample.v64 sample.gbc sample.gba
CORE_INDEX := 1 2 3 4 5

EXT_LIBS := $(foreach a,$(ARCHES),$(foreach v,debug release,build/godot_libretro/$(a)/$(v)/libgodot_libretro.so))
CORE_ZIPS := $(foreach a,$(ARCHES),$(foreach c,$(CORE_IDS),dist/godot_libretro-linux-$(a)-$(c)-$(VERSION).zip))
EXT_ZIP := dist/godot_libretro-linux-extension-$(VERSION).zip
MANIFEST := dist/manifest.md5

.PHONY: all toolchain-image toolchain-images submodules check-image-arch \
	extension extension-all \
	core-fceumm core-bsnes core-mupen64plus-next core-gambatte core-mgba \
	cores cores-all roms test-probe test-godot release package clean \
	check-stripped check-stripped-all check-stripped-godot

GODOT ?= $(shell command -v godot 2>/dev/null)

all: extension-all cores-all roms

toolchain-image:
	$(PODMAN) build --platform linux/$(OCI_PLATFORM) -t $(IMAGE_NAME) $(ROOTDIR)/tools

toolchain-images:
	$(MAKE) toolchain-image ARCH=x86_64
	$(MAKE) toolchain-image ARCH=arm64

# A wrong-architecture image would build wrong-architecture artifacts without
# any error (the recipes never name a compiler), so refuse before building.
.PHONY: check-image-arch
check-image-arch:
ifeq ($(IN_CONTAINER),1)
	@:
else
	@actual=$$( $(image-uname) ); \
	[ "$$actual" = "$(CONTAINER_UNAME)" ] || { \
		echo "Image $(IMAGE_NAME) runs '$$actual', expected $(CONTAINER_UNAME)." >&2; \
		echo "Build it first: make toolchain-image ARCH=$(ARCH)" >&2; exit 1; }
endif

# Single level: the nested gitlink inside the N64 core tree is intentionally
# left uninitialized.
.PHONY: submodules
submodules:
ifeq ($(IN_CONTAINER),1)
	@:
else
	git -C $(ROOTDIR) submodule update --init
endif

.PHONY: extension
extension: submodules check-image-arch ## Build the GDExtension for ARCH/TARGET.
	$(container-run) 'scons -j$(JOBS) target=$(TARGET) arch=$(ARCH) api_version=4.7 debug_symbols=$(DEBUG_SYMBOLS)'

.PHONY: extension-all
extension-all: ## Extension for both arches, debug and release.
	for a in $(ARCHES); do \
		for t in template_debug template_release; do \
			$(MAKE) extension ARCH=$$a TARGET=$$t || exit 1; \
		done; \
	done

# Each architecture builds its cores inside its own copy of the pinned source
# tree: those Makefiles leave .o files next to their sources with no
# architecture in the name, so building the second architecture in the same
# tree reports "make: Nothing to be done for 'all'" and the recipe would then
# copy the first architecture's .so into the second architecture's folder.
# The copy is seeded without build artifacts and only refreshed when the
# pinned commit changes, so repeated builds stay incremental.
define seed_core
	@src=third_party/$(1); dst=build/src/$(ARCH)/$(1); \
	commit=$$(git -C $$src rev-parse HEAD); \
	if [ ! -f "$$dst/.pinned-commit" ] || [ "$$(cat "$$dst/.pinned-commit")" != "$$commit" ]; then \
		echo "seeding $$dst from $$src at $$commit"; \
		rm -rf "$$dst"; mkdir -p "$$(dirname "$$dst")"; cp -a "$$src" "$$dst"; \
		find "$$dst" \( -name '*.o' -o -name '*.os' -o -name '*.d' -o -name '*_libretro.so' \) -delete; \
		rm -rf "$$dst"/build-roomvr-* "$$dst"/build-*; \
		echo "$$commit" > "$$dst/.pinned-commit"; \
	fi
endef

# Where a core .so goes once built.
core_out = mkdir -p build/libretro/$(ARCH) && cp

.PHONY: core-fceumm
core-fceumm: submodules check-image-arch ## FCEUmm (NES) for ARCH.
	$(call seed_core,libretro-fceumm)
	$(container-run) 'make -C build/src/$(ARCH)/libretro-fceumm -f Makefile.libretro platform=unix -j$(JOBS) && $(core_out) build/src/$(ARCH)/libretro-fceumm/fceumm_libretro.so build/libretro/$(ARCH)/'

.PHONY: core-bsnes
core-bsnes: submodules check-image-arch ## bsnes (SNES) for ARCH.
	$(call seed_core,libretro-bsnes)
	$(container-run) 'make -C build/src/$(ARCH)/libretro-bsnes -f Makefile platform=unix -j$(JOBS) && $(core_out) build/src/$(ARCH)/libretro-bsnes/bsnes_libretro.so build/libretro/$(ARCH)/'

# Angrylion software RSP plus the parallel RSP/RDP paths, the recipe that was
# measured fastest on the headset. ARCH is the container's own uname -m.
.PHONY: core-mupen64plus-next
core-mupen64plus-next: submodules check-image-arch ## Mupen64Plus-Next (N64) for ARCH.
	$(call seed_core,libretro-mupen64plus-nx)
	$(container-run) 'make -C build/src/$(ARCH)/libretro-mupen64plus-nx -f Makefile platform=unix ARCH=$(CONTAINER_UNAME) HAVE_THR_AL=1 HAVE_PARALLEL_RSP=1 HAVE_PARALLEL_RDP=1 LLE=1 -j$(JOBS) && $(core_out) build/src/$(ARCH)/libretro-mupen64plus-nx/mupen64plus_next_libretro.so build/libretro/$(ARCH)/'

.PHONY: core-gambatte
core-gambatte: submodules check-image-arch ## Gambatte (GB/GBC) for ARCH.
	$(call seed_core,libretro-gambatte)
	$(container-run) 'make -C build/src/$(ARCH)/libretro-gambatte -f Makefile.libretro platform=unix -j$(JOBS) && $(core_out) build/src/$(ARCH)/libretro-gambatte/gambatte_libretro.so build/libretro/$(ARCH)/'

# The CMake binary directory is outside the source copy and keyed by
# architecture, so no configuration can survive an architecture switch.
.PHONY: core-mgba
core-mgba: submodules check-image-arch ## mGBA (GB/GBC/GBA) for ARCH.
	$(call seed_core,libretro-mgba)
	$(container-run) 'cmake -S build/src/$(ARCH)/libretro-mgba -B build/cmake/$(ARCH)/mgba \
		-DBUILD_LIBRETRO=ON -DBUILD_QT=OFF -DBUILD_SDL=OFF \
		-DBUILD_TEST=OFF -DBUILD_SUITE=OFF -DBUILD_PERF=OFF -DBUILD_PYTHON=OFF -DBUILD_DOCGEN=OFF \
		-DCMAKE_BUILD_TYPE=Release && \
		cmake --build build/cmake/$(ARCH)/mgba --target mgba_libretro -j$(JOBS) && \
		$(core_out) build/cmake/$(ARCH)/mgba/mgba_libretro.so build/libretro/$(ARCH)/'

.PHONY: cores
cores: core-fceumm core-bsnes core-mupen64plus-next core-gambatte core-mgba ## All five cores for ARCH.

.PHONY: cores-all
cores-all: ## All five cores for both arches.
	$(MAKE) cores ARCH=x86_64
	$(MAKE) cores ARCH=arm64

# One original sample ROM per core, compiled from tools/sample_roms.
.PHONY: roms
roms:
	python3 tools/sample_roms/make_sample_roms.py build/roms

# Callback probe of a prebuilt core, no Godot involved: it drives the Libretro
# API against the generated sample ROM and fails if the core stops feeding
# frames. libretro.h comes from the libretro-common submodule.
.PHONY: test-probe
test-probe: submodules roms ## Drive a built FCEUmm core through the raw Libretro callbacks.
	@test "$(ARCH)" = "$(HOST_ARCH)" || { echo "test-probe dlopens the core with the host compiler: ARCH must equal the host architecture ($(HOST_ARCH))." >&2; exit 1; }
	@test -f build/libretro/$(HOST_ARCH)/fceumm_libretro.so || { \
		echo "Missing build/libretro/$(HOST_ARCH)/fceumm_libretro.so: make core-fceumm" >&2; exit 1; }
	cc -O2 -Wall -Wextra -I third_party/libretro-common/include tests/libretro_probe.c -ldl -o build/libretro_probe
	./build/libretro_probe build/libretro/$(HOST_ARCH)/fceumm_libretro.so build/roms/sample.nes

# Headless Godot tests of the extension and the cores, run against the
# host-arch debug extension and the host-arch cores.
.PHONY: test-godot
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

# Full release matrix: the extension for both arches and both variants, all
# cores for both arches, and the sample ROMs.
.PHONY: release
release: extension-all cores-all roms

# Release binaries ship stripped. The build trees keep their symbols for local
# debugging; the packaged copies go through strip --strip-unneeded inside the
# container of their own architecture. A stripped core is only trusted once it
# has booted: the Libretro callback probe dlopens it and runs its sample ROM,
# and check-stripped-godot additionally loads the stripped host-arch cores
# through the extension's real dlmopen path. `make package` refuses to run the
# per-architecture checks unless SKIP_VERIFY=1, and stops if any of them fails.
DIST_CORE_SOS := $(foreach a,$(ARCHES),$(foreach c,$(CORE_SOS),build/dist/$(a)/$(c)))
# The bare callback probe only boots the cores that need nothing beyond the
# video/audio/input callbacks: measured, mupen64plus_next crashes (it asks for
# the host Vulkan device) and gambatte rejects the ROM (it needs GET_CAN_DUPE),
# both of which the Godot harness does provide. So those two are verified
# through --symbols here and through the extension in check-stripped-godot.
PROBE_BOOTS := fceumm:sample.nes bsnes:sample.sfc mgba:sample.gba

ifeq ($(IN_CONTAINER),1)
run_in_arch = bash -c
else
run_in_arch = $(PODMAN) run --rm --platform linux$(if $(filter arm64,$(1)),/arm64,/amd64) --network none -v $(ROOTDIR):/work -w /work $(IMAGE):$(IMAGE_TAG)-$(1) bash -c
endif

define strip_core_rule
build/dist/$(1)/$(word $(2),$(CORE_SOS)): build/libretro/$(1)/$(word $(2),$(CORE_SOS))
	@mkdir -p $$(@D)
	$$(call run_in_arch,$(1)) 'strip --strip-unneeded -o $$@ $$<'
endef
$(foreach a,$(ARCHES),$(foreach i,$(CORE_INDEX),$(eval $(call strip_core_rule,$(a),$(i)))))

.PHONY: check-stripped
# CHECK_CORES narrows the gate to the cores this invocation actually built: CI
# compiles one core per job, so a job must not demand that the other four exist.
CHECK_CORES ?= $(CORE_IDS)

.PHONY: check-stripped
check-stripped: $(foreach c,$(CHECK_CORES),build/dist/$(ARCH)/$(c)_libretro.so) ## Strip and boot-check the ARCH cores named in CHECK_CORES.
	$(call run_in_arch,$(ARCH)) 'cc -O2 -Wall -Wextra -I third_party/libretro-common/include tests/libretro_probe.c -ldl -o build/libretro_probe'
	@for core in $(CHECK_CORES); do \
		printf '== stripped symbols %s/%s\n' "$(ARCH)" "$$core"; \
		$(call run_in_arch,$(ARCH)) "./build/libretro_probe --symbols build/dist/$(ARCH)/$${core}_libretro.so" || exit 1; \
	done
	@for pair in $(PROBE_BOOTS); do \
		core=$${pair%%:*}; rom=$${pair##*:}; \
		case " $(CHECK_CORES) " in *" $$core "*) ;; *) continue;; esac; \
		printf '== stripped boot %s/%s\n' "$(ARCH)" "$$core"; \
		$(call run_in_arch,$(ARCH)) "./build/libretro_probe build/dist/$(ARCH)/$${core}_libretro.so build/roms/$$rom" || exit 1; \
	done

.PHONY: check-stripped-all
check-stripped-all: ## check-stripped for both architectures.
	$(MAKE) check-stripped ARCH=x86_64
	$(MAKE) check-stripped ARCH=arm64

.PHONY: check-stripped-godot
check-stripped-godot: extension roms ## Load the stripped host-arch cores through the extension itself.
	@test -n "$(GODOT)" || { echo "No godot binary found; set GODOT=/path/to/godot"; exit 1; }
	mkdir -p tests/godot/addons/godot_libretro/.bin/linux/template_debug/$(HOST_ARCH) \
		tests/godot/roms tests/godot/build/libretro/$(HOST_ARCH)
	cp build/godot_libretro/$(HOST_ARCH)/debug/libgodot_libretro.so \
		tests/godot/addons/godot_libretro/.bin/linux/template_debug/$(HOST_ARCH)/
	cp build/roms/sample.* tests/godot/roms/
	cp build/dist/$(HOST_ARCH)/*.so tests/godot/build/libretro/$(HOST_ARCH)/
	@$(GODOT) --headless --xr-mode off --path tests/godot --import || true
	@for t in tests/godot/tests/test_*.gd; do \
		echo "== $$t"; \
		$(GODOT) --headless --xr-mode off --path tests/godot --script res://tests/$$(basename $$t) || exit 1; \
	done

# Zips, from the matrix above. One zip per core per architecture (the core .so,
# its upstream license and its sample ROM, all at the archive root), plus a
# single extension zip holding every architecture and variant in the addons/
# layout a Godot project can extract over its root.
$(EXT_ZIP): $(EXT_LIBS) LICENSE
	@mkdir -p dist
	rm -rf build/package
	for a in $(ARCHES); do \
		for pair in template_debug:debug template_release:release; do \
			dir="build/package/addons/godot_libretro/.bin/linux/$${pair%%:*}/$$a"; \
			mkdir -p "$$dir" && cp "build/godot_libretro/$$a/$${pair##*:}/libgodot_libretro.so" "$$dir/"; \
		done; \
	done
	mkdir -p build/package/addons/godot_libretro && cp LICENSE build/package/addons/godot_libretro/
	$(package-run) 'cd build/package && zip -9 -rX /work/$@ addons'

# Generated rules: one core zip per architecture per core.
define core_zip_rule
dist/godot_libretro-linux-$(1)-$(word $(2),$(CORE_IDS))-$(VERSION).zip: \
		build/dist/$(1)/$(word $(2),$(CORE_SOS)) \
		LICENSES/LICENSE_$(word $(2),$(CORE_IDS)) \
		build/roms/$(word $(2),$(CORE_ROMS))
	@mkdir -p dist
	$$(package-run) 'zip -9 -j -X /work/$$@ $$^'
endef
$(foreach a,$(ARCHES),$(foreach i,$(CORE_INDEX),$(eval $(call core_zip_rule,$(a),$(i)))))

$(MANIFEST): $(EXT_ZIP) $(CORE_ZIPS)
	$(package-run) 'cd dist && md5sum *.zip > manifest.md5'

.PHONY: package
# The verification gate runs for both architectures (the arm64 one under qemu)
# plus the extension harness on the host architecture. CI already ran
# check-stripped inside each architecture job, so it passes SKIP_VERIFY=1 to the
# packaging job instead of nesting a container inside a container.
PACKAGE_VERIFY := $(if $(filter 1,$(SKIP_VERIFY)),,check-stripped-all check-stripped-godot)

package: $(PACKAGE_VERIFY) $(EXT_ZIP) $(CORE_ZIPS) $(MANIFEST) ## Build every release zip from the matrix.

.PHONY: clean
clean:
	rm -rf build dist
