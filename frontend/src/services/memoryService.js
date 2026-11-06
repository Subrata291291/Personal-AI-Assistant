
import { api } from '../lib/api.js'

const BASE_PATH = '/api/v1/memories'

export const memoryService = {
  // Get all memories with optional filters and pagination
  async getMemories({
    memoryType,
    limit = 50,
    offset = 0,
  } = {}) {
    const params = new URLSearchParams({
      limit: String(limit),
      offset: String(offset),
    })

    if (memoryType) {
      params.set('memory_type', memoryType)
    }

    return api.get(`${BASE_PATH}?${params.toString()}`)
  },

  // Get a single memory
  async getMemory(memoryId) {
    return api.get(`${BASE_PATH}/${memoryId}`)
  },

  // Create a new memory
  async createMemory(memoryData, authContext) {
    return api.post(BASE_PATH, memoryData, {}, authContext)
  },

  // Update an existing memory
  async updateMemory(memoryId, updates, authContext) {
    return api.patch(`${BASE_PATH}/${memoryId}`, updates, authContext)
  },

  // Delete a memory
  async deleteMemory(memoryId, authContext) {
    return api.delete(`${BASE_PATH}/${memoryId}`, authContext)
  },
}
