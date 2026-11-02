
import { QueryClient } from '@tanstack/react-query'

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 2 * 60 * 1000, // 2 minutes
      gcTime: 30 * 60 * 1000, // 30 minutes

      refetchOnWindowFocus: false,
      refetchOnReconnect: true,
      refetchOnMount: true,

      retry: 1,
    },

    mutations: {
      retry: 0,
    },
  },
})
