<!--
  AI 自治任务详情页（M1/S3 切片 6）
  原则：一切以服务端权威快照为准 —— 审批按钮只由 allowed_operations 驱动，
  事件时间线只由可续传 SSE 的单调 sequence 驱动，前端从不推断可用性。
-->
<template>
  <div class="run-detail">
    <!-- 顶部：返回 + 目标 + 权威状态 -->
    <header class="page-header">
      <div class="page-title">
        <div>
          <div class="run-detail-back">
            <el-button text size="small" :icon="ArrowLeft" @click="router.push('/ai-runs')">
              {{ t('common.action.back') }}
            </el-button>
            <span class="run-detail-id">{{ runId }}</span>
          </div>
          <span class="page-eyebrow">{{ t('aiRuns.eyebrow') }}</span>
          <h2 class="run-detail-goal" :title="snapshot?.goal || undefined">
            {{ snapshot ? goalSummary : '—' }}
          </h2>
          <details v-if="snapshot && snapshot.goal.length > goalSummary.length" class="run-goal-details">
            <summary>{{ t('aiRuns.detail.goalDetails') }}</summary>
            <p>{{ snapshot.goal }}</p>
          </details>
          <p class="page-subtitle run-detail-statusline">
            <el-tag
              v-if="snapshot" size="small" effect="light" round
              :type="statusTagType(snapshot.status)"
            >
              {{ t(`aiRuns.status.${snapshot.status}`) }}
            </el-tag>
            <el-tag
              v-if="snapshot?.outcome" size="small" effect="plain" round
              :type="outcomeTagType(snapshot.outcome)"
            >
              {{ t(`aiRuns.outcome.${snapshot.outcome}`) }}
            </el-tag>
            <span v-if="snapshot?.cancel_requested && !terminal" class="run-detail-note">
              {{ t('aiRuns.detail.cancelRequested') }}
            </span>
          </p>
        </div>
      </div>
      <div class="page-actions">
        <el-button
          v-if="snapshot?.status === 'draft'"
          type="primary" :loading="acting" @click="doStart"
        >
          {{ t('aiRuns.detail.start') }}
        </el-button>
        <el-button
          v-if="snapshot && !terminal && !snapshot.cancel_requested"
          type="danger" plain :loading="acting" @click="doCancel"
        >
          {{ t('aiRuns.detail.cancel') }}
        </el-button>
      </div>
    </header>

    <!-- 加载 / 错误态 -->
    <div v-if="snapshotError" class="panel run-detail-error">
      <el-alert type="error" :closable="false" show-icon :title="snapshotError">
        <el-button size="small" @click="loadSnapshot">{{ t('common.action.retry') }}</el-button>
      </el-alert>
    </div>
    <div v-else-if="!snapshot" v-loading="true" class="panel run-detail-loading" />

    <template v-else>
      <!-- 状态横幅：需要关注 / 恢复中 / 过期，文案只转述服务端状态 -->
      <el-alert
        v-if="snapshot.status === 'needs_attention'"
        class="run-banner" type="error" :closable="false" show-icon
        :title="t('aiRuns.detail.attention.title')"
        :description="t('aiRuns.detail.attention.hint')"
      />
      <el-alert
        v-else-if="snapshot.status === 'recovering'"
        class="run-banner" type="warning" :closable="false" show-icon
        :title="t(`aiRuns.status.recovering`)"
        :description="t('aiRuns.detail.recovering')"
      />
      <el-alert
        v-else-if="snapshot.status === 'expired'"
        class="run-banner" type="info" :closable="false" show-icon
        :description="t('aiRuns.detail.expiredHint')"
      />

      <!-- 结论摘要：先给人读，再把原始事件和 Artifact 放到审计层。 -->
      <section
        v-if="snapshot.outcome || terminal"
        class="panel run-conclusion"
        :class="`is-${conclusionTone}`"
      >
        <div class="run-conclusion-head">
          <div>
            <div class="run-section-kicker">{{ t('aiRuns.detail.conclusion.eyebrow') }}</div>
            <h3 class="run-conclusion-title">{{ conclusionTitle }}</h3>
          </div>
          <el-tag size="small" effect="light" round :type="conclusionTagType">
            {{ conclusionTag }}
          </el-tag>
        </div>
        <p class="run-conclusion-summary">{{ conclusionSummary }}</p>
        <div class="run-conclusion-facts">
          <span>
            <strong>{{ stepCounts.succeeded }}/{{ stepCounts.total }}</strong>
            {{ t('aiRuns.detail.conclusion.stepFact') }}
          </span>
          <span>
            <strong>{{ stepCounts.verificationSucceeded }}/{{ stepCounts.verificationTotal }}</strong>
            {{ t('aiRuns.detail.conclusion.verificationFact') }}
          </span>
          <span>
            <strong>{{ evidence.length }}</strong>
            {{ t('aiRuns.detail.conclusion.evidenceFact') }}
          </span>
        </div>
      </section>

      <!-- 流程光标轨：六阶段完全由服务端快照推导 -->
      <ol class="phase-rail" :aria-label="t('aiRuns.detail.phaseRail')">
        <li
          v-for="phase in phases" :key="phase.key"
          class="phase-node" :class="`is-${phase.state}`"
          :aria-current="phase.state === 'active' ? 'step' : undefined"
        >
          <span class="phase-dot"><span class="phase-dot-core" /></span>
          <span class="phase-label">{{ t(`aiRuns.detail.phases.${phase.key}`) }}</span>
        </li>
      </ol>

      <!-- 元信息 -->
      <section class="panel run-meta">
        <div class="run-meta-grid">
          <div class="run-meta-item">
            <span class="run-meta-label">{{ t('aiRuns.detail.meta.host') }}</span>
            <span class="run-meta-value">{{ snapshot.host_alias }} <em>#{{ snapshot.host_id }}</em></span>
          </div>
          <div class="run-meta-item">
            <span class="run-meta-label">{{ t('aiRuns.detail.meta.credential') }}</span>
            <span class="run-meta-value">{{ snapshot.system_user_alias }}</span>
          </div>
          <div class="run-meta-item">
            <span class="run-meta-label">{{ t('aiRuns.detail.meta.mode') }}</span>
            <span class="run-meta-value run-mono">{{ snapshot.mode }}</span>
          </div>
          <div class="run-meta-item">
            <span class="run-meta-label">{{ t('aiRuns.detail.meta.owner') }}</span>
            <span class="run-meta-value">{{ snapshot.owner }}</span>
          </div>
          <div class="run-meta-item">
            <span class="run-meta-label">{{ t('aiRuns.detail.meta.revision') }}</span>
            <span class="run-meta-value run-mono">r{{ snapshot.revision }}</span>
          </div>
          <div class="run-meta-item">
            <span class="run-meta-label">{{ t('aiRuns.detail.meta.created') }}</span>
            <span class="run-meta-value">{{ absTime(snapshot.created_at) }}</span>
          </div>
          <div class="run-meta-item">
            <span class="run-meta-label">{{ t('aiRuns.detail.meta.started') }}</span>
            <span class="run-meta-value">{{ absTime(snapshot.started_at) }}</span>
          </div>
          <div class="run-meta-item">
            <span class="run-meta-label">{{ t('aiRuns.detail.meta.completed') }}</span>
            <span class="run-meta-value">{{ absTime(snapshot.completed_at) }}</span>
          </div>
        </div>
        <div v-if="budgetChips.length" class="run-meta-budget">
          <span class="run-meta-label">{{ t('aiRuns.detail.meta.budget') }}</span>
          <span v-for="chip in budgetChips" :key="chip.key" class="run-budget-chip">
            <span class="run-mono">{{ chip.value }}</span> {{ chip.unit }}
          </span>
        </div>
      </section>

      <!-- 审批卡：只有 allowed_operations 非空时存在 -->
      <section v-if="waitingStep && allowedOps.length" class="panel run-approval">
        <div class="run-approval-head">
          <span class="run-approval-title">{{ t('aiRuns.detail.approval.title') }}</span>
          <span class="run-approval-hint">{{ t('aiRuns.detail.approval.hint') }}</span>
        </div>
        <div class="run-approval-step">
          <el-tag size="small" effect="plain">{{ stepKindLabel(waitingStep.kind) }}</el-tag>
          <span class="run-approval-summary">{{ waitingStep.summary }}</span>
        </div>
        <div v-if="waitingStep.action_digest" class="run-approval-digest run-mono">
          sha256:{{ waitingStep.action_digest }}
        </div>
        <div class="run-approval-actions">
          <el-button
            v-if="allowedOps.includes('approve')"
            type="primary" :loading="deciding" @click="decide('approve')"
          >
            {{ t('aiRuns.detail.approval.approve') }}
          </el-button>
          <el-button
            v-if="allowedOps.includes('reject')"
            type="danger" plain :loading="deciding" @click="decide('reject')"
          >
            {{ t('aiRuns.detail.approval.reject') }}
          </el-button>
        </div>
      </section>

      <div class="run-detail-columns">
        <!-- 左列：计划步骤 + 事件时间线 -->
        <div class="run-detail-main">
          <section class="panel">
            <div class="panel-head">{{ t('aiRuns.detail.stepsTitle') }}</div>
            <div v-if="snapshot.steps.length === 0" class="run-empty">
              {{ t('aiRuns.detail.stepsEmpty') }}
            </div>
            <ol v-else class="run-steps">
              <li v-for="step in snapshot.steps" :key="step.id" class="run-step">
                <span class="run-step-seq run-mono">{{ String(step.seq).padStart(2, '0') }}</span>
                <div class="run-step-body">
                  <div class="run-step-line">
                    <el-tag size="small" effect="plain">{{ stepKindLabel(step.kind) }}</el-tag>
                    <el-tag size="small" effect="light" :type="stepTagType(step.status)">
                      {{ stepStatusLabel(step.status) }}
                    </el-tag>
                  </div>
                  <div class="run-step-title">{{ t(presentAutonomyStep(step).labelKey) }}</div>
                  <div class="run-step-command">
                    <span class="run-step-label">{{ t('aiRuns.detail.execution.action') }}</span>
                    <code>{{ presentAutonomyStep(step).command }}</code>
                  </div>
                  <div class="run-step-result">
                    <span class="run-step-label">{{ t('aiRuns.detail.execution.result') }}</span>
                    <span>{{ stepExecutionText(step) }}</span>
                  </div>
                  <div v-if="stepArtifacts(step).length" class="run-step-artifacts">
                    <span class="run-step-label">{{ t('aiRuns.detail.execution.evidence') }}</span>
                    <button
                      v-for="artifact in stepArtifacts(step)"
                      :key="artifact.id"
                      class="run-result-link"
                      :disabled="artifact.expired"
                      type="button"
                      @click="openArtifact(artifact)"
                    >
                      {{ artifactActionLabel(artifact.kind) }}
                    </button>
                  </div>
                  <details v-if="step.action_digest || step.note" class="run-audit-details">
                    <summary>{{ t('aiRuns.detail.execution.auditDetails') }}</summary>
                    <div v-if="step.note" class="run-step-note run-mono">{{ step.note }}</div>
                    <div v-if="step.action_digest" class="run-step-digest run-mono">
                      sha256:{{ step.action_digest }}
                    </div>
                  </details>
                </div>
              </li>
            </ol>
          </section>

          <details class="panel run-collapsible-panel" :open="snapshot.status === 'needs_attention'">
            <summary class="panel-head run-collapsible-head">
              <span>{{ t('aiRuns.detail.eventsTitle') }}</span>
              <span class="run-collapsible-count run-mono">{{ events.length }}</span>
              <span class="run-stream-cursor run-mono">seq {{ lastSeq }}</span>
              <span class="run-stream-state" :class="`is-${streamState}`">
                {{ streamStateText }}
              </span>
            </summary>
            <div v-if="events.length === 0" class="run-empty">
              {{ t('aiRuns.detail.eventsEmpty') }}
            </div>
            <div v-else ref="timelineRef" class="run-timeline">
              <div v-for="event in events" :key="event.sequence" class="run-event">
                <span class="run-event-seq run-mono">{{ event.sequence }}</span>
                <div class="run-event-body">
                  <span class="run-event-type">{{ eventLabel(event.event_type) }}</span>
                  <span v-if="eventDetail(event)" class="run-event-detail">{{ eventDetail(event) }}</span>
                </div>
              </div>
            </div>
          </details>
        </div>

        <!-- 右列：证据索引 + 产物 -->
        <aside class="run-detail-side">
          <details class="panel run-collapsible-panel" :open="evidence.length === 0">
            <summary class="panel-head run-collapsible-head">
              <span>{{ t('aiRuns.detail.evidenceTitle') }}</span>
              <span class="run-collapsible-count run-mono">{{ evidence.length }}</span>
              <el-tooltip :content="t('aiRuns.detail.evidenceUntrusted')" placement="top">
                <el-icon class="run-side-info"><InfoFilled /></el-icon>
              </el-tooltip>
            </summary>
            <div v-if="evidence.length === 0" class="run-empty">
              {{ t('aiRuns.detail.evidenceEmpty') }}
            </div>
            <ul v-else class="run-evidence">
              <li v-for="item in evidence" :key="item.id" class="run-evidence-item">
                <div class="run-evidence-line">
                  <span class="run-evidence-kind">{{ evidenceKindLabel(item.kind) }}</span>
                  <span class="run-evidence-time">{{ absTime(item.created_at) }}</span>
                </div>
                <div class="run-evidence-meaning">{{ evidenceMeaning(item) }}</div>
                <div class="run-evidence-result">{{ evidenceResult(item) }}</div>
                <div v-if="evidenceArtifacts(item).length" class="run-evidence-actions">
                  <button
                    v-for="artifact in evidenceArtifacts(item)"
                    :key="artifact.id"
                    class="run-result-link"
                    :disabled="artifact.expired"
                    type="button"
                    @click="openArtifact(artifact)"
                  >
                    {{ artifactActionLabel(artifact.kind) }}
                  </button>
                </div>
                <div v-else-if="item.artifact_ids.length" class="run-evidence-refs">
                  {{ t('aiRuns.detail.evidenceRefs', { n: item.artifact_ids.length }) }}
                </div>
                <details class="run-audit-details">
                  <summary>{{ t('aiRuns.detail.execution.rawEvidence') }}</summary>
                  <div class="run-evidence-summary run-mono">{{ item.summary }}</div>
                </details>
              </li>
            </ul>
          </details>

          <details class="panel run-collapsible-panel">
            <summary class="panel-head run-collapsible-head">
              <span>{{ t('aiRuns.detail.artifactsTitle') }}</span>
              <span class="run-collapsible-count run-mono">{{ artifacts.length }}</span>
            </summary>
            <div v-if="artifacts.length === 0" class="run-empty">
              {{ t('aiRuns.detail.artifactsEmpty') }}
            </div>
            <ul v-else class="run-artifacts">
              <li v-for="artifact in artifacts" :key="artifact.id" class="run-artifact">
                <button
                  class="run-artifact-open" :disabled="artifact.expired"
                  @click="openArtifact(artifact)"
                >
                  <span class="run-artifact-title">{{ artifactDisplayTitle(artifact) }}</span>
                  <span class="run-artifact-kind">{{ artifactKindLabel(artifact.kind) }}</span>
                </button>
                <div class="run-artifact-meta">
                  <span class="run-mono">{{ formatBytes(artifact.size_bytes) }}</span>
                  <el-tag v-if="artifact.truncated" size="small" effect="plain" type="warning">
                    {{ t('aiRuns.detail.artifactTruncated') }}
                  </el-tag>
                  <el-tag v-if="artifact.expired" size="small" effect="plain" type="info">
                    {{ t('aiRuns.detail.artifactExpired') }}
                  </el-tag>
                </div>
              </li>
            </ul>
          </details>
        </aside>
      </div>
    </template>

    <!-- 产物正文对话框（单条按需解密读取） -->
    <el-dialog
      v-model="artifactDialog.visible"
      :title="artifactDialog.title || t('aiRuns.detail.artifactContent')"
      width="720px" append-to-body destroy-on-close
    >
      <div v-loading="artifactDialog.loading">
        <pre class="run-artifact-content run-mono">{{ artifactDialog.content }}</pre>
      </div>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { ArrowLeft, InfoFilled } from '@element-plus/icons-vue'
