extends SceneTree


func _initialize() -> void:
	call_deferred("_run_test")


func _run_test() -> void:
	var core_path := ProjectSettings.globalize_path("res://build/libretro/%s/fceumm_libretro.so" % Engine.get_architecture_name())
	var rom_path := ProjectSettings.globalize_path("res://roms/sample.nes")
	var session_a := LibretroSession.new()
	var session_b := LibretroSession.new()
	session_a.set_save_directory(ProjectSettings.globalize_path("res://build/multi_session_saves/a"))
	session_b.set_save_directory(ProjectSettings.globalize_path("res://build/multi_session_saves/b"))

	if not session_a.load_core(core_path) or not session_b.load_core(core_path):
		push_error("Two independent FCEUmm core instances should load concurrently.")
		session_a.stop()
		session_b.stop()
		quit(1)
		return
	if not session_a.load_game(rom_path) or not session_b.load_game(rom_path):
		push_error("Each core instance should be able to load its own copy of the same ROM.")
		session_a.stop()
		session_b.stop()
		quit(1)
		return

	for frame_index in range(60):
		session_a.run_frame()
		session_b.run_frame()
	if session_a.get_video_frames_received() < 60 or session_b.get_video_frames_received() < 60:
		push_error("Video callbacks should be isolated and reach both active sessions.")
		session_a.stop()
		session_b.stop()
		quit(1)
		return

	var session_a_frames := session_a.get_video_frames_received()
	for frame_index in range(10):
		session_b.run_frame()
	if session_a.get_video_frames_received() != session_a_frames:
		push_error("Running one core must not redirect callbacks into another session.")
		session_a.stop()
		session_b.stop()
		quit(1)
		return

	session_a.stop()
	for frame_index in range(10):
		session_b.run_frame()
	if not session_b.is_game_loaded() or session_b.get_video_frames_received() < 80:
		push_error("Stopping one core must not stop the other core instance.")
		session_b.stop()
		quit(1)
		return
	session_b.stop()

	print("Concurrent Libretro session isolation test passed.")
	quit(0)
