<!--
  AI 自治任务列表页（M1/S3 切片 6）
  原则：列表只转述服务端权威状态；创建按钮可用性由 /ai/autonomy/status 决定，
  其余操作交给详情页的 allowed_operations。任务按服务端状态分组展示。
-->
<template>
  <div class="ai-runs-page">
    <header class="page-header">
      <div class="page-title">
        <div>
          <span class="page-eyebrow">{{ t(props.alertsOnly ? 'aiRuns.alerts.eyebrow' : 'aiRuns.eyebrow') }}</span>
          <h2>{{ t(props.alertsOnly ? 'aiRuns.alerts.title' : 'aiRuns.title') }}</h2>
          <p class="page-subtitle">{{ t(props.alertsOnly ? 'aiRuns.alerts.subtitle' : 'aiRuns.subtitle') }}</p>
        </div>
      </div>
      <div class="page-actions">
        <el-button :icon="Refresh" @click="loadRuns">{{ t('common.action.refresh') }}</el-button>
        <el-button v-if="!props.alertsOnly" type="primary" :icon="Plus" :disabled="!canCreate" @click="openCreate">
          {{ t('aiRuns.create') }}
        </el-button>
      </div>
    </header>

    <!-- 就绪度横幅：只展示服务端返回的 reason 码对应文案 -->
    <el-alert
      v-if="readiness && !readiness.enabled"
      class="runs-banner" type="warning" :closable="false" show-icon
      :title="t('aiRuns.featureDisabled')"
    />
    <el-alert
      v-else-if="readiness && readiness.enabled && !readiness.ready"
      class="runs-banner" type="warning" :closable="false" show-icon
      :title="t('aiRuns.notReady', { reason: reasonText(readiness.reason) })"
    />

    <!-- 过滤条：搜索 / 状态 / 结论 -->
    <div class="runs-filter">
      <el-input
        v-model="searchText"
        class="runs-search"
        clearable
        :placeholder="t(props.alertsOnly ? 'aiRuns.alerts.searchPlaceholder' : 'aiRuns.filter.searchPlaceholder')"
        :aria-label="t(props.alertsOnly ? 'aiRuns.alerts.search' : 'aiRuns.filter.search')"
      />
      <el-select v-model="statusFilter" class="runs-filter-item" size="default">
        <el-option
          :label="`${t(props.alertsOnly ? 'aiRuns.alerts.runStatus' : 'aiRuns.filter.status')}: ${t('aiRuns.filter.all')}`"
          value="all"
        />
        <el-option
          v-for="status in RUN_STATUSES" :key="status"
          :label="`${t(props.alertsOnly ? 'aiRuns.alerts.runStatus' : 'aiRuns.filter.status')}: ${t(`aiRuns.status.${status}`)}`"
          :value="status"
        />
      </el-select>
      <el-select v-model="outcomeFilter" class="runs-filter-item" size="default">
        <el-option
          :label="`${t(props.alertsOnly ? 'aiRuns.alerts.runOutcome' : 'aiRuns.filter.outcome')}: ${t('aiRuns.filter.all')}`"
          value="all"
        />
        <el-option
          v-for="outcome in RUN_OUTCOMES" :key="outcome"
          :label="`${t(props.alertsOnly ? 'aiRuns.alerts.runOutcome' : 'aiRuns.filter.outcome')}: ${t(`aiRuns.outcome.${outcome}`)}`"
          :value="outcome"
        />
      </el-select>
      <span class="runs-count">
        {{ t('aiRuns.filter.count', { visible: filteredRuns.length, total: scopedRuns.length }) }}
      </span>
    </div>

    <!-- 列表主体：loading / error / empty / 分组任务行四态 -->
    <div v-loading="loading" class="panel runs-panel">
      <el-alert
        v-if="loadError && !loading"
        type="error" :closable="false" show-icon
        :title="loadError"
      >
        <el-button size="small" @click="loadRuns">{{ t('common.action.retry') }}</el-button>
      </el-alert>
      <el-empty
        v-if="!loading && !loadError && scopedRuns.length === 0"
        :description="t(props.alertsOnly ? 'aiRuns.alerts.empty' : 'aiRuns.empty')"
      />
      <el-empty
        v-else-if="!loading && !loadError && filteredRuns.length === 0"
        :description="t('aiRuns.emptyFiltered')"
      />
      <div v-else-if="filteredRuns.length && props.alertsOnly" class="alerts-board" :aria-label="t('aiRuns.alerts.listLabel')">
        <p class="alerts-data-boundary">{{ t('aiRuns.alerts.dataBoundary') }}</p>
        <div class="alerts-cards">
          <div v-for="group in alertGroups" :key="group.key" class="alert-group">
            <button
              type="button"
              class="alert-card"
              :class="`is-alert-${alertState(group.latest)}`"
              :aria-label="alertCardLabel(group.latest, group.runs.length)"
              @click="openRun(group.latest)"
            >
              <span class="alert-overview">
                <span class="alert-signal-line">
                  <span class="alert-signal-label">{{ t('aiRuns.alerts.currentState') }}</span>
                  <span class="alert-signal">
                    <span class="alert-signal-dot" aria-hidden="true" />
                    {{ alertStateText(group.latest) }}
                  </span>
                </span>
                <span class="alert-service-label">{{ t('aiRuns.alerts.service') }}</span>
                <strong class="alert-service">{{ alertService(group.latest) }}</strong>
                <span class="alert-asset">{{ t('aiRuns.row.asset') }} · {{ knownText(group.latest.host_alias) }}</span>
              </span>

              <span class="alert-run">
                <span class="alert-run-head">
                  <span class="alert-fact-label">{{ t('aiRuns.alerts.linkedRun') }}</span>
                  <span class="alert-run-id">#{{ group.latest.id.slice(0, 8) }}</span>
                </span>
                <span class="alert-run-tags">
                  <el-tag size="small" :type="statusTagType(group.latest.status)" effect="light" round>
                    {{ t(`aiRuns.status.${group.latest.status}`) }}
                  </el-tag>
                  <el-tag size="small" :type="outcomeTagType(group.latest.outcome || '')" effect="plain" round>
                    {{ group.latest.outcome ? t(`aiRuns.outcome.${group.latest.outcome}`) : t('aiRuns.outcome.none') }}
                  </el-tag>
                </span>
                <span class="alert-run-created">
                  {{ t('aiRuns.alerts.occurrences', { n: group.runs.length }) }}
                  <span aria-hidden="true">·</span>
                  {{ t('aiRuns.alerts.latestNotification') }} ·
                  <time v-if="alertTimestamp(group.latest)" :datetime="alertTimestamp(group.latest)">
                    {{ formatTimeAbs(alertTimestamp(group.latest)) || t('aiRuns.alerts.unknown') }}
                  </time>
                  <span v-else>{{ t('aiRuns.alerts.unknown') }}</span>
                </span>
              </span>
              <span class="runs-row-arrow" aria-hidden="true">→</span>
            </button>
            <details v-if="group.runs.length > 1" class="alert-history">
              <summary>{{ t('aiRuns.alerts.history', { n: group.runs.length - 1 }) }}</summary>
              <button
                v-for="run in group.runs.slice(1)"
                :key="run.id"
                type="button"
                class="alert-history-row"
                :aria-label="alertCardLabel(run, 1)"
                @click="openRun(run)"
              >
                <time v-if="alertTimestamp(run)" :datetime="alertTimestamp(run)">{{ formatTimeAbs(alertTimestamp(run)) }}</time>
                <span>{{ alertStateText(run) }}</span>
                <span>{{ t(`aiRuns.status.${run.status}`) }}<template v-if="run.outcome"> · {{ t(`aiRuns.outcome.${run.outcome}`) }}</template></span>
                <code>#{{ run.id.slice(0, 8) }}</code>
              </button>
            </details>
          </div>
        </div>
      </div>
      <div v-else-if="filteredRuns.length" class="runs-groups">
        <section v-for="group in runGroups" :key="group.key" class="runs-group">
          <header class="runs-group-head">
            <div class="runs-group-title">
              <span>{{ t(`aiRuns.group.${group.key}.title`) }}</span>
              <span class="runs-group-count">{{ group.runs.length }}</span>
            </div>
            <span class="runs-group-hint">{{ t(`aiRuns.group.${group.key}.hint`) }}</span>
          </header>
          <div class="runs-rows">
            <button
              v-for="run in group.runs"
              :key="run.id"
              type="button"
              class="runs-row"
              :class="`is-${run.status}`"
              :aria-label="`${targetSummary(run)} · ${t(`aiRuns.status.${run.status}`)}`"
              @click="openRun(run)"
            >
              <span class="runs-row-main">
                <span class="runs-row-title" :title="run.trigger_summary || run.goal">
                  {{ targetSummary(run) }}
                </span>
                <span class="runs-row-context">
                  <span class="runs-row-trigger">{{ t('aiRuns.row.trigger') }} · {{ triggerSource(run.trigger_type) }}</span>
                  <span aria-hidden="true">·</span>
                  <span class="runs-row-asset" :title="run.host_alias">{{ t('aiRuns.row.asset') }} · {{ run.host_alias }}</span>
                  <span aria-hidden="true">·</span>
                  <span>{{ t('aiRuns.row.credential') }} · {{ run.system_user_alias }}</span>
                </span>
              </span>
              <span class="runs-row-status">
                <el-tag size="small" :type="statusTagType(run.status)" effect="light" round>
                  {{ t(`aiRuns.status.${run.status}`) }}
                </el-tag>
                <el-tag v-if="run.outcome" size="small" :type="outcomeTagType(run.outcome)" effect="plain" round>
                  {{ t(`aiRuns.outcome.${run.outcome}`) }}
                </el-tag>
              </span>
              <span class="runs-row-next">
                <span class="runs-row-label">{{ t('aiRuns.row.next') }}</span>
                <span>{{ nextStep(run.status) }}</span>
              </span>
              <span class="runs-row-time">
                <span>{{ lastActivity(run) }}</span>
                <time :datetime="activityTimestamp(run)">{{ lastActivityAbsolute(run) }}</time>
              </span>
              <span class="runs-row-arrow" aria-hidden="true">→</span>
            </button>
          </div>
        </section>
      </div>
    </div>

    <!-- 新建草稿对话框 -->
    <el-dialog
      v-model="dialogVisible"
      :title="t('aiRuns.dialog.title')"
      width="min(640px, calc(100vw - 24px))"
      :close-on-click-modal="false"
      destroy-on-close
    >
      <el-form ref="formRef" :model="form" :rules="rules" label-position="top" @submit.prevent>
        <el-form-item :label="t('aiRuns.dialog.goal')" prop="goal">
          <el-input
            v-model="form.goal" type="textarea" :rows="2" maxlength="512" show-word-limit
            :placeholder="t('aiRuns.dialog.goalPlaceholder')"
          />
        </el-form-item>
        <div class="runs-dialog-grid">
          <el-form-item :label="t('aiRuns.dialog.host')" prop="host_id">
            <el-select v-model="form.host_id" filterable :placeholder="t('aiRuns.dialog.hostPlaceholder')">
              <el-option
                v-for="host in hostOptions" :key="host.id"
                :label="host.label" :value="host.id"
              />
            </el-select>
          </el-form-item>
          <el-form-item :label="t('aiRuns.dialog.systemUser')" prop="system_user_id">
            <el-select v-model="form.system_user_id" filterable :placeholder="t('aiRuns.dialog.systemUserPlaceholder')">
              <el-option
                v-for="user in sysUserOptions" :key="user.id"
                :label="user.label" :value="user.id"
              />
            </el-select>
          </el-form-item>
        </div>
        <el-form-item :label="t('aiRuns.dialog.mode')" prop="mode">
          <el-radio-group v-model="form.mode" class="runs-mode-options">
            <div
              v-for="mode in CREATE_MODES"
              :key="mode"
              class="runs-mode-card"
              :class="{ active: form.mode === mode }"
              @click="form.mode = mode"
            >
              <el-radio :value="mode">{{ t(`aiRuns.mode.${mode}`) }}</el-radio>
              <span>{{ t(`aiRuns.dialog.modeHint.${mode}`) }}</span>
            </div>
          </el-radio-group>
        </el-form-item>
        <el-form-item v-if="form.mode === 'custom'" :label="t('aiRuns.dialog.categories')" prop="categories">
          <el-checkbox-group v-model="form.categories">
            <el-checkbox
              v-for="category in AUTONOMY_ACTION_CATEGORIES" :key="category"
              :value="category"
            >
              {{ t(`aiRuns.dialog.category.${category}`) }}
            </el-checkbox>
          </el-checkbox-group>
        </el-form-item>
        <el-form-item :label="t('aiRuns.dialog.budget')">
          <div class="runs-budget-head">
            <div class="runs-mode-hint">{{ t('aiRuns.dialog.budgetHint') }}</div>
            <el-button text size="small" @click="budgetExpanded = !budgetExpanded">
              {{ budgetExpanded ? t('aiRuns.dialog.budgetAdvancedClose') : t('aiRuns.dialog.budgetAdvanced') }}
            </el-button>
          </div>
          <div v-if="budgetExpanded" class="runs-budget-grid">
            <div v-for="field in BUDGET_FIELDS" :key="field" class="runs-budget-cell">
              <div class="runs-budget-label">{{ t(`aiRuns.dialog.budgetField.${field}`) }}</div>
              <el-input-number
                v-model="form.budget[field]"
                :min="1" :controls="false" :placeholder="'—'"
              />
            </div>
          </div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">{{ t('common.action.cancel') }}</el-button>
        <el-button type="primary" :loading="submitting" @click="submitCreate">
          {{ t('aiRuns.dialog.submit') }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, type FormInstance, type FormRules } from 'element-plus'
