#include <dlfcn.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <libretro.h>

typedef unsigned (*api_version_fn)(void);
typedef void (*set_environment_fn)(retro_environment_t);
typedef void (*set_video_fn)(retro_video_refresh_t);
typedef void (*set_audio_fn)(retro_audio_sample_t);
typedef void (*set_audio_batch_fn)(retro_audio_sample_batch_t);
typedef void (*set_input_poll_fn)(retro_input_poll_t);
typedef void (*set_input_state_fn)(retro_input_state_t);
typedef void (*lifecycle_fn)(void);
typedef void (*get_system_info_fn)(struct retro_system_info *);
typedef void (*get_system_av_info_fn)(struct retro_system_av_info *);
typedef bool (*load_game_fn)(const struct retro_game_info *);
typedef void (*run_fn)(void);

static unsigned video_frames;
static unsigned audio_frames;

static bool environment_callback(unsigned command, void *data) {
	(void)data;
	return command == RETRO_ENVIRONMENT_SET_PIXEL_FORMAT;
}

static void video_callback(const void *data, unsigned width, unsigned height, size_t pitch) {
	(void)pitch;
	if (data && width && height) {
		video_frames++;
	}
}

static void audio_callback(int16_t left, int16_t right) {
	(void)left;
	(void)right;
	audio_frames++;
}

static size_t audio_batch_callback(const int16_t *data, size_t frames) {
	(void)data;
	audio_frames += (unsigned)frames;
	return frames;
}

static void input_poll_callback(void) {}

static int16_t input_state_callback(unsigned port, unsigned device, unsigned index, unsigned id) {
	(void)port;
	(void)device;
	(void)index;
	(void)id;
	return 0;
}

#define LOAD_FN(handle, name, type) \
	type name = (type)dlsym(handle, #name); \
	if (!(name)) { \
		fprintf(stderr, "Missing Libretro symbol: %s\n", #name); \
		return 2; \
	}

int main(int argc, char **argv) {
	/* --symbols CORE.so: only resolve what the host needs from the core. This is
	 * the check that catches a stripped binary that lost its dynamic symbol
	 * table, and it works for every core, including the ones that need more
	 * environment support than this probe implements (Gambatte, N64). */
	int symbols_only = argc == 3 && strcmp(argv[1], "--symbols") == 0;
	if (!symbols_only && argc != 3) {
		fprintf(stderr, "Usage: %s CORE.so GAME\n       %s --symbols CORE.so\n", argv[0], argv[0]);
		return 2;
	}
	const char *core_path = symbols_only ? argv[2] : argv[1];
	const char *game_path = argv[2];

	void *handle = dlopen(core_path, RTLD_NOW | RTLD_LOCAL);
	if (!handle) {
		fprintf(stderr, "%s\n", dlerror());
		return 2;
	}

	LOAD_FN(handle, retro_api_version, api_version_fn);
	LOAD_FN(handle, retro_set_environment, set_environment_fn);
	LOAD_FN(handle, retro_set_video_refresh, set_video_fn);
	LOAD_FN(handle, retro_set_audio_sample, set_audio_fn);
	LOAD_FN(handle, retro_set_audio_sample_batch, set_audio_batch_fn);
	LOAD_FN(handle, retro_set_input_poll, set_input_poll_fn);
	LOAD_FN(handle, retro_set_input_state, set_input_state_fn);
	LOAD_FN(handle, retro_init, lifecycle_fn);
	LOAD_FN(handle, retro_deinit, lifecycle_fn);
	LOAD_FN(handle, retro_get_system_info, get_system_info_fn);
	LOAD_FN(handle, retro_get_system_av_info, get_system_av_info_fn);
	LOAD_FN(handle, retro_load_game, load_game_fn);
	LOAD_FN(handle, retro_run, run_fn);
	LOAD_FN(handle, retro_unload_game, lifecycle_fn);

	if (symbols_only) {
		struct retro_system_info info = {0};
		retro_get_system_info(&info);
		printf("%s %s: every Libretro symbol the host resolves is present (extensions %s)\n",
				info.library_name, info.library_version,
				info.valid_extensions ? info.valid_extensions : "-");
		dlclose(handle);
		return 0;
	}

	FILE *rom_file = fopen(game_path, "rb");
	if (!rom_file) {
		perror("Could not open test ROM");
		return 2;
	}
	fseek(rom_file, 0, SEEK_END);
	long rom_size = ftell(rom_file);
	rewind(rom_file);
	void *rom_data = malloc((size_t)rom_size);
	if (!rom_data || fread(rom_data, 1, (size_t)rom_size, rom_file) != (size_t)rom_size) {
		fprintf(stderr, "Could not read test ROM.\n");
		return 2;
	}
	fclose(rom_file);

	if (retro_api_version() != RETRO_API_VERSION) {
		fprintf(stderr, "Unexpected Libretro API version.\n");
		return 1;
	}
	retro_set_environment(environment_callback);
	retro_set_video_refresh(video_callback);
	retro_set_audio_sample(audio_callback);
	retro_set_audio_sample_batch(audio_batch_callback);
	retro_set_input_poll(input_poll_callback);
	retro_set_input_state(input_state_callback);
	retro_init();

	struct retro_system_info info = {0};
	retro_get_system_info(&info);
	struct retro_game_info game = {0};
	game.path = argv[2];
	game.data = rom_data;
	game.size = (size_t)rom_size;
	if (!retro_load_game(&game)) {
		fprintf(stderr, "Core rejected the test ROM.\n");
		return 1;
	}

	struct retro_system_av_info av_info = {0};
	retro_get_system_av_info(&av_info);
	for (unsigned frame = 0; frame < 120; frame++) {
		retro_run();
	}
	printf("%s %s: %ux%u @ %.2f FPS, %.0f Hz audio; callbacks: %u video frames, %u audio frames\n",
			info.library_name, info.library_version,
			av_info.geometry.base_width, av_info.geometry.base_height,
			av_info.timing.fps, av_info.timing.sample_rate,
			video_frames, audio_frames);

	retro_unload_game();
	retro_deinit();
	free(rom_data);
	dlclose(handle);
	return video_frames >= 120 ? 0 : 1;
}
