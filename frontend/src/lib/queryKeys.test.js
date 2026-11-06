import { QueryClient } from '@tanstack/react-query'
import { describe, expect, it } from 'vitest'

import { memoryReviewQueryKey } from './queryKeys'

describe('memory review query keys', () => {
  it('separates cached results with different page sizes', async () => {
    const queryClient = new QueryClient()
    const dashboardKey = memoryReviewQueryKey('user-1', 100)
    const memoryCenterKey = memoryReviewQueryKey('user-1', 50)

    await queryClient.fetchQuery({
      queryKey: dashboardKey,
      queryFn: async () => ['dashboard-review'],
    })
    await queryClient.fetchQuery({
      queryKey: memoryCenterKey,
      queryFn: async () => ['memory-center-review'],
    })

    expect(dashboardKey).not.toEqual(memoryCenterKey)
    expect(queryClient.getQueryData(dashboardKey))
      .toEqual(['dashboard-review'])
    expect(queryClient.getQueryData(memoryCenterKey))
      .toEqual(['memory-center-review'])
  })
})
