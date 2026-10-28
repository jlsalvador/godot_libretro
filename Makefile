# Build, verification and packaging of godot_libretro.
#
# Dependency model. Every artifact is a real file with exactly one rule, and
# the architecture is part of its path, so a single make invocation sees the
# whole graph and nothing is built by two rules or by recursive sub-makes:
#
#   build/godot_libretro/<arch>/<debug|release>/libgodot_libretro.so  extension
#   build/dummy/<arch>/dummy_libretro.so                              test core
#   build/probe-host/libretro_probe, build/roms/sample.dmy           test bits
#   dist/*.zip                                                        packages
#
# The extension rule always runs its (incremental) inner build, but only
# replaces the output when its content changed, so everything downstream is
# rebuilt only when a binary really changed. The ARCH variable only picks
# which files the convenience targets (extension, test-godot...) ask for.
#
# Outer parallelism (make -j) is correct, but the inner builds already use
# -j$(JOBS), so `make -j` oversubscribes the CPU: prefer plain `make`.

MAKEFLAGS += --no-builtin-rules
.SUFFIXES:
.DELETE_ON_ERROR:
.SECONDEXPANSION:

ROOTDIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))

# IMAGE, IMAGE_TAG, PODMAN, MOUNT_OPTS, TARGET, ARCH, JOBS, VERSION, GODOT
# and SKIP_VERIFY take an exported environment value over the defaults
# below, so pass them on the make command line.
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

# Debug variants keep full DWARF (-g2 from godot-cpp) so a crash on the
# headset resolves to file and line; the release variant links with -s.
# Measured on the x86_64 addon: 522 KB stripped release, 10.8 MB with DWARF,
# 1.1 MB with the symbol table only.
DEBUG_SYMBOLS_debug := yes
DEBUG_SYMBOLS_release := no
ext_symbols = $(or $(DEBUG_SYMBOLS),$(DEBUG_SYMBOLS_$(1)))

# SKIP_VERIFY=1 drops the test-godot gate from the package: the Godot
# harness is expensive to run, and CI runs it in its own job.
SKIP_VERIFY ?=
SKIP_GATES := $(filter 1,$(SKIP_VERIFY))

# PREBUILT=1 means the extension libraries already exist, e.g. unpacked CI
# artifacts built natively per architecture, and no rule rebuilds them (a
# foreign architecture cannot be built in the current container anyway);
# the files are used as they are. PREBUILT defaults to SKIP_VERIFY: CI
# passes both together.
PREBUILT ?= $(SKIP_VERIFY)
# One pinned Debian 12 (GCC 12) toolchain image per architecture, and every
# build runs natively inside the image of its own architecture. Two reasons,
# both measured: host toolchain drift changes the artifacts, and
# cross-compiling from the host does not work for this stack (godot-cpp's
# SConstruct builds an explicit Environment that discards CC/CXX overrides).
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

# --- Bootlin toolchains: the extension build without a container.
#
# The tarballs are relocatable and carry the target glibc, so the sysroot of
# the toolchain is what fixes the baseline the artifacts require (2.35 here).
# They live under build/ (wiped by make clean) and are pinned by the sha256
# below instead of the checksum file next to them, which comes from the same
# server as the tarball itself.
BOOTLIN_VERSION := 2022.08-1
BOOTLIN_ROOT := build/toolchains
BOOTLIN_BASE := https://toolchains.bootlin.com/downloads/releases/toolchains
# Directory name, tool prefix and pinned sha256 per architecture, keyed by the
# architecture names above (arm64 is aarch64 for Bootlin). Bumping
# BOOTLIN_VERSION means updating the four of each architecture.
bootlin_url_arm64 := aarch64
bootlin_dir_arm64 := aarch64--glibc--stable-$(BOOTLIN_VERSION)
bootlin_triple_arm64 := aarch64-buildroot-linux-gnu
bootlin_sha_arm64 := 844df3c99508030ee9cb1152cb182500bb9816ff01968f2e18591d51d766c9e7
bootlin_url_x86_64 := x86-64
bootlin_dir_x86_64 := x86-64--glibc--stable-$(BOOTLIN_VERSION)
bootlin_triple_x86_64 := x86_64-buildroot-linux-gnu
bootlin_sha_x86_64 := 861c1e8ad0a66e4c28e7a1f8319d68080ab0ff8d16a765e65540f1957203a190
bootlin_dir = $(bootlin_dir_$(1))
bootlin_triple = $(bootlin_triple_$(1))
# Nested $(call) does not see the outer parameters in GNU make (the inner call
# rebinds $(1)), so these expand the per-architecture variables directly.
bootlin_ready = $(BOOTLIN_ROOT)/$(bootlin_dir_$(1))/.verified
BOOTLIN_MARKERS := $(foreach a,$(ARCHES),$(call bootlin_ready,$(a)))
# A cross toolchain has no Vulkan headers, and adding the host /usr/include
# would shadow the target's glibc headers: only the Vulkan header folders
# (vulkan/ and vk_video/) are exposed.
BOOTLIN_VULKAN := $(BOOTLIN_ROOT)/vulkan-include

