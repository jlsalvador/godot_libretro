extends SceneTree


func _initialize() -> void:
	call_deferred("_run_test")


func _run_test() -> void:
	var session := LibretroSession.new()
	var core_path := ProjectSettings.globalize_path("res://build/libretro/%s/dummy_libretro.so" % Engine.get_architecture_name())
	var rom_path := ProjectSettings.globalize_path("res://roms/sample.dmy")
	var save_directory := ProjectSettings.globalize_path("res://build/libretro_session_saves")
	var expected_save_path := save_directory.path_join("dummy_libretro/%s.srm" % rom_path.get_file().get_basename())
	session.set_save_directory(save_directory)
	# The sample ROM counts up in SRAM instead of writing fixed markers, so the
	# run starts from an empty sidecar to make both sessions' counters mean
	# something: written by the first one, restored from disk by the second.
	if FileAccess.file_exists(expected_save_path):
		DirAccess.remove_absolute(expected_save_path)

	if not session.load_core(core_path):
		push_error("Libretro core failed to load: %s" % session.get_last_error())
		quit(1)
		return
	if session.get_save_directory() != save_directory or not DirAccess.dir_exists_absolute(save_directory):
		push_error("Libretro should create and use its configured save directory.")
		session.stop()
		quit(1)
		return
	if not session.load_game(rom_path):
		push_error("Libretro ROM failed to load: %s" % session.get_last_error())
		session.stop()
		quit(1)
		return
	session.set_core_option("roomvr_test_option", "roomvr_test_value")
	if session.get_core_option("roomvr_test_option") != "roomvr_test_value":
		push_error("Core option overrides should round-trip through the session.")
		session.stop()
		quit(1)
		return
	session.set_core_option("roomvr_test_option", "")
	if session.get_core_option("roomvr_test_option") != "":
		push_error("Clearing a core option override should fall back to empty.")
		session.stop()
		quit(1)
		return

	var stream := AudioStreamGenerator.new()
	stream.mix_rate = session.get_audio_sample_rate()
	stream.buffer_length = 0.25
	var audio_player := AudioStreamPlayer.new()
	audio_player.stream = stream
	root.add_child(audio_player)
	audio_player.play()
	var audio_playback := audio_player.get_stream_playback() as AudioStreamGeneratorPlayback
	var audio_frames := 0
	var audio_chunks_pushed := 0
	var texture: ImageTexture
	var start_time := Time.get_ticks_usec()
	for frame_index in range(120):
		session.run_frame()
		var audio := session.get_audio_buffer()
		audio_frames += audio.size()
		if not audio.is_empty() and audio_playback.push_buffer(audio):
			audio_chunks_pushed += 1

		var frame: PackedByteArray = session.get_video_frame()
		var image := Image.create_from_data(session.get_video_width(), session.get_video_height(), false, Image.FORMAT_RGBA8, frame)
		if texture == null:
			texture = ImageTexture.create_from_image(image)
		else:
			texture.update(image)

	var elapsed_seconds := float(Time.get_ticks_usec() - start_time) / 1000000.0
	var expected_size := session.get_video_width() * session.get_video_height() * 4
	var last_frame: PackedByteArray = session.get_video_frame()
	if session.get_video_frames_received() < 120 or last_frame.size() != expected_size or audio_frames == 0 or audio_chunks_pushed == 0:
		push_error("Libretro did not produce complete video and audio output.")
		audio_player.stop()
		session.stop()
		quit(1)
		return
	if texture.get_width() != session.get_video_width() or texture.get_height() != session.get_video_height():
		push_error("The Libretro frame did not convert to a Godot texture.")
		audio_player.stop()
		session.stop()
		quit(1)
		return

	print("Godot LibretroSession passed: %s %s, %dx%d, %d frames, %d RGBA bytes, %d audio frames, %d ms total (%.1f FPS)." % [
		session.get_core_name(),
		session.get_core_version(),
		session.get_video_width(),
		session.get_video_height(),
		session.get_video_frames_received(),
		last_frame.size(),
		audio_frames,
		elapsed_seconds * 1000.0,
		120.0 / elapsed_seconds,
	])
	audio_player.stop()
	audio_player.queue_free()
	audio_playback = null
	texture = null
	stream = null
	session.stop()
	if not FileAccess.file_exists(expected_save_path):
		push_error("Stopping a battery-backed ROM should persist its SRAM file.")
		quit(1)
		return
	var first_counter := _read_save_u16(expected_save_path)
	if first_counter <= 0:
		push_error("The sample ROM counts seconds up in SRAM: the first session should leave a non-zero counter, got %d." % first_counter)
		quit(1)
		return


	if not session.load_core(core_path) or not session.load_game(rom_path):
		push_error("The saved SRAM ROM should be loadable for a second session.")
		session.stop()
		quit(1)
		return
	for frame_index in range(3):
		session.run_frame()
	session.stop()
	var second_counter := _read_save_u16(expected_save_path)
	if second_counter < first_counter or second_counter <= 0:
		push_error("The second session should boot with the first session's SRAM restored: counter %d after %d." % [second_counter, first_counter])
		quit(1)
		return
	quit(0)


## The dummy core keeps a counter at the start of its SAVE_RAM, which is
## the first thing written to the sidecar.
func _read_save_u16(path: String) -> int:
	var file := FileAccess.open(path, FileAccess.READ)
	if file == null or file.get_length() < 2:
		return -1
	file.seek(0)
	var low := file.get_8()
	var high := file.get_8()
	return low | (high << 8)
