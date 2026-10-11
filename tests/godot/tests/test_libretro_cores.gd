extends SceneTree


const CORES := {
	"fceumm": "res://build/libretro/%s/fceumm_libretro.so",
	"bsnes": "res://build/libretro/%s/bsnes_libretro.so",
	"mupen64plus_next": "res://build/libretro/%s/mupen64plus_next_libretro.so",
	"gambatte": "res://build/libretro/%s/gambatte_libretro.so",
	"mgba": "res://build/libretro/%s/mgba_libretro.so",
}


const SAMPLE_ROMS := {
	"fceumm": "res://roms/sample.nes",
	"bsnes": "res://roms/sample.sfc",
	"mupen64plus_next": "res://roms/sample.v64",
	"gambatte": "res://roms/sample.gbc",
	"mgba": "res://roms/sample.gba",
}


const EXPECTED_NAMES := {
	"fceumm": "FCEUmm",
	"bsnes": "bsnes",
	"mupen64plus_next": "Mupen64Plus-Next",
	"gambatte": "Gambatte",
	"mgba": "mGBA",
}


const EXPECTED_EXTENSIONS := {
	"fceumm": ["nes"],
	"bsnes": ["sfc", "smc"],
	"mupen64plus_next": ["n64", "v64", "z64"],
	"gambatte": ["gb", "gbc"],
	"mgba": ["gba"],
}


func _initialize() -> void:
	call_deferred("_run_test")


func _run_test() -> void:
	var failures: Array[String] = []
	var arch := Engine.get_architecture_name()
	for core_id in CORES:
		var session := LibretroSession.new()
		session.set_save_directory(ProjectSettings.globalize_path("res://build/core_info_saves"))
		var core_path := ProjectSettings.globalize_path(CORES[core_id] % arch)
		if not session.load_core(core_path):
			failures.append("%s: failed to load: %s" % [core_id, session.get_last_error()])
			
			continue
		var core_name := session.get_core_name()
		if core_name != EXPECTED_NAMES[core_id]:
			failures.append("%s: expected core name %s, got %s" % [core_id, EXPECTED_NAMES[core_id], core_name])
		var extensions: String = session.get_core_extensions()
		var expected: Array = EXPECTED_EXTENSIONS[core_id]
		var ext_list: PackedStringArray = extensions.split("|")
		for ext in expected:
			if not ext_list.has(ext):
				failures.append("%s: expected rom extension %s in %s" % [core_id, ext, extensions])
		if core_id == "mupen64plus_next":
			# Software RDP so the core runs headless without a GL context.
			session.set_core_option("mupen64plus-rdp-plugin", "angrylion")
		var rom_path := ProjectSettings.globalize_path(SAMPLE_ROMS[core_id])
		if not session.load_game(rom_path):
			failures.append("%s: failed to load %s: %s" % [core_id, rom_path, session.get_last_error()])
			
			continue
		# mupen64plus_next CPU runs in a coroutine that yields back to the
		# frontend inside vi_vertical_interrupt_event -> new_vi() ->
		# retro_return(); the sample boots the VI with a full NTSC register
		# set so each vertical blank dispatches one video frame.
		var video_frames := 0
		var audio_frames := 0
		var start_time := Time.get_ticks_usec()
		for frame_index in range(120):
			session.run_frame()
			if session.get_video_frames_received() == frame_index + 1:
				video_frames = frame_index + 1
			audio_frames += session.get_audio_buffer().size()
		var elapsed_ms := float(Time.get_ticks_usec() - start_time) / 1000.0
		print("%s: %s %s, %s, sample game 120 frames: video=%d audio=%d in %.0f ms" % [
			core_id, core_name, session.get_core_version(), extensions,
			video_frames, audio_frames, elapsed_ms])
		if video_frames < 120:
			failures.append("%s: expected 120 video frames from the sample game, got %d" % [core_id, video_frames])
	if failures.is_empty():
		print("All Libretro cores loaded and ran their sample game: %s" % ", ".join(CORES.keys()))
		quit(0)
	else:
		for failure in failures:
			push_error(failure)
		quit(1)
