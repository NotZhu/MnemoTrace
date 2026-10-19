import { useState } from 'react'

import { ErrorState, PageHeader, Spinner } from '../components/ui'
import { PageEmptyState } from '../components/PageEmptyState'
import { Button } from '../components/ui'
import { Timeline } from '../components/Timeline'
import { useMemories } from '../hooks/queries'
import { copy as t } from '../lib/copy'

export default function TimelinePage() {
  const [limit, setLimit] = useState(60)
  const { data, isLoading, isError, error } = useMemories({ facts_limit: limit, episodes_limit: 0 })

  if (isLoading) return <Spinner label={t.timeline.loading} />
  if (isError) return <ErrorState message={(error as Error).message} />
  if (!data) return null

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <PageHeader title={t.timeline.title} />

      <div className="flex flex-1 flex-col">
        {data.facts.length ? (
          <>
            <Timeline facts={data.facts} />
            {data.facts_page?.has_more && (
              <div className="mt-4 flex justify-center">
                <Button variant="ghost" onClick={() => setLimit((n) => n + 60)}>
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
