
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
  async createMemory(memoryData) {
    return api.post(BASE_PATH, memoryData)
  },

  // Update an existing memory
  async updateMemory(memoryId, updates) {
    return api.patch(`${BASE_PATH}/${memoryId}`, updates)
  },

  // Delete a memory
  async deleteMemory(memoryId) {
    return api.delete(`${BASE_PATH}/${memoryId}`)
  },
}