import { t } from '@/i18n'
import {
  cancelAutonomyRun, decideAutonomyStep, getAutonomyArtifact, getAutonomySnapshot,
  listAutonomyArtifacts, listAutonomyEvidence, startAutonomyRun, streamAutonomyRun,
} from '@/api/autonomy'
import { isTerminalRunStatus } from '@/types/autonomy'
import type {
  AutonomyArtifact, AutonomyEvent, AutonomyEvidence, AutonomySnapshot, AutonomyStep,
} from '@/types/autonomy'
import { formatTimeAbs } from '@/utils/datetime'
import {
  artifactsForAutonomyStep,
  autonomyArtifactLabelKey,
  countAutonomySteps,
  parseAutonomyStepExecutionNote,
  presentAutonomyStep,
  summarizeAutonomyGoal,
} from '@/utils/autonomyPresentation'

const route = useRoute()
const router = useRouter()
const runId = String(route.params.runId || '')

// ===== 权威快照 =====
const snapshot = ref<AutonomySnapshot | null>(null)
const snapshotError = ref('')
const acting = ref(false)
const deciding = ref(false)

const terminal = computed<boolean>(() => (
  snapshot.value ? isTerminalRunStatus(snapshot.value.status) : false
))
const allowedOps = computed<string[]>(() => snapshot.value?.allowed_operations || [])
const waitingStep = computed<AutonomyStep | null>(() => (
  snapshot.value?.steps.find((step) => step.status === 'waiting_approval') || null
))

