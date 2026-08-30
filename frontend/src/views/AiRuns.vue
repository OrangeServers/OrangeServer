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
        :placeholder="t('aiRuns.filter.searchPlaceholder')"
        :aria-label="t('aiRuns.filter.search')"
      />
      <el-select v-model="statusFilter" class="runs-filter-item" size="default">
        <el-option :label="`${t('aiRuns.filter.status')}: ${t('aiRuns.filter.all')}`" value="all" />
        <el-option
          v-for="status in RUN_STATUSES" :key="status"
          :label="`${t('aiRuns.filter.status')}: ${t(`aiRuns.status.${status}`)}`"
          :value="status"
        />
      </el-select>
      <el-select v-model="outcomeFilter" class="runs-filter-item" size="default">
        <el-option :label="`${t('aiRuns.filter.outcome')}: ${t('aiRuns.filter.all')}`" value="all" />
        <el-option
          v-for="outcome in RUN_OUTCOMES" :key="outcome"
          :label="`${t('aiRuns.filter.outcome')}: ${t(`aiRuns.outcome.${outcome}`)}`"
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
        v-else-if="!loading && !loadError && scopedRuns.length === 0"
        :description="t(props.alertsOnly ? 'aiRuns.alerts.empty' : 'aiRuns.empty')"
      />
      <el-empty
        v-else-if="!loading && !loadError && filteredRuns.length === 0"
        :description="t('aiRuns.emptyFiltered')"
      />
      <div v-else class="runs-groups">
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
      width="640px"
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
import { getHostList, getSysUserList } from '@/api'
import {
  createAutonomyRun, getAutonomyStatus, listAutonomyRuns,
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
      .join(' ')
      .toLocaleLowerCase()
    if (!haystack.includes(query)) return false
  }
  return true
}))

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
  void router.push({ name: 'AiOpsRunDetail', params: { runId: run.id } })
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
      getSysUserList(),
    ]) as unknown as [
      { host_list_msg?: Array<{ id: number; alias?: string; host_ip?: string }> },
      { sys_user_list_msg?: Array<{ id: number; alias?: string; host_user?: string }> },
    ]
    hostOptions.value = (hostRes.host_list_msg || []).map((host) => ({
      id: Number(host.id),
      label: host.alias ? `${host.alias} · ${host.host_ip || ''}` : String(host.host_ip || host.id),
    }))
    sysUserOptions.value = (userRes.sys_user_list_msg || []).map((user) => ({
      id: Number(user.id),
      label: user.alias || String(user.host_user || user.id),
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
}
@media (max-width: 520px) {
  .runs-filter-item { width: 100%; }
  .runs-count { width: 100%; margin-left: 0; }
  .runs-group-head { align-items: flex-start; flex-direction: column; gap: 4px; }
  .runs-group-hint { max-width: 100%; }
  .runs-row-context { gap: 4px; }
  .runs-row-context > span:last-child { display: none; }
}
</style>
