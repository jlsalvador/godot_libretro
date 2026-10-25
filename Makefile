# Build, verification and packaging of godot_libretro.
#
# Dependency model. Every artifact is a real file with exactly one rule, and
# the architecture is part of its path, so a single make invocation sees the
# whole graph and nothing is built by two rules or by recursive sub-makes:
#
#   build/libretro[-debug]/<arch>/<core>_libretro.so   core builds
#   build/godot_libretro/<arch>/<debug|release>/...    GDExtension builds
#   build/dist/<arch>/<core>_libretro.so               stripped release cores
#   build/checks/<arch>/{stripped,debug}/<core>.ok     verification stamps
#   dist/*.zip, dist/manifest.md5                      packages
#
# The core and extension rules always run their (incremental) inner build, but
# only replace the output when its content changed, so everything downstream
# is rebuilt only when a binary really changed. The ARCH variable only picks
# which files the convenience targets (cores, core-fceumm, check-stripped...)
# ask for; the *-all targets and `package` cover every architecture.
#
# Outer parallelism (make -j) is correct, but every inner build already uses
# -j$(JOBS), so `make -j` oversubscribes the CPU: prefer plain `make`.

MAKEFLAGS += --no-builtin-rules
.SUFFIXES:
.DELETE_ON_ERROR:
.SECONDEXPANSION:

ROOTDIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))

# IMAGE, IMAGE_TAG, PODMAN, MOUNT_OPTS, TARGET, ARCH, JOBS, VERSION, GODOT,
# DEBUG_SYMBOLS, SKIP_VERIFY, PREBUILT and CHECK_CORES take an exported
# environment value over the defaults below, so pass them on the make command line.
IMAGE ?= godot-libretro-toolchain
IMAGE_TAG ?= 12
PODMAN ?= podman
# Extra options appended to the bind mount of the repository, e.g.
# MOUNT_OPTS=:Z on SELinux hosts (Fedora/RHEL), where the mount is otherwise denied.
MOUNT_OPTS ?=
# Extension variant built by the `extension` convenience target.
TARGET ?= template_debug

# uname -m reports aarch64 on native arm64 Linux; we name it arm64 everywhere
HOST_ARCH := $(patsubst aarch64,arm64,$(shell uname -m))
ARCH ?= $(HOST_ARCH)

# Evaluated once (a recursive ?= would rerun the shell at every expansion).
ifndef JOBS
JOBS := $(shell nproc)
endif
ifndef VERSION
VERSION := $(shell git -C $(ROOTDIR) describe --tags --exact-match 2>/dev/null || git -C $(ROOTDIR) rev-parse --short HEAD 2>/dev/null || echo local)
endif
ifndef GODOT
GODOT := $(shell command -v godot 2>/dev/null)
endif

# Debug variants keep full DWARF (-g2 from godot-cpp) so a crash on the headset
# resolves to file and line; the release variant links with -s. Measured on the
# x86_64 addon: 522 KB stripped release, 10.8 MB with DWARF, 1.1 MB with the
# symbol table only. DEBUG_SYMBOLS=yes|no overrides both variants.
DEBUG_SYMBOLS_debug := yes
DEBUG_SYMBOLS_release := no
ext_symbols = $(or $(DEBUG_SYMBOLS),$(DEBUG_SYMBOLS_$(1)))

# SKIP_VERIFY=1 drops the verification gate from the packages, and implies
# PREBUILT: the core binaries (build/libretro*/) and the extension libraries
# (build/godot_libretro/) already exist, e.g. downloaded CI artifacts built
# natively per architecture, and no rule rebuilds them (a foreign architecture
# cannot be built in the current container anyway). PREBUILT=0 builds them.
SKIP_VERIFY ?=
PREBUILT ?= $(SKIP_VERIFY)
SKIP_GATES := $(filter 1,$(SKIP_VERIFY))

# One pinned Debian 12 (GCC 12) toolchain image per architecture, and every
# build runs natively inside the image of its own architecture. Two reasons,
# both measured: host toolchain drift changes the artifacts (GCC 15 compiles
# the Mupen64Plus-Next core into a ~400x slower binary), and
# cross-compiling from the host
# does not work for this stack (godot-cpp's SConstruct builds an explicit
# Environment that discards CC/CXX overrides, so scons passed -march=armv8-a
# to the x86-64 assembler: "bad value 'armv8-a' for '-march=' switch").
ARCHES := x86_64 arm64
oci_x86_64 := amd64
oci_arm64 := arm64
uname_x86_64 := x86_64
uname_arm64 := aarch64

