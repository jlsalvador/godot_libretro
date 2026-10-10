#include "vulkan_context.h"

#include <cstring>
#include <dlfcn.h>

namespace godot {

VulkanContext::~VulkanContext() {
	shutdown();
}

bool VulkanContext::load_global_entry_points(std::string &r_error) {
	if (vkGetInstanceProcAddr) {
		return true;
	}
	loader_handle = dlopen("libvulkan.so.1", RTLD_NOW | RTLD_LOCAL);
	if (!loader_handle) {
		r_error = "No Vulkan loader found.";
		return false;
	}
	vkGetInstanceProcAddr = reinterpret_cast<PFN_vkGetInstanceProcAddr>(dlsym(loader_handle, "vkGetInstanceProcAddr"));
	if (!vkGetInstanceProcAddr) {
		r_error = "Vulkan loader exports no vkGetInstanceProcAddr.";
		shutdown();
		return false;
	}
	return true;
}

bool VulkanContext::create_frontend_instance(const retro_hw_render_context_negotiation_interface_vulkan *negotiation, std::string &r_error) {
	if (instance != VK_NULL_HANDLE) {
		return true;
	}
	auto vkCreateInstance = reinterpret_cast<PFN_vkCreateInstance>(vkGetInstanceProcAddr(nullptr, "vkCreateInstance"));
	if (!vkCreateInstance) {
		r_error = "Vulkan loader misses vkCreateInstance.";
		return false;
	}
	VkApplicationInfo default_app = {};
	default_app.sType = VK_STRUCTURE_TYPE_APPLICATION_INFO;
	default_app.pApplicationName = "RoomVR";
	default_app.apiVersion = VK_API_VERSION_1_1;
	VkApplicationInfo app_info = default_app;
	if (negotiation && negotiation->get_application_info) {
		if (const VkApplicationInfo *core_app = negotiation->get_application_info()) {
			app_info = *core_app;
		}
	}
	VkInstanceCreateInfo create_info = {};
	create_info.sType = VK_STRUCTURE_TYPE_INSTANCE_CREATE_INFO;
	create_info.pApplicationInfo = &app_info;
	if (vkCreateInstance(&create_info, nullptr, &instance) != VK_SUCCESS || instance == VK_NULL_HANDLE) {
		r_error = "vkCreateInstance failed.";
		instance = VK_NULL_HANDLE;
		return false;
	}
	// Instance-level functions must be queried with the instance handle;
	// a null instance only yields the global commands.
#define LOAD_INSTANCE_FN(name) \
	pfn##name = reinterpret_cast<PFN_vk##name>(vkGetInstanceProcAddr(instance, "vk" #name)); \
	if (!pfn##name) { \
		r_error = "Vulkan loader misses instance entry point vk" #name "."; \
		return false; \
	}
	LOAD_INSTANCE_FN(DestroyInstance);
	LOAD_INSTANCE_FN(EnumeratePhysicalDevices);
	LOAD_INSTANCE_FN(GetPhysicalDeviceQueueFamilyProperties);
	LOAD_INSTANCE_FN(GetPhysicalDeviceMemoryProperties);
	LOAD_INSTANCE_FN(GetDeviceProcAddr);
#undef LOAD_INSTANCE_FN
	return true;
}

bool VulkanContext::load_device_entry_points(std::string &r_error) {
	if (!pfnGetDeviceProcAddr) {
		r_error = "Vulkan loader misses vkGetDeviceProcAddr.";
		return false;
	}
#define LOAD_DEVICE_FN(name) \
	pfn##name = reinterpret_cast<PFN_vk##name>(pfnGetDeviceProcAddr(device, "vk" #name)); \
	if (!pfn##name) { \
		r_error = "Device misses vk" #name "."; \
		return false; \
	}
	LOAD_DEVICE_FN(CreateCommandPool);
	LOAD_DEVICE_FN(DestroyCommandPool);
	LOAD_DEVICE_FN(AllocateCommandBuffers);
	LOAD_DEVICE_FN(BeginCommandBuffer);
	LOAD_DEVICE_FN(EndCommandBuffer);
	LOAD_DEVICE_FN(CmdPipelineBarrier);
	LOAD_DEVICE_FN(CmdCopyImageToBuffer);
	LOAD_DEVICE_FN(CreateBuffer);
	LOAD_DEVICE_FN(DestroyBuffer);
	LOAD_DEVICE_FN(GetBufferMemoryRequirements);
	LOAD_DEVICE_FN(AllocateMemory);
	LOAD_DEVICE_FN(FreeMemory);
	LOAD_DEVICE_FN(BindBufferMemory);
	LOAD_DEVICE_FN(MapMemory);
	LOAD_DEVICE_FN(UnmapMemory);
	LOAD_DEVICE_FN(CreateFence);
	LOAD_DEVICE_FN(DestroyFence);
	LOAD_DEVICE_FN(WaitForFences);
	LOAD_DEVICE_FN(ResetFences);
	LOAD_DEVICE_FN(QueueSubmit);
	LOAD_DEVICE_FN(QueueWaitIdle);
#undef LOAD_DEVICE_FN
	return true;
}

bool VulkanContext::create_helpers(std::string &r_error) {
	VkCommandPoolCreateInfo pool_info = {};
	pool_info.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO;
	pool_info.flags = VK_COMMAND_POOL_CREATE_TRANSIENT_BIT;
	pool_info.queueFamilyIndex = queue_family_index;
	if (pfnCreateCommandPool(device, &pool_info, nullptr, &command_pool) != VK_SUCCESS) {
		r_error = "Could not create Vulkan command pool.";
		return false;
	}
	VkCommandBufferAllocateInfo alloc_info = {};
	alloc_info.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;
	alloc_info.commandPool = command_pool;
	alloc_info.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
	alloc_info.commandBufferCount = 1;
	if (pfnAllocateCommandBuffers(device, &alloc_info, &command_buffer) != VK_SUCCESS) {
		r_error = "Could not allocate Vulkan command buffer.";
		return false;
	}
	VkFenceCreateInfo fence_info = {};
	fence_info.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;
	if (pfnCreateFence(device, &fence_info, nullptr, &copy_fence) != VK_SUCCESS) {
		r_error = "Could not create Vulkan copy fence.";
		return false;
	}
	return true;
}

void VulkanContext::destroy_helpers() {
	if (device == VK_NULL_HANDLE) {
		command_pool = VK_NULL_HANDLE;
		command_buffer = VK_NULL_HANDLE;
		copy_fence = VK_NULL_HANDLE;
		staging_buffer = VK_NULL_HANDLE;
		staging_memory = VK_NULL_HANDLE;
		staging_size = 0;
		return;
	}
	if (staging_buffer != VK_NULL_HANDLE) {
		pfnDestroyBuffer(device, staging_buffer, nullptr);
		staging_buffer = VK_NULL_HANDLE;
	}
	if (staging_memory != VK_NULL_HANDLE) {
		pfnFreeMemory(device, staging_memory, nullptr);
		staging_memory = VK_NULL_HANDLE;
	}
	staging_size = 0;
	if (copy_fence != VK_NULL_HANDLE) {
		pfnDestroyFence(device, copy_fence, nullptr);
		copy_fence = VK_NULL_HANDLE;
	}
	if (command_pool != VK_NULL_HANDLE) {
		pfnDestroyCommandPool(device, command_pool, nullptr);
		command_pool = VK_NULL_HANDLE;
		command_buffer = VK_NULL_HANDLE;
	}
}

bool VulkanContext::ensure_staging(VkDeviceSize size, std::string &r_error) {
	if (staging_buffer != VK_NULL_HANDLE && staging_size >= size) {
		return true;
	}
	destroy_helpers();
	if (!create_helpers(r_error)) {
		return false;
	}
	VkBufferCreateInfo buffer_info = {};
	buffer_info.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;
	buffer_info.size = size;
	buffer_info.usage = VK_BUFFER_USAGE_TRANSFER_DST_BIT;
	buffer_info.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
	if (pfnCreateBuffer(device, &buffer_info, nullptr, &staging_buffer) != VK_SUCCESS) {
		r_error = "Could not create Vulkan staging buffer.";
		return false;
	}
	VkMemoryRequirements requirements = {};
	pfnGetBufferMemoryRequirements(device, staging_buffer, &requirements);
	VkPhysicalDeviceMemoryProperties properties = {};
	pfnGetPhysicalDeviceMemoryProperties(physical_gpu, &properties);
	uint32_t memory_type = UINT32_MAX;
	for (uint32_t i = 0; i < properties.memoryTypeCount; i++) {
		if ((requirements.memoryTypeBits & (1U << i)) &&
				(properties.memoryTypes[i].propertyFlags & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT) &&
				(properties.memoryTypes[i].propertyFlags & VK_MEMORY_PROPERTY_HOST_COHERENT_BIT)) {
			memory_type = i;
			break;
		}
	}
	if (memory_type == UINT32_MAX) {
		r_error = "No host-visible coherent Vulkan memory type.";
		return false;
	}
	VkMemoryAllocateInfo alloc_info = {};
	alloc_info.sType = VK_STRUCTURE_TYPE_MEMORY_ALLOCATE_INFO;
	alloc_info.allocationSize = requirements.size;
	alloc_info.memoryTypeIndex = memory_type;
	if (pfnAllocateMemory(device, &alloc_info, nullptr, &staging_memory) != VK_SUCCESS) {
		r_error = "Could not allocate Vulkan staging memory.";
		return false;
	}
	if (pfnBindBufferMemory(device, staging_buffer, staging_memory, 0) != VK_SUCCESS) {
		r_error = "Could not bind Vulkan staging memory.";
		return false;
	}
	staging_size = size;
	return true;
}

bool VulkanContext::ensure_device(const retro_hw_render_context_negotiation_interface_vulkan *negotiation, std::string &r_error) {
	if (has_device()) {
		return true;
	}
	if (!negotiation || !negotiation->create_device) {
		r_error = "Core offers no Vulkan device negotiation.";
		return false;
	}
	if (!load_global_entry_points(r_error)) {
		return false;
	}
	if (!create_frontend_instance(negotiation, r_error)) {
		shutdown();
		return false;
	}
	// Pick a physical device with graphics+compute queues ourselves: the
	// core's device factory does not handle a null GPU. Ownership of the
	// created logical device stays with us (the frontend).
	uint32_t gpu_count = 0;
	pfnEnumeratePhysicalDevices(instance, &gpu_count, nullptr);
	if (gpu_count == 0) {
		r_error = "No Vulkan physical devices found.";
		shutdown();
		return false;
	}
	std::vector<VkPhysicalDevice> gpus(gpu_count);
	pfnEnumeratePhysicalDevices(instance, &gpu_count, gpus.data());
	VkPhysicalDevice chosen_gpu = VK_NULL_HANDLE;
	for (uint32_t i = 0; i < gpu_count && !chosen_gpu; i++) {
		uint32_t family_count = 0;
		pfnGetPhysicalDeviceQueueFamilyProperties(gpus[i], &family_count, nullptr);
		std::vector<VkQueueFamilyProperties> families(family_count);
		pfnGetPhysicalDeviceQueueFamilyProperties(gpus[i], &family_count, families.data());
		for (uint32_t f = 0; f < family_count; f++) {
			if ((families[f].queueFlags & (VK_QUEUE_GRAPHICS_BIT | VK_QUEUE_COMPUTE_BIT)) ==
					(VK_QUEUE_GRAPHICS_BIT | VK_QUEUE_COMPUTE_BIT) &&
				families[f].queueCount > 0) {
				chosen_gpu = gpus[i];
				break;
			}
		}
	}
	if (!chosen_gpu) {
		chosen_gpu = gpus[0];
	}
	frontend_context = {};
	// Granite dereferences the required-features pointer unconditionally.
	VkPhysicalDeviceFeatures no_required_features = {};
	if (!negotiation->create_device(&frontend_context, instance, chosen_gpu, VK_NULL_HANDLE,
			vkGetInstanceProcAddr, nullptr, 0, nullptr, 0, &no_required_features)) {
		r_error = "Core failed to create its Vulkan device.";
		shutdown();
		return false;
	}
	if (frontend_context.device == VK_NULL_HANDLE || frontend_context.queue == VK_NULL_HANDLE) {
		r_error = "Core negotiation returned no device or queue.";
		shutdown();
		return false;
	}
	device = frontend_context.device;
	queue = frontend_context.queue;
	queue_family_index = frontend_context.queue_family_index;
	physical_gpu = frontend_context.gpu;
	if (!load_device_entry_points(r_error)) {
		shutdown();
		return false;
	}
	if (!create_helpers(r_error)) {
		shutdown();
		return false;
	}
	std::memset(&hw_interface, 0, sizeof(hw_interface));
	hw_interface.interface_type = RETRO_HW_RENDER_INTERFACE_VULKAN;
	hw_interface.interface_version = RETRO_HW_RENDER_INTERFACE_VULKAN_VERSION;
	hw_interface.handle = this;
	hw_interface.instance = instance;
	hw_interface.gpu = physical_gpu;
	hw_interface.device = device;
	hw_interface.get_device_proc_addr = pfnGetDeviceProcAddr;
	hw_interface.get_instance_proc_addr = vkGetInstanceProcAddr;
	hw_interface.queue = queue;
	hw_interface.queue_index = queue_family_index;
	hw_interface.set_image = set_image_callback;
	hw_interface.get_sync_index = get_sync_index_callback;
	hw_interface.get_sync_index_mask = get_sync_index_mask_callback;
	hw_interface.set_command_buffers = set_command_buffers_callback;
	hw_interface.wait_sync_index = wait_sync_index_callback;
	hw_interface.lock_queue = lock_queue_callback;
	hw_interface.unlock_queue = unlock_queue_callback;
	return true;
}

bool VulkanContext::readback_frame(unsigned width, unsigned height, std::vector<uint8_t> &r_rgba, std::string &r_error) {
	if (!has_device() || width == 0 || height == 0) {
		return false;
	}
	VkImage image = VK_NULL_HANDLE;
	VkImageLayout layout = VK_IMAGE_LAYOUT_UNDEFINED;
	{
		std::lock_guard<std::mutex> lock(image_mutex);
		image = presented_image;
		layout = presented_layout;
	}
	if (image == VK_NULL_HANDLE || layout == VK_IMAGE_LAYOUT_UNDEFINED) {
		return false;
	}
	const VkDeviceSize byte_size = static_cast<VkDeviceSize>(width) * height * 4;
	std::lock_guard<std::mutex> lock(queue_mutex);
	if (!ensure_staging(byte_size, r_error)) {
		return false;
	}
	if (pfnResetFences(device, 1, &copy_fence) != VK_SUCCESS) {
		r_error = "Could not reset Vulkan copy fence.";
		return false;
	}
	VkCommandBufferBeginInfo begin_info = {};
	begin_info.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;
	begin_info.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
	if (pfnBeginCommandBuffer(command_buffer, &begin_info) != VK_SUCCESS) {
		r_error = "Could not begin Vulkan copy commands.";
		return false;
	}
	VkImageMemoryBarrier to_transfer = {};
	to_transfer.sType = VK_STRUCTURE_TYPE_IMAGE_MEMORY_BARRIER;
	to_transfer.srcAccessMask = VK_ACCESS_MEMORY_WRITE_BIT;
	to_transfer.dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
	to_transfer.oldLayout = layout;
	to_transfer.newLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
	to_transfer.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
	to_transfer.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
	to_transfer.image = image;
	to_transfer.subresourceRange.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
	to_transfer.subresourceRange.levelCount = 1;
	to_transfer.subresourceRange.layerCount = 1;
	pfnCmdPipelineBarrier(command_buffer, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT,
			0, 0, nullptr, 0, nullptr, 1, &to_transfer);
	VkBufferImageCopy copy = {};
	copy.imageSubresource.aspectMask = VK_IMAGE_ASPECT_COLOR_BIT;
	copy.imageSubresource.layerCount = 1;
	copy.imageExtent.width = width;
	copy.imageExtent.height = height;
	copy.imageExtent.depth = 1;
	pfnCmdCopyImageToBuffer(command_buffer, image, VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL, staging_buffer, 1, &copy);
	VkImageMemoryBarrier to_present = to_transfer;
	to_present.srcAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
	to_present.dstAccessMask = VK_ACCESS_MEMORY_READ_BIT | VK_ACCESS_MEMORY_WRITE_BIT;
	to_present.oldLayout = VK_IMAGE_LAYOUT_TRANSFER_SRC_OPTIMAL;
	to_present.newLayout = layout;
	pfnCmdPipelineBarrier(command_buffer, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_ALL_COMMANDS_BIT,
			0, 0, nullptr, 0, nullptr, 1, &to_present);
	if (pfnEndCommandBuffer(command_buffer) != VK_SUCCESS) {
		r_error = "Could not record Vulkan copy commands.";
		return false;
	}
	VkSubmitInfo submit_info = {};
	submit_info.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO;
	submit_info.commandBufferCount = 1;
	submit_info.pCommandBuffers = &command_buffer;
	if (pfnQueueSubmit(queue, 1, &submit_info, copy_fence) != VK_SUCCESS) {
		r_error = "Could not submit Vulkan copy commands.";
		return false;
	}
	if (pfnWaitForFences(device, 1, &copy_fence, VK_TRUE, 5000000000ULL) != VK_SUCCESS) {
		r_error = "Timed out waiting for Vulkan frame copy.";
		return false;
	}
	void *mapped = nullptr;
	if (pfnMapMemory(device, staging_memory, 0, byte_size, 0, &mapped) != VK_SUCCESS || !mapped) {
		r_error = "Could not map Vulkan staging memory.";
		return false;
	}
	r_rgba.resize(static_cast<size_t>(byte_size));
	std::memcpy(r_rgba.data(), mapped, static_cast<size_t>(byte_size));
	pfnUnmapMemory(device, staging_memory);
	return true;
}

void VulkanContext::shutdown() {
	destroy_helpers();
	if (device != VK_NULL_HANDLE && pfnDestroyInstance) {
		// The logical device was created for us by the core on our
		// instance; destroy it here. Instance outlives it below.
		auto vkDestroyDevice = reinterpret_cast<PFN_vkDestroyDevice>(
				pfnGetDeviceProcAddr ? pfnGetDeviceProcAddr(device, "vkDestroyDevice") : nullptr);
		if (vkDestroyDevice) {
			vkDestroyDevice(device, nullptr);
		}
		device = VK_NULL_HANDLE;
	}
	if (instance != VK_NULL_HANDLE && pfnDestroyInstance) {
		pfnDestroyInstance(instance, nullptr);
		instance = VK_NULL_HANDLE;
	}
	queue = VK_NULL_HANDLE;
	physical_gpu = VK_NULL_HANDLE;
	frontend_context = {};
	std::memset(&hw_interface, 0, sizeof(hw_interface));
	vkGetInstanceProcAddr = nullptr;
	pfnGetDeviceProcAddr = nullptr;
	if (loader_handle) {
		dlclose(loader_handle);
		loader_handle = nullptr;
	}
	{
		std::lock_guard<std::mutex> lock(image_mutex);
		presented_image = VK_NULL_HANDLE;
		presented_layout = VK_IMAGE_LAYOUT_UNDEFINED;
	}
}

uint32_t VulkanContext::get_sync_index_callback(void *handle) {
	auto *context = static_cast<VulkanContext *>(handle);
	return context ? context->sync_index.fetch_add(1) % SYNC_FRAMES : 0;
}

uint32_t VulkanContext::get_sync_index_mask_callback(void *handle) {
	(void)handle;
	return (1U << SYNC_FRAMES) - 1;
}

void VulkanContext::set_command_buffers_callback(void *handle, uint32_t num_cmd, const VkCommandBuffer *cmd) {
	// The core submits its own command buffers; nothing for us to merge.
	(void)handle;
	(void)num_cmd;
	(void)cmd;
}

void VulkanContext::wait_sync_index_callback(void *handle) {
	auto *context = static_cast<VulkanContext *>(handle);
	if (context && context->pfnQueueWaitIdle && context->queue != VK_NULL_HANDLE) {
		std::lock_guard<std::mutex> lock(context->queue_mutex);
		context->pfnQueueWaitIdle(context->queue);
	}
}

void VulkanContext::lock_queue_callback(void *handle) {
	auto *context = static_cast<VulkanContext *>(handle);
	if (context) {
		context->queue_mutex.lock();
	}
}

void VulkanContext::unlock_queue_callback(void *handle) {
	auto *context = static_cast<VulkanContext *>(handle);
	if (context) {
		context->queue_mutex.unlock();
	}
}

void VulkanContext::set_image_callback(void *handle, const struct retro_vulkan_image *image,
		uint32_t num_semaphores, const VkSemaphore *semaphores, uint32_t src_queue_family) {
	(void)num_semaphores;
	(void)semaphores;
	(void)src_queue_family;
	auto *context = static_cast<VulkanContext *>(handle);
	if (!context || !image) {
		return;
	}
	std::lock_guard<std::mutex> lock(context->image_mutex);
	// VkImageViewCreateInfo carries the source VkImage.
	context->presented_image = image->create_info.image;
	context->presented_layout = image->image_layout;
}

} // namespace godot
