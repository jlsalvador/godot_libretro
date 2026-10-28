/* A minimal Libretro core that stands in for a real core in the tests: it
 * feeds one video frame and one audio batch per retro_run, keeps a counter
 * in SAVE_RAM, alternates the two rumble motors, and makes the same optional
 * environment requests a real core makes (frame duping, system directory).
 * It reads the ROM from game.path (the conformant path-only mode) and
 * accepts any non-empty file, so the tests need no real ROM content. */

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#include <libretro.h>

#define VIDEO_WIDTH 320
#define VIDEO_HEIGHT 240
/* 0RGB1555, the format the core declares: 2 bytes per pixel, so the pitch
   Libretro asks for is in bytes, not in pixels. */
#define VIDEO_PITCH (VIDEO_WIDTH * sizeof(uint16_t))
#define VIDEO_FPS 60
#define AUDIO_RATE 48000
/* Audio frames fed per retro_run: AUDIO_RATE / VIDEO_FPS. */
#define AUDIO_FRAMES_PER_RUN 800
#define SAVE_SIZE 32
#define BIOS_FILE "sample.bios"
#define DUMMY_STATE_SIZE 16

static retro_environment_t environment;
static retro_video_refresh_t video_refresh;
static retro_audio_sample_t audio_sample;
static retro_audio_sample_batch_t audio_sample_batch;
static retro_input_poll_t input_poll;
static retro_input_state_t input_state;

static bool can_dupe;
static struct retro_log_callback log_cb;
static struct retro_rumble_interface rumble;
static uint8_t save_ram[SAVE_SIZE];
static uint64_t run_frames;


static void log_info(const char *message) {
  if (log_cb.log)
    log_cb.log(RETRO_LOG_INFO, "%s", message);
}


void retro_set_environment(retro_environment_t cb) {
  environment = cb;
}

void retro_set_video_refresh(retro_video_refresh_t cb) {
  video_refresh = cb;
}
void retro_set_audio_sample(retro_audio_sample_t cb) {
  audio_sample = cb;
}

void retro_set_audio_sample_batch(retro_audio_sample_batch_t cb) {
  audio_sample_batch = cb;
}

void retro_set_input_poll(retro_input_poll_t cb) {
  input_poll = cb;
}

void retro_set_input_state(retro_input_state_t cb) {
  input_state = cb;
}

void retro_init(void) {
  unsigned pixel_format = RETRO_PIXEL_FORMAT_0RGB1555;
  environment(RETRO_ENVIRONMENT_SET_PIXEL_FORMAT, &pixel_format);
  environment(RETRO_ENVIRONMENT_GET_CAN_DUPE, &can_dupe);
  environment(RETRO_ENVIRONMENT_GET_LOG_INTERFACE, &log_cb);
  if (environment(RETRO_ENVIRONMENT_GET_RUMBLE_INTERFACE, &rumble) &&
      rumble.set_rumble_state)
    log_info("rumble interface taken");
  uint64_t quirks = 0;
  if (environment(RETRO_ENVIRONMENT_SET_SERIALIZATION_QUIRKS, &quirks))
    log_info("serialization quirks acknowledged");
}

void retro_deinit(void) {
}

void retro_get_system_info(struct retro_system_info *info) {
  info->library_name = "Dummy";
  info->library_version = "1";
  info->need_fullpath = 1;
  info->valid_extensions = "dmy";
}

void retro_get_system_av_info(struct retro_system_av_info *info) {
  info->geometry.base_width = VIDEO_WIDTH;
  info->geometry.base_height = VIDEO_HEIGHT;
  info->geometry.max_width = VIDEO_WIDTH;
  info->geometry.max_height = VIDEO_HEIGHT;
  info->timing.fps = (double)VIDEO_FPS;
  info->timing.sample_rate = (double)AUDIO_RATE;
}

bool retro_load_game(const struct retro_game_info *game) {
  FILE *file = fopen(game->path, "rb");
  if (!file)
    return false;
  fclose(file);

  char *system_directory;
  if (environment(RETRO_ENVIRONMENT_GET_SYSTEM_DIRECTORY, &system_directory) &&
      system_directory) {
    char path[4096];
    snprintf(path, sizeof(path), "%s/%s", system_directory, BIOS_FILE);
    struct stat bios;
    if (0 == stat(path, &bios))
      log_info("found BIOS fixture in system directory");
  }
  return true;
}

void retro_unload_game(void) {
}

void retro_run(void) {
  static uint16_t frame[VIDEO_WIDTH * VIDEO_HEIGHT];
  static int16_t audio[AUDIO_FRAMES_PER_RUN * 2];
  uint64_t counter;
  memcpy(&counter, save_ram, sizeof(counter));
  /* Alternate the motors every frame, so the frontend has to report strong
     and weak independently: the counter parity makes the order checkable. */
  if (rumble.set_rumble_state) {
    const bool strong_on = (counter & 1u) != 0;
    rumble.set_rumble_state(0, RETRO_RUMBLE_STRONG, strong_on ? 0xFFFF : 0);
    rumble.set_rumble_state(0, RETRO_RUMBLE_WEAK, strong_on ? 0 : 0xFFFF);
  }
  /* The counter rides in the blue bits, so a test can see the frame change
     and a save-state round trip reproduce it. */
  const uint16_t color = (uint16_t)(0x7C00u | (counter & 0x1Fu));
  for (int i = 0; i < VIDEO_WIDTH * VIDEO_HEIGHT; i++)
    frame[i] = color;
  for (int i = 0; i < AUDIO_FRAMES_PER_RUN; i++) {
    audio[i * 2] = (int16_t)(i & 0x7FFF);
    audio[i * 2 + 1] = 0;
  }
  video_refresh(frame, VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_PITCH);
  audio_sample_batch(audio, AUDIO_FRAMES_PER_RUN);

  counter++;
  memcpy(save_ram, &counter, sizeof(counter));
  run_frames++;
}

void *retro_get_memory_data(unsigned id) {
  return id == RETRO_MEMORY_SAVE_RAM ? (void *)save_ram : NULL;
}

size_t retro_get_memory_size(unsigned id) {
  return id == RETRO_MEMORY_SAVE_RAM ? SAVE_SIZE : 0;
}


size_t retro_serialize_size(void) {
  return DUMMY_STATE_SIZE;
}

bool retro_serialize(void *data, size_t size) {
  if (!data || size < DUMMY_STATE_SIZE)
    return false;
  uint8_t *state = (uint8_t *)data;
  memcpy(state, save_ram, 8);
  memcpy(state + 8, &run_frames, 8);
  return true;
}

bool retro_unserialize(const void *data, size_t size) {
  if (!data || size < DUMMY_STATE_SIZE)
    return false;
  const uint8_t *state = (const uint8_t *)data;
  memcpy(save_ram, state, 8);
  memcpy(&run_frames, state + 8, 8);
  return true;
}
unsigned retro_api_version(void) {
  return RETRO_API_VERSION;
}
