export type AiProviderCode =
  | 'openai'
  | 'deepseek'
  | 'minimax'
  | 'kimi'
  | 'qwen'
  | 'glm'
  | 'siliconflow'
  | string

export const AI_CONTEXT_MODE_STANDARD = 'standard_256k' as const
export const AI_CONTEXT_MODE_DEEP = 'deep_diagnostic_1m' as const
export const AI_CONTEXT_TOKENS_STANDARD = 256 * 1024
export const AI_CONTEXT_TOKENS_DEEP = 1024 * 1024
export type AiContextMode =
  | typeof AI_CONTEXT_MODE_STANDARD
  | typeof AI_CONTEXT_MODE_DEEP

export interface AiProvider {
  provider_code: AiProviderCode
  name?: string
  model: string
  context_window_tokens?: number
  enabled?: boolean
  is_default?: boolean
  available?: boolean
  api_key_configured?: boolean
  reason?: 'disabled' | 'model_missing' | 'key_missing' | string
  unavailable_reason?: string
  disabled_reason?: string
}

export type AiMessageRole = 'user' | 'assistant' | 'system'

export interface AiChatMessage {
  id: string
  role: AiMessageRole
  content: string
  created_at?: string
  streaming?: boolean
  error?: boolean
}

export type AiToolEventStatus = 'running' | 'success' | 'error'

export type AiConversationAutonomyMode = 'ask' | 'ai_review' | 'auto' | 'custom'

export interface AiConversationAutonomyProfile {
  action_categories: string[]
}

export interface AiToolEvent {
  id: string
  tool: string
  label: string
  status: AiToolEventStatus
  summary?: string
  result_scope?: AiResultScope
  created_at?: string
}

/** 聊天侧创建的自治任务草稿引用卡（仅草稿，不能从聊天启动）。 */
export interface AiAutonomyDraft {
  id?: string
  run_id?: string
  goal?: string
  status?: string
  mode?: string
  action_categories?: string[]
  host_alias?: string
  created_at?: string
}

export interface AiConversation {
  id: string
  title: string
  provider_code?: AiProviderCode
  model?: string
  context_mode?: AiContextMode
  autonomy_mode?: AiConversationAutonomyMode
  autonomy_profile?: AiConversationAutonomyProfile | null
  updated_at?: string
  created_at?: string
}

export interface AiResultScope {
  result_set_id?: string
  title?: string
  total: number
  online?: number
  offline?: number
  groups?: string[]
  sample?: Array<Record<string, unknown>>
}

export interface AiConversationDetail extends AiConversation {
  messages?: AiChatMessage[]
  tool_events?: AiToolEvent[]
  autonomy_drafts?: AiAutonomyDraft[]
  result_scope?: AiResultScope | null
  diagnostics?: AiDiagnosticRun[]
  active_diagnostic?: AiDiagnosticRun | null
  latest_diagnostic?: AiDiagnosticRun | null
  provider_observability?: AiProviderObservability | null
}

export interface AiProviderObservability {
  usage?: {
    prompt_tokens?: number
    completion_tokens?: number
    total_tokens?: number
  }
  last_finish_reason?: string
  last_latency_ms?: number
  compression_count?: number
  truncation_reason?: string
  context_budget?: {
    context_window_tokens?: number
    output_reserve_tokens?: number
    safety_reserve_tokens?: number
    runtime_reserve_tokens?: number
    effective_input_tokens?: number
    estimated_input_tokens?: number
  }
}

/** 管理员配置的只读监控数据源。令牌只返回是否已配置，不返回密文。 */
export type AiMonitoringSourceType = 'prometheus' | 'grafana' | 'loki' | 'zabbix'
export type AiMonitoringDatasourceType = 'prometheus' | 'loki'

export interface AiMonitoringLabelsRef {
  labels: Record<string, string>
}

/** Grafana 复用已保存 Dashboard Panel 的 Prometheus/Loki 查询。 */
export interface AiMonitoringGrafanaRef {
  datasource_uid: string
  datasource_type: AiMonitoringDatasourceType
  dashboard_uid: string
  labels: Record<string, string>
}

export interface AiMonitoringZabbixRef {
  hostid: string
}

