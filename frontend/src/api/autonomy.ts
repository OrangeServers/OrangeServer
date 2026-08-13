// =============================================================================
// AI 自治任务 API（M1/S3）
// - JSON 端点复用 aiJsonRequest（credentials + csrf 约定与 axios 客户端一致）
// - GET SSE 用 fetch 消费：EventSource 无法携带会话 Cookie 语义之外的控制，
//   且 Last-Event-ID 是浏览器禁止手动设置的请求头，续传统一走 after_seq 参数
//   （后端两者同语义，Last-Event-ID 优先）。
// =============================================================================
import { aiJsonRequest, parseEventBlock } from '@/utils/aiStream'
import { t } from '@/i18n'
import type {
  AutonomyArtifact,
  AutonomyArtifactDetail,
  AutonomyCreateRunPayload,
  AutonomyDecisionPayload,
  AutonomyEnvelope,
  AutonomyEvent,
  AutonomyEvidence,
  AutonomyReadiness,
  AutonomyRun,
  AutonomySnapshot,
} from '@/types/autonomy'

const BASE = '/ai/autonomous-runs'

async function envelopeData<T>(promise: Promise<AutonomyEnvelope<T>>): Promise<T> {
  const envelope = await promise
  if (envelope.code !== 0) throw new Error(envelope.msg || t('common.http.unreachable'))
  return envelope.data as T
}

/** 功能与基础设施就绪度（不受 flag 阻断，user 角色也可读） */
export function getAutonomyStatus(): Promise<AutonomyReadiness> {
  return envelopeData(aiJsonRequest<AutonomyEnvelope<AutonomyReadiness>>('/ai/autonomy/status'))
}

/** 当前用户的 Run 列表（owner 隔离） */
export async function listAutonomyRuns(): Promise<AutonomyRun[]> {
  const data = await envelopeData(
    aiJsonRequest<AutonomyEnvelope<{ runs: AutonomyRun[] }>>(BASE),
  )
  return data.runs || []
}

/** 创建草稿 Run */
export function createAutonomyRun(payload: AutonomyCreateRunPayload): Promise<AutonomyRun> {
  return envelopeData(
    aiJsonRequest<AutonomyEnvelope<AutonomyRun>>(BASE, { method: 'POST', body: { ...payload } }),
  )
}

/** Run 权威快照：run + 有序步骤 + allowed_operations */
export function getAutonomySnapshot(runId: string): Promise<AutonomySnapshot> {
  return envelopeData(
    aiJsonRequest<AutonomyEnvelope<AutonomySnapshot>>(`${BASE}/${encodeURIComponent(runId)}`),
  )
}

/** 启动草稿 Run（服务端状态机校验） */
export function startAutonomyRun(runId: string): Promise<AutonomyRun> {
  return envelopeData(
    aiJsonRequest<AutonomyEnvelope<AutonomyRun>>(
      `${BASE}/${encodeURIComponent(runId)}/start`,
      { method: 'POST', body: {} },
    ),
  )
}

/** 请求取消：Worker 确认远端停止后 Run 才进入 cancelled */
export function cancelAutonomyRun(runId: string): Promise<AutonomyRun> {
  return envelopeData(
    aiJsonRequest<AutonomyEnvelope<AutonomyRun>>(
      `${BASE}/${encodeURIComponent(runId)}/cancel`,
      { method: 'POST', body: {} },
    ),
  )
}

/** 步骤决策：operation 必须来自快照的 allowed_operations */
export function decideAutonomyStep(
  runId: string,
  stepId: string,
  payload: AutonomyDecisionPayload,
): Promise<AutonomyRun> {
  return envelopeData(
    aiJsonRequest<AutonomyEnvelope<AutonomyRun>>(
      `${BASE}/${encodeURIComponent(runId)}/steps/${encodeURIComponent(stepId)}/decision`,
      { method: 'POST', body: { ...payload } },
    ),
  )
}

