#!/usr/bin/env python

import os

godot_cpp_dir = os.environ.get("GODOT_CPP_DIR", "third_party/godot-cpp")
env = SConscript(os.path.join(godot_cpp_dir, "SConstruct"))

# libretro.h is the public Libretro API header; the copies that ship with
# the pinned cores are identical, so either include tree works.
env.Append(
	CPPPATH=[
		"src",
		"third_party/libretro-fceumm/src/drivers/libretro/libretro-common/include",
		"third_party/libretro-mupen64plus-nx/libretro-common/include",
	],
	LIBS=["dl"],
)

target = env["target"]
variant = "debug" if target in ["editor", "template_debug"] else "release"
output_dir = "build/godot_libretro/%s/%s" % (env["arch"], variant)
os.makedirs(output_dir, exist_ok=True)

library = env.SharedLibrary(
	"%s/libgodot_libretro%s" % (output_dir, env["SHLIBSUFFIX"]),
	source=Glob("src/*.cpp"),
)
env.NoCache(library)
Default(library)
