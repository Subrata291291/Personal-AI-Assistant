import { api } from '../lib/api.js'

const BASE_PATH = '/api/v1/drive'

export const driveContractsService = {
  async getPickerConfig() {
    return api.get(`${BASE_PATH}/picker-config`)
  },

  async createPickerToken(connectionId) {
    const params = new URLSearchParams()
    if (connectionId) params.set('connection_id', connectionId)
    const path = params.toString()
      ? `${BASE_PATH}/picker-token?${params.toString()}`
      : `${BASE_PATH}/picker-token`
    return api.get(path)
  },

  async listSelectedFiles() {
    return api.get(`${BASE_PATH}/files`)
  },

  async registerSelectedFile(fileId, connectionId) {
    if (!fileId) throw new Error('A selected Drive file is required.')
    return api.post(`${BASE_PATH}/files`, {
      file_id: fileId,
      ...(connectionId ? { connection_id: connectionId } : {}),
    })
  },

  async removeSelectedFile(fileId) {
    if (!fileId) throw new Error('A selected Drive file is required.')
    return api.delete(`${BASE_PATH}/files/${encodeURIComponent(fileId)}`)
  },
}
