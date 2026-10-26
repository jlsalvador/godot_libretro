extends SceneTree


const CORES := {
	"fceumm": "res://build/libretro/%s/fceumm_libretro.so",
	"bsnes": "res://build/libretro/%s/bsnes_libretro.so",
	"mupen64plus_next": "res://build/libretro/%s/mupen64plus_next_libretro.so",
	"gambatte": "res://build/libretro/%s/gambatte_libretro.so",
	"mgba": "res://build/libretro/%s/mgba_libretro.so",
	"pcsx_rearmed": "res://build/libretro/%s/pcsx_rearmed_libretro.so",
	"picodrive": "res://build/libretro/%s/picodrive_libretro.so",
	"genesis_plus_gx": "res://build/libretro/%s/genesis_plus_gx_libretro.so",
	"ffmpeg": "res://build/libretro/%s/ffmpeg_libretro.so",
}


const SAMPLE_ROMS := {
	"fceumm": "res://roms/sample.nes",
	"bsnes": "res://roms/sample.sfc",
	"mupen64plus_next": "res://roms/sample.z64",
	"gambatte": "res://roms/sample.gbc",
	"mgba": "res://roms/sample.gba",
	"pcsx_rearmed": "res://roms/sample.exe",
	"picodrive": "res://roms/sample.bin",
	"genesis_plus_gx": "res://roms/sample.bin",
	"ffmpeg": "res://roms/sample.mkv",
}


const EXPECTED_NAMES := {
	"fceumm": "FCEUmm",
	"bsnes": "bsnes",
	"mupen64plus_next": "Mupen64Plus-Next",
	"gambatte": "Gambatte",
	"mgba": "mGBA",
	"pcsx_rearmed": "PCSX-ReARMed",
	"picodrive": "PicoDrive",
	"genesis_plus_gx": "Genesis Plus GX",
	"ffmpeg": "FFmpeg",
}


