#pragma once

#include <godot_cpp/classes/ref_counted.hpp>
#include <godot_cpp/variant/packed_byte_array.hpp>
#include <godot_cpp/variant/packed_string_array.hpp>
#include <godot_cpp/variant/packed_vector2_array.hpp>
#include <godot_cpp/variant/string.hpp>

#include <godot_cpp/variant/vector2.hpp>
#include <array>
#include <atomic>
#include <cstdarg>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <deque>
#include <map>
#include <mutex>
#include <string>
#include <vector>

#include <libretro.h>

#include "vulkan_context.h"

namespace godot {

class LibretroSession : public RefCounted {
	GDCLASS(LibretroSession, RefCounted)

	using RetroApiVersion = unsigned (*)();
	using RetroSetEnvironment = void (*)(retro_environment_t);
	using RetroSetVideoRefresh = void (*)(retro_video_refresh_t);
	using RetroSetAudioSample = void (*)(retro_audio_sample_t);
	using RetroSetAudioSampleBatch = void (*)(retro_audio_sample_batch_t);
	using RetroSetInputPoll = void (*)(retro_input_poll_t);
	using RetroSetInputState = void (*)(retro_input_state_t);
	using RetroInit = void (*)();
	using RetroDeinit = void (*)();
	using RetroGetSystemInfo = void (*)(retro_system_info *);
	using RetroGetSystemAvInfo = void (*)(retro_system_av_info *);
	using RetroLoadGame = bool (*)(const retro_game_info *);
	using RetroUnloadGame = void (*)();
	using RetroRun = void (*)();
	using RetroGetMemoryData = void *(*)(unsigned);
	using RetroGetMemorySize = size_t (*)(unsigned);
	using RetroSerializeSize = size_t (*)();
	using RetroSerialize = bool (*)(void *, size_t);
	using RetroUnserialize = bool (*)(const void *, size_t);
	using RetroSetControllerPortDevice = void (*)(unsigned, unsigned);
	using RetroSetRumbleState = bool (*)(unsigned, retro_rumble_effect, uint16_t);

	void *core_handle = nullptr;
	bool core_initialized = false;
	std::string core_path_key;

	// The namespace cache lives in the translation unit: it outlives every
	// session, so it is deliberately never destroyed.
	bool game_loaded = false;
	// The content is copied into memory only for cores that do not declare
	// need_fullpath. Past the cap the load falls back to the path, so the cap
	// bounds the allocation instead of rejecting a load.
	static constexpr uint64_t ROM_DATA_CAP = 64 * 1024 * 1024;
	// A save state larger than this is never captured nor handed to a core.
	static constexpr uint64_t SAVE_STATE_CAP = 1024 * 1024 * 1024;
	retro_pixel_format pixel_format = RETRO_PIXEL_FORMAT_0RGB1555;
	RetroApiVersion api_version = nullptr;
	RetroSetEnvironment set_environment = nullptr;
	RetroSetVideoRefresh set_video_refresh = nullptr;
	RetroSetAudioSample set_audio_sample = nullptr;
	RetroSetAudioSampleBatch set_audio_sample_batch = nullptr;
	RetroSetInputPoll set_input_poll = nullptr;
	RetroSetInputState set_input_state = nullptr;
	RetroInit init_core = nullptr;
	RetroDeinit deinit_core = nullptr;
	RetroGetSystemInfo get_system_info = nullptr;
	RetroGetSystemAvInfo get_system_av_info = nullptr;
	RetroLoadGame core_load_game = nullptr;
	RetroUnloadGame core_unload_game = nullptr;
	RetroRun run_core = nullptr;
	RetroGetMemoryData get_memory_data = nullptr;
	RetroGetMemorySize get_memory_size = nullptr;
	RetroSerializeSize serialize_size = nullptr;
	RetroSerialize serialize = nullptr;
	RetroUnserialize unserialize = nullptr;
	RetroSetControllerPortDevice controller_port_device = nullptr;

