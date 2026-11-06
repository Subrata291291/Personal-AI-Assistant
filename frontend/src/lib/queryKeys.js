export function memoryReviewQueryPrefix(userId) {
  return ['memory-reviews', userId, 'pending']
}

export function memoryReviewQueryKey(userId, limit) {
  return [...memoryReviewQueryPrefix(userId), { limit }]
}