async function loadSnapshot(): Promise<void> {
  snapshotError.value = ''
  try {
    snapshot.value = await getAutonomySnapshot(runId)
  } catch (err) {
    snapshotError.value = err instanceof Error ? err.message : t('aiRuns.detail.loadFailed')
  }
}

// ===== 事件流（可续传） =====
type StreamState = 'connecting' | 'live' | 'reconnecting' | 'closed' | 'error'
const events = ref<AutonomyEvent[]>([])
const streamState = ref<StreamState>('connecting')
const streamError = ref('')
const lastSeq = ref(0)
const timelineRef = ref<HTMLElement | null>(null)

let aborter: AbortController | null = null
let reconnectTimer: ReturnType<typeof setTimeout> | null = null
let streamStopped = false
let reconnectAttempts = 0
let unmounted = false

const MAX_EVENTS = 500

const streamStateText = computed<string>(() => {
  if (streamState.value === 'error') {
    return t('aiRuns.detail.stream.serverError', { reason: streamError.value })
  }
  return t(`aiRuns.detail.stream.${streamState.value}`)
})

function appendEvent(event: AutonomyEvent): void {
  if (event.sequence <= lastSeq.value && events.value.length) return
  lastSeq.value = Math.max(lastSeq.value, event.sequence)
  events.value = [...events.value.slice(-(MAX_EVENTS - 1)), event]
  nextTick(() => {
    const el = timelineRef.value
    if (el) el.scrollTop = el.scrollHeight
  })
}

