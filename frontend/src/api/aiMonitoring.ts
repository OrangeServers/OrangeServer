import { aiJsonRequest } from '@/utils/aiStream'
import { t } from '@/i18n'
import type { AutonomyEnvelope } from '@/types/autonomy'
import type {
  AiMonitoringExternalRef,
  AiMonitoringCandidate,
  AiMonitoringMapping,
  AiMonitoringSource,
  AiMonitoringSourcePayload,
  AiMonitoringSourceUpdatePayload,
} from '@/types/ai'

const ADMIN_SOURCES = '/ai/admin/monitoring-sources'

type MonitoringEnvelope<T> = AutonomyEnvelope<T> & {
  sources?: AiMonitoringSource[]
  mappings?: AiMonitoringMapping[]
  source?: AiMonitoringSource
  mapping?: AiMonitoringMapping
}

function unwrap<T>(response: AutonomyEnvelope<T>, fallback: T): T {
  if (response.code !== 0) throw new Error(response.msg || t('common.http.unreachable'))
  return response.data ?? fallback
}

export async function listMonitoringSources(): Promise<AiMonitoringSource[]> {
  const response = await aiJsonRequest<MonitoringEnvelope<AiMonitoringSource[]>>(ADMIN_SOURCES)
  return unwrap(response, response.sources || [])
}

export function createMonitoringSource(payload: AiMonitoringSourcePayload): Promise<AiMonitoringSource> {
  return aiJsonRequest<MonitoringEnvelope<AiMonitoringSource>>(
    ADMIN_SOURCES,
    { method: 'POST', body: { ...payload } },
  ).then(response => unwrap(response, response.source as AiMonitoringSource))
}

export function updateMonitoringSource(
  sourceId: number,
  payload: AiMonitoringSourceUpdatePayload,
): Promise<AiMonitoringSource> {
  return aiJsonRequest<MonitoringEnvelope<AiMonitoringSource>>(
    `${ADMIN_SOURCES}/${encodeURIComponent(sourceId)}`,
    { method: 'PUT', body: { ...payload } },
  ).then(response => unwrap(response, response.source as AiMonitoringSource))
}

export function deleteMonitoringSource(sourceId: number): Promise<{ deleted: boolean }> {
  return aiJsonRequest<AutonomyEnvelope<{ deleted: boolean }>>(
    `${ADMIN_SOURCES}/${encodeURIComponent(sourceId)}`,
    { method: 'DELETE' },
  ).then(response => unwrap(response, { deleted: false }))
}

export function testMonitoringSource(sourceId: number): Promise<{ ok: boolean; version: string }> {
  return aiJsonRequest<AutonomyEnvelope<{ ok: boolean; version: string }>>(
    `${ADMIN_SOURCES}/${encodeURIComponent(sourceId)}/test`,
    { method: 'POST' },
  ).then(response => unwrap(response, { ok: false, version: '' }))
}

export function discoverMonitoringCandidates(
  sourceId: number,
  hostId: number,
): Promise<AiMonitoringCandidate[]> {
  return aiJsonRequest<AutonomyEnvelope<AiMonitoringCandidate[]>>(
    `${ADMIN_SOURCES}/${encodeURIComponent(sourceId)}/discover?host_id=${encodeURIComponent(hostId)}`,
  ).then(response => unwrap(response, []))
}

export async function listMonitoringMappings(hostId: number): Promise<AiMonitoringMapping[]> {
  const response = await aiJsonRequest<MonitoringEnvelope<AiMonitoringMapping[]>>(
    `/ai/admin/monitoring-mappings?host_id=${encodeURIComponent(hostId)}`,
  )
  return unwrap(response, response.mappings || [])
}

export function saveMonitoringMapping(
  sourceId: number,
  hostId: number,
  externalRef: AiMonitoringExternalRef,
): Promise<AiMonitoringMapping> {
  return aiJsonRequest<MonitoringEnvelope<AiMonitoringMapping>>(
    `${ADMIN_SOURCES}/${encodeURIComponent(sourceId)}/hosts/${encodeURIComponent(hostId)}`,
    { method: 'PUT', body: { external_ref: externalRef } },
  ).then(response => unwrap(response, response.mapping as AiMonitoringMapping))
}

export async function listMonitoringSourcesForHost(hostId: number): Promise<AiMonitoringSource[]> {
  const response = await aiJsonRequest<MonitoringEnvelope<AiMonitoringSource[]>>(
    `/ai/monitoring/sources?host_id=${encodeURIComponent(hostId)}`,
  )
  return unwrap(response, response.sources || [])
}