const EXPECTED_EXTENSIONS := {
	"fceumm": ["nes"],
	"bsnes": ["sfc", "smc"],
	"mupen64plus_next": ["n64", "v64", "z64"],
	"gambatte": ["gb", "gbc"],
	"mgba": ["gba"],
	"pcsx_rearmed": ["exe"],
	"picodrive": ["md", "bin"],
	"genesis_plus_gx": ["md", "bin"],
	"ffmpeg": ["mkv"],
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
		if core_id == "pcsx_rearmed":
			# Real BIOS files are not shipped; the HLE BIOS boots the sample EXE.
			session.set_core_option("pcsx_rearmed_bios", "HLE")
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
		if video_frames < 120 and audio_frames < 120:
			failures.append("%s: expected 120 frames from the sample game, got %d video, %d audio" % [core_id, video_frames, audio_frames])
		# The sample ROMs keep a battery-backed save in every core but the
		# FFmpeg one (it exposes no save RAM): after the run the session
		# must have written <save dir>/<core file>/sample.srm with written
		# data (the save is flushed when the session stops).
		session.stop()
		if core_id != "ffmpeg":
			var save_path := ProjectSettings.globalize_path("res://build/core_info_saves").path_join(core_path.get_file().get_basename() + "/sample.srm")
			var save_file := FileAccess.open(save_path, FileAccess.READ)
			if save_file == null:
				failures.append("%s: no save file at %s" % [core_id, save_path])
			else:
				var save_data := save_file.get_buffer(save_file.get_length())
				save_file.close()
				var wrote_data := false
				for save_byte in save_data:
					if save_byte != 0:
						wrote_data = true
						break
				if not wrote_data:
					failures.append("%s: save file has no written data" % core_id)
	# BIOS lookup: the core must find a BIOS file in the session's system
	# directory and report it through its log interface. The fixture is a
	# 512 KiB file with the core's BIOS signature (00 08 3c 3f at offset 1);
	# an all-zero body reads as the US region. The HLE option keeps the core
	# from executing the (unbootable) file; only the lookup is under test.
	var bios_session := LibretroSession.new()
	bios_session.set_save_directory(ProjectSettings.globalize_path("res://build/core_info_saves"))
	var bios_dir := ProjectSettings.globalize_path("res://bios")
	DirAccess.make_dir_recursive_absolute(bios_dir)
	_write_fake_bios(bios_dir.path_join("scph5500.bin"))
	bios_session.set_system_directory(bios_dir)
	# HLE keeps the core from executing the unbootable fake file.
	bios_session.set_core_option("pcsx_rearmed_bios", "HLE")
	var bios_core_path := ProjectSettings.globalize_path(CORES["pcsx_rearmed"] % arch)
	if not bios_session.load_core(bios_core_path):
		failures.append("pcsx_rearmed bios: failed to load: %s" % [bios_session.get_last_error()])
	else:
		var bios_rom_path := ProjectSettings.globalize_path(SAMPLE_ROMS["pcsx_rearmed"])
		if not bios_session.load_game(bios_rom_path):
			failures.append("pcsx_rearmed bios: failed to load %s: %s" % [bios_rom_path, bios_session.get_last_error()])
		else:
			for _bios_frame in range(120):
				bios_session.run_frame()
			if bios_session.get_video_frames_received() != 120:
				failures.append("pcsx_rearmed bios: expected 120 video frames, got %d" % [bios_session.get_video_frames_received()])
			var bios_found := false
			for line in bios_session.get_core_log_lines():
				if line.begins_with("found US BIOS file"):
					bios_found = true
					break
			if not bios_found:
				failures.append("pcsx_rearmed bios: no BIOS file found in the system directory")
	# Large ROMs: conformant cores receive a path only (a 67 MiB file
	# must reach the core), and the data-reading cores keep their 64 MiB
	# cap as a load error instead of a giant allocation.
	var big_path := ProjectSettings.globalize_path("res://build/big_sparse.img")
	var big_file := FileAccess.open(big_path, FileAccess.WRITE)
	big_file.seek(67 * 1024 * 1024 - 1)
	big_file.store_8(0)
	big_file.close()
	var big_session := LibretroSession.new()
	big_session.set_save_directory(ProjectSettings.globalize_path("res://build/core_load_saves"))
	var big_core_path := ProjectSettings.globalize_path(CORES["pcsx_rearmed"] % arch)
	if not big_session.load_core(big_core_path):
		failures.append("pcsx_rearmed large: failed to load: %s" % [big_session.get_last_error()])
	else:
		big_session.load_game(big_path)
		if big_session.get_last_error().contains("64 MiB"):
			failures.append("pcsx_rearmed large: the path-only load rejected a large ROM: %s" % [big_session.get_last_error()])
	var cap_session := LibretroSession.new()
	cap_session.set_save_directory(ProjectSettings.globalize_path("res://build/core_load_saves"))
	var cap_core_path := ProjectSettings.globalize_path(CORES["gambatte"] % arch)
	if not cap_session.load_core(cap_core_path):
		failures.append("gambatte cap: failed to load: %s" % [cap_session.get_last_error()])
	else:
		cap_session.load_game(big_path)
		if not cap_session.get_last_error().contains("64 MiB"):
			failures.append("gambatte cap: the data cap did not reject a large ROM: %s" % [cap_session.get_last_error()])
	if failures.is_empty():
		print("All Libretro cores loaded and ran their sample game: %s" % ", ".join(CORES.keys()))
		quit(0)
	else:
		for failure in failures:
			push_error(failure)
		quit(1)

# The pcsx_rearmed core accepts a 512 KiB file whose bytes 1..4 are
# 00 08 3c 3f and infers the region from offset 0x7ff51; an all-zero
# body reads as US. The file is a test fixture, not a BIOS.
func _write_fake_bios(path: String) -> void:
	var data := PackedByteArray()
	data.resize(512 * 1024)
	data[1] = 0x00
	data[2] = 0x08
	data[3] = 0x3c
	data[4] = 0x3f
	var file := FileAccess.open(path, FileAccess.WRITE)
	file.store_buffer(data)
	file.close()