ifeq ($(filter $(ARCH),$(ARCHES)),)
$(error Unsupported target architecture: $(ARCH))
endif

image_of = $(IMAGE):$(IMAGE_TAG)-$(1)

# IN_CONTAINER=1 runs the recipes in the current shell: CI starts the toolchain
# container itself and calls make inside it, so the Makefile must not nest a
# podman run there. Packaging always uses the host-architecture image: zipping
# is architecture-neutral and the image is the only place guaranteed to carry
# the same zip and md5sum as every other build.
ifeq ($(IN_CONTAINER),1)
run_in = bash -c
check_image = :
else
run_in = $(PODMAN) run --rm --platform linux/$(oci_$(1)) --network none -v $(ROOTDIR):/work$(MOUNT_OPTS) -w /work $(call image_of,$(1)) bash -c
# A wrong-architecture image would build wrong-architecture artifacts without
# any error (the recipes never name a compiler), so refuse before building.
check_image = actual=$$($(PODMAN) run --rm --platform linux/$(oci_$(1)) $(call image_of,$(1)) uname -m); \
	[ "$$actual" = "$(uname_$(1))" ] || { \
		echo "Image $(call image_of,$(1)) runs '$$actual', expected $(uname_$(1))." >&2; \
		echo "Build it first: make toolchain-image-$(1)" >&2; exit 1; }
endif
package_run = $(call run_in,$(HOST_ARCH))

# Libretro cores. Each has its submodule tree, a sample ROM (from
# tools/samples) and, for the make-based ones, the arguments of its
# upstream build. The license file staged in the zips is the first of
# CORE_LICENSE_CANDIDATES present in the pinned tree (none: packaging fails).
# Every core zip also stages SOURCE, naming the pinned tree it was built from.
CORE_IDS := fceumm bsnes mupen64plus_next gambatte mgba pcsx_rearmed picodrive genesis_plus_gx
CORE_SOS := $(addsuffix _libretro.so,$(CORE_IDS))
CORE_LICENSE_CANDIDATES := LICENSE LICENSE.txt COPYING Copying

core_dir_fceumm := libretro-fceumm
core_dir_bsnes := libretro-bsnes
core_dir_mupen64plus_next := libretro-mupen64plus-nx
core_dir_gambatte := libretro-gambatte
core_dir_mgba := libretro-mgba
core_dir_pcsx_rearmed := libretro-pcsx_rearmed
core_dir_picodrive := libretro-picodrive
core_dir_genesis_plus_gx := libretro-genesis_plus_gx

core_rom_fceumm := sample.nes
core_rom_bsnes := sample.sfc
core_rom_mupen64plus_next := sample.z64
core_rom_gambatte := sample.gbc
core_rom_mgba := sample.gba
core_rom_pcsx_rearmed := sample.exe
core_rom_picodrive := sample.bin
core_rom_genesis_plus_gx := sample.bin

core_make_args_fceumm := -f Makefile.libretro platform=unix
core_make_args_bsnes := -f Makefile platform=unix
# Angrylion software RSP plus the parallel RSP/RDP paths, the recipe that was
# measured fastest on the headset. ARCH is the container's own uname -m.
core_make_args_mupen64plus_next = -f Makefile platform=unix ARCH=$(uname_$(1)) HAVE_THR_AL=1 HAVE_PARALLEL_RSP=1 HAVE_PARALLEL_RDP=1 LLE=1
core_make_args_gambatte := -f Makefile.libretro platform=unix
core_make_args_pcsx_rearmed := -f Makefile.libretro platform=unix
core_make_args_picodrive := -f Makefile.libretro platform=unix
core_make_args_genesis_plus_gx := -f Makefile.libretro platform=unix

# The bare callback probe only boots the cores that need nothing beyond the
# video/audio/input callbacks: measured, mupen64plus_next crashes (it asks for
# the host Vulkan device) and gambatte rejects the ROM (it needs GET_CAN_DUPE),
# both of which the Godot harness does provide. So those two are verified
# through --symbols only, and through the extension in check-stripped-godot.
BOOT_CORES := fceumm bsnes mgba pcsx_rearmed picodrive genesis_plus_gx

# CHECK_CORES narrows the gate to the cores this invocation actually built: CI
# compiles one core per job, so a job must not demand that the other four exist.
CHECK_CORES ?= $(CORE_IDS)