# libretro.h (and libretro_vulkan.h) come from the pinned libretro-common
# submodule: the single header source of the extension build, the C callback
# probe and the test dummy.
LIBRETRO_H := third_party/libretro-common/include/libretro.h

HOST_PROBE := build/probe-host/libretro_probe
DUMMY := build/dummy/$(HOST_ARCH)/dummy_libretro.so
PROBE_ROM := build/roms/sample.dmy
HOST_EXT_DEBUG := build/godot_libretro/$(HOST_ARCH)/debug/libgodot_libretro.so
GODOT_PROJECT := tests/godot
ADDON_DIR := tests/godot/addons/godot_libretro
# The addon descriptor ships in the zip, so editing it must rebuild the zip.
ADDON_GDEXTENSION := $(ADDON_DIR)/godot_libretro.gdextension

CHECK_IMAGE_TARGETS := $(addprefix check-image-,$(ARCHES))
TOOLCHAIN_TARGETS := $(addprefix toolchain-image-,$(ARCHES))

.PHONY: FORCE all help release package clean \
	submodules toolchain-image toolchain-images $(TOOLCHAIN_TARGETS) \
	check-image-arch $(CHECK_IMAGE_TARGETS) \
	extension extension-variants extension-all \
	bootlin-toolchains bootlin-extension bootlin-extension-all dummy \
	test-probe test-godot
FORCE:

##@ General

all: extension-all ## Build the extension for both architectures and variants.
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

submodules: ## Initialize submodules (no-op inside a container).
ifeq ($(IN_CONTAINER),1)
	@:
else
	git -C $(ROOTDIR) submodule update --init
endif

# --- GDExtension: stem is <arch>/<variant>, variant debug or release.
EXT_LIBS := $(foreach a,$(ARCHES),$(foreach v,debug release,build/godot_libretro/$(a)/$(v)/libgodot_libretro.so))

# Files of the matrix.
DIST_ZIP := dist/godot_libretro-linux-extension-$(VERSION).zip

# The extension paths are <arch>/<variant>; these recover the parts (used
# in recipes).
stem_arch = $(firstword $(subst /, ,$*))
stem_variant = $(lastword $(subst /, ,$*))

# The extension is built by godot-cpp's SConstruct (objects per architecture
# and variant, see SConstruct), inside the toolchain container of its own
# architecture.
ifneq ($(PREBUILT),1)
$(EXT_LIBS): build/godot_libretro/%/libgodot_libretro.so: FORCE | submodules check-image-$$(stem_arch)
	$(call run_in,$(stem_arch)) 'scons -j$(JOBS) target=template_$(stem_variant) arch=$(stem_arch) api_version=4.7 debug_symbols=$(call ext_symbols,$(stem_variant))'
endif

# An unknown TARGET only fails here (no rule for its file), not for other targets.
extension: build/godot_libretro/$(ARCH)/$(patsubst template_%,%,$(TARGET))/libgodot_libretro.so ## Build the GDExtension for ARCH/TARGET.
extension-variants: $(foreach v,debug release,build/godot_libretro/$(ARCH)/$(v)/libgodot_libretro.so) ## Extension for ARCH, debug and release.
extension-all: $(EXT_LIBS) ## Extension for both arches, debug and release.

