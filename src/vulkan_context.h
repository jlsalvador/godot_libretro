#pragma once

#include <atomic>
#include <cstddef>
#include <cstdint>
#include <mutex>
#include <string>
#include <vector>

#include <libretro_vulkan.h>

namespace godot {

// Frontend-owned Vulkan device for libretro hardware rendering
// (ParaLLEl-RDP). The core creates its logical device on our instance
// through its negotiation interface; we answer the HW render interface
// (sync indices, queue lock, set_image) and copy presented frames back
// to CPU. Everything is optional: without a Vulkan loader all entry
// points fail cleanly and software cores are unaffected.
class VulkanContext {
public:
	VulkanContext() = default;
	~VulkanContext();

	VulkanContext(const VulkanContext &) = delete;
	VulkanContext &operator=(const VulkanContext &) = delete;

	// Creates the instance (once) and asks the core to create a device
	// on it through its negotiation interface. Idempotent per device.
	bool ensure_device(const retro_hw_render_context_negotiation_interface_vulkan *negotiation, std::string &r_error);
	bool has_device() const { return device != VK_NULL_HANDLE; }
	const retro_hw_render_interface_vulkan *render_interface() const { return has_device() ? &hw_interface : nullptr; }

	// Copies the last presented frame (see set_image) to tightly packed
	// R8G8B8A8. Width/height come from the video refresh callback.
	bool readback_frame(unsigned width, unsigned height, std::vector<uint8_t> &r_rgba, std::string &r_error);

	void shutdown();

private:
	static constexpr uint32_t SYNC_FRAMES = 3;

	void *loader_handle = nullptr;
	PFN_vkGetInstanceProcAddr vkGetInstanceProcAddr = nullptr;

	VkInstance instance = VK_NULL_HANDLE;
	VkPhysicalDevice physical_gpu = VK_NULL_HANDLE;
	VkDevice device = VK_NULL_HANDLE;
	VkQueue queue = VK_NULL_HANDLE;
	uint32_t queue_family_index = 0;
	retro_vulkan_context frontend_context = {};
	retro_hw_render_interface_vulkan hw_interface = {};

	std::mutex queue_mutex;
	std::atomic<uint32_t> sync_index = { 0 };

	std::mutex image_mutex;
	VkImage presented_image = VK_NULL_HANDLE;
	VkImageLayout presented_layout = VK_IMAGE_LAYOUT_UNDEFINED;
	VkFormat presented_format = VK_FORMAT_UNDEFINED;
	// Semaphores the core handed over with the presented image. Waiting on a
	// semaphore consumes it, so the list is taken exactly once per frame.
	std::vector<VkSemaphore> presented_semaphores;

	VkCommandPool command_pool = VK_NULL_HANDLE;
	VkCommandBuffer command_buffer = VK_NULL_HANDLE;
	VkFence copy_fence = VK_NULL_HANDLE;
	VkBuffer staging_buffer = VK_NULL_HANDLE;
	VkDeviceMemory staging_memory = VK_NULL_HANDLE;
	VkDeviceSize staging_size = 0;

	bool load_global_entry_points(std::string &r_error);
	bool create_frontend_instance(const retro_hw_render_context_negotiation_interface_vulkan *negotiation, std::string &r_error);
	bool load_device_entry_points(std::string &r_error);
	bool create_helpers(std::string &r_error);
	bool ensure_staging(VkDeviceSize size, std::string &r_error);
	void destroy_helpers();
	void clear_entry_points();

	static uint32_t get_sync_index_callback(void *handle);
	static uint32_t get_sync_index_mask_callback(void *handle);
	static void set_command_buffers_callback(void *handle, uint32_t num_cmd, const VkCommandBuffer *cmd);
	static void wait_sync_index_callback(void *handle);
	static void lock_queue_callback(void *handle);
	static void unlock_queue_callback(void *handle);
	static void set_image_callback(void *handle, const struct retro_vulkan_image *image, uint32_t num_semaphores, const VkSemaphore *semaphores, uint32_t src_queue_family);

	// Device-level entry points used here (loaded per device).
	PFN_vkDestroyInstance pfnDestroyInstance = nullptr;
	PFN_vkEnumeratePhysicalDevices pfnEnumeratePhysicalDevices = nullptr;
	PFN_vkGetPhysicalDeviceQueueFamilyProperties pfnGetPhysicalDeviceQueueFamilyProperties = nullptr;
	PFN_vkGetPhysicalDeviceMemoryProperties pfnGetPhysicalDeviceMemoryProperties = nullptr;
	PFN_vkGetDeviceProcAddr pfnGetDeviceProcAddr = nullptr;
	PFN_vkCreateCommandPool pfnCreateCommandPool = nullptr;
	PFN_vkDestroyCommandPool pfnDestroyCommandPool = nullptr;
	PFN_vkAllocateCommandBuffers pfnAllocateCommandBuffers = nullptr;
	PFN_vkBeginCommandBuffer pfnBeginCommandBuffer = nullptr;
	PFN_vkEndCommandBuffer pfnEndCommandBuffer = nullptr;
	PFN_vkCmdPipelineBarrier pfnCmdPipelineBarrier = nullptr;
	PFN_vkCmdCopyImageToBuffer pfnCmdCopyImageToBuffer = nullptr;
	PFN_vkCreateBuffer pfnCreateBuffer = nullptr;
	PFN_vkDestroyBuffer pfnDestroyBuffer = nullptr;
	PFN_vkGetBufferMemoryRequirements pfnGetBufferMemoryRequirements = nullptr;
	PFN_vkAllocateMemory pfnAllocateMemory = nullptr;
	PFN_vkFreeMemory pfnFreeMemory = nullptr;
	PFN_vkBindBufferMemory pfnBindBufferMemory = nullptr;
	PFN_vkMapMemory pfnMapMemory = nullptr;
	PFN_vkUnmapMemory pfnUnmapMemory = nullptr;
	PFN_vkCreateFence pfnCreateFence = nullptr;
	PFN_vkDestroyFence pfnDestroyFence = nullptr;
	PFN_vkWaitForFences pfnWaitForFences = nullptr;
	PFN_vkResetFences pfnResetFences = nullptr;
	PFN_vkQueueSubmit pfnQueueSubmit = nullptr;
	PFN_vkQueueWaitIdle pfnQueueWaitIdle = nullptr;
};

} // namespace godot