/** Artifact 元数据列表（不含正文） */
export async function listAutonomyArtifacts(runId: string): Promise<AutonomyArtifact[]> {
  const data = await envelopeData(
    aiJsonRequest<AutonomyEnvelope<{ artifacts: AutonomyArtifact[] }>>(
      `${BASE}/${encodeURIComponent(runId)}/artifacts`,
    ),
  )
  return data.artifacts || []
}

/** 单条 Artifact 解密正文（过期/跨 Run 为 404） */
export function getAutonomyArtifact(runId: string, artifactId: string): Promise<AutonomyArtifactDetail> {
  return envelopeData(
    aiJsonRequest<AutonomyEnvelope<AutonomyArtifactDetail>>(
      `${BASE}/${encodeURIComponent(runId)}/artifacts/${encodeURIComponent(artifactId)}`,
    ),
  )
}

/** Evidence 索引列表（不可信观察） */
export async function listAutonomyEvidence(runId: string): Promise<AutonomyEvidence[]> {
  const data = await envelopeData(
    aiJsonRequest<AutonomyEnvelope<{ evidence: AutonomyEvidence[] }>>(
      `${BASE}/${encodeURIComponent(runId)}/evidence`,
    ),
  )
  return data.evidence || []
}

// ---------------------------------------------------------------------------
// 可续传事件流
// ---------------------------------------------------------------------------

export interface AutonomyStreamHandlers {
  /** 按单调 sequence 交付一条业务事件（重连不重复：游标之后的事件） */
  onEvent: (event: AutonomyEvent) => void
  /** 服务端确认终态并追平游标后的最终权威快照；收到后应停止重连 */
  onTerminal: (snapshot: AutonomySnapshot) => void
  /** 服务端显式错误帧（run not found / stream interrupted） */
  onServerError: (reason: string) => void
}

/**
 * 消费 Run 的 GET SSE 事件流。
 * 流自然关闭（未终态到期关流）时本函数正常返回，调用方按当前游标重连；
 * 收到 terminal 帧后返回并调用 onTerminal，调用方不应重连。
 */
export async function streamAutonomyRun(
  runId: string,
  afterSeq: number,
  handlers: AutonomyStreamHandlers,
  signal: AbortSignal,
): Promise<void> {
  const url = `${BASE}/${encodeURIComponent(runId)}/stream?after_seq=${Math.max(0, Math.floor(afterSeq))}`
  const response = await fetch(url, {
    method: 'GET',
    credentials: 'include',
    headers: { Accept: 'text/event-stream' },
    signal,
  })

  if (response.status === 401) {
    window.location.href = '/login'
    throw new Error(t('common.http.sessionExpired'))
  }
  if (!response.ok) {
    let message = t('common.http.requestFailed', { status: response.status })
    try {
      const payload = await response.json() as { msg?: string }
      if (payload.msg) message = payload.msg
    } catch { /* 非 JSON 错误体保留默认文案 */ }
    throw new Error(message)
  }
  if (!response.body) throw new Error(t('common.http.noStream'))

  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  const dispatch = (block: string): void => {
    const frame = parseEventBlock(block)
    if (!frame) return
    if (frame.type === 'terminal') {
      handlers.onTerminal(frame.data as unknown as AutonomySnapshot)
      return
    }
    if (frame.type === 'error') {
      handlers.onServerError(String(frame.data.reason || 'stream interrupted'))
      return
    }
    handlers.onEvent({
      sequence: Number(frame.id || 0),
      event_type: frame.type,
      payload: frame.data,
      created_at: null,
    })
  }

  while (true) {
    const { done, value } = await reader.read()
    buffer += decoder.decode(value, { stream: !done })
    const blocks = buffer.split(/\r?\n\r?\n/)
    buffer = blocks.pop() || ''
    for (const block of blocks) dispatch(block)
    if (done) break
  }
  if (buffer.trim()) dispatch(buffer)
}