import { Plus, Refresh } from '@element-plus/icons-vue'
import { t } from '@/i18n'
import { getHostList } from '@/api'
import {
  createAutonomyRun, getAutonomyStatus, listAutonomyRuns, listAutonomySystemUsers,
} from '@/api/autonomy'
import { isTerminalRunStatus, AUTONOMY_ACTION_CATEGORIES } from '@/types/autonomy'
import type {
  AutonomyBudget, AutonomyReadiness, AutonomyRun, AutonomyRunOutcome, AutonomyRunStatus,
} from '@/types/autonomy'
import { formatTimeAbs, formatTimeRel } from '@/utils/datetime'
import { summarizeAutonomyGoal } from '@/utils/autonomyPresentation'

const router = useRouter()
const props = defineProps<{ alertsOnly?: boolean }>()
const emit = defineEmits<{ (event: 'runs-changed'): void }>()

// ===== 列表状态 =====
const runs = ref<AutonomyRun[]>([])
const loading = ref(false)
const loadError = ref('')
const readiness = ref<AutonomyReadiness | null>(null)
const statusFilter = ref<string>('all')
const outcomeFilter = ref<string>('all')
const searchText = ref('')

const RUN_STATUSES: readonly AutonomyRunStatus[] = [
  'draft', 'queued', 'running', 'waiting_approval', 'recovering',
  'needs_attention', 'completed', 'failed', 'cancelled', 'expired',
]
const RUN_OUTCOMES: readonly AutonomyRunOutcome[] = ['resolved', 'not_resolved', 'inconclusive']
const CREATE_MODES = ['ask', 'ai_review', 'auto', 'custom'] as const
const BUDGET_FIELDS = [
  'duration_seconds', 'max_loops', 'max_actions',
  'command_timeout_seconds', 'step_output_bytes', 'run_artifact_bytes',
] as const