# Release and debug variants of the core builds. The debug one is a second copy
# of the pinned tree, built with each core's upstream debug build (DEBUG=1 ->
# -O0 -g; CMake Debug for mGBA) so the debug zips carry DWARF and crashes
# resolve to file and line. The release trees and the stripped dist/ copies are
# untouched.
mode_src_release := build/src
mode_src_debug := build/src-debug
mode_cmake_release := build/cmake
mode_cmake_debug := build/cmake-debug
mode_cmake_type_release := Release
mode_cmake_type_debug := Debug
mode_flags_release :=
mode_flags_debug := DEBUG=1

# Files of the matrix.
EXT_LIBS := $(foreach a,$(ARCHES),$(foreach v,debug release,build/godot_libretro/$(a)/$(v)/libgodot_libretro.so))
CORE_SO_RELEASE := $(foreach a,$(ARCHES),$(foreach c,$(CORE_IDS),build/libretro/$(a)/$(c)_libretro.so))
CORE_SO_DEBUG := $(foreach a,$(ARCHES),$(foreach c,$(CORE_IDS),build/libretro-debug/$(a)/$(c)_libretro.so))
DIST_SOS := $(foreach a,$(ARCHES),$(foreach c,$(CORE_IDS),build/dist/$(a)/$(c)_libretro.so))
PROBES := $(foreach a,$(ARCHES),build/probe/$(a)/libretro_probe)
CHECK_STRIPPED_ALL := $(foreach a,$(ARCHES),$(foreach c,$(CORE_IDS),build/checks/$(a)/stripped/$(c).ok))
CHECK_DEBUG_ALL := $(foreach a,$(ARCHES),$(foreach c,$(CORE_IDS),build/checks/$(a)/debug/$(c).ok))
CORE_ZIPS_RELEASE := $(foreach a,$(ARCHES),$(foreach c,$(CORE_IDS),dist/godot_libretro-linux-$(a)-$(c)-release-$(VERSION).zip))
CORE_ZIPS_DEBUG := $(foreach a,$(ARCHES),$(foreach c,$(CORE_IDS),dist/godot_libretro-linux-$(a)-$(c)-debug-$(VERSION).zip))
EXT_ZIP := dist/godot_libretro-linux-extension-$(VERSION).zip
MANIFEST := dist/manifest.md5

ROMS_STAMP := build/roms/.stamp
LIBRETRO_H := third_party/libretro-common/include/libretro.h
HOST_PROBE := build/probe-host/libretro_probe
HOST_EXT_DEBUG := build/godot_libretro/$(HOST_ARCH)/debug/libgodot_libretro.so
GODOT_PROJECT := tests/godot

CHECK_IMAGE_TARGETS := $(addprefix check-image-,$(ARCHES))
TOOLCHAIN_TARGETS := $(addprefix toolchain-image-,$(ARCHES))

.PHONY: FORCE all help release package clean \
	submodules toolchain-image toolchain-images $(TOOLCHAIN_TARGETS) \
	check-image-arch $(CHECK_IMAGE_TARGETS) \
	extension extension-variants extension-all \
	core-fceumm core-bsnes core-mupen64plus-next core-gambatte core-mgba core-pcsx-rearmed core-picodrive core-genesis-plus-gx \
	core-debug-fceumm core-debug-bsnes core-debug-mupen64plus-next core-debug-gambatte core-debug-mgba core-debug-pcsx-rearmed core-debug-picodrive core-debug-genesis-plus-gx \
	cores cores-all cores-debug cores-debug-all roms \
	test-probe test-godot \
	check-stripped check-stripped-all check-stripped-godot check-debug check-debug-all \
	release-zips debug-zips
FORCE:

# Targets whose stem is "<arch>/<...>/<core>" or "<arch>/<variant>" or
# "<arch>-<core>" tell their parts apart with these (used in recipes and in
# secondary expansion).
stem_arch = $(firstword $(subst /, ,$*))
stem_core = $(lastword $(subst /, ,$*))
zip_arch = $(firstword $(subst -, ,$*))
zip_core = $(word 2,$(subst -, ,$*))

##@ General

all: extension-all cores-all roms ## Build the full matrix: extensions, cores and sample ROMs.
help: ## Display this help.
	@awk 'BEGIN {FS = ":.*##"; printf "\nUsage:\n  make \033[36m<target>\033[0m\n"} /^[a-zA-Z_0-9-]+:.*?##/ { printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2 } /^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) } ' $(MAKEFILE_LIST)

##@ Build

$(TOOLCHAIN_TARGETS): toolchain-image-%:
	$(PODMAN) build --platform linux/$(oci_$*) -t $(call image_of,$*) $(ROOTDIR)/tools

toolchain-image: toolchain-image-$(ARCH) ## Build the toolchain image for ARCH.
toolchain-images: $(TOOLCHAIN_TARGETS) ## Build both toolchain images.

