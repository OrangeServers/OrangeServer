<template>
  <article class="draft-card" aria-live="polite">
    <header class="draft-head">
      <span class="draft-mark">
        <el-icon><DocumentAdd /></el-icon>
      </span>
      <div class="draft-title">
        <span>{{ $t('ai.autonomyDraft.kicker') }}</span>
        <h3>{{ draft.goal || $t('ai.autonomyDraft.untitled') }}</h3>
      </div>
      <div class="draft-tags">
        <el-tag v-if="modeLabel" effect="plain" size="small">{{ modeLabel }}</el-tag>
        <el-tag type="info" effect="light" size="small">{{ statusLabel }}</el-tag>
      </div>
    </header>

    <p class="draft-hint">
      <el-icon><InfoFilled /></el-icon>
      <span>{{ $t('ai.autonomyDraft.hint') }}</span>
    </p>

    <footer class="draft-actions">
      <span v-if="draft.host_alias">
        {{ $t('ai.autonomyDraft.host') }} <code>{{ draft.host_alias }}</code>
      </span>
      <el-button
        plain
        size="small"
        :disabled="!draft.run_id"
        @click="openRun"
      >
        {{ $t('ai.autonomyDraft.open') }}
        <el-icon><ArrowRight /></el-icon>
      </el-button>
    </footer>
  </article>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import {
  ArrowRight,
  DocumentAdd,
  InfoFilled,
} from '@element-plus/icons-vue'
import { t } from '@/i18n'
import type { AiAutonomyDraft } from '@/types/ai'

const props = defineProps<{ draft: AiAutonomyDraft }>()

const router = useRouter()

const CHAT_DRAFT_MODES = new Set(['ask', 'ai_review', 'auto', 'custom'])

const modeLabel = computed(() => {
  const mode = props.draft.mode || ''
  return CHAT_DRAFT_MODES.has(mode) ? t(`aiRuns.mode.${mode}`) : ''
})

const statusLabel = computed(() => {
  const status = props.draft.status || 'draft'
  return status === 'draft' ? t('aiRuns.status.draft') : status
})

function openRun(): void {
  const runId = props.draft.run_id
  if (!runId) return
  router.push({ name: 'AiOpsRunDetail', params: { runId } })
}
</script>

<style scoped>
.draft-card {
  width: calc(100% - 40px);
  margin-left: 40px;
  overflow: hidden;
  border: 1px solid var(--ogs-border);
  border-top: 3px solid var(--ogs-primary);
  border-radius: 12px;
  background: var(--ogs-surface, #fff);
  box-shadow: 0 10px 28px rgb(15 23 42 / 5%);
}
.draft-head {
  display: grid;
  grid-template-columns: 36px minmax(0, 1fr) auto;
  align-items: center;
  gap: 10px;
  padding: 12px 16px;
}
.draft-mark {
  width: 34px;
  height: 34px;
  display: grid;
  place-items: center;
  border-radius: 9px;
  color: var(--ogs-primary);
  background: var(--ogs-primary-soft);
}
.draft-title { min-width: 0; }
.draft-title > span {
  color: var(--ogs-primary);
  font-family: var(--ogs-mono);
  font-size: 9px;
  font-weight: 700;
  letter-spacing: .13em;
  text-transform: uppercase;
}
.draft-title h3 {
  margin: 2px 0 0;
  overflow: hidden;
  color: var(--ogs-text);
  font-size: 14px;
  line-height: 1.45;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.draft-tags { display: flex; gap: 5px; }
.draft-hint {
  margin: 0;
  display: flex;
  align-items: flex-start;
  gap: 7px;
  padding: 9px 16px;
  border-top: 1px solid var(--ogs-border-subtle);
  color: var(--ogs-text-secondary);
  background: var(--ogs-bg-soft, #fafafa);
  font-size: 11px;
  line-height: 1.55;
}
.draft-hint .el-icon { margin-top: 2px; flex: 0 0 auto; color: var(--ogs-info); }
.draft-actions {
  min-height: 46px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 16px;
  border-top: 1px solid var(--ogs-border-subtle);
}
.draft-actions > span {
  overflow: hidden;
  color: var(--ogs-text-muted);
  font-size: 10px;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.draft-actions code {
  color: var(--ogs-text-secondary);
  font-family: var(--ogs-mono);
}
.draft-card :deep(.el-button:focus-visible) { outline: 2px solid var(--ogs-primary); outline-offset: 2px; }
@media (max-width: 760px) {
  .draft-card { width: 100%; margin-left: 0; }
  .draft-head { padding: 11px 12px; }
  .draft-hint { padding-inline: 12px; }
  .draft-actions { padding-inline: 12px; }
}
@media (max-width: 440px) {
  .draft-tags { display: none; }
}
@media (prefers-reduced-motion: reduce) {
  .draft-card :deep(.is-loading) { animation: none; }
}
</style>