const RUN_GROUPS: ReadonlyArray<{
  key: 'waiting' | 'running' | 'attention' | 'recent'
  statuses: readonly AutonomyRunStatus[]
}> = [
  { key: 'waiting', statuses: ['draft', 'waiting_approval'] },
  { key: 'running', statuses: ['queued', 'running', 'recovering'] },
  { key: 'attention', statuses: ['needs_attention', 'failed'] },
  { key: 'recent', statuses: ['completed', 'cancelled', 'expired'] },
]

const NEXT_STEP_KEYS: Record<AutonomyRunStatus, string> = {
  draft: 'aiRuns.nextStep.draft',
  queued: 'aiRuns.nextStep.queued',
  running: 'aiRuns.nextStep.running',
  waiting_approval: 'aiRuns.nextStep.waiting_approval',
  recovering: 'aiRuns.nextStep.recovering',
  needs_attention: 'aiRuns.nextStep.needs_attention',
  completed: 'aiRuns.nextStep.completed',
  failed: 'aiRuns.nextStep.failed',
  cancelled: 'aiRuns.nextStep.cancelled',
  expired: 'aiRuns.nextStep.expired',
}

const scopedRuns = computed<AutonomyRun[]>(() => (
  props.alertsOnly
    ? runs.value.filter(run => run.trigger_type === 'alertmanager')
    : runs.value
))

