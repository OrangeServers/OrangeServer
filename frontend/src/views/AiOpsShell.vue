<template>
  <section class="aiops-shell">
    <nav class="aiops-tabs" :aria-label="t('ai.ops.navigation')">
      <button
        v-for="tab in tabs"
        :key="tab.name"
        type="button"
        :class="{ active: activeTab === tab.name }"
        @click="router.push({ name: tab.route })"
      >
        {{ t(tab.label) }}
        <span v-if="tab.count" class="aiops-tab-count">{{ tab.count }}</span>
      </button>
      <span class="aiops-runtime">{{ runtimeText }}</span>
    </nav>

    <div
      class="aiops-body"
      :class="{ 'has-rail': showRail, 'rail-collapsed': showRail && railCollapsed }"
    >
      <aside
        v-if="showRail"
        class="aiops-run-rail"
        :class="{ 'is-collapsed': railCollapsed }"
        :aria-label="t('ai.ops.rail.title')"
      >
        <header>
          <strong v-if="!railCollapsed">{{ t('ai.ops.rail.title') }}</strong>
          <button
            v-if="!railCollapsed"
            class="aiops-rail-refresh"
            type="button"
            :aria-label="t('common.action.refresh')"
            @click="load"
          >
            <el-icon :class="{ 'is-loading': loading }"><Refresh /></el-icon>
          </button>
          <button
            class="aiops-rail-toggle"
            type="button"
            :aria-expanded="!railCollapsed"
            :aria-label="t(railCollapsed ? 'ai.ops.rail.expand' : 'ai.ops.rail.collapse')"
            :title="t(railCollapsed ? 'ai.ops.rail.expand' : 'ai.ops.rail.collapse')"
            @click="railCollapsed = !railCollapsed"
          >
            <el-icon><Expand v-if="railCollapsed" /><Fold v-else /></el-icon>
          </button>
        </header>
        <template v-if="railCollapsed">
          <button
            class="aiops-rail-summary"
            type="button"
            :aria-label="t('ai.ops.rail.attentionCount', { n: railAttentionCount })"
            :title="t('ai.ops.rail.attentionCount', { n: railAttentionCount })"
            @click="router.push({ name: 'AiOpsTasks' })"
          >
            <el-icon><List /></el-icon>
            <span v-if="railAttentionCount" class="aiops-rail-summary-count">
              {{ railAttentionCount }}
            </span>
          </button>
        </template>
        <template v-else>
          <div v-if="railError" class="aiops-rail-error" role="alert">
            <span>{{ railError }}</span>
            <button type="button" @click="load">{{ t('common.action.retry') }}</button>
          </div>
          <RunRail
            v-if="runs.length || !railError"
            :groups="runGroups"
            :current-run-id="currentRunId"
            @select="openRun"
          />
        </template>
      </aside>

      <main class="aiops-view" :class="{ 'is-workbench': route.name === 'AiOpsWorkbench' }">
        <div v-if="showRail" class="aiops-mobile-tools">
          <el-button plain size="small" @click="railDrawer = true">
            <el-icon><List /></el-icon>{{ t('ai.ops.rail.mobile') }}
          </el-button>
        </div>
        <router-view v-slot="{ Component }">
          <component :is="Component" :key="route.fullPath" @runs-changed="load" />
        </router-view>
      </main>
    </div>

    <el-drawer
      v-model="railDrawer"
      append-to-body
      direction="ltr"
      size="min(340px, 92vw)"
      :title="t('ai.ops.rail.title')"
    >
      <div v-if="railError" class="aiops-rail-error" role="alert">
        <span>{{ railError }}</span>
        <button type="button" @click="load">{{ t('common.action.retry') }}</button>
      </div>
      <RunRail
        v-if="runs.length || !railError"
        :groups="runGroups" :current-run-id="currentRunId" @select="openRun"
      />
    </el-drawer>
  </section>
</template>

<script setup lang="ts">
import { computed, defineComponent, h, onBeforeUnmount, onMounted, ref, type PropType } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { Expand, Fold, List, Refresh } from '@element-plus/icons-vue'
import { getAIOpsStatus, listAutonomyRuns } from '@/api/autonomy'
import { t } from '@/i18n'
import { formatTimeRel } from '@/utils/datetime'
import { summarizeAutonomyGoal } from '@/utils/autonomyPresentation'
import type { AIOpsStatus, AutonomyRun } from '@/types/autonomy'

interface RunGroup {
  key: string
  runs: AutonomyRun[]
}

const route = useRoute()
const router = useRouter()
const runs = ref<AutonomyRun[]>([])
const status = ref<AIOpsStatus | null>(null)
const loading = ref(false)
const railError = ref('')
const railDrawer = ref(false)
const railCollapsed = ref(false)
let refreshTimer: ReturnType<typeof setInterval> | null = null

