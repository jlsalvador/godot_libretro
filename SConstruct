#!/usr/bin/env python

import os

godot_cpp_dir = os.environ.get("GODOT_CPP_DIR", "third_party/godot-cpp")

# An external SDK toolchain can replace the host compiler, so the extension
# builds without a container. TOOLCHAIN is the root of the toolchain and
# TOOLCHAIN_TRIPLE the prefix of its tools (e.g. the Bootlin tarballs, whose
# sysroot decides the glibc baseline the artifacts require). godot-cpp imports
# a preconfigured `env` when the top-level SConstruct exports one, which is what
# puts the toolchain in place before its compiler detection runs: setting the
# tools afterwards would leave the flags chosen for the host compiler.
toolchain = os.environ.get("TOOLCHAIN", "")
toolchain_triple = os.environ.get("TOOLCHAIN_TRIPLE", "")
if toolchain:
	def toolchain_tool(name):
		return os.path.join(toolchain, "bin", "%s-%s" % (toolchain_triple, name))

	env = Environment(tools=["default"])
	env["CC"] = toolchain_tool("gcc")
	env["CXX"] = toolchain_tool("g++")
	env["LINK"] = env["CXX"]
	env["SHLINK"] = env["CXX"]
	env["AR"] = toolchain_tool("ar")
	env["AS"] = toolchain_tool("as")
	env["NM"] = toolchain_tool("nm")
	env["RANLIB"] = toolchain_tool("ranlib")
	env["STRIP"] = toolchain_tool("strip")
	Export("env")

env = SConscript(os.path.join(godot_cpp_dir, "SConstruct"))

# A cross toolchain's sysroot does not carry the Vulkan headers, and the host
# include directory must not leak into a cross build (its glibc headers would
# shadow the target's). The caller points this at a directory holding only the
# Vulkan header folders (vulkan/ and vk_video/).
vulkan_include = os.environ.get("VULKAN_INCLUDE", "")
if vulkan_include:
	env.Append(CPPPATH=[vulkan_include])

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
# command line and file contents, and two toolchains that pass the very same
# command line (the native container of one architecture and a cross compiler)
# would otherwise share objects, so an x86-64 object could be reused for the
# arm64 library. duplicate=0 keeps src/ clean.
obj_dir = "build/obj/%s/%s" % (env["arch"], variant)
env.VariantDir(obj_dir, "src", duplicate=0)
sources = [os.path.join(obj_dir, node.name) for node in Glob("src/*.cpp")]

library = env.SharedLibrary(
	"%s/libgodot_libretro%s" % (output_dir, env["SHLIBSUFFIX"]),
	source=sources,
)
env.NoCache(library)
Default(library)