const filteredRuns = computed<AutonomyRun[]>(() => scopedRuns.value.filter((run) => {
  if (statusFilter.value !== 'all' && run.status !== statusFilter.value) return false
  if (outcomeFilter.value !== 'all' && run.outcome !== outcomeFilter.value) return false
  const query = searchText.value.trim().toLocaleLowerCase()
  if (query) {
    const haystack = [run.goal, run.id, run.host_alias, run.system_user_alias]
      .concat(run.trigger_summary, run.trigger_ref || '')
      .join(' ')
      .toLocaleLowerCase()
    if (!haystack.includes(query)) return false
  }
  return true
}))

interface AlertGroup {
  key: string
  latest: AutonomyRun
  runs: AutonomyRun[]
}

const alertGroups = computed<AlertGroup[]>(() => {
  const groups = new Map<string, AutonomyRun[]>()
  for (const run of filteredRuns.value) {
    const service = alertService(run)
    const key = service === t('aiRuns.alerts.unknown')
      ? `run:${run.id}`
      : `${run.host_id}:${service.toLocaleLowerCase()}`
    const group = groups.get(key) || []
    group.push(run)
    groups.set(key, group)
  }
  return [...groups.entries()].map(([key, groupedRuns]) => {
    const ordered = [...groupedRuns].sort((left, right) => (
      alertTimestampValue(right) - alertTimestampValue(left)
    ))
    return { key, latest: ordered[0], runs: ordered }
  })
})

const runGroups = computed(() => RUN_GROUPS
  .map(group => ({
    key: group.key,
    runs: filteredRuns.value.filter(run => group.statuses.includes(run.status)),
  }))
  .filter(group => group.runs.length > 0))

/** 创建按钮可用性只由服务端就绪度决定；未加载完成前不禁用 */
const canCreate = computed<boolean>(() => readiness.value === null || readiness.value.ready)

function reasonText(reason: string): string {
  const known = ['ready', 'feature_disabled', 'redis_not_configured', 'checkpoint_unavailable', 'worker_unavailable']
  return known.includes(reason) ? t(`aiRuns.reason.${reason}`) : reason
}