function scheduleReconnect(): void {
  if (unmounted || streamStopped) return
  streamState.value = 'reconnecting'
  // 逐次指数退避（2s → 30s 封顶），避免后端不可用时形成重试风暴；
  // 收到事件即重置计数（服务端 300s 到期关流后的续传也是同样路径）。
  reconnectAttempts += 1
  const delay = Math.min(30000, 1000 * 2 ** Math.min(reconnectAttempts, 5))
  reconnectTimer = setTimeout(() => { openStream() }, delay)
}

async function openStream(): Promise<void> {
  if (unmounted || streamStopped) return
  aborter?.abort()
  aborter = new AbortController()
  const signal = aborter.signal
  streamState.value = lastSeq.value > 0 ? 'reconnecting' : 'connecting'
  try {
    await streamAutonomyRun(runId, lastSeq.value, {
      onEvent: (event) => {
        reconnectAttempts = 0
        streamState.value = 'live'
        appendEvent(event)
      },
      onTerminal: (finalSnapshot) => {
        streamStopped = true
        snapshot.value = finalSnapshot
        streamState.value = 'closed'
        reloadSidePanels()
      },
      onServerError: (reason) => {
        streamStopped = true
        streamError.value = reason
        streamState.value = 'error'
      },
    }, signal)
    // 流自然关闭：未终态且未收到 terminal 帧 → 携当前游标重连续传
    if (!streamStopped && !unmounted) {
      if (snapshot.value && isTerminalRunStatus(snapshot.value.status)) {
        streamState.value = 'closed'
      } else {
        scheduleReconnect()
      }
    }
  } catch (err) {
    if (signal.aborted || unmounted || streamStopped) return
    streamError.value = err instanceof Error ? err.message : t('aiRuns.detail.stream.fetchFailed')
    scheduleReconnect()
  }
}

function stopStream(): void {
  streamStopped = true
  if (reconnectTimer) clearTimeout(reconnectTimer)
  aborter?.abort()
}

// ===== 证据 / 产物 =====
const evidence = ref<AutonomyEvidence[]>([])
const artifacts = ref<AutonomyArtifact[]>([])
const artifactDialog = reactive({
  visible: false,
  loading: false,
  content: '',
  title: '',
})

const stepCounts = computed(() => countAutonomySteps(snapshot.value?.steps || []))
const goalSummary = computed(() => summarizeAutonomyGoal(snapshot.value?.goal || '', 112))

const conclusionTag = computed(() => {
  const current = snapshot.value
  if (!current) return '—'
  return current.outcome
    ? t(`aiRuns.outcome.${current.outcome}`)
    : t(`aiRuns.status.${current.status}`)
})

const conclusionTagType = computed(() => {
  const current = snapshot.value
  return current?.outcome
    ? outcomeTagType(current.outcome)
    : statusTagType(current?.status || '')
})

const conclusionTone = computed<'success' | 'danger' | 'warning' | 'info'>(() => {
  switch (snapshot.value?.outcome) {
    case 'resolved': return 'success'
    case 'not_resolved': return 'danger'
    case 'inconclusive': return 'warning'
    default: return 'info'
  }
})

const conclusionTitle = computed(() => {
  switch (snapshot.value?.outcome) {
    case 'resolved': return t('aiRuns.detail.conclusion.resolvedTitle')
    case 'not_resolved': return t('aiRuns.detail.conclusion.notResolvedTitle')
    case 'inconclusive': return t('aiRuns.detail.conclusion.inconclusiveTitle')
    default: return t('aiRuns.detail.conclusion.pendingTitle')
  }
})

const conclusionSummary = computed(() => {
  const current = snapshot.value
  const counts = stepCounts.value
  const params = {
    succeeded: counts.succeeded,
    total: counts.total,
    failed: counts.failed,
    verificationSucceeded: counts.verificationSucceeded,
    verificationTotal: counts.verificationTotal,
    status: current ? t(`aiRuns.status.${current.status}`) : '—',
  }
  switch (current?.outcome) {
    case 'resolved': return t('aiRuns.detail.conclusion.resolvedSummary', params)
    case 'not_resolved': return t('aiRuns.detail.conclusion.notResolvedSummary', params)
    case 'inconclusive': return t('aiRuns.detail.conclusion.inconclusiveSummary', params)
    default: return t('aiRuns.detail.conclusion.pendingSummary', params)
  }
})

function stepForId(stepId: string | null): AutonomyStep | null {
  if (!stepId) return null
  return snapshot.value?.steps.find((step) => step.id === stepId) || null
}

function stepArtifacts(step: AutonomyStep): AutonomyArtifact[] {
  return artifactsForAutonomyStep(artifacts.value, step.id)
}

function stepExecutionText(step: AutonomyStep): string {
  const parsed = parseAutonomyStepExecutionNote(step.note || '')
  let result: string
  if (parsed.exitCode !== null) {
    const code = t('aiRuns.detail.execution.exitCode', { code: parsed.exitCode })
    result = parsed.exitCode === 0
      ? `${t('aiRuns.detail.execution.completed')} · ${code}`
      : `${t('aiRuns.detail.execution.failed')} · ${code}`
  } else if (step.status === 'succeeded') {
    result = t('aiRuns.detail.execution.completed')
  } else if (step.status === 'failed') {
    result = t('aiRuns.detail.execution.failed')
  } else if (step.status === 'outcome_unknown') {
    result = t('aiRuns.detail.execution.unknown')
  } else if (['skipped', 'cancelled'].includes(step.status)) {
    result = stepStatusLabel(step.status)
  } else if (['proposed', 'waiting_approval', 'approved', 'running'].includes(step.status)) {
    result = step.status === 'running'
      ? t('aiRuns.detail.execution.running')
      : t('aiRuns.detail.execution.pending')
  } else {
    result = stepStatusLabel(step.status)
  }
  return parsed.outputTruncated
    ? `${result} · ${t('aiRuns.detail.execution.truncated')}`
    : result
}