const activeTab = computed<'workbench' | 'tasks' | 'alerts'>(() => {
  if (route.name === 'AiOpsAlerts') return 'alerts'
  if (['AiOpsTasks', 'AiOpsRunDetail'].includes(String(route.name || ''))) return 'tasks'
  return 'workbench'
})
const showRail = computed(() => route.name === 'AiOpsWorkbench')
const currentRunId = computed(() => String(route.params.runId || ''))
const activeRuns = computed(() => runs.value.filter(run => !['completed', 'failed', 'cancelled', 'expired'].includes(run.status)))
const alertRuns = computed(() => activeRuns.value.filter(run => run.trigger_type === 'alertmanager'))
const railAttentionCount = computed(() => (
  runs.value.filter(run => ['needs_attention', 'failed'].includes(run.status)).length
))
const tabs = computed(() => [
  { name: 'workbench' as const, route: 'AiOpsWorkbench', label: 'ai.ops.tabs.workbench', count: 0 },
  { name: 'tasks' as const, route: 'AiOpsTasks', label: 'ai.ops.tabs.tasks', count: activeRuns.value.length },
  { name: 'alerts' as const, route: 'AiOpsAlerts', label: 'ai.ops.tabs.alerts', count: alertRuns.value.length },
])
const runtimeText = computed(() => {
  const current = status.value
  if (!current) return t('ai.ops.runtimeUnknown')
  return t('ai.ops.runtime', {
    n: current.autonomy_concurrency,
    state: t(`ai.ops.knowledge.${current.knowledge_index_state || 'unknown'}`),
  })
})

const runGroups = computed<RunGroup[]>(() => {
  const definitions: Array<[string, string[]]> = [
    ['waiting', ['draft', 'waiting_approval']],
    ['running', ['queued', 'running', 'recovering']],
    ['attention', ['needs_attention', 'failed']],
    ['recent', ['completed', 'cancelled', 'expired']],
  ]
  return definitions
    .map(([key, states]) => ({ key, runs: runs.value.filter(run => states.includes(run.status)) }))
    .filter(group => group.runs.length > 0)
})

const RunRail = defineComponent({
  name: 'AiOpsRunRail',
  props: {
    groups: { type: Array as PropType<RunGroup[]>, required: true },
    currentRunId: { type: String, default: '' },
  },
  emits: ['select'],
  setup(props, { emit }) {
    return () => h('div', { class: 'aiops-run-list' }, props.groups.length
      ? props.groups.flatMap(group => [
          h('div', { class: 'aiops-run-group' }, [
            h('span', t(`ai.ops.rail.${group.key}`)),
            h('small', String(group.runs.length)),
          ]),
          ...group.runs.map((run) => {
            const stamp = run.completed_at || run.started_at || run.created_at
            return h('button', {
              type: 'button',
              class: ['aiops-run-row', `is-${run.status}`, { active: run.id === props.currentRunId }],
              'aria-label': `${railTitle(run)}，${railState(run)}，${t(`aiRuns.nextStep.${run.status}`)}`,
              onClick: () => emit('select', run.id),
            }, [
              h('span', { class: 'aiops-run-row-head' }, [
                h('span', { class: 'aiops-run-kind' }, railKind(run.trigger_type)),
                h('span', { class: 'aiops-run-state' }, railState(run)),
              ]),
              h('strong', { class: 'aiops-run-title' }, railTitle(run)),
              h('small', { class: 'aiops-run-context' }, `${run.host_alias} · ${stamp ? formatTimeRel(stamp) : '—'}`),
              h('small', { class: 'aiops-run-next' }, [
                h('span', t('aiRuns.row.next')),
                ` · ${t(`aiRuns.nextStep.${run.status}`)}`,
              ]),
            ])
          }),
        ])
      : [h('p', { class: 'aiops-run-empty' }, t('ai.ops.rail.empty'))])
  },
})

function railKind(triggerType: string): string {
  const known = ['manual', 'chat', 'alertmanager'].includes(triggerType) ? triggerType : 'unknown'
  return t(`ai.ops.rail.kind.${known}`)
}

function railState(run: AutonomyRun): string {
  const statusText = t(`aiRuns.status.${run.status}`)
  return run.outcome ? `${statusText} · ${t(`aiRuns.outcome.${run.outcome}`)}` : statusText
}

