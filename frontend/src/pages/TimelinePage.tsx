import { ErrorState, PageHeader, Spinner } from '../components/ui'
import { PageEmptyState } from '../components/PageEmptyState'
import { Button } from '../components/ui'
import { Timeline } from '../components/Timeline'
import { useMemoryPages } from '../hooks/queries'
import { copy as t } from '../lib/copy'

export default function TimelinePage() {
  const { data, isLoading, isError, error, refetch, loadMore, isFetching } = useMemoryPages({ facts_limit: 60, episodes_limit: 0 })

  if (isLoading) return <Spinner label={t.timeline.loading} />
  if (!data) return isError ? <ErrorState message={(error as Error).message} onRetry={() => { void refetch() }} /> : null

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <PageHeader title={t.timeline.title} />
      {isError && <ErrorState message={(error as Error).message} onRetry={() => { void refetch() }} />}

      <div className="flex flex-1 flex-col">
        {data.facts.length ? (
          <>
            <Timeline facts={data.facts} />
            {data.facts_page?.has_more && (
              <div className="mt-4 flex justify-center">
                <Button variant="ghost" disabled={isFetching || isError} onClick={() => { void loadMore() }}>
                  {t.common.loadMore}
                </Button>
              </div>
            )}
          </>
        ) : (
          <PageEmptyState title={t.timeline.emptyTitle} hint={t.timeline.emptyHint} />
        )}
      </div>
    </div>
  )
}
