import type { components } from './generated/openapi'

type Schemas = components['schemas']

export type SessionResponse = Schemas['SessionResponse']
export type RememberMessage = Schemas['RememberMessage']
export type RememberRequest = Schemas['RememberRequest']
export type OperationStatus = Schemas['OperationResponse']['status']
export type OperationError = Schemas['OperationError']
export type OperationResponse = Schemas['OperationResponse']
export type OperationSummary = Schemas['OperationSummary']
export type OperationPage = Schemas['OperationPage']
export type FactChange = Schemas['FactChangeResponse']
export type FactChangePage = Schemas['FactChangePage']
export type OperationFactChangePage = Schemas['OperationFactChangePage']
export type TerminalOperation = OperationResponse & { status: 'succeeded' | 'failed' }
export type RecallRequest = Schemas['RecallRequest']
export type RecallResponse = Schemas['RecallResponse']
export type RecallEvidence = Schemas['EvidenceResponse']
export type RecallFilter = Schemas['RecallFilterRequest']
export type FactLabel = NonNullable<RecallFilter['labels']>[number]
export type FactSource = Schemas['Source']
export type FactResponse = Schemas['FactResponse']
export type FactPage = Schemas['FactPage']
export type GraphOptions = Schemas['GraphOptions']
export type MessageResponse = Schemas['MessageResponse']
export type MessagePage = Schemas['MessagePage']

export type PageQuery = {
  limit?: number
  cursor?: string | null
}

export type OperationListQuery = PageQuery & {
  status?: OperationStatus | null
  session_id?: string | null
}

export type FactChangeListQuery = PageQuery & {
  include_sensitive?: boolean
}

export type FactListQuery = PageQuery & {
  state?: Schemas['FactState']
  as_of?: string | null
  include_sensitive?: boolean
  labels?: readonly FactLabel[]
  source?: FactSource | null
}

export interface OperationHandle {
  spaceId: string
  operationId: string
}

export interface RememberSubmission {
  handle: OperationHandle
  operation: OperationResponse
}

export interface RememberOptions {
  idempotencyKey: string
  submitTimeoutMs?: number
}

export interface WaitOperationOptions {
  timeoutMs?: number
}

export interface RememberAndWaitOptions extends RememberOptions, WaitOperationOptions {}
