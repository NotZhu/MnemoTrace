import { useState, type FormEvent } from 'react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { api } from '../lib/api'
import { currentSession, isCurrentSession, useAuth } from '../store/auth'

export function SpacePicker() {
  const { spaceId, selectSpace, epoch } = useAuth()
  const [showCreate, setShowCreate] = useState(false)
  const [name, setName] = useState('')
  const spaces = useQuery({
    queryKey: ['spaces', epoch],
    queryFn: ({ signal }) => api.spaces(signal),
    retry: false,
  })
  const create = useMutation({
    mutationFn: ({ name }: { name: string; session: ReturnType<typeof currentSession> }) => api.createSpace(name),
    onSuccess: (space, { session }) => {
      if (!isCurrentSession(session)) return
      selectSpace(space.space_id)
    },
  })

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (name.trim()) create.mutate({ name: name.trim(), session: currentSession() })
  }

  return (
    <div className="relative flex items-center gap-2">
      <select aria-label="记忆空间" className="input max-w-48" value={spaceId ?? ''}
        disabled={!spaces.data?.length}
        onChange={(event) => selectSpace(event.target.value || null)}>
        <option value="">{spaces.isPending ? '读取空间中' : '选择记忆空间'}</option>
        {spaces.data?.map(space => <option key={space.space_id} value={space.space_id}>{space.name}</option>)}
      </select>
      <button type="button" className="rounded-lg border border-line px-2 py-1 text-sm"
        aria-expanded={showCreate} onClick={() => setShowCreate(value => !value)}>新建空间</button>
      {spaces.isError && <button type="button" onClick={() => void spaces.refetch()}>重试读取空间</button>}
      {showCreate && (
        <form onSubmit={submit} className="absolute left-0 top-full mt-2 w-60 space-y-2 rounded-lg border border-line bg-ink p-3">
          <label htmlFor="space-name" className="text-sm">空间名称</label>
          <input id="space-name" className="input" required maxLength={128} value={name}
            onChange={(event) => setName(event.target.value)} />
          <button type="submit" className="rounded-lg border border-line px-2 py-1 text-sm"
            disabled={create.isPending || !name.trim()}>创建并选择</button>
          {create.isError && <p role="alert" className="text-sm text-brand-rose">{create.error.message}</p>}
        </form>
      )}
    </div>
  )
}
