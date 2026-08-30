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

    <div class="aiops-body" :class="{ 'has-rail': showRail }">
      <aside v-if="showRail" class="aiops-run-rail" :aria-label="t('ai.ops.rail.title')">
        <header>
          <strong>{{ t('ai.ops.rail.title') }}</strong>
          <button type="button" :aria-label="t('common.action.refresh')" @click="load">
            <el-icon :class="{ 'is-loading': loading }"><Refresh /></el-icon>
          </button>
        </header>
        <RunRail :groups="runGroups" :current-run-id="currentRunId" @select="openRun" />
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
      <RunRail :groups="runGroups" :current-run-id="currentRunId" @select="openRun" />
    </el-drawer>
  </section>
</template>

<script setup lang="ts">
import { computed, defineComponent, h, onBeforeUnmount, onMounted, ref, type PropType } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { List, Refresh } from '@element-plus/icons-vue'
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
const railDrawer = ref(false)
let refreshTimer: ReturnType<typeof setInterval> | null = null

const activeTab = computed<'workbench' | 'tasks' | 'alerts'>(() => {
  if (route.name === 'AiOpsAlerts') return 'alerts'
  if (route.name === 'AiOpsTasks') return 'tasks'
  return 'workbench'
})
const showRail = computed(() => ['AiOpsWorkbench', 'AiOpsRunDetail'].includes(String(route.name || '')))
const currentRunId = computed(() => String(route.params.runId || ''))
const activeRuns = computed(() => runs.value.filter(run => !['completed', 'failed', 'cancelled', 'expired'].includes(run.status)))
const alertRuns = computed(() => activeRuns.value.filter(run => run.trigger_type === 'alertmanager'))
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
              class: ['aiops-run-row', { active: run.id === props.currentRunId }],
              onClick: () => emit('select', run.id),
            }, [
              h('i', { class: `is-${run.status}` }),
              h('span', [
                h('strong', summarizeAutonomyGoal(run.trigger_summary || run.goal, 52)),
                h('small', `${run.host_alias} · ${t(`aiRuns.status.${run.status}`)}`),
                h('small', stamp ? formatTimeRel(stamp) : '—'),
              ]),
            ])
          }),
        ])
      : [h('p', { class: 'aiops-run-empty' }, t('ai.ops.rail.empty'))])
  },
})

async function load(): Promise<void> {
  loading.value = true
  try {
    const [nextRuns, nextStatus] = await Promise.allSettled([
      listAutonomyRuns(),
      getAIOpsStatus(),
    ])
    if (nextRuns.status === 'fulfilled') runs.value = nextRuns.value
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
}
.aiops-body.has-rail { grid-template-columns: 286px minmax(0, 1fr); }
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
  margin-left: auto;
  display: grid;
  place-items: center;
  border: 0;
  color: var(--ogs-text-muted);
  background: transparent;
  cursor: pointer;
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
  display: grid;
  grid-template-columns: 8px minmax(0, 1fr);
  gap: 9px;
  padding: 10px 9px;
  border: 1px solid transparent;
  border-radius: 5px;
  color: inherit;
  background: transparent;
  text-align: left;
  cursor: pointer;
}
.aiops-run-list :deep(.aiops-run-row:hover) { background: var(--ogs-bg-sunken); }
.aiops-run-list :deep(.aiops-run-row.active) { border-color: var(--ogs-primary); background: var(--ogs-primary-soft); }
.aiops-run-list :deep(.aiops-run-row > i) {
  width: 7px;
  height: 7px;
  margin-top: 4px;
  border-radius: 50%;
  background: var(--ogs-text-muted);
}
.aiops-run-list :deep(.aiops-run-row > i.is-running),
.aiops-run-list :deep(.aiops-run-row > i.is-waiting_approval),
.aiops-run-list :deep(.aiops-run-row > i.is-needs_attention) { background: var(--ogs-primary); box-shadow: 0 0 0 3px var(--ogs-primary-soft); }
.aiops-run-list :deep(.aiops-run-row strong),
.aiops-run-list :deep(.aiops-run-row small) { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.aiops-run-list :deep(.aiops-run-row strong) { color: var(--ogs-text); font-size: 12px; line-height: 1.35; }
.aiops-run-list :deep(.aiops-run-row small) { margin-top: 4px; color: var(--ogs-text-muted); font-size: 10px; }
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
</style>