function railTitle(run: AutonomyRun): string {
  if (run.trigger_type === 'alertmanager') {
    const match = /^(firing|resolved):\s+(.+)\s+on asset #\d+$/.exec(String(run.trigger_summary || '').trim())
    if (match) {
      const alertState = run.alert_state || match[1]
      return t(alertState === 'resolved'
        ? 'ai.ops.rail.alertResolvedTitle'
        : 'ai.ops.rail.alertFiringTitle', { service: match[2].trim() })
    }
  }
  return summarizeAutonomyGoal(run.trigger_summary || run.goal, 64)
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const [nextRuns, nextStatus] = await Promise.allSettled([
      listAutonomyRuns(),
      getAIOpsStatus(),
    ])
    if (nextRuns.status === 'fulfilled') {
      runs.value = nextRuns.value
      railError.value = ''
    } else {
      railError.value = t('ai.ops.rail.loadFailed')
    }
    if (nextStatus.status === 'fulfilled') status.value = nextStatus.value
  } finally {
    loading.value = false
  }
}

function openRun(runId: string): void {
  railDrawer.value = false
  void router.push({ name: 'AiOpsRunDetail', params: { runId } })
}

onMounted(() => {
  void load()
  refreshTimer = setInterval(load, 30000)
})

onBeforeUnmount(() => {
  if (refreshTimer) clearInterval(refreshTimer)
})
</script>

