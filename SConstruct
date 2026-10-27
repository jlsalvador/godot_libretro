#!/usr/bin/env python

import os

godot_cpp_dir = os.environ.get("GODOT_CPP_DIR", "third_party/godot-cpp")
env = SConscript(os.path.join(godot_cpp_dir, "SConstruct"))

# libretro.h (and libretro_vulkan.h) come from our own pinned
# libretro-common submodule: the single header source of the extension,
# the C callback probe and the test dummy.
env.Append(
	CPPPATH=[
		"src",
		"third_party/libretro-common/include",
	],
	LIBS=["dl"],
)

target = env["target"]
variant = "debug" if target in ["editor", "template_debug"] else "release"
output_dir = "build/godot_libretro/%s/%s" % (env["arch"], variant)
os.makedirs(output_dir, exist_ok=True)

# Objects live in a directory keyed by architecture and variant. godot-cpp
# already names its own objects that way; ours would otherwise be plain
# src/*.cpp.os, shared by every build: SCons decides by signature of the
# command line and file contents, and two native toolchain containers (one
# per architecture) pass the very same command line, so an x86-64 object
# would be reused for the arm64 library. duplicate=0 keeps src/ clean.
obj_dir = "build/obj/%s/%s" % (env["arch"], variant)
env.VariantDir(obj_dir, "src", duplicate=0)
sources = [os.path.join(obj_dir, node.name) for node in Glob("src/*.cpp")]

library = env.SharedLibrary(
	"%s/libgodot_libretro%s" % (output_dir, env["SHLIBSUFFIX"]),
	source=sources,
)
env.NoCache(library)
Default(library)