function evidenceKindLabel(kind: string): string {
  if (kind === 'action_observation') return t('aiRuns.detail.evidenceKind.action')
  if (kind === 'verification_observation') return t('aiRuns.detail.evidenceKind.verification')
  return t('aiRuns.detail.evidenceKind.generic')
}

function evidenceMeaning(item: AutonomyEvidence): string {
  const step = stepForId(item.step_id)
  return step
    ? t(presentAutonomyStep(step).labelKey)
    : t('aiRuns.detail.evidenceKind.generic')
}

function evidenceResult(item: AutonomyEvidence): string {
  const step = stepForId(item.step_id)
  return step
    ? stepExecutionText(step)
    : t('aiRuns.detail.execution.evidenceObserved')
}

function evidenceArtifacts(item: AutonomyEvidence): AutonomyArtifact[] {
  const ids = new Set(item.artifact_ids)
  return artifacts.value.filter((artifact) => ids.has(artifact.id))
}

function artifactKindLabel(kind: string): string {
  return t(`aiRuns.detail.artifactKind.${autonomyArtifactLabelKey(kind)}`)
}

function artifactActionLabel(kind: string): string {
  return t(`aiRuns.detail.artifactAction.${autonomyArtifactLabelKey(kind)}`)
}

function artifactDisplayTitle(artifact: AutonomyArtifact): string {
  const step = stepForId(artifact.step_id)
  const action = step ? t(presentAutonomyStep(step).labelKey) : ''
  const kind = artifactKindLabel(artifact.kind)
  return action ? `${action} · ${kind}` : kind
}

async function reloadSidePanels(): Promise<void> {
  try {
    const [artifactList, evidenceList] = await Promise.all([
      listAutonomyArtifacts(runId),
      listAutonomyEvidence(runId),
    ])
    artifacts.value = artifactList
    evidence.value = evidenceList
  } catch {
    // 侧栏失败不阻断主视图；下次事件终局或手动刷新会重试
  }
}

async function openArtifact(artifact: AutonomyArtifact): Promise<void> {
  artifactDialog.visible = true
  artifactDialog.loading = true
  artifactDialog.content = ''
  artifactDialog.title = artifactDisplayTitle(artifact)
  try {
    const detail = await getAutonomyArtifact(runId, artifact.id)
    artifactDialog.content = detail.content
  } catch (err) {
    artifactDialog.content = err instanceof Error ? err.message : t('aiRuns.detail.artifactLoadFailed')
  } finally {
    artifactDialog.loading = false
  }
}

// ===== 操作：启动 / 取消 / 决策（均以服务端响应为准） =====
async function refreshAfterAction(): Promise<void> {
  await loadSnapshot()
  reloadSidePanels()
}

async function doStart(): Promise<void> {
  acting.value = true
  try {
    await startAutonomyRun(runId)
    await refreshAfterAction()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : t('aiRuns.detail.startFailed'))
  } finally {
    acting.value = false
  }
}

async function doCancel(): Promise<void> {
  try {
    await ElMessageBox.confirm(t('aiRuns.detail.cancelConfirm'), t('common.crud.prompt'), {
      confirmButtonText: t('common.action.confirm'),
      cancelButtonText: t('common.action.cancel'),
      type: 'warning',
    })
  } catch {
    return
  }
  acting.value = true
  try {
    await cancelAutonomyRun(runId)
    await refreshAfterAction()
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : t('aiRuns.detail.cancelFailed'))
  } finally {
    acting.value = false
  }
}

async function decide(operation: string): Promise<void> {
  const step = waitingStep.value
  const current = snapshot.value
  if (!step || !current) return
  deciding.value = true
  try {
    await decideAutonomyStep(runId, step.id, {
      operation,
      expected_revision: current.revision,
    })
    ElMessage.success(t('aiRuns.detail.approval.decided'))
    await refreshAfterAction()
  } catch (err) {
    // 决策失败先刷新权威快照：若已无待审批步骤，说明状态已被并发改变
    await loadSnapshot()
    if (!waitingStep.value || allowedOps.value.length === 0) {
      ElMessage.warning(t('aiRuns.detail.approval.conflict'))
    } else {
      const message = err instanceof Error ? err.message : t('aiRuns.detail.approval.failed')
      ElMessage.error(`${t('aiRuns.detail.approval.failed')}: ${message}`)
    }
  } finally {
    deciding.value = false
  }
}

// ===== 展示辅助 =====
const KNOWN_EVENTS = new Set([
  'run_created', 'run_started', 'run_cancel_requested', 'run_cancelled',
  'step_proposed', 'plan_proposed', 'steps_waiting_approval', 'step_decision',
  'artifact_created', 'run_concluded', 'step_cancelled', 'step_execution_started',
  'step_executed', 'checkpoint_unavailable', 'step_policy_decided', 'guardian_decision',
  'plan_completed', 'plan_authorization_invalidated', 'run_expired', 'recovery_cursor_unresolved',
])

function eventLabel(eventType: string): string {
  return KNOWN_EVENTS.has(eventType)
    ? t(`aiRuns.detail.events.${eventType}`)
    : `${t('aiRuns.detail.events.unknown')} ${eventType}`
}

function eventDetail(event: AutonomyEvent): string {
  const payload = event.payload || {}
  for (const key of ['summary', 'reason', 'outcome', 'operation', 'status']) {
    const value = payload[key]
    if (typeof value === 'string' && value) return value
  }
  return ''
}

function stepKindLabel(kind: string): string {
  return ['plan', 'action', 'verification'].includes(kind)
    ? t(`aiRuns.detail.stepKind.${kind}`)
    : kind
}

function stepStatusLabel(status: string): string {
  const known = [
    'proposed', 'waiting_approval', 'approved', 'running',
    'succeeded', 'failed', 'skipped', 'outcome_unknown', 'cancelled',
  ]
  return known.includes(status) ? t(`aiRuns.detail.stepStatus.${status}`) : status
}

function stepTagType(status: string): '' | 'success' | 'warning' | 'info' | 'danger' {
  if (status === 'running') return ''
  if (status === 'succeeded' || status === 'approved') return 'success'
  if (status === 'waiting_approval' || status === 'outcome_unknown') return 'warning'
  if (status === 'failed') return 'danger'
  return 'info'
}

