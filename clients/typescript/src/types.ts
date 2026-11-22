import type { components } from './generated/openapi'

type Schemas = components['schemas']

export type SessionResponse = Schemas['SessionResponse']
export type RememberMessage = Schemas['RememberMessage']
export type RememberRequest = Schemas['RememberRequest']
export type OperationStatus = Schemas['OperationResponse']['status']
export type OperationError = Schemas['OperationError']
export type OperationResponse = Schemas['OperationResponse']
export type TerminalOperation = OperationResponse & { status: 'succeeded' | 'failed' }
export type RecallRequest = Schemas['RecallRequest']
export type RecallResponse = Schemas['RecallResponse']
export type RecallEvidence = Schemas['EvidenceResponse']
export type GraphOptions = Schemas['GraphOptions']
export type MessageResponse = Schemas['MessageResponse']

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