$(CHECK_IMAGE_TARGETS): check-image-%:
	@$(call check_image,$*)

check-image-arch: check-image-$(ARCH) ## Check that the toolchain image for ARCH runs the expected architecture.

# The top level stays single, because the nested gitlink of the
# Mupen64Plus-Next core tree is intentionally left uninitialized; the
# PicoDrive core declares nested submodules of its own and gets a
# recursion scoped to its directory.
submodules: ## Initialize submodules (no-op inside a container).
ifeq ($(IN_CONTAINER),1)
	@:
else
	git -C $(ROOTDIR) submodule update --init
	git -C $(ROOTDIR)/third_party/libretro-picodrive submodule update --init --recursive
endif

# --- GDExtension: stem is <arch>/<variant>, variant debug or release.
ifneq ($(PREBUILT),1)
$(EXT_LIBS): build/godot_libretro/%/libgodot_libretro.so: FORCE | submodules check-image-$$(stem_arch)
	$(call run_in,$(stem_arch)) 'scons -j$(JOBS) target=template_$(stem_core) arch=$(stem_arch) api_version=4.7 debug_symbols=$(call ext_symbols,$(stem_core))'
endif

# An unknown TARGET only fails here (no rule for its file), not for other targets.
extension: build/godot_libretro/$(ARCH)/$(patsubst template_%,%,$(TARGET))/libgodot_libretro.so ## Build the GDExtension for ARCH/TARGET.
extension-variants: $(foreach v,debug release,build/godot_libretro/$(ARCH)/$(v)/libgodot_libretro.so) ## Extension for ARCH, debug and release.
extension-all: $(EXT_LIBS) ## Extension for both arches, debug and release.

# --- Cores: stem is <arch>/<core>. Each architecture builds inside its own copy
# of the pinned source tree: those Makefiles leave .o files next to their
# sources with no architecture in the name, so building the second
# architecture in the same tree reports "make: Nothing to be done for 'all'"
# and the recipe would then copy the first architecture's .so into the second
# architecture's folder. The copy is seeded without build artifacts and only
# refreshed when the pinned commit changes, so repeated builds stay incremental.
# The seeding runs in the shell of the recipe (host or container), the build in
# the toolchain container.
#
# seed_core(core, mode, arch)
seed_core = src=third_party/$(core_dir_$(1)); dst=$(mode_src_$(2))/$(3)/$(core_dir_$(1)); \
	commit=$$(git -C $$src rev-parse HEAD) || exit 1; \
	if [ ! -f "$$dst/.pinned-commit" ] || [ "$$(cat "$$dst/.pinned-commit")" != "$$commit" ]; then \
		echo "seeding $$dst from $$src at $$commit"; \
		rm -rf "$$dst"; mkdir -p "$$(dirname "$$dst")"; cp -a "$$src" "$$dst"; \
		find "$$dst" \( -name '*.o' -o -name '*.os' -o -name '*.d' -o -name '*_libretro.so' \) -delete; \
		rm -rf "$$dst"/build-*; \
		echo "$$commit" > "$$dst/.pinned-commit"; \
	fi

# core_tree/cmake_dir/core_built/core_compile/core_build(core, arch, mode)
core_tree = $(mode_src_$(3))/$(2)/$(core_dir_$(1))
# The CMake binary directory is outside the source copy and keyed by
# architecture, so no configuration can survive an architecture switch.
cmake_dir = $(mode_cmake_$(3))/$(2)/mgba
core_built = $(if $(filter mgba,$(1)),$(call cmake_dir,$(1),$(2),$(3))/mgba_libretro.so,$(call core_tree,$(1),$(2),$(3))/$(1)_libretro.so)
core_compile = $(if $(filter mgba,$(1)),cmake -S $(call core_tree,$(1),$(2),$(3)) -B $(call cmake_dir,$(1),$(2),$(3)) -DBUILD_LIBRETRO=ON -DBUILD_QT=OFF -DBUILD_SDL=OFF -DBUILD_TEST=OFF -DBUILD_SUITE=OFF -DBUILD_PERF=OFF -DBUILD_PYTHON=OFF -DBUILD_DOCGEN=OFF -DCMAKE_BUILD_TYPE=$(mode_cmake_type_$(3)) && cmake --build $(call cmake_dir,$(1),$(2),$(3)) --target mgba_libretro -j$(JOBS),make -C $(call core_tree,$(1),$(2),$(3)) $(call core_make_args_$(1),$(2)) $(mode_flags_$(3)) -j$(JOBS))
# The output is replaced only when its content changed, which keeps its mtime
# (and everything built from it) stable across no-op builds.
core_build = $(call core_compile,$(1),$(2),$(3)) && mkdir -p $(@D) && { cmp -s $(call core_built,$(1),$(2),$(3)) $@ || cp $(call core_built,$(1),$(2),$(3)) $@; }

