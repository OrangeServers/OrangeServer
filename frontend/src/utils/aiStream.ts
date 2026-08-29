import { t } from '@/i18n'
import type { AiSseEvent } from '@/types/ai'

function getCookie(name: string): string {
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const match = document.cookie.match(new RegExp(`(?:^|;\\s*)${escaped}=([^;]*)`))
  return match ? decodeURIComponent(match[1]) : ''
}

function csrfHeaders(): Record<string, string> {
  const token = getCookie('csrf_token')
  return token ? { 'X-CSRF-Token': token } : {}
}

async function responseError(response: Response): Promise<Error> {
  let message = t('common.http.requestFailed', { status: response.status })
  try {
    const payload = await response.json() as { msg?: string; message?: string; error?: string }
    message = payload.msg || payload.message || payload.error || message
  } catch {
    const text = await response.text().catch(() => '')
    if (text.trim()) message = text.trim()
  }
  const error = new Error(message)
  Object.assign(error, { status: response.status })
  return error
}

/** 解析单个 SSE 帧块（id:/event:/data:）；供 POST 与 GET SSE 消费共用。 */
export function parseEventBlock(block: string): AiSseEvent | null {
  let eventType = ''
  let eventId = ''
  const dataLines: string[] = []

  for (const rawLine of block.split(/\r?\n/)) {
    const line = rawLine.trimEnd()
    if (!line || line.startsWith(':')) continue
    if (line.startsWith('event:')) eventType = line.slice(6).trim()
    else if (line.startsWith('id:')) eventId = line.slice(3).trim()
    else if (line.startsWith('data:')) dataLines.push(line.slice(5).trimStart())
  }

  if (!dataLines.length) return null
  const rawData = dataLines.join('\n')
  if (rawData === '[DONE]') return { type: 'run.completed', data: {}, id: eventId || undefined }

  let data: Record<string, unknown>
  try {
    const parsed = JSON.parse(rawData) as unknown
    data = parsed && typeof parsed === 'object'
      ? parsed as Record<string, unknown>
      : { content: parsed }
  } catch {
    data = { content: rawData }
  }

  const type = eventType || String(data.type || data.event || 'message')
  return { type, data, id: eventId || undefined }
}

export interface AiStreamOptions {
  signal?: AbortSignal
  onEvent: (event: AiSseEvent) => void | Promise<void>
}

/**
 * 使用 fetch 消费 POST SSE。
 * EventSource 不支持 POST，本函数保留 HttpOnly 会话 Cookie，并与现有 axios
 * 客户端使用同一 csrf_token Cookie 约定。
 */
export async function postAiStream(
  url: string,
  body: Record<string, unknown>,
  options: AiStreamOptions,
): Promise<void> {
  const response = await fetch(url, {
    method: 'POST',
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      Accept: 'text/event-stream',
      ...csrfHeaders(),
    },
    body: JSON.stringify(body),
    signal: options.signal,
  })

  if (response.status === 401) {
    window.location.href = '/login'
    throw new Error(t('common.http.sessionExpired'))
  }
  if (!response.ok) throw await responseError(response)
  if (!response.body) throw new Error(t('common.http.noStream'))

  const reader = response.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''

  while (true) {
    const { done, value } = await reader.read()
    buffer += decoder.decode(value, { stream: !done })
    const blocks = buffer.split(/\r?\n\r?\n/)
    buffer = blocks.pop() || ''
    for (const block of blocks) {
      const event = parseEventBlock(block)
      if (event) await options.onEvent(event)
    }
    if (done) break
  }

  if (buffer.trim()) {
    const event = parseEventBlock(buffer)
    if (event) await options.onEvent(event)
  }
}

export async function aiJsonRequest<T>(
  url: string,
  options: {
    method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE'
    body?: Record<string, unknown>
    signal?: AbortSignal
  } = {},
): Promise<T> {
  const method = options.method || 'GET'
  const response = await fetch(url, {
    method,
    credentials: 'include',
    headers: {
      Accept: 'application/json',
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      ...(method === 'POST' || method === 'PUT' || method === 'PATCH' || method === 'DELETE' ? csrfHeaders() : {}),
    },
    body: options.body ? JSON.stringify(options.body) : undefined,
    signal: options.signal,
  })

  if (response.status === 401) {
    window.location.href = '/login'
    throw new Error(t('common.http.sessionExpired'))
  }
  if (!response.ok) throw await responseError(response)
  return await response.json() as T
}
