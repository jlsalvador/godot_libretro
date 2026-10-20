#pragma once

#include <godot_cpp/classes/ref_counted.hpp>
#include <godot_cpp/variant/packed_byte_array.hpp>
#include <godot_cpp/variant/packed_vector2_array.hpp>
#include <godot_cpp/variant/string.hpp>

#include <array>
#include <cstdarg>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <map>
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

	void *core_handle = nullptr;
	bool core_initialized = false;
	std::string core_path_key;

	// Process-wide cache of per-core linker namespaces. glibc never
	// reclaims static TLS blocks, so a fresh dlmopen per session exhausts
	// them after a handful of loads; namespaces are therefore reused once
	// their session ends (a second concurrent session of the same core
	// still gets a fresh namespace for isolation).
	static std::map<std::string, std::vector<void *>> free_namespaces;	bool game_loaded = false;
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

	PackedByteArray rom_data;
	std::string rom_path;
	std::string core_id;
	std::string system_directory;
	std::string save_directory;
	std::string save_directory_override;
	std::string save_directory_core;
	std::string save_ram_path;
	std::string last_error;
	std::string core_name;
	std::string core_version;
	std::string core_extensions;
	std::vector<uint8_t> video_rgba;
	std::vector<int16_t> audio_samples;
	static constexpr unsigned MAX_PLAYERS = 4;
	// One RetroPad state per multiplayer port. GDScript routes the held
	// pad (or a future network source) into its cabled port; the core
	// polls each port independently.
	std::array<std::array<bool, 16>, MAX_PLAYERS> port_buttons = {};
	std::array<std::array<int16_t, 4>, MAX_PLAYERS> port_axes = {};
	std::map<std::string, std::string> core_option_defaults;
	std::map<std::string, std::string> core_option_overrides;
	std::map<std::string, std::string> core_option_resolved;
	unsigned video_width = 0;
	unsigned video_height = 0;
	unsigned video_frames_received = 0;
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

	static bool _environment_callback(unsigned command, void *data);
	static void _log_callback(retro_log_level level, const char *format, ...);
	static void _video_refresh_callback(const void *data, unsigned width, unsigned height, size_t pitch);
	static void _audio_sample_callback(int16_t left, int16_t right);
	static size_t _audio_sample_batch_callback(const int16_t *data, size_t frames);
	static void _input_poll_callback();
	static int16_t _input_state_callback(unsigned port, unsigned device, unsigned index, unsigned id);

	bool _resolve_symbols();
	void *_acquire_namespace(const std::string &path, std::string &error);
	void _release_namespace();
	void _store_core_option_variable(const retro_variable *variable);
	void _load_save_ram();
	void _save_save_ram();
	void _receive_video(const void *data, unsigned width, unsigned height, size_t pitch);
	void _receive_hw_video(unsigned width, unsigned height);
	void _receive_audio(int16_t left, int16_t right);
	void _receive_audio_batch(const int16_t *data, size_t frames);
	int16_t _read_input(unsigned port, unsigned device, unsigned index, unsigned id) const;
	bool _start_hw_render();
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
	bool load_game(const String &path);
	void run_frame();
	void stop();

	void set_joypad_button(int port, int button_id, bool pressed);
	void set_analog_axis(int port, int axis_id, float value);
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
};

} // namespace godot