define core_recipe
@$(call seed_core,$(stem_core),$(MODE),$(stem_arch))
$(call run_in,$(stem_arch)) '$(call core_build,$(stem_core),$(stem_arch),$(MODE))'
endef

ifneq ($(PREBUILT),1)
$(CORE_SO_RELEASE): MODE := release
$(CORE_SO_DEBUG): MODE := debug
$(CORE_SO_RELEASE): build/libretro/%_libretro.so: FORCE | submodules check-image-$$(stem_arch)
	$(core_recipe)
$(CORE_SO_DEBUG): build/libretro-debug/%_libretro.so: FORCE | submodules check-image-$$(stem_arch)
	$(core_recipe)
endif

core-fceumm: build/libretro/$(ARCH)/fceumm_libretro.so ## FCEUmm for ARCH.
core-bsnes: build/libretro/$(ARCH)/bsnes_libretro.so ## bsnes for ARCH.
core-mupen64plus-next: build/libretro/$(ARCH)/mupen64plus_next_libretro.so ## Mupen64Plus-Next for ARCH.
core-gambatte: build/libretro/$(ARCH)/gambatte_libretro.so ## Gambatte for ARCH.
core-mgba: build/libretro/$(ARCH)/mgba_libretro.so ## mGBA for ARCH.
core-pcsx-rearmed: build/libretro/$(ARCH)/pcsx_rearmed_libretro.so ## pcsx_rearmed for ARCH.
core-picodrive: build/libretro/$(ARCH)/picodrive_libretro.so ## PicoDrive for ARCH.
core-genesis-plus-gx: build/libretro/$(ARCH)/genesis_plus_gx_libretro.so ## Genesis Plus GX for ARCH.
cores: $(foreach c,$(CORE_IDS),build/libretro/$(ARCH)/$(c)_libretro.so) ## All cores for ARCH.
cores-all: $(CORE_SO_RELEASE) ## All cores for both arches.

core-debug-fceumm: build/libretro-debug/$(ARCH)/fceumm_libretro.so ## FCEUmm (with debug info) for ARCH.
core-debug-bsnes: build/libretro-debug/$(ARCH)/bsnes_libretro.so ## bsnes (with debug info) for ARCH.
core-debug-mupen64plus-next: build/libretro-debug/$(ARCH)/mupen64plus_next_libretro.so ## Mupen64Plus-Next (with debug info) for ARCH.
core-debug-gambatte: build/libretro-debug/$(ARCH)/gambatte_libretro.so ## Gambatte (with debug info) for ARCH.
core-debug-mgba: build/libretro-debug/$(ARCH)/mgba_libretro.so ## mGBA (with debug info) for ARCH.
core-debug-pcsx-rearmed: build/libretro-debug/$(ARCH)/pcsx_rearmed_libretro.so ## pcsx_rearmed (with debug info) for ARCH.
core-debug-picodrive: build/libretro-debug/$(ARCH)/picodrive_libretro.so ## PicoDrive (with debug info) for ARCH.
core-debug-genesis-plus-gx: build/libretro-debug/$(ARCH)/genesis_plus_gx_libretro.so ## Genesis Plus GX (with debug info) for ARCH.
cores-debug: $(foreach c,$(CORE_IDS),build/libretro-debug/$(ARCH)/$(c)_libretro.so) ## All cores (with debug info) for ARCH.
cores-debug-all: $(CORE_SO_DEBUG) ## All cores for both arches.

