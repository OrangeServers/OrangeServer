// =============================================================================
// AI 自治任务（M1/S3）类型契约 —— 与后端 app/ai/autonomy 序列化形状一一对应。
// 后端是唯一权威状态源：前端类型只描述"服务端返回什么"，不做本地推断。
// =============================================================================

/** Run 状态（state.RunStatus 全集，含终态） */
export type AutonomyRunStatus =
  | 'draft'
  | 'queued'
  | 'running'
  | 'waiting_approval'
  | 'recovering'
  | 'needs_attention'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | 'expired'

/** Run 三态结论（仅终态 Run 可能有值） */
export type AutonomyRunOutcome = 'resolved' | 'not_resolved' | 'inconclusive'

/** 权限档案模式（CANONICAL_RUN_MODES；read_only/assisted/lab_autonomous 为历史兼容值） */
export type AutonomyRunMode = 'ask' | 'ai_review' | 'auto' | 'custom' | string

/** Step 类别：计划 / 动作 / 验证 */
export type AutonomyStepKind = 'plan' | 'action' | 'verification' | string

/** Step 状态（state.StepStatus 全集） */
export type AutonomyStepStatus =
  | 'proposed'
  | 'waiting_approval'
  | 'approved'
  | 'running'
  | 'succeeded'
  | 'failed'
  | 'skipped'
  | 'outcome_unknown'
  | 'cancelled'

/** custom 模式动作类别白名单 */
export const AUTONOMY_ACTION_CATEGORIES = [
  'file_read',
  'file_patch',
  'file_restore',
  'package_install',
  'shell',
  'systemd',
] as const
export type AutonomyActionCategory = (typeof AUTONOMY_ACTION_CATEGORIES)[number]

/** 执行预算（policy.Budget 六字段，服务端有默认值与硬上限） */
export interface AutonomyBudget {
  duration_seconds?: number
  max_loops?: number
  max_actions?: number
  command_timeout_seconds?: number
  step_output_bytes?: number
  run_artifact_bytes?: number
}

/** custom 模式的权限档案 */
export interface AutonomyCustomProfile {
  action_categories: string[]
}

export interface AutonomyConclusion {
  confirmed_facts: string[]
  impact_scope: string
  root_cause_hypothesis: string
  confidence: 'low' | 'medium' | 'high'
  unknowns: string[]
  recommended_actions: string[]
  final_status: AutonomyRunOutcome
  evidence_ids: string[]
}

/** Run 主体（repository._run_to_dict） */
export interface AutonomyRun {
  id: string
  owner: string
  goal: string
  host_id: number
  host_alias: string
  system_user_id: number
  system_user_alias: string
  mode: AutonomyRunMode
  custom_profile: AutonomyCustomProfile | null
  status: AutonomyRunStatus
  outcome: AutonomyRunOutcome | null
  conclusion: AutonomyConclusion | null
  trigger_type: 'manual' | 'chat' | 'alertmanager' | string
  trigger_ref: string | null
  trigger_summary: string
  revision: number
  graph_version: string
  budget: AutonomyBudget
  latest_event_seq: number
  cancel_requested: boolean
  started_at: string | null
  completed_at: string | null
  created_at: string | null
}

/** 计划步骤（repository._step_to_dict） */
export interface AutonomyStep {
  id: string
  run_id: string
  kind: AutonomyStepKind
  status: AutonomyStepStatus
  seq: number
  summary: string
  action_digest: string
  note: string
  created_at: string | null
  /** 服务端从已签名计划快照生成的逐项审批摘要。 */
  plan_actions?: string[]
}

/** Run 权威快照 = Run + 有序步骤 + 服务端允许的操作集合 */
export interface AutonomySnapshot extends AutonomyRun {
  steps: AutonomyStep[]
  /** 决策词汇只可能来自这里（当前为 approve/reject）；空数组 = 无待决策 */
  allowed_operations: string[]
}

/** 事件（repository._event_to_dict；sequence 单调递增，是 SSE 续传游标） */
export interface AutonomyEvent {
  sequence: number
  event_type: string
  payload: Record<string, unknown>
  created_at: string | null
}

