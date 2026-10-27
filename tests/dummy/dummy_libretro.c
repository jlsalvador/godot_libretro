/* A minimal Libretro core that stands in for a real core in the tests: it
 * feeds one video frame and one audio batch per retro_run, keeps a counter
 * in SAVE_RAM, and makes the same optional environment requests a real
 * core makes (frame duping, system directory, Vulkan hardware rendering).
 * It reads the ROM from game.path (the conformant path-only mode) and
 * accepts any non-empty file, so the tests need no real ROM content. */

#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#include <libretro.h>
#include <libretro_vulkan.h>

#define VIDEO_WIDTH 320
#define VIDEO_HEIGHT 240
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
static uint8_t save_ram[SAVE_SIZE];
static uint64_t run_frames;
static bool vulkan_requested;

/* Vulkan state: the extension (frontend) creates the instance and asks
 * this core for a logical device through its negotiation interface; the
 * core never renders through it, it only proves the device path works. */
static PFN_vkDestroyDevice pfn_destroy_device;

static void log_info(const char *message) {
  if (log_cb.log)
    log_cb.log(RETRO_LOG_INFO, "%s", message);
}

static bool dummy_create_device(struct retro_vulkan_context *ctx,
                               VkInstance instance, VkPhysicalDevice gpu,
                               VkSurfaceKHR surface,
                               PFN_vkGetInstanceProcAddr gipa,
                               const char **required_extensions,
                               unsigned num_required_extensions,
                               const char **required_layers,
                               unsigned num_required_layers,
                               const VkPhysicalDeviceFeatures *required_features) {
  (void)surface;
  (void)required_extensions;
  (void)num_required_extensions;
  (void)required_layers;
  (void)num_required_layers;
  (void)required_features;

  PFN_vkCreateDevice pfn_create_device;
  PFN_vkGetDeviceQueue pfn_get_device_queue;
  PFN_vkGetPhysicalDeviceQueueFamilyProperties pfn_get_families;
  pfn_create_device =
      (PFN_vkCreateDevice)gipa(instance, "vkCreateDevice");
  pfn_destroy_device =
      (PFN_vkDestroyDevice)gipa(instance, "vkDestroyDevice");
  pfn_get_device_queue =
      (PFN_vkGetDeviceQueue)gipa(instance, "vkGetDeviceQueue");
  pfn_get_families =
      (PFN_vkGetPhysicalDeviceQueueFamilyProperties)gipa(
          instance, "vkGetPhysicalDeviceQueueFamilyProperties");
  if (!pfn_create_device || !pfn_destroy_device ||
      !pfn_get_device_queue || !pfn_get_families) {
    log_info("dummy: missing entry point from gipa");
    return false;
  }

  uint32_t family_count = 0;
  pfn_get_families(gpu, &family_count, NULL);
  if (family_count == 0) {
    log_info("dummy: no queue families");
    return false;
  }

  VkQueueFamilyProperties *families =
      malloc(family_count * sizeof(*families));
  if (!families)
    return false;
  pfn_get_families(gpu, &family_count, families);
  uint32_t queue_family_index = 0;
  for (uint32_t i = 0; i < family_count; i++) {
    if ((families[i].queueFlags &
        (VK_QUEUE_GRAPHICS_BIT | VK_QUEUE_COMPUTE_BIT)) ==
        (VK_QUEUE_GRAPHICS_BIT | VK_QUEUE_COMPUTE_BIT)) {
      queue_family_index = i;
      break;
    }
  }
  free(families);

  VkDeviceQueueCreateInfo queue_info;
  memset(&queue_info, 0, sizeof(queue_info));
  queue_info.sType = VK_STRUCTURE_TYPE_DEVICE_QUEUE_CREATE_INFO;
  queue_info.flags = 0;
  queue_info.queueCount = 1;
  queue_info.queueFamilyIndex = queue_family_index;
  queue_info.pQueuePriorities = (const float[]){ 1.0f };

  VkDeviceCreateInfo device_info;
  memset(&device_info, 0, sizeof(device_info));
  device_info.sType = VK_STRUCTURE_TYPE_DEVICE_CREATE_INFO;
  device_info.flags = 0;
  device_info.queueCreateInfoCount = 1;
  device_info.pQueueCreateInfos = &queue_info;
  device_info.enabledExtensionCount = 0;
  device_info.ppEnabledLayerNames = NULL;
  device_info.enabledLayerCount = 0;
  device_info.pEnabledFeatures = NULL;

  ctx->gpu = gpu;
  ctx->device = VK_NULL_HANDLE;
  ctx->queue = VK_NULL_HANDLE;
  ctx->queue_family_index = queue_family_index;
  if (VK_SUCCESS != pfn_create_device(gpu, &device_info, NULL, &ctx->device)) {
    log_info("dummy: vkCreateDevice failed");
    return false;
  }
  pfn_get_device_queue(ctx->device, queue_family_index, 0, &ctx->queue);
  log_info("vulkan device acquired");
  return true;
}

static void dummy_context_reset(void) {
  const struct retro_hw_render_interface_vulkan *interface;
  if (environment(RETRO_ENVIRONMENT_GET_HW_RENDER_INTERFACE, &interface))
    log_info("vulkan render interface available");
}

static void dummy_context_destroy(void) {
  log_info("vulkan render context destroyed");
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

  uint64_t quirks = 0;
  if (environment(RETRO_ENVIRONMENT_SET_SERIALIZATION_QUIRKS, &quirks))
    log_info("serialization quirks acknowledged");
  static struct retro_hw_render_callback hw_cb;
  static struct retro_hw_render_context_negotiation_interface_vulkan
      negotiation;
  memset(&hw_cb, 0, sizeof(hw_cb));
  memset(&negotiation, 0, sizeof(negotiation));
  hw_cb.context_type = RETRO_HW_CONTEXT_VULKAN;
  hw_cb.context_reset = dummy_context_reset;
  hw_cb.context_destroy = dummy_context_destroy;
  negotiation.create_device = dummy_create_device;
  vulkan_requested =
      environment(
          RETRO_ENVIRONMENT_SET_HW_RENDER_CONTEXT_NEGOTIATION_INTERFACE,
          &negotiation) &&
      environment(RETRO_ENVIRONMENT_SET_HW_RENDER, &hw_cb);
}

void retro_deinit(void) {
  if (vulkan_requested)
    dummy_context_destroy();
  pfn_destroy_device = NULL;
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
  static uint32_t frame[VIDEO_WIDTH * VIDEO_HEIGHT];
  static int16_t audio[AUDIO_FRAMES_PER_RUN * 2];
  uint64_t counter;
  memcpy(&counter, save_ram, sizeof(counter));
  memset(frame, (unsigned)(counter & 0xFF), sizeof(frame));
  for (int i = 0; i < AUDIO_FRAMES_PER_RUN; i++) {
    audio[i * 2] = (int16_t)(i & 0x7FFF);
    audio[i * 2 + 1] = 0;
  }
  video_refresh(frame, VIDEO_WIDTH, VIDEO_HEIGHT, VIDEO_WIDTH);
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
