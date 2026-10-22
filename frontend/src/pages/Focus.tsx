import { useEffect, useState } from 'react'
import { BellOff, Save, Star } from 'lucide-react'

import { Button, Card, CardTitle, ErrorState, PageHeader, Spinner } from '../components/ui'
import { TagInput } from '../components/TagInput'
import { toast } from '../components/Toast'
import { useFocus, useSetFocus } from '../hooks/queries'
import { copy as t } from '../lib/copy'

export default function FocusPage() {
  const { data, isLoading, isError, error, refetch } = useFocus()
  const save = useSetFocus()

  const [track, setTrack] = useState<string[]>([])
  const [mute, setMute] = useState<string[]>([])
  const [dirty, setDirty] = useState(false)

  // 从服务端同步，除非用户有未保存的修改。
  useEffect(() => {
    if (data && !dirty) {
      setTrack(data.track)
      setMute(data.mute)
    }
  }, [data, dirty])

  if (!data) return isLoading ? <Spinner label={t.focus.loading} /> : isError
    ? <ErrorState message={(error as Error).message} onRetry={() => { void refetch() }} /> : null

  const update = (which: 'track' | 'mute', next: string[]) => {
    setDirty(true)
    if (which === 'track') setTrack(next)
    else setMute(next)
  }

  const onSave = () =>
    save.mutate(
      { track, mute },
      {
        onSuccess: () => {
          setDirty(false)
          toast.success(t.focus.saved)
        },
        onError: (e) => toast.error(String((e as Error).message)),
      },
    )

  return (
    <div className="space-y-6">
      {isError && <ErrorState message={`关注点更新失败，保留当前草稿：${(error as Error).message}`} onRetry={() => { void refetch() }} />}
      <PageHeader
        title={t.focus.title}
        actions={
          <Button onClick={onSave} loading={save.isPending} disabled={!dirty}>
            <Save className="h-4 w-4" /> {t.common.save}
          </Button>
        }
      />

      <div className="grid grid-cols-1 gap-6">
        <Card>
          <CardTitle>
            <span className="inline-flex items-center gap-2">
              <Star className="h-4 w-4 text-brand-amber" /> {t.focus.trackTitle}
            </span>
          </CardTitle>
          <p className="mb-3 text-sm text-ghost">
            {t.focus.trackDescPre}
            <span className="text-slate-200">{t.focus.trackDescEmph}</span>
            {t.focus.trackDescPost}
          </p>
          <TagInput tags={track} onChange={(n) => update('track', n)} placeholder={t.focus.trackPlaceholder} tone="cyan" />
        </Card>

        <Card>
          <CardTitle>
            <span className="inline-flex items-center gap-2">
              <BellOff className="h-4 w-4 text-brand-rose" /> {t.focus.muteTitle}
            </span>
          </CardTitle>
          <p className="mb-3 text-sm text-ghost">
            {t.focus.muteDescPre}
            <span className="text-slate-200">{t.focus.muteDescEmph}</span>
            {t.focus.muteDescPost}
          </p>
          <TagInput tags={mute} onChange={(n) => update('mute', n)} placeholder={t.focus.mutePlaceholder} tone="rose" />
        </Card>
      </div>
    </div>
  )
}