# --- Bootlin builds: nothing here starts a container, so the compilers and the
# Vulkan headers come from the tarballs above and the host.
#
# $(1) = architecture, $(2) = variant (debug or release). One scons run per
# architecture and variant; the toolchain of the target architecture is picked
# by name, so a single line commands one build.
bootlin_compile = echo '== $(1)/$(2)'; TOOLCHAIN='$(ROOTDIR)/$(BOOTLIN_ROOT)/$(bootlin_dir_$(1))' TOOLCHAIN_TRIPLE='$(bootlin_triple_$(1))' VULKAN_INCLUDE='$(ROOTDIR)/$(BOOTLIN_VULKAN)' scons -j$(JOBS) target=template_$(2) arch=$(1) api_version=4.7 debug_symbols=$(call ext_symbols,$(2));

# $(1) = toolchain directory, $(2) = architecture of its URL, $(3) = sha256.
define bootlin_fetch
set -e; mkdir -p '$(ROOTDIR)/$(BOOTLIN_ROOT)/download'; \
	dir='$(1)'; cd '$(ROOTDIR)/$(BOOTLIN_ROOT)/download'; \
	curl -fsSL -o "$$dir.tar.bz2" '$(BOOTLIN_BASE)/$(2)/tarballs/'"$$dir.tar.bz2"; \
	printf '%s  %s\n' '$(3)' "$$dir.tar.bz2" | sha256sum -c -; \
	tar -xjf "$$dir.tar.bz2" -C ..; \
	touch '$(ROOTDIR)/$(BOOTLIN_ROOT)/'"$$dir/.verified"
endef

$(call bootlin_ready,arm64):
	@$(call bootlin_fetch,$(bootlin_dir_arm64),$(bootlin_url_arm64),$(bootlin_sha_arm64))

$(call bootlin_ready,x86_64):
	@$(call bootlin_fetch,$(bootlin_dir_x86_64),$(bootlin_url_x86_64),$(bootlin_sha_x86_64))

# The Vulkan headers are the only host headers a cross build needs (the
# extension never links libvulkan, it opens it at runtime), and the toolchain's
# sysroot does not carry them. Only those two folders are exposed: adding the
# host /usr/include would shadow the target's glibc headers.
$(BOOTLIN_VULKAN)/.headers:
	@set -e; for dir in vulkan vk_video; do \
		test -d /usr/include/$$dir || { \
			echo "Missing /usr/include/$$dir: install libvulkan-dev (only its headers are needed)." >&2; exit 1; }; \
	done
	@mkdir -p $(@D)
	ln -sfn /usr/include/vulkan $(@D)/vulkan
	ln -sfn /usr/include/vk_video $(@D)/vk_video
	touch $@

bootlin-toolchains: $(BOOTLIN_MARKERS) ## Download and verify the pinned cross toolchains.
bootlin-extension: $(call bootlin_ready,$(ARCH)) $(BOOTLIN_VULKAN)/.headers ## Build ARCH/TARGET with the Bootlin toolchain, no container.
	@set -e; $(call bootlin_compile,$(ARCH),$(patsubst template_%,%,$(TARGET)))
bootlin-extension-all: $(BOOTLIN_MARKERS) $(BOOTLIN_VULKAN)/.headers ## Build both arches and variants with the Bootlin toolchains.
	@set -e; $(foreach a,$(ARCHES),$(foreach v,debug release,$(call bootlin_compile,$(a),$(v))))

# --- Test bits: the dummy core, the C callback probe and the placeholder ROM.
# The dummy (tests/dummy) is built against the pinned libretro.h inside the
# toolchain container of the host architecture, like the extension.
$(DUMMY): tests/dummy/dummy_libretro.c $(LIBRETRO_H) | check-image-$(HOST_ARCH)
	@mkdir -p $(@D)
	$(call run_in,$(HOST_ARCH)) 'cc -O2 -Wall -Wextra -Wno-unused-function -fPIC -shared -I third_party/libretro-common/include tests/dummy/dummy_libretro.c -o $@'

# The callback probe of the dummy core, no Godot involved: it drives the
# Libretro API against the placeholder ROM and fails if the core stops
# feeding frames.
$(HOST_PROBE): tests/libretro_probe.c $(LIBRETRO_H)
	@mkdir -p $(@D)
	cc -O2 -Wall -Wextra -I third_party/libretro-common/include $< -ldl -o $@

# The placeholder ROM: the dummy reads game.path and accepts any non-empty
# file, so the tests need no real ROM content.
$(PROBE_ROM): FORCE
	@mkdir -p $(@D)
	@printf 'dummy rom' > $@

