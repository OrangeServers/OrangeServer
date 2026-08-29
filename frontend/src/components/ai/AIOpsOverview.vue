<template>
  <section class="ops-overview" :aria-label="$t('ai.ops.aria')">
    <div class="ops-overview-head">
      <div>
        <span class="ops-kicker">{{ $t('ai.ops.kicker') }}</span>
        <h3>{{ $t('ai.ops.title') }}</h3>
        <p>{{ $t('ai.ops.subtitle') }}</p>
      </div>
      <el-button text :loading="loading" @click="load">
        <el-icon><Refresh /></el-icon>
        {{ $t('ai.ops.refresh') }}
      </el-button>
    </div>

    <div v-if="error" class="ops-error" role="alert">
      <span>{{ error }}</span>
      <el-button text @click="load">{{ $t('ai.ops.retry') }}</el-button>
    </div>

    <template v-else>
      <div class="ops-metrics" :aria-busy="loading">
        <div class="ops-metric is-alert">
          <span>{{ $t('ai.ops.metrics.alerts') }}</span>
          <strong>{{ status?.pending_alerts.length ?? '—' }}</strong>
          <small>{{ configuredText(status?.alertmanager_configured) }}</small>
        </div>
        <div class="ops-metric">
          <span>{{ $t('ai.ops.metrics.active') }}</span>
          <strong>{{ status?.active_runs ?? '—' }}</strong>
          <small>{{ $t('ai.ops.metrics.queued', { n: status?.queued_runs ?? 0 }) }}</small>
        </div>
        <div class="ops-metric">
          <span>{{ $t('ai.ops.metrics.worker') }}</span>
          <strong>{{ status?.autonomy_concurrency ?? '—' }}</strong>
          <small>{{ status?.autonomy_pool || '—' }}</small>
        </div>
        <div class="ops-metric">
          <span>{{ $t('ai.ops.metrics.knowledge') }}</span>
          <strong class="ops-state">{{ knowledgeText }}</strong>
          <small>{{ $t('ai.ops.metrics.index') }}</small>
        </div>
      </div>

      <div class="ops-columns">
        <div class="ops-list">
          <h4>{{ $t('ai.ops.sections.alerts') }}</h4>
          <button v-for="run in status?.pending_alerts" :key="run.id" type="button" @click="openRun(run.id)">
            <span class="ops-dot is-alert" />
            <span><strong>{{ run.trigger_summary || run.goal }}</strong><small>{{ run.host_alias }} · {{ statusText(run.status) }}</small></span>
          </button>
          <p v-if="!status?.pending_alerts.length" class="ops-empty">{{ $t('ai.ops.empty.alerts') }}</p>
        </div>

        <div class="ops-list">
          <h4>{{ $t('ai.ops.sections.running') }}</h4>
          <button v-for="run in status?.running_runs" :key="run.id" type="button" @click="openRun(run.id)">
            <span class="ops-dot" />
            <span><strong>{{ run.goal }}</strong><small>{{ run.host_alias }} · {{ statusText(run.status) }}</small></span>
          </button>
          <p v-if="!status?.running_runs.length" class="ops-empty">{{ $t('ai.ops.empty.running') }}</p>
        </div>

        <div class="ops-list">
          <h4>{{ $t('ai.ops.sections.conclusions') }}</h4>
          <button v-for="run in status?.recent_conclusions" :key="run.id" type="button" @click="openRun(run.id)">
            <span class="ops-outcome" :class="`is-${run.outcome}`">{{ outcomeText(run.outcome) }}</span>
            <span><strong>{{ run.goal }}</strong><small>{{ formatTime(run.completed_at) }}</small></span>
          </button>
          <p v-if="!status?.recent_conclusions.length" class="ops-empty">{{ $t('ai.ops.empty.conclusions') }}</p>
        </div>
      </div>
    </template>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { useRouter } from 'vue-router'
import { getAIOpsStatus } from '@/api/autonomy'
import { t } from '@/i18n'
import type { AIOpsStatus, AutonomyRunOutcome } from '@/types/autonomy'

const router = useRouter()
const status = ref<AIOpsStatus | null>(null)
const loading = ref(false)
const error = ref('')

const knowledgeText = computed(() => {
  const state = status.value?.knowledge_index_state || 'unknown'
  return t(`ai.ops.knowledge.${state}`)
})

function configuredText(value?: boolean): string {
  return t(value ? 'ai.ops.configured' : 'ai.ops.notConfigured')
}

