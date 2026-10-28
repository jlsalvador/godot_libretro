extends SceneTree


# Drives the test dummy (tests/dummy/dummy_libretro.c) through the
# extension: the same session paths a real core exercises, minus real
# emulation. The dummy requests frame duping and a system-directory BIOS
# fixture lookup, and the assertions below cover those paths plus the save-RAM
# sidecar and save states.

func _initialize() -> void:
	call_deferred("_run_test")


func _run_test() -> void:
	var failures: Array[String] = []
	var arch := Engine.get_architecture_name()
	var core_path := "res://build/libretro/%s/dummy_libretro.so" % arch
	var rom_path := "res://roms/sample.dmy"

	var session := LibretroSession.new()
	session.set_save_directory(ProjectSettings.globalize_path("res://build/dummy_saves"))
	if not session.load_core(ProjectSettings.globalize_path(core_path)):
		push_error("dummy: failed to load: %s" % session.get_last_error())
		quit(1)
		return
	if session.get_core_name() != "Dummy":
		push_error("dummy: expected core name Dummy, got %s" % session.get_core_name())
		quit(1)
		return
	var extensions: String = session.get_core_extensions()
	if not extensions.split("|").has("dmy"):
		push_error("dummy: expected dmy in core extensions, got %s" % extensions)
		quit(1)
		return

	if not session.load_game(ProjectSettings.globalize_path(rom_path)):
		push_error("dummy: failed to load ROM: %s" % session.get_last_error())
		session.stop()
		quit(1)
		return

	var start_time := Time.get_ticks_usec()
	var video_frames := 0
	var audio_frames := 0
	for frame_index in range(120):
		session.run_frame()
		if session.get_video_frames_received() == frame_index + 1:
			video_frames = frame_index + 1
		audio_frames += session.get_audio_buffer().size()
	var elapsed_ms := float(Time.get_ticks_usec() - start_time) / 1000.0
	print("dummy: %s %s, sample game 120 frames: video=%d audio=%d in %.0f ms" % [
		session.get_core_name(), session.get_core_version(),
		video_frames, audio_frames, elapsed_ms])
	if video_frames < 120 and audio_frames < 120:
		failures.append("dummy: expected 120 frames from the sample game, got %d video, %d audio" % [video_frames, audio_frames])
	# Save states: the dummy's state is its counters and its first video
	# byte carries them, so a restored state must reproduce the frame
	# seen thirty runs after the capture.
	var state_size := session.get_save_state_size()
	var state := session.create_save_state()
	if state_size <= 0 or state.size() != state_size:
		failures.append("dummy states: size query and capture disagree (%d vs %d)" % [state_size, state.size()])
	if state_size > 0 and not state.is_empty():
		var first_byte: int = session.get_video_frame()[2]
		for _state_frame in range(30):
			session.run_frame()
		var advanced_byte: int = session.get_video_frame()[2]
		if advanced_byte == first_byte:
			failures.append("dummy states: the frame counter did not advance")
		if not session.restore_save_state(state):
			failures.append("dummy states: restore rejected the state: %s" % session.get_last_error())
		else:
			for _state_frame in range(30):
				session.run_frame()
			if session.get_video_frame()[2] != advanced_byte:
				failures.append("dummy states: the restored state did not reproduce the advanced frame")
		var quirks_seen := false
		for line in session.get_core_log_lines():
			if line == "serialization quirks acknowledged":
				quirks_seen = true
				break
		if not quirks_seen:
			failures.append("dummy states: no serialization quirks acknowledgement in the core log")

	# A core without a loaded game cannot serialize.
	var stateless := LibretroSession.new()
	stateless.set_save_directory(ProjectSettings.globalize_path("res://build/stateless_saves"))
	if stateless.load_core(ProjectSettings.globalize_path(core_path)):
		if not stateless.create_save_state().is_empty():
			failures.append("dummy states: create without a game must fail")
		if stateless.restore_save_state(state):
			failures.append("dummy states: restore without a game must fail")
	else:
		failures.append("dummy states: failed to load: %s" % stateless.get_last_error())
	stateless.stop()
	# The dummy keeps a counter at the start of its SAVE_RAM, so the
	# sidecar must carry written data after the run; the path is read
	# before stop, which clears it.
	var save_path := session.get_save_ram_path()
	session.stop()
	var save_file := FileAccess.open(save_path, FileAccess.READ)
	if save_file == null:
		failures.append("dummy: no save file at %s" % save_path)
	else:
		var save_data := save_file.get_buffer(save_file.get_length())
		save_file.close()
		var wrote_data := false
		for save_byte in save_data:
			if save_byte != 0:
				wrote_data = true
				break
		if not wrote_data:
			failures.append("dummy: save file has no written data")

	# BIOS lookup: the dummy must find the fixture in the session's system
	# directory and report it through its log interface.
	var bios_session := LibretroSession.new()
	var bios_dir := ProjectSettings.globalize_path("res://bios")
	DirAccess.make_dir_recursive_absolute(bios_dir)
	if not _write_bios_fixture(bios_dir.path_join("sample.bios")):
		failures.append("dummy bios: could not write the BIOS fixture")
	bios_session.set_system_directory(bios_dir)
	if not bios_session.load_core(ProjectSettings.globalize_path(core_path)):
		failures.append("dummy bios: failed to load: %s" % bios_session.get_last_error())
	else:
		if not bios_session.load_game(ProjectSettings.globalize_path(rom_path)):
			failures.append("dummy bios: failed to load ROM: %s" % bios_session.get_last_error())
		else:
			for _bios_frame in range(120):
				bios_session.run_frame()
			if bios_session.get_video_frames_received() != 120:
				failures.append("dummy bios: expected 120 video frames, got %d" % [bios_session.get_video_frames_received()])
			var bios_found := false
			for line in bios_session.get_core_log_lines():
				if line.begins_with("found BIOS fixture in system directory"):
					bios_found = true
					break
			if not bios_found:
				failures.append("dummy bios: no BIOS fixture found in the system directory")
		bios_session.stop()

	# Large ROMs: the dummy declares need_fullpath, so it gets the path only
	# and a 67 MiB ROM must still reach it; the 64 MiB content cap applies to
	# cores that do not declare it and never becomes a load error by itself.
	var big_path := ProjectSettings.globalize_path("res://build/big_sparse.img")
	var big_file := FileAccess.open(big_path, FileAccess.WRITE)
	if big_file == null:
		failures.append("dummy large: could not create the large ROM file")
	else:
		big_file.seek(67 * 1024 * 1024 - 1)
		big_file.store_8(0)
		big_file.close()
		var big_session := LibretroSession.new()
		big_session.set_save_directory(ProjectSettings.globalize_path("res://build/core_load_saves"))
		if not big_session.load_core(ProjectSettings.globalize_path(core_path)):
			failures.append("dummy large: failed to load: %s" % big_session.get_last_error())
		elif not big_session.load_game(big_path):
			failures.append("dummy large: the path-only load failed: %s" % big_session.get_last_error())
		big_session.stop()

	if failures.is_empty():
		print("Dummy core loaded and ran its sample game through the extension.")
		quit(0)
	else:
		for failure in failures:
			push_error(failure)
		quit(1)


# The fixture is a test stand-in for a BIOS file: the dummy only stats it.
func _write_bios_fixture(path: String) -> bool:
	var file := FileAccess.open(path, FileAccess.WRITE)
	if file == null:
		return false
	file.store_8(0x00)
	file.store_8(0x08)
	file.store_8(0x3C)
	file.store_8(0x3F)
	file.close()
	return true