/** Artifact 元数据（正文必须单条读取；expired 后正文不可取） */
export interface AutonomyArtifact {
  id: string
  run_id: string
  step_id: string | null
  kind: string
  title: string
  size_bytes: number
  truncated: boolean
  expired: boolean
  created_at: string | null
}

/** Artifact 详情 = 元数据 + 解密正文 */
export interface AutonomyArtifactDetail extends AutonomyArtifact {
  content: string
}

/** Evidence：不可信观察的有界索引（trusted 永远不代表结论凭据） */
export interface AutonomyEvidence {
  id: string
  run_id: string
  step_id: string | null
  kind: string
  summary: string
  artifact_ids: string[]
  trusted: boolean
  created_at: string | null
}

/** GET /ai/autonomy/status：功能与基础设施就绪度（只含布尔与 reason 码） */
export interface AutonomyReadiness {
  enabled: boolean
  configured: boolean
  checkpoint_ready: boolean
  worker_ready: boolean
  ready: boolean
  worker_pool: string
  worker_concurrency_configured: number | null
  worker_concurrency_observed: number | null
  reason:
    | 'ready'
    | 'feature_disabled'
    | 'redis_not_configured'
    | 'checkpoint_unavailable'
    | 'worker_unavailable'
    | string
}

/** GET /ai/ops/status：当前用户可见的 AIOps 聚合，只引用现有 Run。 */
export interface AIOpsStatus extends AutonomyReadiness {
  web_worker_class: string
  autonomy_pool: string
  autonomy_concurrency: number
  active_runs: number
  queued_runs: number
  knowledge_index_state: string
  alertmanager_configured: boolean
  prometheus_configured: boolean
  pending_alerts: AutonomyRun[]
  running_runs: AutonomyRun[]
  recent_conclusions: AutonomyRun[]
}

export type KnowledgeIndexState = 'empty' | 'ready' | 'stale' | 'rebuilding' | 'error'

export interface KnowledgeEmbeddingConfig {
  provider_type: 'local' | 'openai_compatible'
  base_url: string
  model: string
  dimension: number
  api_key_configured: boolean
  model_fingerprint: string
  indexed_fingerprint: string | null
  index_state: KnowledgeIndexState
  indexed_chunks: number
  created_at: string | null
  updated_at: string | null
}

export interface KnowledgeDocument {
  id: string
  title: string
  source_type: 'runbook' | 'verified_run'
  source_ref: string | null
  scope: string
  content?: string
  content_sha256: string
  version: number
  approved: boolean
  indexed: boolean
  chunk_count: number
  created_by: string
  created_at: string | null
  updated_at: string | null
}

export interface KnowledgeDocumentPayload {
  title: string
  scope: string
  content: string
}

export interface KnowledgeSearchResult {
  citation_id: string
  document_id: string
  version: number
  title: string
  source_type: KnowledgeDocument['source_type']
  source_ref: string | null
  heading: string
  scope: string
  excerpt: string
  score: number | null
  match_reason: string
}

/** 创建草稿请求体 */
export interface AutonomyCreateRunPayload {
  goal: string
  host_id: number
  system_user_id: number
  mode: AutonomyRunMode
  budget?: AutonomyBudget
  profile?: AutonomyCustomProfile
}

/** 步骤决策请求体（恰好这两个字段；operation 必须来自 allowed_operations） */
export interface AutonomyDecisionPayload {
  operation: string
  expected_revision: number
}

/** 后端统一信封（api_response / api_error） */
export interface AutonomyEnvelope<T> {
  code: number
  msg: string
  data?: T
  [key: string]: unknown
}

/** 终态集合：这些状态下不再需要事件流 */
export const AUTONOMY_TERMINAL_STATUSES: readonly AutonomyRunStatus[] = [
  'completed',
  'failed',
  'cancelled',
  'expired',
]

export function isTerminalRunStatus(status: string): boolean {
  return (AUTONOMY_TERMINAL_STATUSES as readonly string[]).includes(status)
}