function targetSummary(run: AutonomyRun): string {
  return summarizeAutonomyGoal(run.trigger_summary || run.goal, 112)
}

function triggerSource(triggerType: string): string {
  if (triggerType === 'manual') return t('aiRuns.trigger.manual')
  if (triggerType === 'chat') return t('aiRuns.trigger.chat')
  if (triggerType === 'alertmanager') return t('aiRuns.trigger.alertmanager')
  return triggerType || t('aiRuns.trigger.unknown')
}

type AlertState = 'firing' | 'resolved' | 'unknown'

function alertService(run: AutonomyRun): string {
  const match = /^(?:firing|resolved):\s+(.+)\s+on asset #\d+$/.exec(run.trigger_summary.trim())
  return knownText(match?.[1])
}

function alertState(run: AutonomyRun): AlertState {
  return run.alert_state === 'firing' || run.alert_state === 'resolved'
    ? run.alert_state
    : 'unknown'
}

function alertStateText(run: AutonomyRun): string {
  return t(`aiRuns.alerts.signal.${alertState(run)}`)
}

function knownText(value: string | null | undefined): string {
  return value?.trim() || t('aiRuns.alerts.unknown')
}

function alertCardLabel(run: AutonomyRun, occurrences: number): string {
  return t('aiRuns.alerts.cardLabel', {
    signal: alertStateText(run),
    service: alertService(run),
    status: t(`aiRuns.status.${run.status}`),
    n: occurrences,
  })
}

function alertTimestamp(run: AutonomyRun): string {
  return run.alert_updated_at || run.created_at || ''
}

function alertTimestampValue(run: AutonomyRun): number {
  const value = Date.parse(alertTimestamp(run))
  return Number.isFinite(value) ? value : 0
}

function nextStep(status: AutonomyRunStatus): string {
  return t(NEXT_STEP_KEYS[status])
}

function statusTagType(status: string): '' | 'success' | 'warning' | 'info' | 'danger' {
  if (status === 'running') return ''
  if (status === 'completed') return 'success'
  if (status === 'waiting_approval' || status === 'recovering') return 'warning'
  if (status === 'failed' || status === 'needs_attention') return 'danger'
  return 'info'
}

function outcomeTagType(outcome: string): '' | 'success' | 'warning' | 'info' | 'danger' {
  if (!outcome) return 'info'
  if (outcome === 'resolved') return 'success'
  if (outcome === 'not_resolved') return 'danger'
  return 'warning'
}

/** 最近动态：优先 completed_at，其次 started_at / created_at */
function lastActivity(run: AutonomyRun): string {
  const stamp = activityTimestamp(run)
  return stamp ? formatTimeRel(stamp) : '—'
}

function lastActivityAbsolute(run: AutonomyRun): string {
  const stamp = activityTimestamp(run)
  return stamp ? formatTimeAbs(stamp) || '—' : '—'
}

function activityTimestamp(run: AutonomyRun): string {
  return run.completed_at || run.started_at || run.created_at || ''
}

function openRun(run: AutonomyRun): void {
  void router.push({
    name: 'AiOpsRunDetail',
    params: { runId: run.id },
  })
}

async function loadRuns(): Promise<void> {
  loading.value = true
  loadError.value = ''
  try {
    runs.value = await listAutonomyRuns()
  } catch (err) {
    loadError.value = err instanceof Error ? err.message : t('aiRuns.loadFailed')
  } finally {
    loading.value = false
  }
}

async function loadReadiness(): Promise<void> {
  try {
    readiness.value = await getAutonomyStatus()
  } catch {
    readiness.value = null
  }
}

onMounted(() => {
  loadReadiness()
  loadRuns()
})

// ===== 新建草稿 =====
interface OptionItem { id: number; label: string }
interface DraftForm {
  goal: string
  host_id: number | undefined
  system_user_id: number | undefined
  mode: string
  categories: string[]
  budget: Partial<Record<(typeof BUDGET_FIELDS)[number], number | undefined>>
}

const dialogVisible = ref(false)
const submitting = ref(false)
const formRef = ref<FormInstance>()
const hostOptions = ref<OptionItem[]>([])
const sysUserOptions = ref<OptionItem[]>([])
const optionsLoaded = ref(false)
const budgetExpanded = ref(false)

const form = reactive<DraftForm>({
  goal: '',
  host_id: undefined,
  system_user_id: undefined,
  mode: 'ask',
  categories: [],
  budget: {},
})