function statusTagType(status: string): '' | 'success' | 'warning' | 'info' | 'danger' {
  if (status === 'running') return ''
  if (status === 'completed') return 'success'
  if (status === 'waiting_approval' || status === 'recovering') return 'warning'
  if (status === 'failed' || status === 'needs_attention') return 'danger'
  return 'info'
}

function outcomeTagType(outcome: string): '' | 'success' | 'warning' | 'info' | 'danger' {
  if (outcome === 'resolved') return 'success'
  if (outcome === 'not_resolved') return 'danger'
  return 'warning'
}

function absTime(value: string | null): string {
  return value ? (formatTimeAbs(value) || '—') : '—'
}

function formatBytes(size: number): string {
  if (size >= 1048576) return `${(size / 1048576).toFixed(1)} MB`
  if (size >= 1024) return `${(size / 1024).toFixed(1)} KB`
  return `${size} B`
}

// ===== 流程光标轨：六阶段由快照纯推导，不含任何本地业务猜测 =====
interface Phase { key: string; state: 'done' | 'active' | 'pending' }

const phases = computed<Phase[]>(() => {
  const all: Phase[] = [
    'drafted', 'executing', 'approving', 'verifying', 'concluding', 'terminal',
  ].map((key) => ({ key, state: 'pending' as const }))
  const current = snapshot.value
  if (!current) return all

  const steps = current.steps
  const isTerminal = isTerminalRunStatus(current.status)
  const executedSteps = steps.filter((step) => (
    ['succeeded', 'failed', 'skipped', 'outcome_unknown'].includes(step.status)
  ))
  const pastApproval = steps.some((step) => step.status !== 'proposed' && step.status !== 'waiting_approval')
  const verificationSteps = steps.filter((step) => step.kind === 'verification')
  const verificationDone = verificationSteps.some((step) => (
    ['succeeded', 'failed', 'skipped', 'outcome_unknown'].includes(step.status)
  ))
  // proposed 仍属未启动，只有获批/执行中才算进入验证阶段
  const verificationActive = verificationSteps.some((step) => (
    ['approved', 'running'].includes(step.status)
  ))

  // 提议：有步骤或已离开草稿即完成；草稿态为当前阶段
  all[0].state = (current.status !== 'draft' || steps.length > 0) ? 'done' : 'active'
  // 执行：有已终局步骤即完成；排队/运行/恢复中为当前阶段
  if (executedSteps.length > 0) all[1].state = 'done'
  else if (['queued', 'running', 'recovering'].includes(current.status)) all[1].state = 'active'
  // 审批：等待审批为当前阶段；任何步骤越过审批即完成过审批
  if (current.status === 'waiting_approval') all[2].state = 'active'
  else if (pastApproval) all[2].state = 'done'
  // 验证：有验证步骤终局即完成；进行中的验证步骤为当前阶段
  if (verificationDone) all[3].state = 'done'
  else if (verificationActive && !isTerminal) all[3].state = 'active'
  // 结论：终态即完成；needs_attention 表示结论悬而未决，为当前阶段
  if (isTerminal) all[4].state = 'done'
  else if (current.status === 'needs_attention') all[4].state = 'active'
  // 终结：终态即完成
  if (isTerminal) all[5].state = 'done'
  return all
})

const BUDGET_UNITS: Record<string, string> = {
  duration_seconds: 'duration_seconds',
  max_loops: 'max_loops',
  max_actions: 'max_actions',
  command_timeout_seconds: 'command_timeout_seconds',
  step_output_bytes: 'step_output_bytes',
  run_artifact_bytes: 'run_artifact_bytes',
}

const budgetChips = computed<Array<{ key: string; value: number; unit: string }>>(() => {
  const budget = snapshot.value?.budget || {}
  return Object.keys(BUDGET_UNITS)
    .filter((key) => typeof budget[key as keyof typeof budget] === 'number')
    .map((key) => ({
      key,
      value: budget[key as keyof typeof budget] as number,
      unit: t(`aiRuns.detail.budgetUnit.${BUDGET_UNITS[key]}`),
    }))
})

// ===== 生命周期 =====
onMounted(() => {
  loadSnapshot().then(() => {
    openStream()
  })
  reloadSidePanels()
})

onBeforeUnmount(() => {
  unmounted = true
  stopStream()
})
</script>

