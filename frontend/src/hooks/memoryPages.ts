import { hashKey, useInfiniteQuery, type InfiniteData } from '@tanstack/react-query'

import { api, MemoryRevisionChangedError, type MemoriesQuery } from '../lib/api'
import { useAuth } from '../store/auth'
import type { MemoryDump } from '../types'

type BrowseQuery = Omit<MemoriesQuery, 'facts_offset' | 'episodes_offset' | 'expected_revision' | 'evaluated_at'>
type Position = { offset: number; revision?: string; evaluatedAt?: number }
const firstPosition: Position = { offset: 0 }

export class PaginationProtocolError extends Error {}

function collectionOf(params: BrowseQuery) {
  return params.episodes_limit && !params.facts_limit ? 'episodes' : 'facts'
}

function scopeOf(params: BrowseQuery) {
  const { q: _q, ...scope } = params
  return hashKey([{ ...scope, include_sensitive: params.include_sensitive ?? false }])
}

// One cache entry owns all pages of a query. A position can never become a different filter's page.
export function memoryPagesOptions(namespace: string | null, params: BrowseQuery) {
  const collection = collectionOf(params)
  return {
    queryKey: ['memories', namespace, 'pages', params] as const,
    enabled: !!namespace,
    retry: false as const,
    initialPageParam: firstPosition,
    queryFn: async ({ pageParam, signal }: { pageParam: Position; signal: AbortSignal }) => {
      const result = await api.memories({ ...params, [`${collection}_offset`]: pageParam.offset,
        expected_revision: pageParam.revision, evaluated_at: pageParam.evaluatedAt }, { signal })
      const page = result[`${collection}_page`]
      if (!result.data_revision || !Number.isFinite(result.evaluated_at) || result.evaluated_at < 0
        || (pageParam.evaluatedAt !== undefined && result.evaluated_at !== pageParam.evaluatedAt)
        || !page || page.offset !== pageParam.offset
        || (page.has_more && (!page.items.length || page.next_offset === null || page.next_offset <= page.offset))
        || (!page.has_more && page.next_offset !== null)) {
        throw new PaginationProtocolError('分页响应无效，请重新加载')
      }
      if (pageParam.revision && result.data_revision !== pageParam.revision) throw new MemoryRevisionChangedError()
      return result
    },
    getNextPageParam: (last: MemoryDump) => {
      const page = last[`${collection}_page`]!
      return page.has_more ? { offset: page.next_offset!, revision: last.data_revision, evaluatedAt: last.evaluated_at } : undefined
    },
    placeholderData: (previous: InfiniteData<MemoryDump, Position> | undefined, previousQuery: { queryKey: readonly unknown[] } | undefined) => {
      const previousParams = previousQuery?.queryKey[3] as BrowseQuery | undefined
      return previousQuery?.queryKey[1] === namespace && previousParams
        && scopeOf(previousParams) === scopeOf(params) ? previous : undefined
    },
  }
}

export function mergeMemoryPages(data: InfiniteData<MemoryDump> | undefined): MemoryDump | undefined {
  if (!data?.pages.length) return undefined
  const last = data.pages[data.pages.length - 1]
  const unique = <T extends { id: string }>(rows: T[]) => [...new Map(rows.map(row => [row.id, row])).values()]
  return { ...last, facts: unique(data.pages.flatMap(page => page.facts)),
    episodes: unique(data.pages.flatMap(page => page.episodes)) }
}

export function useMemoryPages(params: BrowseQuery) {
  const namespace = useAuth(s => s.apiKey)
  const query = useInfiniteQuery(memoryPagesOptions(namespace, params))
  const revisionChanged = query.error instanceof MemoryRevisionChangedError
  const protocolError = query.error instanceof PaginationProtocolError
  const blocked = query.isFetching || query.isPlaceholderData || revisionChanged || protocolError
  // A refetch rebuilds all loaded pages and commits them together; failed refreshes keep editors mounted.
  const reload = () => query.refetch({ cancelRefetch: true })
  const loadMore = () => blocked ? Promise.resolve() : query.fetchNextPage({ cancelRefetch: false })
  const refetch = () => query.isFetchNextPageError && !revisionChanged && !protocolError
    ? query.fetchNextPage({ cancelRefetch: false }) : reload()
  return { ...query, data: mergeMemoryPages(query.data), revisionChanged, reload, refetch, loadMore,
    hasMore: Boolean(query.hasNextPage && !revisionChanged && !protocolError),
    canLoadMore: Boolean(query.hasNextPage && !blocked && !query.error) }
}