# One original sample ROM per core, compiled from tools/samples.
$(ROMS_STAMP): $(wildcard tools/samples/*)
	@mkdir -p $(@D)
	python3 tools/samples/all.py $(@D)
	@touch $@
roms: $(ROMS_STAMP) ## Build the sample ROMs.

##@ Test

# Callback probe of a prebuilt core, no Godot involved: it drives the Libretro
# API against the generated sample ROM and fails if the core stops feeding
# frames. libretro.h comes from the libretro-common submodule. test-probe uses
# a copy built with the host compiler; the stripped-core checks use one built
# inside the toolchain container of each architecture (build/probe/<arch>/).
$(LIBRETRO_H): | submodules ;

$(HOST_PROBE): tests/libretro_probe.c $(LIBRETRO_H)
	@mkdir -p $(@D)
	cc -O2 -Wall -Wextra -I third_party/libretro-common/include $< -ldl -o $@

test-probe: $(HOST_PROBE) build/libretro/$(HOST_ARCH)/fceumm_libretro.so $(ROMS_STAMP) ## Build FCEUmm and drive it through the raw Libretro callbacks.
	./$(HOST_PROBE) build/libretro/$(HOST_ARCH)/fceumm_libretro.so build/roms/sample.nes

# Headless Godot tests of the extension and the cores, run against the
# host-arch debug extension and the host-arch cores. Both godot targets stage
# their files in the same Godot project, so the lock keeps them apart.
define godot_recipe
@test -n "$(GODOT)" || { echo "No godot binary found; set GODOT=/path/to/godot"; exit 1; }
@mkdir -p build
@( flock 9 || exit 1; \
	rm -f $(GODOT_PROJECT)/build/libretro/$(HOST_ARCH)/*.so; \
	mkdir -p $(GODOT_PROJECT)/addons/godot_libretro/.bin/linux/template_debug/$(HOST_ARCH) \
		$(GODOT_PROJECT)/roms $(GODOT_PROJECT)/build/libretro/$(HOST_ARCH) || exit 1; \
	cp $(HOST_EXT_DEBUG) $(GODOT_PROJECT)/addons/godot_libretro/.bin/linux/template_debug/$(HOST_ARCH)/ || exit 1; \
	cp build/roms/sample.* $(GODOT_PROJECT)/roms/ || exit 1; \
	cp $(addprefix $(GODOT_CORES)/,$(CORE_SOS)) $(GODOT_PROJECT)/build/libretro/$(HOST_ARCH)/ || exit 1; \
	"$(GODOT)" --headless --xr-mode off --path $(GODOT_PROJECT) --import || true; \
	for t in $(GODOT_PROJECT)/tests/test_*.gd; do \
		echo "== $$t"; \
		"$(GODOT)" --headless --xr-mode off --path $(GODOT_PROJECT) --script res://tests/$$(basename $$t) || exit 1; \
	done ) 9>build/godot-project.lock
endef

test-godot: GODOT_CORES := build/libretro/$(HOST_ARCH)
test-godot: $(HOST_EXT_DEBUG) $(foreach c,$(CORE_SOS),build/libretro/$(HOST_ARCH)/$(c)) $(ROMS_STAMP) ## Run the headless Godot tests (extension + host-arch cores).
	$(godot_recipe)

##@ Release

# Full release matrix: the extension for both arches and both variants, all
# cores for both arches, and the sample ROMs.
release: extension-all cores-all roms ## Build the full release matrix (no strip or packaging).

# Release binaries ship stripped. The build trees keep their symbols for local
# debugging; the packaged copies go through strip --strip-unneeded inside the
# container of their own architecture. A stripped core is only trusted once it
# has booted: the Libretro callback probe dlopens it and runs its sample ROM,
# and check-stripped-godot additionally loads the stripped host-arch cores
# through the extension's real dlmopen path. `make package` runs these
# checks unless SKIP_VERIFY=1, and stops if any of them fails.
#
# Stem is <arch>/<core>.
$(DIST_SOS): build/dist/%_libretro.so: build/libretro/%_libretro.so | check-image-$$(stem_arch)
	@mkdir -p $(@D)
	$(call run_in,$(stem_arch)) 'strip --strip-unneeded -o $@ $<'

$(PROBES): build/probe/%/libretro_probe: tests/libretro_probe.c $(LIBRETRO_H) | check-image-%
	@mkdir -p $(@D)
	$(call run_in,$*) 'cc -O2 -Wall -Wextra -I third_party/libretro-common/include $< -ldl -o $@'

# Verification stamps. Stem is <arch>/stripped/<core> or <arch>/debug/<core>.
is_boot = $(filter $(stem_core),$(BOOT_CORES))

$(CHECK_STRIPPED_ALL): build/checks/%.ok: build/dist/$$(stem_arch)/$$(stem_core)_libretro.so build/probe/$$(stem_arch)/libretro_probe $(ROMS_STAMP)
	@echo "== stripped symbols $(stem_arch)/$(stem_core)"
	$(call run_in,$(stem_arch)) './build/probe/$(stem_arch)/libretro_probe --symbols build/dist/$(stem_arch)/$(stem_core)_libretro.so'
	$(if $(is_boot),@echo "== stripped boot $(stem_arch)/$(stem_core)")
	$(if $(is_boot),$(call run_in,$(stem_arch)) './build/probe/$(stem_arch)/libretro_probe build/dist/$(stem_arch)/$(stem_core)_libretro.so build/roms/$(core_rom_$(stem_core))')
	@mkdir -p $(@D) && touch $@

$(CHECK_DEBUG_ALL): build/checks/%.ok: build/libretro-debug/$$(stem_arch)/$$(stem_core)_libretro.so
	@echo "== debug info $(stem_arch)/$(stem_core)"
	$(call run_in,$(stem_arch)) 'f=build/libretro-debug/$(stem_arch)/$(stem_core)_libretro.so; readelf -S "$$f" | grep -q "\.symtab" || { echo "$$f: no .symtab" >&2; exit 1; }; readelf -S "$$f" | grep -q "\.debug_" || { echo "$$f: no debug info" >&2; exit 1; }'
	@mkdir -p $(@D) && touch $@

check-stripped: $(foreach c,$(CHECK_CORES),build/checks/$(ARCH)/stripped/$(c).ok) ## Strip and boot-check the ARCH cores named in CHECK_CORES.
check-stripped-all: $(foreach a,$(ARCHES),$(foreach c,$(CHECK_CORES),build/checks/$(a)/stripped/$(c).ok)) ## check-stripped for both architectures.
check-debug: $(foreach c,$(CHECK_CORES),build/checks/$(ARCH)/debug/$(c).ok) ## Verify the ARCH debug cores carry debug info.
check-debug-all: $(foreach a,$(ARCHES),$(foreach c,$(CHECK_CORES),build/checks/$(a)/debug/$(c).ok)) ## check-debug for both architectures.

check-stripped-godot: GODOT_CORES := build/dist/$(HOST_ARCH)
check-stripped-godot: $(HOST_EXT_DEBUG) $(foreach c,$(CORE_SOS),build/dist/$(HOST_ARCH)/$(c)) $(ROMS_STAMP) ## Load the stripped host-arch cores through the extension itself.
	$(godot_recipe)

##@ Packaging

# The first license file present in the pinned tree, staged as LICENSE.
# Stem is the core id.
build/licenses/%/LICENSE: FORCE | submodules
	@mkdir -p $(@D)
	@src=third_party/$(core_dir_$*); \
	for n in $(CORE_LICENSE_CANDIDATES); do \
		if [ -f "$$src/$$n" ]; then cmp -s "$$src/$$n" $@ || cp "$$src/$$n" $@; exit 0; fi; \
	done; \
	echo "No license file among $(CORE_LICENSE_CANDIDATES) in $$src" >&2; exit 1

# The corresponding source of a core, staged as SOURCE: which tree this binary
# was built from and where to get it. The copyleft cores (GPL, MPL)
# require the source to be conveyed with the binary, and the others get
# the same treatment for uniformity; a URL plus the exact commit names it
# unambiguously. Both values are
# read from this repository (the URL from .gitmodules, the commit from the
# gitlink), so the file cannot drift from what a cloner gets. The pinned tree
# is built unmodified (seed_core copies it and only deletes build
# artifacts), so the tree at that commit is the complete corresponding
# source. Like the
# license file, SOURCE is only rewritten when its content changes.
build/licenses/%/SOURCE: FORCE
	@mkdir -p $(@D)
	@dir=third_party/$(core_dir_$*); \
	name=$$(basename "$$dir"); \
	url=$$(git config -f .gitmodules --get "submodule.$$dir.url") || exit 1; \
	sha=$$(git rev-parse "HEAD:$$dir") || exit 1; \
	{ \
		echo "godot_libretro: corresponding source of this core"; \
		echo "================================================"; \
		echo; \
		echo "Core:    $*"; \
		echo "Binary:  $*_libretro.so"; \
		echo; \
		echo "License: the LICENSE file in this archive is a verbatim copy of"; \
		echo "         the license file of the pinned upstream tree below."; \
		echo; \
		echo "Upstream repository:"; \
		echo "    $$url"; \
		echo; \
		echo "Pinned commit:"; \
		echo "    $$sha"; \
		echo; \
		echo "The complete corresponding source of this binary is the upstream"; \
		echo "tree at that commit, used with no source modifications (build"; \
		echo "arguments only) and built with GCC 12 on Debian 12. To obtain it:"; \
		echo; \
		echo "    git clone $$url"; \
		echo "    cd $$name"; \
		echo "    git checkout $$sha"; \
		echo; \
		echo "The same tree is pinned as a submodule of the project that built this"; \
		echo "binary; 'git submodule update --init' (or 'make submodules') checks"; \
		echo "out exactly that commit:"; \
		echo; \
		echo "    $$dir"; \
	} > $@.tmp
	@cmp -s $@.tmp $@ || mv $@.tmp $@; rm -f $@.tmp

# Zips, from the matrix above. One zip per core per architecture and variant
# (the core .so, its license staged as LICENSE, the SOURCE file naming its
# upstream tree, and its sample ROM, all at the archive root; the release one
# stripped, the debug one unstripped), plus a
# single extension zip holding every architecture and variant in the addons/
# layout a Godot project can extract over its root. A zip is removed before
# rebuilding: zip would otherwise update the old archive and keep stale entries.
# Unless SKIP_VERIFY=1, a core zip also requires its verification stamp.
# Stem is <arch>-<core>.
release_gate = $(if $(SKIP_GATES),,build/checks/$(zip_arch)/stripped/$(zip_core).ok)
debug_gate = $(if $(SKIP_GATES),,build/checks/$(zip_arch)/debug/$(zip_core).ok)

$(CORE_ZIPS_RELEASE): dist/godot_libretro-linux-%-release-$(VERSION).zip: build/dist/$$(zip_arch)/$$(zip_core)_libretro.so $(ROMS_STAMP) build/licenses/$$(zip_core)/LICENSE build/licenses/$$(zip_core)/SOURCE $$(release_gate)
	@rm -f $@
	@mkdir -p $(@D)
	$(package_run) 'zip -9 -j -X $@ build/dist/$(zip_arch)/$(zip_core)_libretro.so build/roms/$(core_rom_$(zip_core)) build/licenses/$(zip_core)/LICENSE build/licenses/$(zip_core)/SOURCE'

$(CORE_ZIPS_DEBUG): dist/godot_libretro-linux-%-debug-$(VERSION).zip: build/libretro-debug/$$(zip_arch)/$$(zip_core)_libretro.so $(ROMS_STAMP) build/licenses/$$(zip_core)/LICENSE build/licenses/$$(zip_core)/SOURCE $$(debug_gate)
	@rm -f $@
	@mkdir -p $(@D)
	$(package_run) 'zip -9 -j -X $@ build/libretro-debug/$(zip_arch)/$(zip_core)_libretro.so build/roms/$(core_rom_$(zip_core)) build/licenses/$(zip_core)/LICENSE build/licenses/$(zip_core)/SOURCE'

# The extension libraries are real files: the zip is only rebuilt when one of
# them changed.
$(EXT_ZIP): $(EXT_LIBS) LICENSE
	@rm -f $@
	@mkdir -p $(@D)
	rm -rf build/package/addons
	for a in $(ARCHES); do \
		for v in debug release; do \
			dir=build/package/addons/godot_libretro/.bin/linux/template_$$v/$$a; \
			mkdir -p $$dir && cp build/godot_libretro/$$a/$$v/libgodot_libretro.so $$dir/ || exit 1; \
		done; \
	done
	mkdir -p build/package/addons/godot_libretro
	cp LICENSE build/package/addons/godot_libretro/
	cp tests/godot/addons/godot_libretro/godot_libretro.gdextension build/package/addons/godot_libretro/
	@for f in godot_libretro.gdextension LICENSE \
		.bin/linux/template_debug/arm64/libgodot_libretro.so \
		.bin/linux/template_debug/x86_64/libgodot_libretro.so \
		.bin/linux/template_release/arm64/libgodot_libretro.so \
		.bin/linux/template_release/x86_64/libgodot_libretro.so; do \
		test -f build/package/addons/godot_libretro/$$f || { echo "extension package is missing $$f" >&2; exit 1; }; \
	done
	$(package_run) 'cd build/package && zip -9 -rX ../../$@ addons'

release-zips: $(CORE_ZIPS_RELEASE) ## Release (stripped) core zips, with LICENSE, SOURCE and the sample ROM.
debug-zips: $(CORE_ZIPS_DEBUG) ## Debug (unstripped, with debug info) core zips, with LICENSE, SOURCE and the sample ROM.

# Only this version's zips go in the manifest: older versions pile up in dist/.
# The Godot harness (a host-architecture test of the stripped cores) is an
# order-only gate: the manifest is not written unless it passed, and it does not
# force the manifest to be rewritten. SKIP_VERIFY=1 skips it.
PACKAGE_GATE := $(if $(SKIP_GATES),,check-stripped-godot)

$(MANIFEST): $(EXT_ZIP) $(CORE_ZIPS_RELEASE) $(CORE_ZIPS_DEBUG) | $(PACKAGE_GATE)
	$(package_run) 'cd dist && md5sum *-$(VERSION).zip > manifest.md5'

package: $(MANIFEST) ## Build all release and debug zips from the matrix.

clean: ## Remove build/ and dist/.
	rm -rf build dist
