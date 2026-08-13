<!--
  AI 自治任务列表页（M1/S3 切片 6）
  原则：列表只转述服务端权威状态；创建按钮可用性由 /ai/autonomy/status 决定，
  其余操作交给详情页的 allowed_operations。
-->
<template>
  <div class="ai-runs-page">
    <header class="page-header">
      <div class="page-title">
        <div>
          <span class="page-eyebrow">{{ t('aiRuns.eyebrow') }}</span>
          <h2>{{ t('aiRuns.title') }}</h2>
          <p class="page-subtitle">{{ t('aiRuns.subtitle') }}</p>
        </div>
      </div>
      <div class="page-actions">
        <el-button :icon="Refresh" @click="loadRuns">{{ t('common.action.refresh') }}</el-button>
        <el-button type="primary" :icon="Plus" :disabled="!canCreate" @click="openCreate">
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

    <!-- 过滤条：状态 / 结论 -->
    <div class="runs-filter">
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
      <span class="runs-count">{{ filteredRuns.length }} / {{ runs.length }}</span>
    </div>

    <!-- 列表主体：loading / error / empty / table 四态 -->
    <div v-loading="loading" class="panel runs-panel">
      <el-alert
        v-if="loadError && !loading"
        type="error" :closable="false" show-icon
        :title="loadError"
      >
        <el-button size="small" @click="loadRuns">{{ t('common.action.retry') }}</el-button>
      </el-alert>
      <el-empty
        v-else-if="!loading && !loadError && runs.length === 0"
        :description="t('aiRuns.empty')"
      />
      <el-empty
        v-else-if="!loading && !loadError && filteredRuns.length === 0"
        :description="t('aiRuns.empty')"
      />
      <el-table
        v-else
        :data="filteredRuns"
        class="runs-table"
        @row-click="(row: AutonomyRun) => router.push(`/ai-runs/${row.id}`)"
      >
        <el-table-column :label="t('aiRuns.table.goal')" min-width="260">
          <template #default="{ row }">
            <div class="runs-goal">{{ row.goal }}</div>
            <div class="runs-goal-id">{{ row.id }}</div>
          </template>
        </el-table-column>
        <el-table-column :label="t('aiRuns.table.host')" min-width="150">
          <template #default="{ row }">
            <div>{{ row.host_alias }}</div>
            <div class="runs-cell-sub">#{{ row.host_id }}</div>
          </template>
        </el-table-column>
        <el-table-column :label="t('aiRuns.table.credential')" prop="system_user_alias" min-width="110" />
        <el-table-column :label="t('aiRuns.table.mode')" min-width="110">
          <template #default="{ row }">
            <span class="runs-mode">{{ modeLabel(row.mode) }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('aiRuns.table.status')" min-width="130">
          <template #default="{ row }">
            <el-tag size="small" :type="statusTagType(row.status)" effect="light" round>
              {{ t(`aiRuns.status.${row.status}`) }}
            </el-tag>
            <div v-if="row.cancel_requested && !isTerminalRunStatus(row.status)" class="runs-cell-sub">
              {{ t('aiRuns.detail.cancelRequested') }}
            </div>
          </template>
        </el-table-column>
        <el-table-column :label="t('aiRuns.table.outcome')" min-width="100">
          <template #default="{ row }">
            <el-tag
              v-if="row.outcome" size="small"
              :type="outcomeTagType(row.outcome)" effect="plain" round
            >
              {{ t(`aiRuns.outcome.${row.outcome}`) }}
            </el-tag>
            <span v-else class="runs-cell-sub">—</span>
          </template>
        </el-table-column>
        <el-table-column :label="t('aiRuns.table.owner')" prop="owner" min-width="100" />
        <el-table-column :label="t('aiRuns.table.updated')" min-width="110">
          <template #default="{ row }">
            <span class="runs-time">{{ lastActivity(row) }}</span>
          </template>
        </el-table-column>
      </el-table>
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
          <el-radio-group v-model="form.mode">
            <el-radio v-for="mode in CREATE_MODES" :key="mode" :value="mode">
              {{ t(`aiRuns.mode.${mode}`) }}
            </el-radio>
          </el-radio-group>
          <div class="runs-mode-hint">{{ t(`aiRuns.dialog.modeHint.${form.mode}`) }}</div>
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
          <div class="runs-mode-hint runs-budget-hint">{{ t('aiRuns.dialog.budgetHint') }}</div>
          <div class="runs-budget-grid">
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
import { formatTimeRel } from '@/utils/datetime'

const router = useRouter()

// ===== 列表状态 =====
const runs = ref<AutonomyRun[]>([])
const loading = ref(false)
const loadError = ref('')
const readiness = ref<AutonomyReadiness | null>(null)
const statusFilter = ref<string>('all')
const outcomeFilter = ref<string>('all')

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

const KNOWN_MODES = new Set([
  'ask', 'ai_review', 'auto', 'custom', 'read_only', 'assisted', 'lab_autonomous',
])

const filteredRuns = computed<AutonomyRun[]>(() => runs.value.filter((run) => {
  if (statusFilter.value !== 'all' && run.status !== statusFilter.value) return false
  if (outcomeFilter.value !== 'all' && run.outcome !== outcomeFilter.value) return false
  return true
}))

/** 创建按钮可用性只由服务端就绪度决定；未加载完成前不禁用 */
const canCreate = computed<boolean>(() => readiness.value === null || readiness.value.ready)

function reasonText(reason: string): string {
  const known = ['ready', 'feature_disabled', 'redis_not_configured', 'checkpoint_unavailable', 'worker_unavailable']
  return known.includes(reason) ? t(`aiRuns.reason.${reason}`) : reason
}

function modeLabel(mode: string): string {
  return KNOWN_MODES.has(mode) ? t(`aiRuns.mode.${mode}`) : mode
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
  const stamp = run.completed_at || run.started_at || run.created_at
  return stamp ? formatTimeRel(stamp) : '—'
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
    router.push(`/ai-runs/${run.id}`)
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
.runs-filter-item { width: 220px; }
.runs-count {
  margin-left: auto;
  font-family: var(--ogs-mono);
  font-size: 12px;
  color: var(--ogs-text-tertiary);
}
.runs-panel { min-height: 320px; }
.runs-table { cursor: pointer; }
.runs-goal {
  font-weight: 600;
  color: var(--ogs-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 420px;
}
.runs-goal-id, .runs-cell-sub {
  font-family: var(--ogs-mono);
  font-size: 11px;
  color: var(--ogs-text-tertiary);
  margin-top: 2px;
}
.runs-mode {
  font-family: var(--ogs-mono);
  font-size: 12px;
  color: var(--ogs-text-secondary);
}
.runs-time {
  font-size: 12px;
  color: var(--ogs-text-secondary);
  white-space: nowrap;
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
.runs-budget-hint { margin-bottom: 8px; }
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
  .runs-budget-grid { grid-template-columns: repeat(2, 1fr); }
  .runs-goal { max-width: 220px; }
}
</style>