export type AiMonitoringExternalRef =
  | AiMonitoringLabelsRef
  | AiMonitoringGrafanaRef
  | AiMonitoringZabbixRef

export interface AiMonitoringSource {
  id: number
  name: string
  source_type: AiMonitoringSourceType
  base_url: string
  token_configured: boolean
  verify_tls: boolean
  enabled: boolean
  created_by?: string
}

export interface AiMonitoringSourcePayload {
  name: string
  source_type: AiMonitoringSourceType
  base_url: string
  token?: string
  verify_tls: boolean
  enabled: boolean
}

export type AiMonitoringSourceUpdatePayload = Omit<AiMonitoringSourcePayload, 'source_type'>

export interface AiMonitoringMapping {
  id: number
  source_id: number
  host_id: number
  external_ref: AiMonitoringExternalRef
  confirmed_by?: string
}

/** 后端候选发现结果；候选必须由管理员确认后才成为 mapping。 */
export interface AiMonitoringCandidate {
  label?: string
  match?: string
  source_name?: string
  datasource_uid?: string
  datasource_name?: string
  datasource_type?: AiMonitoringDatasourceType
  identity_label?: string
  identity_value?: string
  external_ref: AiMonitoringExternalRef
}

export type AiDiagnosticStatus =
  | 'queued'
  | 'running'
  | 'completed'
  | 'partial'
  | 'failed'
  | 'cancelled'
  | 'interrupted'
  | 'expired'

export type AiDiagnosticAssetStatus =
  | 'queued'
  | 'running'
  | 'completed'
  | 'failed'
  | 'skipped'

export interface AiDiagnosticProfile {
  id: string
  name: string
  description?: string
  category?: string
  target_type?: 'linux' | 'docker' | string
  probe_count?: number
  max_targets?: number
  parameters?: Array<{
    name: string
    label?: string
    type?: 'enum' | 'string' | 'integer' | string
    required?: boolean
    enum?: Array<string | number>
    default?: unknown
  }>
}

export interface AiDiagnosticAssetProgress {
  target_id?: string | number
  alias: string
  status: AiDiagnosticAssetStatus
  completed_probes?: number
  total_probes?: number
  finding_count?: number
  error?: string
}

export interface AiDiagnosticEvidence {
  id: string
  run_id?: string
  asset_alias?: string
  probe_id?: string
  title: string
  kind?: string
  content?: string
  collected_at?: string
  expires_at?: string
  truncated?: boolean
}

export interface AiDiagnosticFinding {
  id?: string
  title: string
  summary?: string
  severity?: 'info' | 'warning' | 'high' | 'critical' | string
  asset_alias?: string
  evidence_ids?: string[]
  recommendation?: string
}

export interface AiDiagnosticReport {
  run_id: string
  status?: AiDiagnosticStatus
  summary?: string
  severity?: string
  findings?: AiDiagnosticFinding[]
  evidence_insufficient?: boolean
  generated_at?: string
}

export interface AiDiagnosticRun {
  id: string
  conversation_id?: string
  profile_id?: string
  profile_name?: string
  status: AiDiagnosticStatus
  system_user?: {
    id?: number
    alias?: string
    is_privileged?: boolean
  }
  target_count?: number
  success_count?: number
  failed_count?: number
  started_at?: string
  completed_at?: string
  evidence_expires_at?: string
  latest_event_seq?: number
  parameters?: Record<string, unknown>
  asset_progress?: AiDiagnosticAssetProgress[]
  summary?: {
    severity?: string
    finding_count?: number
    evidence_count?: number
  }
  report?: AiDiagnosticReport | null
  created_at?: string
  updated_at?: string
  error?: string
}

export interface AiDiagnosticStartRequest {
  profile_id: string
  target_ids?: Array<string | number>
  result_set_id?: string
  system_user_id: number
  conversation_id?: string
  parameters?: Record<string, string | number | boolean>
}

export interface AiSseEvent<T = Record<string, unknown>> {
  type: string
  data: T
  id?: string
}

export interface AiApiResponse<T> {
  code?: number
  data?: T
  msg?: string
  [key: string]: unknown
}