const rules: FormRules = {
  goal: [{ required: true, message: () => t('aiRuns.dialog.goalRequired'), trigger: 'blur' }],
  host_id: [{ required: true, message: () => t('aiRuns.dialog.hostRequired'), trigger: 'change' }],
  system_user_id: [{ required: true, message: () => t('aiRuns.dialog.systemUserRequired'), trigger: 'change' }],
  categories: [{
    validator: (_rule: unknown, value: string[], callback: (error?: Error) => void) => {
      if (form.mode === 'custom' && (!value || value.length === 0)) {
        callback(new Error(t('aiRuns.dialog.categoriesRequired')))
        return
      }
      callback()
    },
    trigger: 'change',
  }],
}

async function openCreate(): Promise<void> {
  dialogVisible.value = true
  budgetExpanded.value = false
  if (optionsLoaded.value) return
  try {
    const [hostRes, userRes] = await Promise.all([
      getHostList(),
      listAutonomySystemUsers(),
    ]) as unknown as [
      { host_list_msg?: Array<{ id: number; alias?: string; host_ip?: string }> },
      Array<{ id: number; alias: string }>,
    ]
    hostOptions.value = (hostRes.host_list_msg || []).map((host) => ({
      id: Number(host.id),
      label: host.alias ? `${host.alias} · ${host.host_ip || ''}` : String(host.host_ip || host.id),
    }))
    sysUserOptions.value = userRes.map((user) => ({
      id: Number(user.id),
      label: user.alias || String(user.id),
    }))
    optionsLoaded.value = true
  } catch {
    ElMessage.error(t('aiRuns.dialog.optionsLoadFailed'))
  }
}