<style scoped>
.aiops-shell {
  height: 100%;
  min-height: 0;
  display: grid;
  grid-template-rows: 50px minmax(0, 1fr);
  color: var(--ogs-text);
  background: var(--ogs-bg);
}
.aiops-tabs {
  min-width: 0;
  display: flex;
  align-items: stretch;
  padding: 0 18px;
  border-bottom: 1px solid var(--ogs-border);
  background: var(--ogs-surface);
}
.aiops-tabs > button {
  position: relative;
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 0 14px;
  border: 0;
  color: var(--ogs-text-secondary);
  background: transparent;
  cursor: pointer;
  font-size: 12px;
  font-weight: 600;
}
.aiops-tabs > button::after {
  position: absolute;
  right: 14px;
  bottom: -1px;
  left: 14px;
  height: 2px;
  content: '';
  background: transparent;
}
.aiops-tabs > button:hover,
.aiops-tabs > button.active { color: var(--ogs-text); }
.aiops-tabs > button.active::after { background: var(--ogs-primary); }
.aiops-tab-count {
  min-width: 17px;
  padding: 1px 5px;
  border-radius: 9px;
  color: var(--ogs-primary);
  background: var(--ogs-primary-soft);
  font: 10px/1.5 var(--ogs-mono);
  text-align: center;
}
.aiops-runtime {
  min-width: 0;
  margin-left: auto;
  align-self: center;
  overflow: hidden;
  color: var(--ogs-text-muted);
  font: 10px/1 var(--ogs-mono);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.aiops-body {
  min-width: 0;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  transition: grid-template-columns .16s ease;
}
.aiops-body.has-rail { grid-template-columns: 286px minmax(0, 1fr); }
.aiops-body.has-rail.rail-collapsed { grid-template-columns: 52px minmax(0, 1fr); }
.aiops-run-rail {
  min-width: 0;
  min-height: 0;
  display: grid;
  grid-template-rows: 48px minmax(0, 1fr);
  overflow: hidden;
  border-right: 1px solid var(--ogs-border);
  background: var(--ogs-surface);
}
.aiops-run-rail > header {
  height: 48px;
  display: flex;
  align-items: center;
  padding: 0 14px 0 16px;
  border-bottom: 1px solid var(--ogs-border-subtle);
}
.aiops-run-rail > header strong { font-size: 12px; }
.aiops-run-rail > header button {
  width: 28px;
  height: 28px;
  display: grid;
  place-items: center;
  border: 0;
  color: var(--ogs-text-muted);
  background: transparent;
  cursor: pointer;
}
.aiops-run-rail > header button:hover,
.aiops-run-rail > header button:focus-visible { color: var(--ogs-primary); }
.aiops-rail-refresh { margin-left: auto; }
.aiops-rail-toggle { margin-left: 4px; }
.aiops-run-rail.is-collapsed > header { justify-content: center; padding: 0; }
.aiops-run-rail.is-collapsed .aiops-rail-toggle { margin: 0; }
.aiops-rail-summary {
  position: relative;
  width: 36px;
  height: 36px;
  margin: 10px auto;
  display: grid;
  place-items: center;
  border: 1px solid transparent;
  border-radius: 4px;
  color: var(--ogs-text-secondary);
  background: transparent;
  cursor: pointer;
}
.aiops-rail-summary:hover,
.aiops-rail-summary:focus-visible {
  border-color: var(--ogs-primary);
  color: var(--ogs-primary);
  background: var(--ogs-primary-soft);
}
.aiops-rail-summary-count {
  position: absolute;
  top: 2px;
  right: 1px;
  min-width: 14px;
  padding: 0 3px;
  border-radius: 7px;
  color: var(--ogs-bg);
  background: var(--ogs-primary);
  font: 700 9px/14px var(--ogs-mono);
  text-align: center;
}
.aiops-rail-error {
  margin: 10px 8px;
  padding: 10px;
  border: 1px solid var(--ogs-danger-soft);
  border-radius: 4px;
  color: var(--ogs-text-secondary);
  background: var(--ogs-danger-soft);
  font-size: 11px;
  line-height: 1.45;
}
.aiops-rail-error button {
  display: block;
  margin-top: 6px;
  padding: 0;
  border: 0;
  color: var(--ogs-primary);
  background: transparent;
  cursor: pointer;
  font: inherit;
  font-weight: 700;
}
.aiops-run-list { min-height: 0; overflow-y: auto; padding: 8px; }
.aiops-run-list :deep(.aiops-run-group) {
  display: flex;
  align-items: center;
  margin: 10px 7px 5px;
  color: var(--ogs-text-muted);
  font: 600 10px/1.4 var(--ogs-mono);
  letter-spacing: .08em;
  text-transform: uppercase;
}
.aiops-run-list :deep(.aiops-run-group small) { margin-left: auto; font: inherit; }
.aiops-run-list :deep(.aiops-run-row) {
  width: 100%;
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 5px;
  padding: 10px 9px;
  border: 1px solid transparent;
  border-left: 3px solid transparent;
  border-radius: 5px;
  color: inherit;
  background: transparent;
  text-align: left;
  cursor: pointer;
}
.aiops-run-list :deep(.aiops-run-row:hover) { background: var(--ogs-bg-sunken); }
.aiops-run-list :deep(.aiops-run-row.active) { border-color: var(--ogs-primary); background: var(--ogs-primary-soft); }
.aiops-run-list :deep(.aiops-run-row.is-running),
.aiops-run-list :deep(.aiops-run-row.is-waiting_approval),
.aiops-run-list :deep(.aiops-run-row.is-needs_attention) { border-left-color: var(--ogs-primary); }
.aiops-run-list :deep(.aiops-run-row.is-failed) { border-left-color: var(--ogs-danger); }
.aiops-run-list :deep(.aiops-run-row.is-completed) { border-left-color: var(--ogs-success); }
.aiops-run-list :deep(.aiops-run-row-head) {
  display: flex;
  align-items: center;
  gap: 8px;
}
.aiops-run-list :deep(.aiops-run-kind) {
  padding: 1px 5px;
  border: 1px solid var(--ogs-border);
  border-radius: 3px;
  color: var(--ogs-text-secondary);
  font: 600 9px/1.4 var(--ogs-mono);
}
.aiops-run-list :deep(.aiops-run-state) {
  margin-left: auto;
  overflow: hidden;
  color: var(--ogs-text-secondary);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.aiops-run-list :deep(.aiops-run-row.is-failed .aiops-run-state),
.aiops-run-list :deep(.aiops-run-row.is-needs_attention .aiops-run-state) { color: var(--ogs-danger); }
.aiops-run-list :deep(.aiops-run-title) {
  display: -webkit-box;
  overflow: hidden;
  color: var(--ogs-text);
  font-size: 12px;
  line-height: 1.4;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}
.aiops-run-list :deep(.aiops-run-context),
.aiops-run-list :deep(.aiops-run-next) {
  display: block;
  overflow: hidden;
  color: var(--ogs-text-secondary);
  font-size: 10px;
  line-height: 1.4;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.aiops-run-list :deep(.aiops-run-next span) { color: var(--ogs-primary); }
.aiops-run-list :deep(.aiops-run-empty) { padding: 24px 10px; color: var(--ogs-text-muted); font-size: 12px; text-align: center; }
.aiops-view { min-width: 0; min-height: 0; overflow-y: auto; }
.aiops-view.is-workbench { overflow: hidden; }
.aiops-mobile-tools { display: none; }

@media (max-width: 860px) {
  .aiops-body.has-rail { grid-template-columns: minmax(0, 1fr); }
  .aiops-run-rail { display: none; }
  .aiops-mobile-tools {
    height: 44px;
    display: flex;
    align-items: center;
    padding: 0 12px;
    border-bottom: 1px solid var(--ogs-border-subtle);
    background: var(--ogs-surface);
  }
  .aiops-view.is-workbench { display: grid; grid-template-rows: 44px minmax(0, 1fr); }
}

@media (max-width: 520px) {
  .aiops-tabs { padding: 0 4px; }
  .aiops-tabs > button { flex: 1; justify-content: center; padding-inline: 6px; }
  .aiops-tabs > button::after { right: 8px; left: 8px; }
  .aiops-runtime { display: none; }
}

@media (prefers-reduced-motion: reduce) {
  .aiops-body { transition: none; }
}
</style>