function statusText(value: string): string {
  return t(`aiRuns.status.${value}`)
}

function outcomeText(value: AutonomyRunOutcome | null): string {
  return t(`aiRuns.outcome.${value || 'inconclusive'}`)
}

function formatTime(value: string | null): string {
  return value ? new Date(value).toLocaleString() : t('ai.ops.noTime')
}

function openRun(id: string): void {
  void router.push(`/ai-runs/${encodeURIComponent(id)}`)
}

async function load(): Promise<void> {
  loading.value = true
  error.value = ''
  try {
    status.value = await getAIOpsStatus()
  } catch (reason) {
    error.value = reason instanceof Error ? reason.message : t('ai.ops.loadFailed')
  } finally {
    loading.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.ops-overview {
  margin-bottom: 20px;
  padding: 22px;
  border: 1px solid var(--ogs-border);
  border-radius: var(--ogs-radius);
  background: var(--ogs-surface);
}
.ops-overview-head { display: flex; justify-content: space-between; gap: 20px; align-items: flex-start; }
.ops-kicker { color: var(--ogs-primary); font: 600 11px/1 var(--ogs-mono); letter-spacing: .12em; text-transform: uppercase; }
.ops-overview h3 { margin: 7px 0 5px; color: var(--ogs-text); font-size: 20px; }
.ops-overview-head p { margin: 0; color: var(--ogs-text-secondary); font-size: 13px; }
.ops-error { display: flex; justify-content: space-between; align-items: center; margin-top: 18px; padding: 12px 14px; color: var(--ogs-danger); background: var(--ogs-danger-soft); border-radius: var(--ogs-radius-sm); }
.ops-metrics { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 10px; margin-top: 18px; }
.ops-metric { min-height: 94px; padding: 14px; border: 1px solid var(--ogs-border-subtle); border-radius: var(--ogs-radius-sm); background: var(--ogs-bg-elevated); }
.ops-metric span, .ops-metric small { display: block; color: var(--ogs-text-secondary); font-size: 12px; }
.ops-metric strong { display: block; margin: 6px 0 4px; color: var(--ogs-text); font: 700 26px/1 var(--ogs-mono); }
.ops-metric.is-alert strong { color: var(--ogs-warning); }
.ops-metric .ops-state { overflow: hidden; font: 650 15px/1.25 inherit; text-overflow: ellipsis; white-space: nowrap; }
.ops-columns { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; margin-top: 12px; }
.ops-list { min-width: 0; padding: 14px; border: 1px solid var(--ogs-border-subtle); border-radius: var(--ogs-radius-sm); }
.ops-list h4 { margin: 0 0 8px; color: var(--ogs-text); font-size: 13px; }
.ops-list button { display: flex; width: 100%; gap: 9px; align-items: center; padding: 9px 0; color: inherit; text-align: left; border: 0; border-top: 1px solid var(--ogs-border-subtle); background: transparent; cursor: pointer; }
.ops-list button:hover strong { color: var(--ogs-primary); }
.ops-list button > span:last-child { min-width: 0; }
.ops-list button strong, .ops-list button small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.ops-list button strong { color: var(--ogs-text); font-size: 12px; }
.ops-list button small { margin-top: 3px; color: var(--ogs-text-muted); font-size: 11px; }
.ops-dot { flex: 0 0 7px; width: 7px; height: 7px; border-radius: 50%; background: var(--ogs-primary); box-shadow: 0 0 0 3px var(--ogs-primary-soft); }
.ops-dot.is-alert { background: var(--ogs-warning); box-shadow: 0 0 0 3px var(--ogs-warning-soft); }
.ops-outcome { flex: 0 0 auto; padding: 3px 5px; color: var(--ogs-text-secondary); font-size: 10px; border: 1px solid var(--ogs-border); border-radius: 3px; }
.ops-outcome.is-resolved { color: var(--ogs-success); border-color: var(--ogs-success); }
.ops-outcome.is-not_resolved { color: var(--ogs-danger); border-color: var(--ogs-danger); }
.ops-empty { margin: 12px 0 4px; color: var(--ogs-text-muted); font-size: 12px; }
@media (max-width: 900px) {
  .ops-metrics { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .ops-columns { grid-template-columns: 1fr; }
}
@media (max-width: 560px) {
  .ops-overview { padding: 16px; }
  .ops-overview-head { align-items: center; }
  .ops-overview-head p { display: none; }
}
</style>