async function submitCreate(): Promise<void> {
  if (!formRef.value) return
  try {
    await formRef.value.validate()
  } catch {
    return
  }
  submitting.value = true
  try {
    const budget: AutonomyBudget = {}
    for (const field of BUDGET_FIELDS) {
      const value = form.budget[field]
      if (typeof value === 'number' && Number.isFinite(value)) budget[field] = value
    }
    const run = await createAutonomyRun({
      goal: form.goal,
      host_id: Number(form.host_id),
      system_user_id: Number(form.system_user_id),
      mode: form.mode,
      ...(Object.keys(budget).length ? { budget } : {}),
      ...(form.mode === 'custom' ? { profile: { action_categories: form.categories } } : {}),
    })
    dialogVisible.value = false
    emit('runs-changed')
    void router.push({ name: 'AiOpsRunDetail', params: { runId: run.id } })
  } catch (err) {
    ElMessage.error(err instanceof Error ? err.message : t('common.crud.operationFail'))
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped>
.ai-runs-page {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.runs-banner { border-radius: 4px; }
.runs-filter {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
}
.runs-search { width: min(360px, 100%); }
.runs-filter-item { width: 220px; }
.runs-count {
  margin-left: auto;
  font-size: 12px;
  color: var(--ogs-text-tertiary);
}
.runs-panel { min-height: 320px; }
.alerts-board {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.alerts-data-boundary {
  margin: 0;
  padding: 9px 12px;
  border-left: 3px solid var(--ogs-warning);
  color: var(--ogs-text-secondary);
  background: var(--ogs-warning-soft);
  font-size: 12px;
  line-height: 1.5;
}
.alerts-cards {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.alert-group { min-width: 0; }
.alert-card {
  width: 100%;
  min-width: 0;
  display: grid;
  grid-template-columns: minmax(220px, 1.15fr) minmax(300px, 1fr) 16px;
  align-items: stretch;
  gap: 16px;
  padding: 0;
  overflow: hidden;
  border: 1px solid var(--ogs-border);
  border-left: 4px solid var(--ogs-text-muted);
  border-radius: 6px;
  color: var(--ogs-text);
  background: var(--ogs-surface);
  text-align: left;
  cursor: pointer;
  transition: border-color .15s ease, box-shadow .15s ease, transform .15s ease;
}
.alert-card:hover {
  border-color: var(--ogs-primary);
  box-shadow: 0 5px 18px rgba(15, 23, 42, .08);
  transform: translateY(-1px);
}
.alert-card:focus-visible {
  outline: 2px solid var(--ogs-primary);
  outline-offset: 2px;
}
.alert-card.is-alert-firing { border-left-color: var(--ogs-danger); }
.alert-card.is-alert-resolved { border-left-color: var(--ogs-success); }
.alert-overview,
.alert-run {
  display: flex;
  min-width: 0;
  flex-direction: column;
  justify-content: center;
  gap: 5px;
  padding: 14px 0;
}
.alert-overview { padding-left: 14px; }
.alert-signal-line,
.alert-run-head,
.alert-run-tags {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
}
.alert-signal-label,
.alert-service-label,
.alert-fact-label {
  color: var(--ogs-text-secondary);
  font: 10px/1.2 var(--ogs-mono);
  letter-spacing: .06em;
  text-transform: uppercase;
}
.alert-signal {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  color: var(--ogs-text-secondary);
  font: 700 11px/1.2 var(--ogs-mono);
}
.alert-signal-dot {
  width: 7px;
  height: 7px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: var(--ogs-text-muted);
}
.is-alert-firing .alert-signal { color: var(--ogs-danger); }
.is-alert-firing .alert-signal-dot { background: var(--ogs-danger); }
.is-alert-resolved .alert-signal { color: var(--ogs-success); }
.is-alert-resolved .alert-signal-dot { background: var(--ogs-success); }
.alert-service {
  overflow: hidden;
  font-size: 15px;
  line-height: 1.35;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.alert-asset,
.alert-run-created {
  overflow: hidden;
  color: var(--ogs-text-secondary);
  font-size: 11px;
  line-height: 1.35;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.is-mono,
.alert-run-id { font-family: var(--ogs-mono); }
.alert-run-id {
  overflow: hidden;
  color: var(--ogs-text-secondary);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.alert-run-tags { flex-wrap: wrap; }
.alert-run-created time { font-family: var(--ogs-mono); }
.alert-card > .runs-row-arrow {
  align-self: center;
  padding-right: 12px;
}
.alert-history {
  margin: -1px 8px 0;
  overflow: hidden;
  border: 1px solid var(--ogs-border);
  border-top: 0;
  border-radius: 0 0 5px 5px;
  background: var(--ogs-bg-elevated);
}
.alert-history > summary {
  padding: 8px 12px;
  color: var(--ogs-text-secondary);
  font-size: 11px;
  cursor: pointer;
}
.alert-history > summary:focus-visible {
  outline: 2px solid var(--ogs-primary);
  outline-offset: -2px;
}
.alert-history-row {
  width: 100%;
  min-width: 0;
  display: grid;
  grid-template-columns: 120px minmax(80px, .7fr) minmax(160px, 1fr) 72px;
  gap: 10px;
  padding: 9px 12px;
  border: 0;
  border-top: 1px solid var(--ogs-border-subtle);
  color: var(--ogs-text-secondary);
  background: transparent;
  text-align: left;
  cursor: pointer;
  font-size: 11px;
}
.alert-history-row:hover,
.alert-history-row:focus-visible { background: var(--ogs-bg-sunken); }
.alert-history-row:focus-visible { outline: 2px solid var(--ogs-primary); outline-offset: -2px; }
.alert-history-row time,
.alert-history-row code { font-family: var(--ogs-mono); }
.alert-history-row code { color: var(--ogs-text-secondary); text-align: right; }
.runs-groups {
  display: flex;
  flex-direction: column;
  gap: 22px;
  min-width: 0;
}
.runs-group { min-width: 0; }
.runs-group-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 14px;
  padding: 0 4px 8px;
  border-bottom: 1px solid var(--ogs-border);
}
.runs-group-title {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--ogs-text);
  font-size: 13px;
  font-weight: 700;
}
.runs-group-count {
  min-width: 20px;
  padding: 1px 6px;
  border-radius: 10px;
  color: var(--ogs-text-secondary);
  background: var(--ogs-bg-sunken);
  font: 11px/1.5 var(--ogs-mono);
  text-align: center;
}
.runs-group-hint {
  color: var(--ogs-text-tertiary);
  font-size: 11px;
}
.runs-rows {
  overflow: hidden;
  border: 1px solid var(--ogs-border);
  border-top: 0;
  border-radius: 0 0 5px 5px;
}
.runs-row {
  width: 100%;
  min-width: 0;
  display: grid;
  grid-template-columns: minmax(220px, 1.65fr) auto minmax(170px, 1fr) auto 16px;
  align-items: center;
  gap: 14px;
  padding: 13px 14px;
  border: 0;
  border-bottom: 1px solid var(--ogs-border-subtle);
  color: var(--ogs-text);
  background: var(--ogs-surface);
  text-align: left;
  cursor: pointer;
  transition: background-color .15s ease, box-shadow .15s ease;
}
.runs-row:last-child { border-bottom: 0; }
.runs-row:hover,
.runs-row:focus-visible { background: var(--ogs-bg-sunken); }
.runs-row:focus-visible {
  outline: 2px solid var(--ogs-primary);
  outline-offset: -2px;
}
.runs-row.is-waiting_approval,
.runs-row.is-running,
.runs-row.is-needs_attention {
  box-shadow: inset 2px 0 0 var(--ogs-primary);
}
.runs-row-main,
.runs-row-next,
.runs-row-status,
.runs-row-time { min-width: 0; }
.runs-row-main {
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.runs-row-title {
  display: block;
  overflow: hidden;
  color: var(--ogs-text);
  font-size: 13px;
  font-weight: 650;
  line-height: 1.4;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.runs-row-context {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  overflow: hidden;
  color: var(--ogs-text-tertiary);
  font-size: 11px;
  line-height: 1.4;
  white-space: nowrap;
}
.runs-row-trigger {
  flex: 0 0 auto;
  color: var(--ogs-primary);
  font-family: var(--ogs-mono);
}
.runs-row-asset {
  overflow: hidden;
  text-overflow: ellipsis;
}
.runs-row-status {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: flex-start;
  gap: 5px;
}
.runs-row-next {
  display: flex;
  flex-direction: column;
  gap: 4px;
  overflow: hidden;
  color: var(--ogs-text-secondary);
  font-size: 12px;
  line-height: 1.35;
}
.runs-row-next > span:last-child {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.runs-row-label {
  color: var(--ogs-text-muted);
  font: 10px/1.2 var(--ogs-mono);
  letter-spacing: .08em;
  text-transform: uppercase;
}
.runs-row-time {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 4px;
  color: var(--ogs-text-secondary);
  font-size: 12px;
  line-height: 1.2;
  white-space: nowrap;
}
.runs-row-time time {
  color: var(--ogs-text-tertiary);
  font: 10px/1.2 var(--ogs-mono);
}
.runs-row-arrow {
  color: var(--ogs-text-muted);
  font-size: 16px;
  text-align: right;
}
.runs-dialog-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0 16px;
}
.runs-mode-hint {
  font-size: 12px;
  color: var(--ogs-text-tertiary);
  margin-top: 4px;
  line-height: 1.5;
}
.runs-mode-options {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  width: 100%;
}
.runs-mode-card {
  display: flex;
  flex-direction: column;
  gap: 3px;
  padding: 9px 10px;
  border: 1px solid var(--ogs-border);
  border-radius: 4px;
  background: var(--ogs-bg);
  cursor: pointer;
  transition: border-color 0.15s ease, background 0.15s ease;
}
.runs-mode-card:hover,
.runs-mode-card.active {
  border-color: var(--ogs-primary);
  background: var(--ogs-primary-soft);
}
.runs-mode-card :deep(.el-radio) { margin-right: 0; }
.runs-mode-card > span {
  padding-left: 24px;
  color: var(--ogs-text-tertiary);
  font-size: 11px;
  line-height: 1.45;
}
.runs-budget-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}
.runs-budget-head .runs-mode-hint { margin-top: 0; }
.runs-budget-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 10px 16px;
  width: 100%;
}
.runs-budget-cell :deep(.el-input-number) { width: 100%; }
.runs-budget-label {
  font-size: 12px;
  color: var(--ogs-text-secondary);
  margin-bottom: 4px;
}
@media (max-width: 900px) {
  .runs-dialog-grid { grid-template-columns: 1fr; }
  .runs-mode-options { grid-template-columns: 1fr; }
  .runs-budget-grid { grid-template-columns: repeat(2, 1fr); }
  .runs-row {
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 9px 12px;
    padding: 12px;
  }
  .runs-row-main { grid-column: 1; grid-row: 1; }
  .runs-row-status { grid-column: 2; grid-row: 1; justify-content: flex-end; }
  .runs-row-next { grid-column: 1 / -1; grid-row: 2; }
  .runs-row-time { grid-column: 1; grid-row: 3; align-items: flex-start; }
  .runs-row-arrow { grid-column: 2; grid-row: 3; }
  .runs-row-context { flex-wrap: wrap; white-space: normal; }
  .runs-row-title { white-space: normal; }
  .runs-row-status :deep(.el-tag) { font-size: 10px; }
  .runs-group-hint {
    max-width: 48%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .alert-card {
    grid-template-columns: minmax(180px, 1fr) minmax(260px, 1fr) 16px;
    gap: 12px;
  }
  .alert-card > .runs-row-arrow { grid-column: 3; }
}
@media (max-width: 520px) {
  .runs-filter-item { width: 100%; }
  .runs-count { width: 100%; margin-left: 0; }
  .runs-group-head { align-items: flex-start; flex-direction: column; gap: 4px; }
  .runs-group-hint { max-width: 100%; }
  .runs-row-context { gap: 4px; }
  .runs-row-context > span:last-child { display: none; }
  .alert-card {
    grid-template-columns: minmax(0, 1fr) 16px;
    gap: 0 10px;
  }
  .alert-overview { grid-column: 1; grid-row: 1; padding-right: 12px; }
  .alert-run {
    grid-column: 1 / -1;
    grid-row: 2;
    flex-wrap: wrap;
    padding: 12px 14px;
    border-top: 1px solid var(--ogs-border-subtle);
  }
  .alert-card > .runs-row-arrow { grid-column: 2; grid-row: 1; }
  .alert-history { margin-inline: 4px; }
  .alert-history-row {
    grid-template-columns: 1fr auto;
    gap: 4px 10px;
  }
  .alert-history-row code { grid-column: 2; grid-row: 1; }
}
</style>