<style scoped>
.run-detail {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.run-mono { font-family: var(--ogs-mono); }

/* ---- 头部 ---- */
.run-detail-back {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 2px;
}
.run-detail-id {
  font-family: var(--ogs-mono);
  font-size: 11px;
  color: var(--ogs-text-tertiary);
}
.run-detail-goal {
  max-width: 760px;
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  overflow: hidden;
  overflow-wrap: anywhere;
}
.run-goal-details {
  max-width: 760px;
  margin-top: 6px;
  color: var(--ogs-text-secondary);
  font-size: 12px;
}
.run-goal-details summary {
  display: inline-block;
  color: var(--ogs-primary);
  cursor: pointer;
}
.run-goal-details p {
  margin: 6px 0 0;
  line-height: 1.55;
  overflow-wrap: anywhere;
}
.run-detail-statusline {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.run-detail-note {
  font-size: 12px;
  color: var(--ogs-warning);
}
.run-detail-loading { min-height: 240px; }
.run-detail-error { padding: 12px; }

/* ---- 横幅 ---- */
.run-banner { border-radius: 4px; }

/* ---- 结论：先回答“这次到底怎么样”，再展开审计细节 ---- */
.run-conclusion {
  padding: 16px 18px;
  border-left: 4px solid var(--ogs-primary);
  background: color-mix(in srgb, var(--ogs-primary) 4%, var(--ogs-surface));
}
.run-conclusion.is-success {
  border-left-color: var(--ogs-success);
  background: color-mix(in srgb, var(--ogs-success) 5%, var(--ogs-surface));
}
.run-conclusion.is-danger {
  border-left-color: var(--ogs-danger);
  background: color-mix(in srgb, var(--ogs-danger) 5%, var(--ogs-surface));
}
.run-conclusion.is-warning {
  border-left-color: var(--ogs-warning);
  background: color-mix(in srgb, var(--ogs-warning) 5%, var(--ogs-surface));
}
.run-conclusion-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.run-section-kicker {
  font-size: 11px;
  color: var(--ogs-text-tertiary);
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.run-conclusion-title {
  margin: 2px 0 0;
  font-size: 18px;
  line-height: 1.35;
  color: var(--ogs-text);
}
.run-conclusion-summary {
  margin: 10px 0 0;
  max-width: 860px;
  color: var(--ogs-text-secondary);
  font-size: 13px;
  line-height: 1.6;
}
.run-conclusion-facts {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 18px;
  margin-top: 12px;
  color: var(--ogs-text-tertiary);
  font-size: 12px;
}
.run-conclusion-facts strong { color: var(--ogs-text); font-family: var(--ogs-mono); }

/* ---- 流程光标轨（signature）：六阶段服务端推导，活动阶段脉冲 ---- */
.phase-rail {
  display: flex;
  align-items: flex-start;
  list-style: none;
  margin: 0;
  padding: 14px 18px;
  background: var(--ogs-surface);
  border: 1px solid var(--ogs-border);
  border-radius: 4px;
  overflow-x: auto;
}
.phase-node {
  position: relative;
  flex: 1;
  min-width: 84px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
}
.phase-node:not(:last-child)::after {
  content: '';
  position: absolute;
  top: 7px;
  left: calc(50% + 12px);
  width: calc(100% - 24px);
  height: 2px;
  background: var(--ogs-border);
}
.phase-node.is-done:not(:last-child)::after { background: var(--ogs-primary-ring); }
.phase-dot {
  width: 16px;
  height: 16px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--ogs-bg);
  border: 2px solid var(--ogs-border);
  z-index: 1;
}
.phase-dot-core {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: transparent;
}
.phase-node.is-done .phase-dot { border-color: var(--ogs-primary); }
.phase-node.is-done .phase-dot-core { background: var(--ogs-primary); }
.phase-node.is-active .phase-dot { border-color: var(--ogs-primary); }
.phase-node.is-active .phase-dot-core {
  background: var(--ogs-primary);
  animation: ogs-phase-pulse 1.6s ease-in-out infinite;
}
.phase-label {
  font-size: 12px;
  font-weight: 600;
  letter-spacing: 0.04em;
  color: var(--ogs-text-tertiary);
  white-space: nowrap;
}
.phase-node.is-done .phase-label { color: var(--ogs-text-secondary); }
.phase-node.is-active .phase-label { color: var(--ogs-primary); }
@keyframes ogs-phase-pulse {
  0%, 100% { transform: scale(1); opacity: 1; }
  50% { transform: scale(1.9); opacity: 0.45; }
}
@media (prefers-reduced-motion: reduce) {
  .phase-node.is-active .phase-dot-core { animation: none; }
}

/* ---- 元信息 ---- */
.run-meta { padding: 14px 18px; display: flex; flex-direction: column; gap: 10px; }
.run-meta-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px 24px;
}
.run-meta-item { display: flex; flex-direction: column; gap: 2px; min-width: 0; }
.run-meta-label {
  font-size: 11px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
  color: var(--ogs-text-tertiary);
}
.run-meta-value {
  font-size: 13px;
  color: var(--ogs-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.run-meta-value em {
  font-style: normal;
  font-family: var(--ogs-mono);
  font-size: 11px;
  color: var(--ogs-text-tertiary);
}
.run-meta-budget {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  border-top: 1px dashed var(--ogs-border-subtle);
  padding-top: 10px;
}
.run-budget-chip {
  font-size: 12px;
  color: var(--ogs-text-secondary);
  background: var(--ogs-bg);
  border: 1px solid var(--ogs-border);
  border-radius: 4px;
  padding: 2px 8px;
}

/* ---- 审批卡 ---- */
.run-approval {
  padding: 16px 18px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  border-color: var(--ogs-warning);
  background: color-mix(in srgb, var(--ogs-warning) 5%, var(--ogs-surface));
}
.run-approval-head { display: flex; flex-direction: column; gap: 2px; }
.run-approval-title { font-weight: 700; font-size: 14px; color: var(--ogs-text); }
.run-approval-hint { font-size: 12px; color: var(--ogs-text-secondary); }
.run-approval-step { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.run-approval-summary { font-size: 13px; color: var(--ogs-text); }
.run-approval-digest {
  font-size: 11px;
  color: var(--ogs-text-tertiary);
  overflow-wrap: anywhere;
}
.run-approval-actions { display: flex; gap: 8px; }

/* ---- 双列布局 ---- */
.run-detail-columns {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 340px;
  gap: 14px;
  align-items: start;
}
.run-detail-main { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.run-detail-side { display: flex; flex-direction: column; gap: 14px; min-width: 0; }
.run-collapsible-panel { overflow: hidden; }
.run-collapsible-head {
  display: flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  list-style: none;
}
.run-collapsible-head::-webkit-details-marker { display: none; }
.run-collapsible-count {
  color: var(--ogs-text-tertiary);
  font-size: 11px;
}
@media (max-width: 1100px) {
  .run-detail-columns { grid-template-columns: 1fr; }
  .run-meta-grid { grid-template-columns: repeat(2, 1fr); }
}

.run-empty {
  padding: 20px;
  font-size: 13px;
  color: var(--ogs-text-tertiary);
  text-align: center;
}

/* ---- 计划步骤 ---- */
.run-steps { list-style: none; margin: 0; padding: 8px 0; }
.run-step {
  display: flex;
  gap: 12px;
  padding: 10px 18px;
}
.run-step + .run-step { border-top: 1px solid var(--ogs-border-subtle); }
.run-step-seq {
  flex-shrink: 0;
  width: 26px;
  font-size: 12px;
  font-weight: 700;
  color: var(--ogs-text-tertiary);
  padding-top: 2px;
}
.run-step-body { display: flex; flex-direction: column; gap: 4px; min-width: 0; }
.run-step-line { display: flex; gap: 6px; align-items: center; flex-wrap: wrap; }
.run-step-title { font-size: 14px; font-weight: 700; color: var(--ogs-text); line-height: 1.4; }
.run-step-command,
.run-step-result {
  display: grid;
  grid-template-columns: 76px minmax(0, 1fr);
  gap: 8px;
  align-items: start;
  font-size: 12px;
  line-height: 1.5;
}
.run-step-command {
  padding: 7px 9px;
  background: var(--ogs-bg);
  border: 1px solid var(--ogs-border-subtle);
  border-radius: 4px;
}
.run-step-command code {
  min-width: 0;
  color: var(--ogs-text);
  font-family: var(--ogs-mono);
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}
.run-step-result { color: var(--ogs-text-secondary); }
.run-step-label {
  color: var(--ogs-text-tertiary);
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.02em;
}
.run-step-artifacts {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}
.run-result-link {
  border: 1px solid color-mix(in srgb, var(--ogs-primary) 35%, var(--ogs-border));
  border-radius: 4px;
  padding: 2px 7px;
  background: var(--ogs-surface);
  color: var(--ogs-primary);
  cursor: pointer;
  font-size: 11px;
  line-height: 1.4;
}
.run-result-link:hover { background: var(--ogs-primary-soft); }
.run-result-link:disabled { cursor: not-allowed; opacity: 0.55; }
.run-result-link:focus-visible {
  outline: 2px solid var(--ogs-primary);
  outline-offset: 2px;
}
.run-audit-details {
  color: var(--ogs-text-tertiary);
  font-size: 11px;
}
.run-audit-details summary {
  cursor: pointer;
  display: inline-block;
  padding: 2px 0;
}
.run-audit-details summary:hover { color: var(--ogs-primary); }
.run-step-digest {
  font-size: 11px;
  color: var(--ogs-text-tertiary);
  overflow-wrap: anywhere;
}
.run-step-note { font-size: 12px; color: var(--ogs-text-secondary); }

/* ---- 事件时间线 ---- */
.run-stream-cursor {
  margin-left: auto;
  font-size: 11px;
  color: var(--ogs-text-tertiary);
}
.run-stream-state {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 4px;
  border: 1px solid var(--ogs-border);
  color: var(--ogs-text-secondary);
  white-space: nowrap;
}
.run-stream-state.is-live {
  color: var(--ogs-success);
  border-color: color-mix(in srgb, var(--ogs-success) 40%, transparent);
  background: color-mix(in srgb, var(--ogs-success) 8%, transparent);
}
.run-stream-state.is-reconnecting, .run-stream-state.is-connecting {
  color: var(--ogs-warning);
  border-color: color-mix(in srgb, var(--ogs-warning) 40%, transparent);
  background: color-mix(in srgb, var(--ogs-warning) 8%, transparent);
}
.run-stream-state.is-error {
  color: var(--ogs-danger);
  border-color: color-mix(in srgb, var(--ogs-danger) 40%, transparent);
  background: color-mix(in srgb, var(--ogs-danger) 8%, transparent);
}
.run-timeline {
  padding: 6px 0;
}
.run-event {
  display: flex;
  gap: 12px;
  padding: 6px 18px;
}
.run-event:hover { background: var(--ogs-bg); }
.run-event-seq {
  flex-shrink: 0;
  width: 44px;
  text-align: right;
  font-size: 11px;
  color: var(--ogs-text-tertiary);
  padding-top: 2px;
}
.run-event-body { display: flex; flex-direction: column; gap: 1px; min-width: 0; }
.run-event-type { font-size: 13px; font-weight: 600; color: var(--ogs-text); }
.run-event-detail {
  font-size: 12px;
  color: var(--ogs-text-secondary);
  overflow-wrap: anywhere;
}

/* ---- 证据 ---- */
.run-side-info { color: var(--ogs-text-tertiary); font-size: 14px; margin-left: 6px; }
.run-evidence { list-style: none; margin: 0; padding: 6px 0; }
.run-evidence-item { padding: 8px 16px; display: flex; flex-direction: column; gap: 3px; }
.run-evidence-item + .run-evidence-item { border-top: 1px solid var(--ogs-border-subtle); }
.run-evidence-line { display: flex; justify-content: space-between; gap: 8px; }
.run-evidence-kind {
  font-size: 11px;
  color: var(--ogs-primary);
  background: var(--ogs-primary-soft);
  border-radius: 4px;
  padding: 0 6px;
}
.run-evidence-time { font-size: 11px; color: var(--ogs-text-tertiary); }
.run-evidence-meaning { font-size: 12px; font-weight: 700; color: var(--ogs-text); }
.run-evidence-result { font-size: 12px; color: var(--ogs-text-secondary); overflow-wrap: anywhere; }
.run-evidence-actions { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 2px; }
.run-evidence-summary { font-size: 12px; color: var(--ogs-text); overflow-wrap: anywhere; padding-top: 4px; }
.run-evidence-refs { font-size: 11px; color: var(--ogs-text-tertiary); }

/* ---- 产物 ---- */
.run-artifacts { list-style: none; margin: 0; padding: 6px 0; }
.run-artifact { padding: 8px 16px; display: flex; flex-direction: column; gap: 4px; }
.run-artifact + .run-artifact { border-top: 1px solid var(--ogs-border-subtle); }
.run-artifact-open {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  background: none;
  border: none;
  padding: 0;
  cursor: pointer;
  text-align: left;
}
.run-artifact-open:disabled { cursor: not-allowed; opacity: 0.6; }
.run-artifact-open:focus-visible {
  outline: 2px solid var(--ogs-primary);
  outline-offset: 2px;
  border-radius: 2px;
}
.run-artifact-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--ogs-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.run-artifact-open:hover .run-artifact-title { color: var(--ogs-primary); }
.run-artifact-kind {
  flex-shrink: 0;
  font-size: 11px;
  color: var(--ogs-text-tertiary);
}
.run-artifact-meta { display: flex; align-items: center; gap: 6px; font-size: 11px; color: var(--ogs-text-tertiary); }
.run-artifact-content {
  max-height: 420px;
  overflow: auto;
  background: var(--ogs-bg);
  border: 1px solid var(--ogs-border);
  border-radius: 4px;
  padding: 12px;
  font-size: 12px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

@media (max-width: 640px) {
  .run-conclusion { padding: 14px; }
  .run-conclusion-head { flex-direction: column; gap: 8px; }
  .run-conclusion-title { font-size: 16px; }
  .run-step { padding: 10px 12px; gap: 8px; }
  .run-step-command,
  .run-step-result { grid-template-columns: 1fr; gap: 2px; }
  .run-artifact-open { align-items: flex-start; flex-direction: column; }
}
</style>