	PackedByteArray rom_data;
	std::string rom_path;
	std::string core_id;
	std::string system_directory;
	std::string system_directory_override;
	std::string save_directory;
	std::string save_directory_override;
	std::string save_directory_core;
	std::string save_ram_path;
	std::string last_error;
	std::string core_name;
	std::string core_version;
	std::string core_extensions;
	// retro_system_info.need_fullpath: when false, the API requires the
	// frontend to hand the content over in retro_game_info.data.
	bool core_needs_fullpath = false;
	// Guards the two output buffers: one thread may drive the session while
	// the caller reads the last frame or audio chunk from another.
	mutable std::mutex output_mutex;
	std::vector<uint8_t> video_rgba;
	std::vector<int16_t> audio_samples;
	static constexpr unsigned MAX_PLAYERS = 4;
	// One RetroPad state per multiplayer port. GDScript routes the held
	// pad (or a future network source) into its cabled port; the core
	// polls each port independently.
	std::array<std::array<bool, 16>, MAX_PLAYERS> port_buttons = {};
	std::array<std::array<int16_t, 4>, MAX_PLAYERS> port_axes = {};
	// Per-port rumble strength (0..1) set by the core through the rumble
	// interface: 0 = strong (large) motor, 1 = weak (small) motor.
	std::array<std::array<float, 2>, MAX_PLAYERS> port_rumble = {};
	// Separate from output_mutex: a rumble read should not wait behind a frame
	// conversion, and the Vulkan readback holds that lock for a whole copy.
	mutable std::mutex rumble_mutex;
	// Written from the core's thread when it takes the interface, read from
	// the caller's.
	std::atomic<bool> rumble_interface_taken = false;
	// Option maps shared with the core: the caller writes them through
	// set_core_option(), the core reads them from GET_VARIABLE while it runs.
	mutable std::mutex options_mutex;
	std::map<std::string, std::string> core_option_defaults;
	std::map<std::string, std::string> core_option_overrides;
	// Value last resolved for GET_VARIABLE: a session-owned buffer, so the
	// pointer handed to the core outlives later option changes.
	std::string core_option_value;
	// Set by set_core_option(), consumed by GET_VARIABLE_UPDATE.
	std::atomic<bool> core_options_dirty = false;
	// Quirks the core declared, kept to know how strict a state must be.
	uint64_t serialization_quirks = 0;
	// Tail of the core's log, as formatted by its log interface. The core
	// logs through a C varargs callback; the lines are kept for inspection
	// (a BIOS lookup report, a load error) without a live console.
	static constexpr size_t CORE_LOG_LINES_MAX = 1024;
	std::deque<std::string> core_log_lines;
	// Geometry and frame counter of the last presented frame: atomic scalars
	// rather than output_mutex, so a caller polling them never waits behind a
	// frame conversion or a slow Vulkan readback. Each read is coherent on its
	// own; a width/height pair can still straddle two frames, exactly as it can
	// between two getter calls.
	std::atomic<unsigned> video_width = 0;
	std::atomic<unsigned> video_height = 0;
	std::atomic<unsigned> video_frames_received = 0;
	double frames_per_second = 60.0;
	double audio_sample_rate = 48000.0;
	unsigned frames_since_save = 0;

	// Optional hardware rendering (Vulkan, e.g. ParaLLEl-RDP). Only used
	// when the core requests it; software cores never touch this path.
	VulkanContext vulkan_context;
	retro_hw_render_callback hw_render_request = {};
	retro_hw_render_context_negotiation_interface_vulkan hw_negotiation = {};
	bool hw_render_requested = false;
	bool has_hw_negotiation = false;
	// True between a context_reset this session ran and its context_destroy.
	bool hw_context_started = false;
	// Last readback failure, reported once instead of every frame.
	std::string hw_readback_error;

	static bool _environment_callback(unsigned command, void *data);
	static void _log_callback(retro_log_level level, const char *format, ...);
	static bool _set_rumble_state(unsigned p_port, retro_rumble_effect p_effect, uint16_t p_strength);
	static void _video_refresh_callback(const void *data, unsigned width, unsigned height, size_t pitch);
	static void _audio_sample_callback(int16_t left, int16_t right);
	static size_t _audio_sample_batch_callback(const int16_t *data, size_t frames);
	static void _input_poll_callback();
	static int16_t _input_state_callback(unsigned port, unsigned device, unsigned index, unsigned id);

	bool _resolve_symbols();
	void *_acquire_namespace(const std::string &path, std::string &error);
	void _release_namespace();
	bool _configure_directories();
	void _store_core_option_variable(const retro_variable *variable);
	void _load_save_ram();
	void _save_save_ram();
	void _receive_video(const void *data, unsigned width, unsigned height, size_t pitch);
	void _receive_hw_video(unsigned width, unsigned height);
	void _receive_audio(int16_t left, int16_t right);
	void _receive_audio_batch(const int16_t *data, size_t frames);
	int16_t _read_input(unsigned port, unsigned device, unsigned index, unsigned id) const;
	void _start_hw_render();
	void _stop_hw_render();

protected:
	static void _bind_methods();

public:
	LibretroSession() = default;
	~LibretroSession();

	bool load_core(const String &path);
	void set_save_directory(const String &path);
	String get_save_directory() const;
	String get_save_ram_path() const;
	void set_system_directory(const String &path);
	String get_system_directory() const;
	PackedStringArray get_core_log_lines() const;
	bool load_game(const String &path);
	void run_frame();
	void stop();

	void set_joypad_button(int port, int button_id, bool pressed);
	void set_analog_axis(int port, int axis_id, float value);
	void set_controller_port_device(int port, unsigned device);
	bool get_joypad_button(int port, int button_id) const;
	float get_analog_axis(int port, int axis) const;
	Vector2 get_rumble(int port) const;
	bool has_rumble_interface() const;
	void set_core_option(const String &key, const String &value);
	String get_core_option(const String &key) const;

	PackedByteArray get_video_frame() const;
	PackedVector2Array get_audio_buffer();
	String get_last_error() const;
	String get_core_name() const;
	String get_core_version() const;
	String get_core_extensions() const;
	unsigned get_video_width() const;
	unsigned get_video_height() const;
	unsigned get_video_frames_received() const;
	double get_frames_per_second() const;
	double get_audio_sample_rate() const;
	bool is_core_loaded() const;
	bool is_game_loaded() const;
	int get_save_state_size();
	PackedByteArray create_save_state();
	bool restore_save_state(const PackedByteArray &state);
};

} // namespace godot
