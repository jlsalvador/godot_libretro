#include "libretro_session.h"

#include <godot_cpp/core/class_db.hpp>
#include <godot_cpp/classes/dir_access.hpp>
#include <godot_cpp/classes/file_access.hpp>
#include <godot_cpp/classes/os.hpp>

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <dlfcn.h>
#if defined(__GLIBC__)
#include <link.h>
#endif

namespace godot {

namespace {

// Libretro callbacks lack a user-data argument, so scope the current session to each core call.
thread_local LibretroSession *active_session = nullptr;

class ActiveSessionScope {
	LibretroSession *previous_session;

public:
	explicit ActiveSessionScope(LibretroSession *p_session) : previous_session(active_session) {
		active_session = p_session;
	}
	~ActiveSessionScope() {
		active_session = previous_session;
	}
};

template <typename Function>
bool resolve_symbol(void *p_handle, Function &r_function, const char *p_name, std::string &r_error) {
	r_function = reinterpret_cast<Function>(dlsym(p_handle, p_name));
	if (r_function) {
		return true;
	}
	r_error = std::string("Missing Libretro symbol: ") + p_name;
	return false;
}

uint8_t expand_5bit(uint16_t p_value) {
	return static_cast<uint8_t>((p_value << 3) | (p_value >> 2));
}

uint8_t expand_6bit(uint16_t p_value) {
	return static_cast<uint8_t>((p_value << 2) | (p_value >> 4));
}

} // namespace

void LibretroSession::_bind_methods() {
	ClassDB::bind_method(D_METHOD("load_core", "path"), &LibretroSession::load_core);
	ClassDB::bind_method(D_METHOD("set_save_directory", "path"), &LibretroSession::set_save_directory);
	ClassDB::bind_method(D_METHOD("get_save_directory"), &LibretroSession::get_save_directory);
	ClassDB::bind_method(D_METHOD("get_save_ram_path"), &LibretroSession::get_save_ram_path);
	ClassDB::bind_method(D_METHOD("set_system_directory", "path"), &LibretroSession::set_system_directory);
	ClassDB::bind_method(D_METHOD("get_system_directory"), &LibretroSession::get_system_directory);
	ClassDB::bind_method(D_METHOD("get_core_log_lines"), &LibretroSession::get_core_log_lines);
	ClassDB::bind_method(D_METHOD("load_game", "path"), &LibretroSession::load_game);
	ClassDB::bind_method(D_METHOD("run_frame"), &LibretroSession::run_frame);
	ClassDB::bind_method(D_METHOD("stop"), &LibretroSession::stop);
	ClassDB::bind_method(D_METHOD("set_joypad_button", "port", "button_id", "pressed"), &LibretroSession::set_joypad_button);
	ClassDB::bind_method(D_METHOD("set_analog_axis", "port", "axis_id", "value"), &LibretroSession::set_analog_axis);
	ClassDB::bind_method(D_METHOD("set_core_option", "key", "value"), &LibretroSession::set_core_option);
	ClassDB::bind_method(D_METHOD("get_core_option", "key"), &LibretroSession::get_core_option);
	ClassDB::bind_method(D_METHOD("get_video_frame"), &LibretroSession::get_video_frame);
	ClassDB::bind_method(D_METHOD("get_audio_buffer"), &LibretroSession::get_audio_buffer);
	ClassDB::bind_method(D_METHOD("get_last_error"), &LibretroSession::get_last_error);
	ClassDB::bind_method(D_METHOD("get_core_name"), &LibretroSession::get_core_name);
	ClassDB::bind_method(D_METHOD("get_core_version"), &LibretroSession::get_core_version);
	ClassDB::bind_method(D_METHOD("get_core_extensions"), &LibretroSession::get_core_extensions);
	ClassDB::bind_method(D_METHOD("get_video_width"), &LibretroSession::get_video_width);
	ClassDB::bind_method(D_METHOD("get_video_height"), &LibretroSession::get_video_height);
	ClassDB::bind_method(D_METHOD("get_video_frames_received"), &LibretroSession::get_video_frames_received);
	ClassDB::bind_method(D_METHOD("get_frames_per_second"), &LibretroSession::get_frames_per_second);
	ClassDB::bind_method(D_METHOD("get_audio_sample_rate"), &LibretroSession::get_audio_sample_rate);
	ClassDB::bind_method(D_METHOD("is_core_loaded"), &LibretroSession::is_core_loaded);
	ClassDB::bind_method(D_METHOD("is_game_loaded"), &LibretroSession::is_game_loaded);
}

LibretroSession::~LibretroSession() {
	stop();
}

void LibretroSession::_release_namespace() {
	if (core_handle && !core_path_key.empty()) {
		free_namespaces[core_path_key].push_back(core_handle);
	}
	core_handle = nullptr;
	core_path_key.clear();
}

std::map<std::string, std::vector<void *>> LibretroSession::free_namespaces;

bool LibretroSession::_resolve_symbols() {
	const bool required_symbols = resolve_symbol(core_handle, api_version, "retro_api_version", last_error) &&
			resolve_symbol(core_handle, set_environment, "retro_set_environment", last_error) &&
			resolve_symbol(core_handle, set_video_refresh, "retro_set_video_refresh", last_error) &&
			resolve_symbol(core_handle, set_audio_sample, "retro_set_audio_sample", last_error) &&
			resolve_symbol(core_handle, set_audio_sample_batch, "retro_set_audio_sample_batch", last_error) &&
			resolve_symbol(core_handle, set_input_poll, "retro_set_input_poll", last_error) &&
			resolve_symbol(core_handle, set_input_state, "retro_set_input_state", last_error) &&
			resolve_symbol(core_handle, init_core, "retro_init", last_error) &&
			resolve_symbol(core_handle, deinit_core, "retro_deinit", last_error) &&
			resolve_symbol(core_handle, get_system_info, "retro_get_system_info", last_error) &&
			resolve_symbol(core_handle, get_system_av_info, "retro_get_system_av_info", last_error) &&
			resolve_symbol(core_handle, core_load_game, "retro_load_game", last_error) &&
			resolve_symbol(core_handle, core_unload_game, "retro_unload_game", last_error) &&
			resolve_symbol(core_handle, run_core, "retro_run", last_error);
	get_memory_data = reinterpret_cast<RetroGetMemoryData>(dlsym(core_handle, "retro_get_memory_data"));
	get_memory_size = reinterpret_cast<RetroGetMemorySize>(dlsym(core_handle, "retro_get_memory_size"));
	return required_symbols;
}

void *LibretroSession::_acquire_namespace(const std::string &p_path, std::string &r_error) {
	auto cached = free_namespaces.find(p_path);
	if (cached != free_namespaces.end() && !cached->second.empty()) {
		void *handle = cached->second.back();
		cached->second.pop_back();
		return handle;
	}
#if defined(__GLIBC__)
	// Each core gets its own ELF namespace so core-global state stays
	// independent across concurrent sessions.
	void *handle = dlmopen(LM_ID_NEWLM, p_path.c_str(), RTLD_NOW | RTLD_LOCAL);
#else
	void *handle = dlopen(p_path.c_str(), RTLD_NOW | RTLD_LOCAL);
#endif
	if (!handle) {
		const char *error = dlerror();
		r_error = error ? error : "";
	}
	return handle;
}

bool LibretroSession::load_core(const String &p_path) {
	stop();
	last_error.clear();
	core_id = p_path.get_file().get_basename().utf8().get_data();

	std::string key = p_path.utf8().get_data();
	std::string dl_error;
	core_handle = _acquire_namespace(key, dl_error);
	if (!core_handle) {
		last_error = dl_error.empty() ? "Could not load Libretro core." : dl_error;
		return false;
	}
	core_path_key = key;
	if (!_resolve_symbols()) {
		stop();
		return false;
	}
	if (api_version() != RETRO_API_VERSION) {
		last_error = "Libretro API version mismatch.";
		stop();
		return false;
	}

	// One system directory per core: the libretro spec hands each core a
	// directory for system files (BIOS, caches); the default base is the
	// Godot user data directory, overridable per session.
	if (system_directory_override.empty()) {
		system_directory = std::string(OS::get_singleton()->get_user_data_dir().utf8().get_data()) + "/system/" + core_id;
	} else {
		system_directory = system_directory_override;
	}
	if (save_directory_override.empty()) {
		String executable_save_directory = OS::get_singleton()->get_executable_path().get_base_dir().path_join("saves");
		save_directory = executable_save_directory.utf8().get_data();
	} else {
		save_directory = save_directory_override;
	}
	save_directory_core = String(save_directory.c_str()).path_join(String(core_id.c_str())).utf8().get_data();
	DirAccess::make_dir_recursive_absolute(String(system_directory.c_str()));
	DirAccess::make_dir_recursive_absolute(String(save_directory.c_str()));
	{
		ActiveSessionScope active_scope(this);
		set_environment(_environment_callback);
		set_video_refresh(_video_refresh_callback);
		set_audio_sample(_audio_sample_callback);
		set_audio_sample_batch(_audio_sample_batch_callback);
		set_input_poll(_input_poll_callback);
		set_input_state(_input_state_callback);
		init_core();
		core_initialized = true;

		retro_system_info info = {};
		get_system_info(&info);
		core_name = info.library_name ? info.library_name : "Libretro Core";
		core_version = info.library_version ? info.library_version : "";
		core_extensions = info.valid_extensions ? info.valid_extensions : "";
	}
	return true;
}

void LibretroSession::set_save_directory(const String &p_path) {
	save_directory_override = p_path.utf8().get_data();
}

String LibretroSession::get_save_directory() const {
	return String(save_directory.c_str());
}

String LibretroSession::get_save_ram_path() const {
	return String(save_ram_path.c_str());
}

void LibretroSession::set_system_directory(const String &p_path) {
	system_directory_override = p_path.utf8().get_data();
}

String LibretroSession::get_system_directory() const {
	return String(system_directory.c_str());
}

PackedStringArray LibretroSession::get_core_log_lines() const {
	PackedStringArray lines;
	for (const std::string &line : core_log_lines) {
		lines.append(String(line.c_str()));
	}
	return lines;
}

bool LibretroSession::load_game(const String &p_path) {
	if (!core_initialized || !core_load_game) {
		last_error = "Load a Libretro core before loading a game.";
		return false;
	}
	if (game_loaded) {
		_save_save_ram();
		ActiveSessionScope active_scope(this);
		core_unload_game();
		game_loaded = false;
	}

	Ref<FileAccess> file = FileAccess::open(p_path, FileAccess::READ);
	if (file.is_null()) {
		last_error = "Could not open ROM file: " + std::string(p_path.utf8().get_data());
		return false;
	}
	uint64_t file_size = file->get_length();
	if (file_size == 0) {
		last_error = "ROM file is empty.";
		return false;
	}
	rom_path = p_path.utf8().get_data();
	// Most cores open the file themselves from game.path. A few upstream
	// frontends are non-conformant and read game.data unconditionally
	// (bsnes, gambatte, mupen64plus); only they get the file read in, capped at
	// 64 MiB so the cap stays a load error, not a giant allocation.
	static const std::set<std::string> data_cores = {
		"bsnes_libretro", "gambatte_libretro", "mupen64plus_next_libretro",
	};
	const bool needs_data = data_cores.find(core_id) != data_cores.end();
	if (needs_data) {
		if (file_size > 64 * 1024 * 1024) {
			last_error = "ROM file is larger than the 64 MiB data cap of this core.";
			return false;
		}
		rom_data = file->get_buffer(file_size);
		if (rom_data.size() != file_size) {
			last_error = "Could not read complete ROM file.";
			return false;
		}
	} else {
		rom_data.clear();
	}

	retro_game_info game = {};
	game.path = rom_path.c_str();
	game.data = needs_data ? rom_data.ptr() : nullptr;
	game.size = needs_data ? rom_data.size() : 0;
	{
		ActiveSessionScope active_scope(this);
		if (!core_load_game(&game)) {
			last_error = "Libretro core rejected the ROM.";
			return false;
	}
	game_loaded = true;
	if (hw_render_requested && !_start_hw_render()) {
		core_unload_game();
		game_loaded = false;
		return false;
	}
	String core_save_directory = String(save_directory.c_str()).path_join(String(core_id.c_str()));
	DirAccess::make_dir_recursive_absolute(core_save_directory);
	save_ram_path = core_save_directory.path_join(p_path.get_file().get_basename() + ".srm").utf8().get_data();
		frames_since_save = 0;
		_load_save_ram();

		retro_system_av_info av_info = {};
		get_system_av_info(&av_info);
		video_width = av_info.geometry.base_width;
		video_height = av_info.geometry.base_height;
		frames_per_second = av_info.timing.fps;
		audio_sample_rate = av_info.timing.sample_rate;
	}
	return video_width > 0 && video_height > 0;
}

void LibretroSession::run_frame() {
	if (!game_loaded || !run_core) {
		return;
	}
	audio_samples.clear();
	{
		ActiveSessionScope active_scope(this);
		run_core();
	}
	if (++frames_since_save >= 300) {
		_save_save_ram();
		frames_since_save = 0;
	}
}

void LibretroSession::stop() {
	if (hw_render_requested) {
		ActiveSessionScope active_scope(this);
		_stop_hw_render();
	}
	if (game_loaded && core_unload_game) {
		_save_save_ram();
		ActiveSessionScope active_scope(this);
		core_unload_game();
	}
	game_loaded = false;
	if (core_initialized && deinit_core) {
		ActiveSessionScope active_scope(this);
		deinit_core();
	}
	core_initialized = false;
	_release_namespace();
	rom_data.clear();
	rom_path.clear();
	core_id.clear();
	core_option_defaults.clear();
	core_option_resolved.clear();
	video_rgba.clear();
	audio_samples.clear();
	video_width = 0;
	video_height = 0;
	video_frames_received = 0;
	save_directory_core.clear();
	save_ram_path.clear();
	frames_since_save = 0;
	get_memory_data = nullptr;
	get_memory_size = nullptr;
}

bool LibretroSession::_start_hw_render() {
	if (!hw_render_requested) {
		return true;
	}
	if (!has_hw_negotiation) {
		last_error = "Core requested Vulkan rendering without negotiation info.";
		return false;
	}
	std::string error;
	if (!vulkan_context.ensure_device(&hw_negotiation, error)) {
		last_error = error.empty() ? "Could not create Vulkan device for hardware rendering." : error;
		return false;
	}
	// The core deferred graphics initialization to this call.
	hw_render_request.context_reset();
	return true;
}

void LibretroSession::_stop_hw_render() {
	if (hw_render_requested && hw_render_request.context_destroy) {
		hw_render_request.context_destroy();
	}
	hw_render_requested = false;
	has_hw_negotiation = false;
	std::memset(&hw_render_request, 0, sizeof(hw_render_request));
	std::memset(&hw_negotiation, 0, sizeof(hw_negotiation));
	vulkan_context.shutdown();
}


void LibretroSession::_load_save_ram() {
	if (!game_loaded || save_ram_path.empty() || !get_memory_data || !get_memory_size) {
		return;
	}
	void *memory = get_memory_data(RETRO_MEMORY_SAVE_RAM);
	const size_t memory_size = get_memory_size(RETRO_MEMORY_SAVE_RAM);
	if (!memory || memory_size == 0) {
		return;
	}
	ActiveSessionScope active_scope(this);
	Ref<FileAccess> file = FileAccess::open(String(save_ram_path.c_str()), FileAccess::READ);
	if (file.is_null()) {
		return;
	}
	const uint64_t save_size = file->get_length();
	if (save_size == 0 || save_size > 64 * 1024 * 1024) {
		return;
	}
	PackedByteArray save_data = file->get_buffer(save_size);
	if (save_data.is_empty()) {
		return;
	}
	const size_t bytes_to_copy = std::min(memory_size, static_cast<size_t>(save_data.size()));
	std::memset(memory, 0, memory_size);
	std::memcpy(memory, save_data.ptr(), bytes_to_copy);
}


void LibretroSession::_save_save_ram() {
	if (!game_loaded || save_ram_path.empty() || !get_memory_data || !get_memory_size) {
		return;
	}
	const void *memory = get_memory_data(RETRO_MEMORY_SAVE_RAM);
	const size_t memory_size = get_memory_size(RETRO_MEMORY_SAVE_RAM);
	if (!memory || memory_size == 0) {
		return;
	}
	ActiveSessionScope active_scope(this);
	PackedByteArray save_data;
	save_data.resize(static_cast<int64_t>(memory_size));
	std::memcpy(save_data.ptrw(), memory, memory_size);
	Ref<FileAccess> file = FileAccess::open(String(save_ram_path.c_str()), FileAccess::WRITE);
	if (file.is_valid()) {
		file->store_buffer(save_data);
	}
}

void LibretroSession::set_joypad_button(int p_port, int p_button_id, bool p_pressed) {
	if (p_port < 0 || p_port >= static_cast<int>(MAX_PLAYERS)) {
		return;
	}
	if (p_button_id >= 0 && p_button_id < static_cast<int>(port_buttons[p_port].size())) {
		port_buttons[p_port][p_button_id] = p_pressed;
	}
}

void LibretroSession::set_analog_axis(int p_port, int p_axis_id, float p_value) {
	if (p_port < 0 || p_port >= static_cast<int>(MAX_PLAYERS)) {
		return;
	}
	if (p_axis_id < 0 || p_axis_id >= static_cast<int>(port_axes[p_port].size())) {
		return;
	}
	float clamped = std::clamp(p_value, -1.0f, 1.0f);
	port_axes[p_port][p_axis_id] = static_cast<int16_t>(clamped * 32767.0f);
}

void LibretroSession::set_core_option(const String &p_key, const String &p_value) {
	const std::string key = p_key.utf8().get_data();
	const std::string value = p_value.utf8().get_data();
	if (key.empty()) {
		return;
	}
	if (value.empty()) {
		core_option_overrides.erase(key);
		core_option_resolved.erase(key);
	} else {
		core_option_overrides[key] = value;
		core_option_resolved[key] = value;
	}
}

String LibretroSession::get_core_option(const String &p_key) const {
	const std::string key = p_key.utf8().get_data();
	const auto override = core_option_overrides.find(key);
	if (override != core_option_overrides.end()) {
		return String(override->second.c_str());
	}
	const auto fallback = core_option_defaults.find(key);
	if (fallback != core_option_defaults.end()) {
		return String(fallback->second.c_str());
	}
	return String();
}

void LibretroSession::_store_core_option_variable(const retro_variable *p_variable) {
	if (!p_variable || !p_variable->key || !p_variable->value) {
		return;
	}
	// Format: "Description; default|option|...". The first token wins.
	const std::string text = p_variable->value;
	const size_t separator = text.find("; ");
	if (separator == std::string::npos) {
		return;
	}
	std::string first = text.substr(separator + 2);
	const size_t pipe = first.find('|');
	if (pipe != std::string::npos) {
		first = first.substr(0, pipe);
	}
	first.erase(0, first.find_first_not_of(' '));
	first.erase(first.find_last_not_of(' ') + 1);
	if (!first.empty()) {
		core_option_defaults[p_variable->key] = first;
	}
}

PackedByteArray LibretroSession::get_video_frame() const {
	PackedByteArray result;
	result.resize(video_rgba.size());
	if (!video_rgba.empty()) {
		std::memcpy(result.ptrw(), video_rgba.data(), video_rgba.size());
	}
	return result;
}

PackedVector2Array LibretroSession::get_audio_buffer() {
	PackedVector2Array result;
	const int frame_count = static_cast<int>(audio_samples.size() / 2);
	result.resize(frame_count);
	Vector2 *output = result.ptrw();
	for (int index = 0; index < frame_count; index++) {
		output[index] = Vector2(
				static_cast<float>(audio_samples[index * 2]) / 32768.0f,
				static_cast<float>(audio_samples[index * 2 + 1]) / 32768.0f);
	}
	audio_samples.clear();
	return result;
}

String LibretroSession::get_last_error() const { return String(last_error.c_str()); }
String LibretroSession::get_core_name() const { return String(core_name.c_str()); }
String LibretroSession::get_core_version() const { return String(core_version.c_str()); }
String LibretroSession::get_core_extensions() const { return String(core_extensions.c_str()); }
unsigned LibretroSession::get_video_width() const { return video_width; }
unsigned LibretroSession::get_video_height() const { return video_height; }
unsigned LibretroSession::get_video_frames_received() const { return video_frames_received; }
double LibretroSession::get_frames_per_second() const { return frames_per_second; }
double LibretroSession::get_audio_sample_rate() const { return audio_sample_rate; }
bool LibretroSession::is_core_loaded() const { return core_initialized; }
bool LibretroSession::is_game_loaded() const { return game_loaded; }

bool LibretroSession::_environment_callback(unsigned p_command, void *p_data) {
	if (!active_session) {
		return false;
	}
	switch (p_command) {
		case RETRO_ENVIRONMENT_SET_PIXEL_FORMAT: {
			if (!p_data) {
				return false;
			}
			const auto format = *static_cast<retro_pixel_format *>(p_data);
			if (format != RETRO_PIXEL_FORMAT_0RGB1555 && format != RETRO_PIXEL_FORMAT_RGB565 && format != RETRO_PIXEL_FORMAT_XRGB8888) {
				return false;
			}
			active_session->pixel_format = format;
			return true;
		}
		case RETRO_ENVIRONMENT_GET_CAN_DUPE:
			if (p_data) {
				// Some cores (e.g. Gambatte) refuse to load without dupe
				// support; repeated frames re-expose the last buffer.
				*static_cast<bool *>(p_data) = true;
				return true;
			}
			return false;
		case RETRO_ENVIRONMENT_GET_SYSTEM_DIRECTORY:
			if (p_data) {
				*static_cast<const char **>(p_data) = active_session->system_directory.c_str();
				return true;
			}
			return false;
		case RETRO_ENVIRONMENT_GET_SAVE_DIRECTORY:
			if (p_data) {
				// Cores that persist their own state (e.g. bsnes) store
				// their saves under this directory; serve the same
				// <core id> subfolder the extension uses for its own
				// SRAM sidecar (save_ram_path) so that every core's
				// saves share one layout.
				*static_cast<const char **>(p_data) = active_session->save_directory_core.c_str();
				return true;
			}
			return false;
		case RETRO_ENVIRONMENT_GET_LOG_INTERFACE: {
			if (!p_data) {
				return false;
			}
			retro_log_callback *log = static_cast<retro_log_callback *>(p_data);
			log->log = _log_callback;
			return true;
		}
		case RETRO_ENVIRONMENT_SET_VARIABLES: {
			if (!p_data) {
				return false;
			}
			for (const retro_variable *variable = static_cast<const retro_variable *>(p_data); variable->key; ++variable) {
				active_session->_store_core_option_variable(variable);
			}
			return true;
		}
		case RETRO_ENVIRONMENT_GET_VARIABLE: {			if (!p_data) {
				return false;
			}
			retro_variable *variable = static_cast<retro_variable *>(p_data);
			if (!variable->key) {
				return false;
			}
			const std::string key = variable->key;
			const auto override = active_session->core_option_overrides.find(key);
			const auto fallback = active_session->core_option_defaults.find(key);
			if (override == active_session->core_option_overrides.end() && fallback == active_session->core_option_defaults.end()) {
				return false;
			}
			active_session->core_option_resolved[key] = override != active_session->core_option_overrides.end() ? override->second : fallback->second;
			variable->value = active_session->core_option_resolved[key].c_str();
			return true;
		}
		case RETRO_ENVIRONMENT_SET_HW_RENDER: {
			if (!p_data) {
				return false;
			}
			const retro_hw_render_callback *request = static_cast<const retro_hw_render_callback *>(p_data);
			if (request->context_type != RETRO_HW_CONTEXT_VULKAN) {
				return false;
			}
			active_session->hw_render_request = *request;
			active_session->hw_render_requested = true;
			return true;
		}
		case RETRO_ENVIRONMENT_SET_HW_RENDER_CONTEXT_NEGOTIATION_INTERFACE: {
			if (!p_data) {
				return false;
			}
			active_session->hw_negotiation =
					*static_cast<const retro_hw_render_context_negotiation_interface_vulkan *>(p_data);
			active_session->has_hw_negotiation = true;
			return true;
		}
		case RETRO_ENVIRONMENT_GET_HW_RENDER_INTERFACE: {
			if (!p_data) {
				return false;
			}
			const retro_hw_render_interface_vulkan *interface = active_session->vulkan_context.render_interface();
			if (!interface) {
				return false;
			}
			*static_cast<const retro_hw_render_interface_vulkan **>(p_data) = interface;
			return true;
		}
		default:
			return false;
	}
}

void LibretroSession::_log_callback(retro_log_level p_level, const char *p_format, ...) {
	if (!p_format) {
		return;
	}
	char buffer[1024];
	va_list args;
	va_start(args, p_format);
	vsnprintf(buffer, sizeof(buffer), p_format, args);
	va_end(args);
	if (active_session) {
		std::string line = buffer;
		while (!line.empty() && (line.back() == '\n' || line.back() == '\r')) {
			line.pop_back();
		}
		active_session->core_log_lines.push_back(line);
		while (active_session->core_log_lines.size() > CORE_LOG_LINES_MAX) {
			active_session->core_log_lines.pop_front();
		}
	}
	const char *prefix = "Libretro";
	switch (p_level) {
		case RETRO_LOG_DEBUG:
			prefix = "Libretro [debug]";
			break;
		case RETRO_LOG_INFO:
			prefix = "Libretro [info]";
			break;
		case RETRO_LOG_WARN:
			prefix = "Libretro [warn]";
			break;
		case RETRO_LOG_ERROR:
			prefix = "Libretro [error]";
			break;
		default:
			break;
	}
	fprintf(stderr, "%s %s", prefix, buffer);
}

void LibretroSession::_video_refresh_callback(const void *p_data, unsigned p_width, unsigned p_height, size_t p_pitch) {
	if (!active_session) {
		return;
	}
	if (p_data == RETRO_HW_FRAME_BUFFER_VALID) {
		active_session->_receive_hw_video(p_width, p_height);
		return;
	}
	active_session->_receive_video(p_data, p_width, p_height, p_pitch);
}

void LibretroSession::_receive_hw_video(unsigned p_width, unsigned p_height) {
	if (p_width == 0 || p_height == 0) {
		return;
	}
	std::string error;
	if (!vulkan_context.readback_frame(p_width, p_height, video_rgba, error)) {
		// Keep the previous frame on readback failure (dupe behavior).
		return;
	}
	video_width = p_width;
	video_height = p_height;
	video_frames_received++;
}

void LibretroSession::_audio_sample_callback(int16_t p_left, int16_t p_right) {
	if (active_session) {
		active_session->_receive_audio(p_left, p_right);
	}
}

size_t LibretroSession::_audio_sample_batch_callback(const int16_t *p_data, size_t p_frames) {
	if (active_session) {
		active_session->_receive_audio_batch(p_data, p_frames);
	}
	return p_frames;
}

void LibretroSession::_input_poll_callback() {}

int16_t LibretroSession::_input_state_callback(unsigned p_port, unsigned p_device, unsigned p_index, unsigned p_id) {
	if (!active_session) {
		return 0;
	}
	return active_session->_read_input(p_port, p_device, p_index, p_id);
}

void LibretroSession::_receive_video(const void *p_data, unsigned p_width, unsigned p_height, size_t p_pitch) {
	if (!p_data) {
		// Duplicate frame: re-expose the previous buffer when one exists.
		if (!video_rgba.empty()) {
			video_frames_received++;
		}
		return;
	}
	if (p_width == 0 || p_height == 0) {
		return;
	}
	video_width = p_width;
	video_height = p_height;
	video_rgba.resize(static_cast<size_t>(p_width) * p_height * 4);
	const auto *source = static_cast<const uint8_t *>(p_data);
	for (unsigned y = 0; y < p_height; y++) {
		const uint8_t *row = source + static_cast<size_t>(y) * p_pitch;
		for (unsigned x = 0; x < p_width; x++) {
			uint8_t *destination = &video_rgba[(static_cast<size_t>(y) * p_width + x) * 4];
			if (pixel_format == RETRO_PIXEL_FORMAT_XRGB8888) {
				uint32_t pixel;
				std::memcpy(&pixel, row + static_cast<size_t>(x) * 4, sizeof(pixel));
				destination[0] = static_cast<uint8_t>((pixel >> 16) & 0xFF);
				destination[1] = static_cast<uint8_t>((pixel >> 8) & 0xFF);
				destination[2] = static_cast<uint8_t>(pixel & 0xFF);
			} else {
				uint16_t pixel;
				std::memcpy(&pixel, row + static_cast<size_t>(x) * 2, sizeof(pixel));
				if (pixel_format == RETRO_PIXEL_FORMAT_RGB565) {
					destination[0] = expand_5bit((pixel >> 11) & 0x1F);
					destination[1] = expand_6bit((pixel >> 5) & 0x3F);
					destination[2] = expand_5bit(pixel & 0x1F);
				} else {
					destination[0] = expand_5bit((pixel >> 10) & 0x1F);
					destination[1] = expand_5bit((pixel >> 5) & 0x1F);
					destination[2] = expand_5bit(pixel & 0x1F);
				}
			}
			destination[3] = 255;
		}
	}
	video_frames_received++;
}

void LibretroSession::_receive_audio(int16_t p_left, int16_t p_right) {
	audio_samples.push_back(p_left);
	audio_samples.push_back(p_right);
}

void LibretroSession::_receive_audio_batch(const int16_t *p_data, size_t p_frames) {
	if (p_data && p_frames > 0) {
		audio_samples.insert(audio_samples.end(), p_data, p_data + p_frames * 2);
	}
}

int16_t LibretroSession::_read_input(unsigned p_port, unsigned p_device, unsigned p_index, unsigned p_id) const {
	if (p_port >= MAX_PLAYERS) {
		return 0;
	}
	const std::array<bool, 16> &buttons = port_buttons[p_port];
	if (p_device == RETRO_DEVICE_JOYPAD) {
		if (p_id == RETRO_DEVICE_ID_JOYPAD_MASK) {
			uint16_t mask = 0;
			for (unsigned index = 0; index < buttons.size(); index++) {
				if (buttons[index]) {
					mask |= static_cast<uint16_t>(1U << index);
				}
			}
			return static_cast<int16_t>(mask);
		}
		if (p_id < buttons.size()) {
			return buttons[p_id] ? 1 : 0;
		}
	}
	if (p_device == RETRO_DEVICE_ANALOG && p_index <= RETRO_DEVICE_INDEX_ANALOG_RIGHT && p_id <= RETRO_DEVICE_ID_ANALOG_Y) {
		unsigned axis = p_index * 2 + p_id;
		return port_axes[p_port][axis];
	}
	return 0;
}

} // namespace godot