dummy: $(DUMMY) ## Build the test dummy core for the host architecture.
test-probe: $(HOST_PROBE) $(DUMMY) $(PROBE_ROM) ## Build the dummy and drive it through the raw Libretro callbacks.
	./$(HOST_PROBE) $(DUMMY) $(PROBE_ROM)

# Headless Godot tests of the extension, run against the host-arch debug
# extension and the dummy core. Both targets stage their files in the same
# Godot project, so the lock keeps them apart.
# The harness is a game-mode consumer: it stages the addon and writes
# .godot/extension_list.cfg, the list the engine's startup load reads,
# so no editor pass runs. An editor --import queues a doc regeneration
# that 4.7 flushes at teardown, on a freed EditorHelp when the worker
# thread lands late; the flush timing is machine-dependent, and the
# test never needs the editor's other output.
define godot_recipe
@test -n "$(GODOT)" || { echo "No godot binary found; set GODOT=/path/to/godot"; exit 1; }
@mkdir -p build
@( flock 9 || exit 1; \
	rm -f $(GODOT_PROJECT)/build/libretro/$(HOST_ARCH)/*.so; \
	rm -rf $(GODOT_PROJECT)/.godot; \
	mkdir -p $(GODOT_PROJECT)/addons/godot_libretro/.bin/linux/template_debug/$(HOST_ARCH) \
		$(GODOT_PROJECT)/.godot $(GODOT_PROJECT)/roms $(GODOT_PROJECT)/build/libretro/$(HOST_ARCH) || exit 1; \
	cp $(HOST_EXT_DEBUG) $(GODOT_PROJECT)/addons/godot_libretro/.bin/linux/template_debug/$(HOST_ARCH)/ || exit 1; \
	printf 'dummy rom' > $(GODOT_PROJECT)/roms/sample.dmy || exit 1; \
	cp $(DUMMY) $(GODOT_PROJECT)/build/libretro/$(HOST_ARCH)/ || exit 1; \
	printf 'res://addons/godot_libretro/godot_libretro.gdextension\n' > $(GODOT_PROJECT)/.godot/extension_list.cfg || exit 1; \
	for t in $(GODOT_PROJECT)/tests/test_*.gd; do \
		echo "== $$t"; \
		"$(GODOT)" --headless --xr-mode off --path $(GODOT_PROJECT) --script res://tests/$$(basename $$t) || exit 1; \
	done ) 9>build/godot-project.lock
endef

test-godot: $(HOST_EXT_DEBUG) $(DUMMY) $(PROBE_ROM) ## Run the headless Godot tests (extension + dummy core).
	$(godot_recipe)

##@ Release

# Full release matrix: the extension for both arches and both variants.
release: extension-all ## Build the full release matrix (no packaging).

##@ Packaging

# A single extension zip holding every architecture and variant in the
# addons/ layout a Godot project can extract over its root. A zip is removed
# before rebuilding: zip would otherwise update the old archive and keep
# stale entries.
$(DIST_ZIP): $(EXT_LIBS) LICENSE $(ADDON_GDEXTENSION)
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
	cp $(ADDON_GDEXTENSION) build/package/addons/godot_libretro/
	@for f in godot_libretro.gdextension LICENSE \
		.bin/linux/template_debug/arm64/libgodot_libretro.so \
		.bin/linux/template_debug/x86_64/libgodot_libretro.so \
		.bin/linux/template_release/arm64/libgodot_libretro.so \
		.bin/linux/template_release/x86_64/libgodot_libretro.so; do \
		test -f build/package/addons/godot_libretro/$$f || { echo "extension package is missing $$f" >&2; exit 1; }; \
	done
	$(package_run) 'cd build/package && zip -9 -rX ../../$@ addons'

# The Godot harness (a host-architecture test against the dummy core) is an
# order-only gate: the package is not built unless it passed, and it does
# not force the zip to be rewritten. SKIP_VERIFY=1 skips it.
PACKAGE_GATE := $(if $(SKIP_GATES),,test-godot)

package: $(DIST_ZIP) | $(PACKAGE_GATE) ## Verify, then the dist/ zip.

clean: ## Remove build/ and dist/.
	rm -rf build dist
