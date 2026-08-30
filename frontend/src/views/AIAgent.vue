<template>
  <div class="ai-agent-page">
    <div class="agent-workspace">
      <section class="conversation-panel" :aria-label="$t('ai.conversation.aria')">
        <div class="conversation-head">
          <div class="conversation-identity">
            <span class="agent-mark" :class="{ thinking: isThinking }"><OrangeMark /></span>
            <div class="conversation-title-block">
              <span class="conversation-kicker">{{ $t('ai.conversation.current') }}</span>
              <strong :title="currentTitle">{{ currentTitle }}</strong>
            </div>
          </div>
          <div class="conversation-head-actions">
            <el-button v-if="isAdmin" text @click="openModelSettings">
              <el-icon><Setting /></el-icon><span class="head-action-label">{{ $t('ai.header.modelSettings') }}</span>
            </el-button>
            <el-button text @click="conversationDrawer = true">
              <el-icon><Clock /></el-icon><span class="head-action-label">{{ $t('ai.header.recent') }}</span>
            </el-button>
            <el-button text @click="startNewConversation">
              <el-icon><Plus /></el-icon><span class="head-action-label">{{ $t('ai.header.new') }}</span>
            </el-button>
            <el-button class="mobile-context-button" text @click="contextDrawer = true">
              <el-icon><View /></el-icon><span class="head-action-label">{{ $t('ai.conversation.contextButton') }}</span>
            </el-button>
          </div>
        </div>

        <div
          ref="messageScroller"
          class="message-stream"
          @scroll.passive="handleMessageScroll"
        >
          <div v-if="loadingConversation" class="stream-loading">
            <el-icon class="is-loading"><Loading /></el-icon>
            {{ $t('ai.conversation.restoring') }}
          </div>

          <div v-else-if="!timeline.length" class="agent-empty">
            <div class="empty-terminal" aria-hidden="true">
              <span class="terminal-led" />
              <span class="terminal-led" />
              <span class="terminal-led" />
              <code>ops@orangeserver:~$ <b>ask agent</b></code>
            </div>
            <h3>{{ $t('ai.empty.title') }}</h3>
            <p>{{ $t('ai.empty.desc') }}</p>
            <div class="prompt-grid">
              <button
                v-for="prompt in examplePrompts"
                :key="prompt.title"
                type="button"
                class="prompt-card"
                @click="useExample(prompt.text)"
              >
                <span class="prompt-card-icon">
                  <el-icon><component :is="prompt.icon" /></el-icon>
                </span>
                <span>
                  <strong>{{ prompt.title }}</strong>
                  <small>{{ prompt.text }}</small>
                </span>
                <el-icon class="prompt-arrow"><ArrowRight /></el-icon>
              </button>
            </div>
          </div>

          <template v-else>
            <div
              v-for="item in timeline"
              :key="timelineItemKey(item)"
              class="timeline-row"
              :class="`is-${item.kind}`"
            >
              <template v-if="item.kind === 'message'">
                <div class="message-avatar">
                  <el-avatar v-if="item.value.role === 'user'" :size="30" :src="store.user.avatar" class="user-avatar">{{ userInitial }}</el-avatar>
                  <OrangeMark v-else :class="{ thinking: isThinking }" />
                </div>
                <div class="message-body" :class="{ 'has-error': item.value.error }">
                  <div class="message-content" :title="formatTime(item.value.created_at)">
                    <span
                      v-if="item.value.role === 'assistant' && !item.value.error"
                      class="md-body"
                      v-html="renderMarkdown(item.value.content)"
                    />
                    <template v-else-if="item.value.error">
                      <strong class="message-error-label">{{ $t('ai.msg.errorSummary') }}</strong>
                      <span>{{ readableMessage(item.value.content) }}</span>
                      <details class="message-error-details">
                        <summary>{{ $t('ai.msg.viewTechnicalDetails') }}</summary>
                        <code>{{ item.value.content }}</code>
                      </details>
                    </template>
                    <template v-else>{{ item.value.content }}</template>
                    <span v-if="item.value.streaming" class="stream-caret" />
                  </div>
                </div>
              </template>

              <div
                v-else-if="item.kind === 'tool'"
                class="tool-event"
                :class="`status-${item.value.status}`"
                role="status"
                aria-live="polite"
              >
                <span class="tool-event-icon">
                  <el-icon v-if="item.value.status === 'running'" class="is-loading"><Loading /></el-icon>
                  <el-icon v-else-if="item.value.status === 'success'"><CircleCheckFilled /></el-icon>
                  <el-icon v-else><WarningFilled /></el-icon>
                </span>
                <div>
                  <div class="tool-event-title">
                    <strong>{{ item.value.label }}</strong>
                    <span class="tool-event-state">
                      {{ item.value.status === 'running' ? $t('ai.tool.running') : item.value.status === 'success' ? $t('ai.tool.done') : $t('ai.tool.failed') }}
                    </span>
                    <details class="tool-event-technical">
                      <summary>{{ $t('ai.tool.technicalDetails') }}</summary>
                      <code>{{ item.value.tool }}</code>
                    </details>
                  </div>
                  <p v-if="item.value.summary">
                    {{ item.value.status === 'error' ? readableToolSummary(item.value.summary) : item.value.summary }}
                  </p>
                  <details v-if="item.value.summary && item.value.status === 'error'" class="tool-error-details">
                    <summary>{{ $t('ai.msg.viewTechnicalDetails') }}</summary>
                    <code>{{ item.value.summary }}</code>
                  </details>
                </div>
              </div>

              <DiagnosticRunCard
                v-else-if="item.kind === 'diagnostic'"
                :run="item.value"
                @cancel="cancelDiagnosticRun"
                @open-evidence="openDiagnosticEvidence"
              />

              <AutonomyDraftCard
                v-else-if="item.kind === 'autonomy_draft'"
                :draft="item.value"
              />

            </div>
          </template>

          <div v-if="sending && !hasStreamingMessage" class="thinking-row">
            <span class="thinking-mark"><OrangeMark class="thinking" /></span>
            <span>{{ $t('ai.thinking') }}</span>
            <i /><i /><i />
          </div>
        </div>

        <footer class="composer-shell">
          <div class="composer">
            <el-input
              v-model="draft"
              type="textarea"
              resize="none"
              :autosize="{ minRows: 2, maxRows: 6 }"
              :disabled="!canChat"
              :placeholder="composerPlaceholder"
              :aria-label="$t('ai.composer.inputAria')"
              @keydown="handleComposerKeydown"
            />
            <div class="composer-toolbar">
              <div class="composer-controls">
                <el-select
                  v-model="selectedProvider"
                  class="provider-select"
                  popper-class="provider-select-popper"
                  :placeholder="$t('ai.composer.selectModel')"
                  size="small"
                  :disabled="sending"
                  @change="handleProviderChange"
                >
                  <el-option
                    v-for="provider in providers"
                    :key="provider.provider_code"
                    :value="provider.provider_code"
                    :label="providerLabel(provider)"
                    :disabled="!providerAvailable(provider)"
                  >
                    <div class="provider-option">
                      <span class="provider-option-name">
                        <svg viewBox="0 0 24 24" width="14" height="14" :fill="providerBrandColor(provider.provider_code)" v-html="providerIcon(provider.provider_code)" />
                        {{ provider.name || providerName(provider.provider_code) }}
                      </span>
                      <span class="provider-option-detail">
                        <span class="provider-option-model">{{ provider.model || $t('ai.provider.modelUnset') }}</span>
                        <small v-if="!providerAvailable(provider)">{{ providerUnavailableReason(provider) }}</small>
                      </span>
                    </div>
                  </el-option>
                </el-select>
                <el-radio-group
                  v-model="selectedContextMode"
                  size="small"
                  class="context-mode-toggle"
                  :disabled="sending"
                  @change="handleContextModeChange"
                >
                  <el-radio-button value="standard_256k">256K</el-radio-button>
                  <el-radio-button
                    value="deep_diagnostic_1m"
                    :disabled="!activeProviderSupportsDeep"
                    :title="activeProviderSupportsDeep ? $t('ai.composer.deepAvailable') : $t('ai.composer.deepMissing')"
                  >{{ $t('ai.composer.deepOption') }}</el-radio-button>
                </el-radio-group>
                <span v-if="!activeProviderReady" class="provider-status">
                  <i />
                  {{ providerUnavailableReason(activeProvider) }}
                </span>
              </div>
              <el-button
                class="send-button"
                type="primary"
                :disabled="!draft.trim() || !canChat"
                :loading="sending"
                :aria-label="$t('ai.composer.sendAria')"
                @click="sendMessage"
              >
                <el-icon v-if="!sending"><Promotion /></el-icon>
              </el-button>
            </div>
          </div>
          <div class="composer-hint">
            <span>{{ $t('ai.composer.hintKeys') }}</span>
            <span><i /> {{ $t('ai.composer.hintConfirm') }}</span>
          </div>
        </footer>
      </section>

      <aside class="context-panel" :aria-label="$t('ai.context.aria')">
        <ContextContent />
      </aside>
    </div>

    <el-drawer v-model="conversationDrawer" :title="$t('ai.drawer.conversations')" size="380px" class="conversation-drawer">
      <div class="drawer-toolbar">
        <span>{{ $t('ai.drawer.keepRecent') }}</span>
        <el-button type="primary" plain @click="startNewConversation">
          <el-icon><Plus /></el-icon>{{ $t('ai.drawer.newShort') }}
        </el-button>
      </div>
      <div v-if="!conversations.length" class="drawer-empty">
        <el-icon><ChatLineRound /></el-icon>
        <p>{{ $t('ai.drawer.emptyTitle') }}</p>
        <span>{{ $t('ai.drawer.emptyDesc') }}</span>
      </div>
      <div v-else class="conversation-list">
        <div
          v-for="conversation in conversations"
          :key="conversation.id"
          class="conversation-item"
          :class="{ active: conversation.id === currentConversationId }"
          role="button"
          tabindex="0"
          @click="openConversation(conversation.id)"
          @keydown.enter.prevent="openConversation(conversation.id)"
          @keydown.space.prevent="openConversation(conversation.id)"
        >
          <span class="conversation-item-icon"><el-icon><ChatLineRound /></el-icon></span>
          <span class="conversation-item-body">
            <strong :title="conversation.title || $t('ai.conversation.untitled')">
              {{ conversation.title || $t('ai.conversation.untitled') }}
            </strong>
            <small>{{ providerName(conversation.provider_code || '') }} · {{ relativeTime(conversation.updated_at) }}</small>
            <small class="conversation-item-id">
              #{{ conversation.id.slice(0, 8) }}
            </small>
          </span>
          <el-button
            class="conversation-delete"
            text
            circle
            :aria-label="$t('ai.drawer.deleteAria')"
            @click.stop="deleteConversation(conversation)"
          >
            <el-icon><Delete /></el-icon>
          </el-button>
        </div>
      </div>
    </el-drawer>

    <el-drawer v-model="contextDrawer" :title="$t('ai.drawer.contextTitle')" size="min(380px, 92vw)">
      <ContextContent />
    </el-drawer>

    <el-drawer v-model="resultDrawer" :title="$t('ai.drawer.resultTitle')" size="min(760px, 94vw)">
      <div class="result-drawer-head">
        <span>{{ $t('ai.drawer.resultSummary', { n: resultTotal }) }}</span>
        <el-tag size="small" effect="plain">{{ resultKind || 'result' }}</el-tag>
      </div>
      <el-table v-loading="resultLoading" :data="resultRows" stripe height="calc(100vh - 210px)">
        <el-table-column
          v-for="column in resultColumns"
          :key="column"
          :prop="column"
          :label="resultColumnLabel(column)"
          min-width="130"
          show-overflow-tooltip
        />
      </el-table>
      <div class="result-pagination">
        <el-pagination
          v-model:current-page="resultPage"
          :page-size="resultPageSize"
          :total="resultTotal"
          layout="prev, pager, next"
          @current-change="loadResultPage"
        />
      </div>
    </el-drawer>

    <el-drawer
      v-model="evidenceDrawer"
      :title="$t('ai.drawer.evidenceTitle')"
      size="min(760px, 94vw)"
      class="evidence-drawer"
    >
      <div v-loading="evidenceLoading" class="evidence-content">
        <section v-if="selectedDiagnosticReport" class="evidence-report">
          <div class="evidence-report-head">
            <span>{{ $t('ai.drawer.reportTitle') }}</span>
            <el-tag
              v-if="selectedDiagnosticReport.severity"
              size="small"
              :type="diagnosticSeverityTagType(selectedDiagnosticReport.severity)"
              effect="plain"
            >{{ diagnosticSeverityLabel(selectedDiagnosticReport.severity) }}</el-tag>
          </div>
          <p>{{ selectedDiagnosticReport.summary || $t('ai.drawer.reportNoSummary') }}</p>
          <div
            v-if="selectedDiagnosticReport.evidence_insufficient"
            class="evidence-insufficient"
          >
            <el-icon><WarningFilled /></el-icon>
            {{ $t('ai.drawer.evidenceInsufficient') }}
          </div>
        </section>
        <div v-if="!evidenceLoading && !diagnosticEvidence.length" class="drawer-empty evidence-empty">
          <el-icon><Document /></el-icon>
          <p>{{ $t('ai.drawer.evidenceEmptyTitle') }}</p>
          <span>{{ $t('ai.drawer.evidenceEmptyDesc') }}</span>
        </div>
        <div v-else class="evidence-list">
          <details
            v-for="item in diagnosticEvidence"
            :key="item.id"
            class="evidence-item"
          >
            <summary>
              <span class="evidence-kind">{{ item.kind || 'PROBE' }}</span>
              <span class="evidence-title">
                <strong>{{ item.title }}</strong>
                <small>{{ item.asset_alias || $t('ai.drawer.platform') }} · {{ item.probe_id || $t('ai.drawer.probe') }}</small>
              </span>
              <el-tag v-if="item.truncated" size="small" type="warning" effect="plain">{{ $t('ai.drawer.truncatedTag') }}</el-tag>
              <el-icon><ArrowRight /></el-icon>
            </summary>
            <pre>{{ item.content || $t('ai.drawer.evidenceNoContent') }}</pre>
          </details>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<script setup lang="ts">
import {
  computed,
  defineComponent,
  h,
  nextTick,
  onBeforeUnmount,
  onMounted,
  ref,
  type Component,
  type PropType,
} from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { useRouter } from 'vue-router'
import {
  ArrowRight,
  ChatLineRound,
  CircleCheckFilled,
  Clock,
  DataAnalysis,
  Delete,
  Document,
  Loading,
  Monitor,
  Plus,
  Promotion,
  Setting,
  Tickets,
  View,
  WarningFilled,
} from '@element-plus/icons-vue'
import { store } from '@/store'
import { currentLocale, t } from '@/i18n'
import { providerBrandColor, providerIcon } from '@/assets/provider-logos'
import DiagnosticRunCard from '@/components/ai/DiagnosticRunCard.vue'
import AutonomyDraftCard from '@/components/ai/AutonomyDraftCard.vue'
import OrangeMark from '@/components/OrangeMark.vue'
import {
  cancelDiagnostic,
  getDiagnosticEvidence,
  getDiagnosticReport,
  getDiagnosticRun,
} from '@/api/aiDiagnostics'
import { aiJsonRequest, postAiStream } from '@/utils/aiStream'
import { sortAutonomyDrafts } from '@/utils/autonomyDrafts'
import {
  AI_CONTEXT_MODE_DEEP,
  AI_CONTEXT_MODE_STANDARD,
  AI_CONTEXT_TOKENS_DEEP,
} from '@/types/ai'
import type {
  AiApiResponse,
  AiAutonomyDraft,
  AiChatMessage,
  AiConversation,
  AiConversationDetail,
  AiContextMode,
  AiDiagnosticAssetProgress,
  AiDiagnosticEvidence,
  AiDiagnosticReport,
  AiDiagnosticRun,
  AiDiagnosticStatus,
  AiProviderObservability,
  AiProvider,
  AiResultScope,
  AiSseEvent,
  AiToolEvent,
} from '@/types/ai'

type TimelineItem =
  | { kind: 'message'; value: AiChatMessage }
  | { kind: 'tool'; value: AiToolEvent }
  | { kind: 'diagnostic'; value: AiDiagnosticRun }
  | { kind: 'autonomy_draft'; value: AiAutonomyDraft }

const PROVIDER_NAMES: Record<string, string> = {
  openai: 'OpenAI',
  deepseek: 'DeepSeek',
  minimax: 'MiniMax',
  kimi: 'Kimi',
  qwen: 'Qwen',
  glm: 'GLM',
  siliconflow: '硅基流动', // i18n-ignore 厂商品牌名，非 UI 文案
}

const examplePrompts = computed<Array<{ title: string; text: string; icon: Component }>>(() => [
  { title: t('ai.prompts.overviewTitle'), text: t('ai.prompts.overviewText'), icon: DataAnalysis },
  { title: t('ai.prompts.assetsTitle'), text: t('ai.prompts.assetsText'), icon: Monitor },
  { title: t('ai.prompts.cronTitle'), text: t('ai.prompts.cronText'), icon: Tickets },
  { title: t('ai.prompts.auditTitle'), text: t('ai.prompts.auditText'), icon: Document },
])

const providers = ref<AiProvider[]>([])
const router = useRouter()
const isAdmin = computed<boolean>(() => store.user.role === 'admin')
const selectedProvider = ref('')
const selectedContextMode = ref<AiContextMode>(AI_CONTEXT_MODE_STANDARD)
const conversations = ref<AiConversation[]>([])
const currentConversationId = ref('')
const messages = ref<AiChatMessage[]>([])
const toolEvents = ref<AiToolEvent[]>([])
const autonomyDrafts = ref<AiAutonomyDraft[]>([])
const resultScope = ref<AiResultScope | null>(null)
const diagnosticRuns = ref<AiDiagnosticRun[]>([])
const providerObservability = ref<AiProviderObservability | null>(null)
const draft = ref('')
const sending = ref(false)
const loadingConversation = ref(false)
const conversationDrawer = ref(false)
const contextDrawer = ref(false)
const resultDrawer = ref(false)
const resultLoading = ref(false)
const resultRows = ref<Array<Record<string, unknown>>>([])
const resultPage = ref(1)
const resultPageSize = 20
const resultTotal = ref(0)
const resultKind = ref('')
const evidenceDrawer = ref(false)
const evidenceLoading = ref(false)
const diagnosticEvidence = ref<AiDiagnosticEvidence[]>([])
const selectedDiagnosticReport = ref<AiDiagnosticReport | null>(null)
const selectedDiagnosticRunId = ref('')
const messageScroller = ref<HTMLElement | null>(null)
let activeController: AbortController | null = null
let diagnosticPollTimer: ReturnType<typeof setTimeout> | null = null
/** 最近一次已生效的模型/上下文选项，用于确认弹窗取消时回退。 */
let lastAppliedProvider = ''
let lastAppliedContextMode: AiContextMode = AI_CONTEXT_MODE_STANDARD

const timeline = computed<TimelineItem[]>(() => {
  const items: TimelineItem[] = [
    ...messages.value.map(value => ({ kind: 'message' as const, value })),
    ...toolEvents.value.map(value => ({ kind: 'tool' as const, value })),
    ...diagnosticRuns.value.map(value => ({ kind: 'diagnostic' as const, value })),
    ...autonomyDrafts.value.map(value => ({ kind: 'autonomy_draft' as const, value })),
  ]
  return items.sort((a, b) => timelineTime(a) - timelineTime(b))
})

const activeProvider = computed(() =>
  providers.value.find(provider => provider.provider_code === selectedProvider.value),
)
const activeProviderReady = computed(() =>
  Boolean(activeProvider.value && providerAvailable(activeProvider.value)),
)
const activeProviderSupportsDeep = computed(() =>
  providerSupportsDeep(activeProvider.value),
)
const contextModeAvailable = computed(() =>
  selectedContextMode.value === AI_CONTEXT_MODE_STANDARD || activeProviderSupportsDeep.value,
)
const canChat = computed(() =>
  activeProviderReady.value
  && contextModeAvailable.value
  && !sending.value
)
const hasStreamingMessage = computed(() => messages.value.some(message => message.streaming))
/** 思考态：请求中或流式回复中 → 橘子旋转动画 */
const isThinking = computed(() => sending.value || hasStreamingMessage.value)
const currentConversation = computed(() =>
  conversations.value.find(conversation => conversation.id === currentConversationId.value),
)
const latestDiagnostic = computed(() => diagnosticRuns.value[diagnosticRuns.value.length - 1])
const latestAutonomyDraft = computed(() => autonomyDrafts.value[autonomyDrafts.value.length - 1])
const activeDiagnostic = computed(() =>
  [...diagnosticRuns.value]
    .reverse()
    .find(run => ['queued', 'running'].includes(run.status)),
)
const contextBudget = computed(() => providerObservability.value?.context_budget)
const contextBudgetPercent = computed(() => {
  const used = Number(contextBudget.value?.estimated_input_tokens || 0)
  const available = Number(contextBudget.value?.effective_input_tokens || 0)
  if (!available) return 0
  return Math.min(100, Math.max(0, Math.round((used / available) * 100)))
})
const currentTitle = computed(() => currentConversation.value?.title || t('ai.conversation.defaultTitle'))
const currentModelLabel = computed(() => {
  const provider = activeProvider.value
  if (!provider) return t('ai.provider.selectToStart')
  return `${provider.name || providerName(provider.provider_code)} · ${contextModeLabel(selectedContextMode.value)}`
})
const currentModelTechnical = computed(() => {
  const provider = activeProvider.value
  if (!provider) return ''
  return provider.model || t('ai.provider.modelNotConfigured')
})
const composerPlaceholder = computed(() => {
  if (!providers.value.length) return t('ai.composer.placeholder.noProvider')
  if (!activeProviderReady.value) return t('ai.composer.placeholder.unavailable', { reason: providerUnavailableReason(activeProvider.value) })
  if (!contextModeAvailable.value) return t('ai.composer.placeholder.deepUnsupported')
  return t('ai.composer.placeholder.default')
})
const userInitial = computed(() =>
  (store.user.alias || store.user.username || 'U').trim().slice(0, 1).toUpperCase(),
)
const resultColumns = computed(() =>
  Object.keys(resultRows.value[0] || {}).filter(key => !['id', 'host_id'].includes(key)).slice(0, 7),
)

function ContextView(): ReturnType<typeof h> {
  const scope = resultScope.value
  const rawBudget = contextBudget.value
  // 全 0 的预算卡是噪音：模型至少完成一次响应（有 token 数）才展示
  const budget = rawBudget
    && (Number(rawBudget.effective_input_tokens) > 0 || Number(rawBudget.estimated_input_tokens) > 0)
    ? rawBudget
    : null
  const hasAnyData = Boolean(budget || scope)

  const sections: Array<ReturnType<typeof h> | null> = [
    h('section', { class: 'context-section' }, [
      h('span', { class: 'context-label' }, t('ai.context.runState')),
      h('div', { class: 'run-state' }, [
        h('span', { class: ['run-state-dot', sending.value || activeDiagnostic.value ? 'busy' : ''] }),
        h('div', [
          h('strong', activeDiagnostic.value
            ? t('ai.context.stateDiagnostic')
            : sending.value
              ? t('ai.context.stateProcessing')
              : latestAutonomyDraft.value
                ? t('ai.context.stateDraft')
                : latestDiagnostic.value
                  ? t('ai.context.stateDiagnosticDone')
                  : t('ai.context.stateIdle')),
          h('span', currentModelLabel.value),
          currentModelTechnical.value
            ? h('details', { class: 'context-technical' }, [
                h('summary', t('ai.context.modelDetails')),
                h('code', currentModelTechnical.value),
              ])
            : null,
        ]),
      ]),
    ]),
  ]

  if (budget) {
    sections.push(h('section', { class: 'context-section' }, [
      h('span', { class: 'context-label' }, t('ai.context.budget')),
      h('div', { class: 'context-budget' }, [
        h('div', { class: 'context-budget-copy' }, [
          h('span', contextModeLabel(selectedContextMode.value)),
          h('strong', `${contextBudgetPercent.value}%`),
        ]),
        h('div', {
          class: 'context-budget-track',
          role: 'progressbar',
          'aria-label': t('ai.context.budgetAria'),
          'aria-valuemin': '0',
          'aria-valuemax': '100',
          'aria-valuenow': String(contextBudgetPercent.value),
        }, [
          h('i', {
            class: {
              warning: contextBudgetPercent.value >= 80,
              danger: contextBudgetPercent.value >= 95,
            },
            style: { width: `${contextBudgetPercent.value}%` },
          }),
        ]),
        h('div', { class: 'context-budget-facts' }, [
          h('span', [
            h('b', formatTokenCount(budget.estimated_input_tokens)),
            t('ai.context.inputTokens'),
          ]),
          h('span', [
            h('b', formatTokenCount(budget.effective_input_tokens)),
            t('ai.context.availableTokens'),
          ]),
          h('span', [
            h('b', String(providerObservability.value?.compression_count || 0)),
            t('ai.context.compressions'),
          ]),
        ]),
        providerObservability.value?.truncation_reason
          ? h('p', { class: 'context-budget-warning' }, [
              h(WarningFilled),
              t('ai.context.truncationWarning'),
            ])
          : null,
      ]),
    ]))
  }

  if (scope) {
    sections.push(h('section', { class: 'context-section' }, [
      h('span', { class: 'context-label' }, t('ai.context.assetScope')),
      h('div', { class: 'scope-card' }, [
        h('div', { class: 'scope-total' }, [
          h('span', t('ai.context.selected')),
          h('strong', String(scope.total || 0)),
          h('small', t('ai.context.hostsUnit')),
        ]),
        h('div', { class: 'scope-stats' }, [
          scope.online != null ? h('span', [h('i', { class: 'online' }), t('ai.context.online', { n: scope.online })]) : null,
          scope.offline != null ? h('span', [h('i', { class: 'offline' }), t('ai.context.offline', { n: scope.offline })]) : null,
        ]),
        scope.groups?.length
          ? h('div', { class: 'scope-groups' }, scope.groups.slice(0, 4).map(group => h('code', group)))
          : null,
        scope.result_set_id
          ? h('button', {
              type: 'button',
              class: 'scope-detail-button',
              onClick: () => openResultDetails(),
            }, t('ai.context.viewFullDetails'))
          : null,
      ]),
    ]))
  }


  if (!hasAnyData) {
    sections.push(h('div', { class: 'context-empty' }, [
      h('span', t('ai.context.empty')),
    ]))
  }

  sections.push(h('div', { class: 'safety-note' }, [
    h('span', [h(WarningFilled)]),
    h('p', [h('strong', t('ai.context.safetyTitle')), h('br'), t('ai.context.safetyDesc')]),
  ]))

  return h('div', { class: 'context-content' }, sections)
}

const ContextContent = defineComponent({
  name: 'AiAgentContextContent',
  setup: () => ContextView,
})

function providerName(code: string): string {
  return PROVIDER_NAMES[code] || code || t('ai.provider.unknown')
}

function providerSupportsDeep(provider?: AiProvider): boolean {
  return Number(provider?.context_window_tokens || 0) >= AI_CONTEXT_TOKENS_DEEP
}

function ensureContextModeSupported(provider?: AiProvider): void {
  if (
    selectedContextMode.value === AI_CONTEXT_MODE_DEEP
    && !providerSupportsDeep(provider)
  ) {
    selectedContextMode.value = AI_CONTEXT_MODE_STANDARD
  }
}

function contextModeLabel(mode?: AiContextMode): string {
  return mode === AI_CONTEXT_MODE_DEEP ? t('ai.contextMode.deep') : t('ai.contextMode.standard')
}

function formatTokenCount(value?: number): string {
  const tokens = Math.max(0, Number(value || 0))
  if (tokens >= 1024 * 1024) return `${(tokens / (1024 * 1024)).toFixed(1)}M`
  if (tokens >= 1024) return `${Math.round(tokens / 1024)}K`
  return String(tokens)
}

function openModelSettings(): void {
  void router.push({ name: 'Settings', query: { tab: 'ai' } })
}

function providerAvailable(provider: AiProvider): boolean {
  return provider.available !== false
    && provider.enabled !== false
    && provider.api_key_configured !== false
    && Boolean(provider.model)
}

function providerUnavailableReason(provider?: AiProvider): string {
  if (!provider) return t('ai.provider.notSelected')
  const reason = provider.reason || provider.unavailable_reason || provider.disabled_reason
  const labels: Record<string, string> = {
    disabled: t('ai.provider.disabledReason'),
    model_missing: t('ai.provider.modelMissing'),
    key_missing: t('ai.provider.keyMissing'),
  }
  if (reason && labels[reason]) return labels[reason]
  if (reason && !['false', 'unavailable'].includes(reason)) return reason
  if (provider.enabled === false) return t('ai.provider.disabledReason')
  if (!provider.api_key_configured) return t('ai.provider.keyMissing')
  if (!provider.model) return t('ai.provider.modelMissing')
  return t('ai.provider.unavailable')
}

function providerLabel(provider: AiProvider): string {
  const base = `${provider.name || providerName(provider.provider_code)} · ${provider.model || t('ai.provider.notConfigured')}`
  return providerAvailable(provider)
    ? base
    : t('ai.provider.labelUnavailable', { base, reason: providerUnavailableReason(provider) })
}

function readableMessage(content: string): string {
  const raw = String(content || '').replace(/^detail:\s*/i, '').trim()
  if (/active autonomous run already exists for this host/i.test(raw)) {
    return t('ai.msg.activeAutonomyConflict')
  }
  return raw || t('ai.msg.requestFail')
}

function readableToolSummary(summary: string): string {
  const raw = String(summary || '').trim()
  const withoutTechnicalDetail = raw.replace(/\s*detail:\s*.*$/i, '').trim()
  if (withoutTechnicalDetail) return withoutTechnicalDetail
  return readableMessage(raw)
}

function unwrapArray<T>(payload: unknown, key: string): T[] {
  if (Array.isArray(payload)) return payload as T[]
  if (!payload || typeof payload !== 'object') return []
  const object = payload as Record<string, unknown>
  if (Array.isArray(object[key])) return object[key] as T[]
  if (Array.isArray(object.data)) return object.data as T[]
  if (object.data && typeof object.data === 'object') {
    const data = object.data as Record<string, unknown>
    if (Array.isArray(data[key])) return data[key] as T[]
  }
  return []
}

function unwrapObject<T extends object>(payload: unknown, key?: string): T | null {
  if (!payload || typeof payload !== 'object') return null
  const object = payload as Record<string, unknown>
  if (key && object[key] && typeof object[key] === 'object') return object[key] as T
  if (object.data && typeof object.data === 'object' && !Array.isArray(object.data)) {
    const data = object.data as Record<string, unknown>
    if (key && data[key] && typeof data[key] === 'object') return data[key] as T
    return data as T
  }
  return object as T
}

function timeValue(value?: string): number {
  const parsed = value ? Date.parse(value) : Number.NaN
  return Number.isFinite(parsed) ? parsed : 0
}

function timelineTime(item: TimelineItem): number {
  if (item.kind === 'diagnostic') {
    return timeValue(item.value.started_at || item.value.created_at || item.value.updated_at)
  }
  return timeValue(item.value.created_at)
}

function timelineItemKey(item: TimelineItem): string {
  if (item.kind === 'diagnostic') return `diagnostic-${item.value.id}`
  if (item.kind === 'autonomy_draft') return `draft-${item.value.id || item.value.run_id}`
  return `${item.kind}-${item.value.id}`
}

function uid(prefix: string): string {
  return `${prefix}-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
}

function nowIso(): string {
  return new Date().toISOString()
}

const BOTTOM_FOLLOW_THRESHOLD = 72
let followNewContent = true
let scrollGeneration = 0

function isNearMessageBottom(target: HTMLElement): boolean {
  return target.scrollHeight - target.scrollTop - target.clientHeight <= BOTTOM_FOLLOW_THRESHOLD
}

function handleMessageScroll(): void {
  const target = messageScroller.value
  if (!target) return
  followNewContent = isNearMessageBottom(target)
}

function scrollToBottom(force = false): void {
  if (!force && !followNewContent) return
  const generation = scrollGeneration
  void nextTick(() => {
    if (generation !== scrollGeneration) return
    const target = messageScroller.value
    if (!target) return
    if (!force && !followNewContent) return
    // Direct assignment is deliberate: restoring a long conversation must
    // never animate from the top, and streaming updates should not build up a
    // queue of smooth-scroll animations.
    target.scrollTop = target.scrollHeight
    followNewContent = true
  })
}

/** Switching conversations invalidates queued scrolls and restores bottom-follow. */
function resetScrollState(): void {
  scrollGeneration += 1
  followNewContent = true
}

async function loadProviders(): Promise<void> {
  try {
    const payload = await aiJsonRequest<AiApiResponse<AiProvider[]> & { providers?: AiProvider[] }>('/ai/providers')
    providers.value = unwrapArray<AiProvider>(payload, 'providers')
    const stored = localStorage.getItem('ogs:ai-provider')
    const preferred = providers.value.find(provider => provider.provider_code === stored && providerAvailable(provider))
      || providers.value.find(provider => provider.is_default && providerAvailable(provider))
      || providers.value.find(providerAvailable)
      || providers.value[0]
    selectedProvider.value = preferred?.provider_code || ''
    ensureContextModeSupported(preferred)
    lastAppliedProvider = selectedProvider.value
    lastAppliedContextMode = selectedContextMode.value
  } catch (error) {
    ElMessage.error(errorMessage(error, t('ai.msg.loadProvidersFail')))
  }
}

async function loadConversations(): Promise<void> {
  try {
    const payload = await aiJsonRequest<AiApiResponse<AiConversation[]> & { conversations?: AiConversation[] }>('/ai/conversations')
    conversations.value = unwrapArray<AiConversation>(payload, 'conversations')
    const stored = localStorage.getItem('ogs:ai-conversation')
    const target = conversations.value.find(item => item.id === stored) || conversations.value[0]
    if (target) await openConversation(target.id, false)
  } catch (error) {
    ElMessage.error(errorMessage(error, t('ai.msg.loadConversationsFail')))
  }
}

const DIAGNOSTIC_STATUSES: AiDiagnosticStatus[] = [
  'queued',
  'running',
  'completed',
  'partial',
  'failed',
  'cancelled',
  'interrupted',
  'expired',
]

function diagnosticRunFromData(
  data: Record<string, unknown>,
  fallback?: AiDiagnosticRun,
): AiDiagnosticRun | null {
  const raw = data.run && typeof data.run === 'object'
    ? data.run as Record<string, unknown>
    : data.diagnostic && typeof data.diagnostic === 'object'
      ? data.diagnostic as Record<string, unknown>
      : data
  const id = String(raw.id || raw.run_id || fallback?.id || '')
  if (!id) return null
  const rawStatus = String(raw.status || fallback?.status || 'queued')
  const status = DIAGNOSTIC_STATUSES.includes(rawStatus as AiDiagnosticStatus)
    ? rawStatus as AiDiagnosticStatus
    : fallback?.status || 'queued'
  const rawSystemUser = raw.system_user
  const systemUser = rawSystemUser && typeof rawSystemUser === 'object'
    ? rawSystemUser as AiDiagnosticRun['system_user']
    : typeof rawSystemUser === 'string'
      ? { alias: rawSystemUser, is_privileged: /^root(?:$|@)/i.test(rawSystemUser) }
      : fallback?.system_user
  const rawAssets = Array.isArray(raw.asset_progress)
    ? raw.asset_progress
    : Array.isArray(raw.assets)
      ? raw.assets
      : undefined
  const assetProgress = rawAssets
    ? rawAssets
        .filter(item => item && typeof item === 'object')
        .map(item => normalizeDiagnosticAsset(item as Record<string, unknown>))
    : fallback?.asset_progress
  const rawSummary = raw.summary && typeof raw.summary === 'object'
    ? raw.summary as AiDiagnosticRun['summary']
    : fallback?.summary
  const rawReport = raw.report && typeof raw.report === 'object'
    ? raw.report as AiDiagnosticReport
    : fallback?.report
  return {
    ...fallback,
    id,
    conversation_id: typeof raw.conversation_id === 'string'
      ? raw.conversation_id
      : fallback?.conversation_id || currentConversationId.value,
    profile_id: String(raw.profile_id || fallback?.profile_id || ''),
    profile_name: String(raw.profile_name || fallback?.profile_name || t('ai.diagnostic.defaultProfile')),
    status,
    system_user: systemUser,
    target_count: numberOrUndefined(raw.target_count) ?? fallback?.target_count,
    success_count: numberOrUndefined(raw.success_count) ?? fallback?.success_count,
    failed_count: numberOrUndefined(raw.failed_count) ?? fallback?.failed_count,
    started_at: typeof raw.started_at === 'string' ? raw.started_at : fallback?.started_at,
    completed_at: typeof raw.completed_at === 'string' ? raw.completed_at : fallback?.completed_at,
    evidence_expires_at: typeof raw.evidence_expires_at === 'string'
      ? raw.evidence_expires_at
      : fallback?.evidence_expires_at,
    latest_event_seq: numberOrUndefined(raw.latest_event_seq ?? raw.event_seq)
      ?? fallback?.latest_event_seq,
    parameters: raw.parameters && typeof raw.parameters === 'object'
      ? raw.parameters as Record<string, unknown>
      : fallback?.parameters,
    asset_progress: assetProgress,
    summary: rawSummary,
    report: rawReport,
    created_at: typeof raw.created_at === 'string'
      ? raw.created_at
      : fallback?.created_at || (typeof raw.started_at === 'string' ? raw.started_at : nowIso()),
    updated_at: typeof raw.updated_at === 'string'
      ? raw.updated_at
      : typeof raw.completed_at === 'string'
        ? raw.completed_at
        : nowIso(),
    error: typeof raw.error === 'string'
      ? raw.error
      : typeof raw.message === 'string' && ['failed', 'interrupted'].includes(status)
        ? raw.message
        : fallback?.error,
  }
}

function normalizeDiagnosticAsset(raw: Record<string, unknown>): AiDiagnosticAssetProgress {
  const rawStatus = String(raw.status || 'queued')
  const status: AiDiagnosticAssetProgress['status'] = [
    'queued', 'running', 'completed', 'failed', 'skipped',
  ].includes(rawStatus)
    ? rawStatus as AiDiagnosticAssetProgress['status']
    : 'queued'
  return {
    target_id: typeof raw.target_id === 'number' || typeof raw.target_id === 'string'
      ? raw.target_id
      : undefined,
    alias: String(raw.alias || raw.host || raw.hostname || t('ai.diagnostic.unknownAsset')),
    status,
    completed_probes: numberOrUndefined(raw.completed_probes),
    total_probes: numberOrUndefined(raw.total_probes),
    finding_count: numberOrUndefined(raw.finding_count),
    error: typeof raw.error === 'string' ? raw.error : undefined,
  }
}

function upsertDiagnosticRun(data: Record<string, unknown>): AiDiagnosticRun | null {
  const source = data.run && typeof data.run === 'object'
    ? data.run as Record<string, unknown>
    : data
  const runId = String(
    source.id
    || data.run_id
    || data.id
    || '',
  )
  const existing = diagnosticRuns.value.find(item => item.id === runId)
  const incomingSeq = numberOrUndefined(source.latest_event_seq ?? data.event_seq)
  if (
    existing?.latest_event_seq != null
    && incomingSeq != null
    && incomingSeq < existing.latest_event_seq
  ) return existing
  const incomingStatus = String(source.status || data.status || '')
  if (
    existing
    && !['queued', 'running'].includes(existing.status)
    && ['queued', 'running'].includes(incomingStatus)
    && incomingSeq == null
  ) return existing
  const normalized = diagnosticRunFromData(data, existing)
  if (!normalized) return null

  const rawAsset = data.asset && typeof data.asset === 'object'
    ? normalizeDiagnosticAsset(data.asset as Record<string, unknown>)
    : null
  if (rawAsset) {
    const assets = [...(normalized.asset_progress || [])]
    const index = assets.findIndex(item =>
      (rawAsset.target_id != null && item.target_id === rawAsset.target_id)
      || item.alias === rawAsset.alias,
    )
    if (index >= 0) assets[index] = { ...assets[index], ...rawAsset }
    else assets.push(rawAsset)
    normalized.asset_progress = assets
  }
  if (data.report && typeof data.report === 'object') {
    normalized.report = data.report as AiDiagnosticReport
  }

  if (existing) Object.assign(existing, normalized)
  else diagnosticRuns.value = [...diagnosticRuns.value, normalized].slice(-5)

  removeDiagnosticToolEvents(data)
  scheduleDiagnosticPoll()
  return existing || normalized
}

function applyDiagnosticState(detail: AiConversationDetail): void {
  const candidates = [
    ...(detail.diagnostics || []),
    detail.active_diagnostic,
    detail.latest_diagnostic,
  ].filter((item): item is AiDiagnosticRun => Boolean(item?.id))
  const restored: AiDiagnosticRun[] = []
  for (const candidate of candidates) {
    const existing = restored.find(item => item.id === candidate.id)
    const normalized = diagnosticRunFromData(
      candidate as unknown as Record<string, unknown>,
      existing,
    )
    if (!normalized) continue
    if (existing) Object.assign(existing, normalized)
    else restored.push(normalized)
  }
  diagnosticRuns.value = restored.slice(-5)
  if (restored.length) {
    toolEvents.value = toolEvents.value.filter(item => !isDiagnosticTool(item.tool))
  }
  scheduleDiagnosticPoll()
}

function stopDiagnosticPoll(): void {
  if (diagnosticPollTimer) {
    clearTimeout(diagnosticPollTimer)
    diagnosticPollTimer = null
  }
}

function scheduleDiagnosticPoll(delay = 1800): void {
  stopDiagnosticPoll()
  const active = [...diagnosticRuns.value]
    .reverse()
    .find(run => ['queued', 'running'].includes(run.status))
  if (!active?.id) return
  diagnosticPollTimer = setTimeout(() => {
    void pollDiagnosticRun(active.id)
  }, delay)
}

async function pollDiagnosticRun(runId: string): Promise<void> {
  const existing = diagnosticRuns.value.find(item => item.id === runId)
  if (!existing || !['queued', 'running'].includes(existing.status)) return
  try {
    const run = await getDiagnosticRun(runId)
    const normalized = upsertDiagnosticRun(run as unknown as Record<string, unknown>)
    if (normalized && !['queued', 'running'].includes(normalized.status)) {
      try {
        normalized.report = await getDiagnosticReport(runId)
      } catch {
        // 报告可能仍在异步生成；运行快照仍然是权威状态。
      }
      stopDiagnosticPoll()
    }
  } catch {
    existing.error = t('ai.msg.syncRetry')
    existing.updated_at = nowIso()
    scheduleDiagnosticPoll(3200)
  }
}

function isDiagnosticTool(tool: string): boolean {
  return /diagnostic|readonly_probe|run_probe/i.test(tool)
}

function removeDiagnosticToolEvents(data: Record<string, unknown>): void {
  const toolCallId = String(data.tool_call_id || '')
  toolEvents.value = toolEvents.value.filter(item =>
    !(toolCallId && item.id === toolCallId) && !isDiagnosticTool(item.tool),
  )
}

async function openConversation(id: string, closeDrawer = true): Promise<void> {
  if (sending.value) return
  stopDiagnosticPoll()
  resetScrollState()
  loadingConversation.value = true
  try {
    const payload = await aiJsonRequest<AiApiResponse<AiConversationDetail> & { conversation?: AiConversationDetail }>(
      `/ai/conversations/${encodeURIComponent(id)}`,
    )
    const detail = unwrapObject<AiConversationDetail>(payload, 'conversation')
    if (!detail) throw new Error(t('ai.msg.emptyConversation'))
    currentConversationId.value = detail.id || id
    messages.value = detail.messages || []
    toolEvents.value = restoreToolEvents(detail.tool_events || [])
    autonomyDrafts.value = restoreAutonomyDrafts(detail.autonomy_drafts || [])
    applyDiagnosticState(detail)
    providerObservability.value = detail.provider_observability || null
    resultScope.value = null
    if (detail.result_scope) {
      updateResultScope({ result_scope: detail.result_scope as unknown as Record<string, unknown> })
    }
    if (detail.provider_code) selectedProvider.value = detail.provider_code
    selectedContextMode.value = detail.context_mode || AI_CONTEXT_MODE_STANDARD
    lastAppliedProvider = selectedProvider.value
    lastAppliedContextMode = selectedContextMode.value
    localStorage.setItem('ogs:ai-conversation', currentConversationId.value)
    if (closeDrawer) conversationDrawer.value = false
    scrollToBottom(true)
  } catch (error) {
    ElMessage.error(errorMessage(error, t('ai.msg.restoreFail')))
  } finally {
    loadingConversation.value = false
  }
}

async function refreshProviderObservability(conversationId: string): Promise<void> {
  if (!conversationId || currentConversationId.value !== conversationId) return
  try {
    const payload = await aiJsonRequest<
      AiApiResponse<AiConversationDetail> & { conversation?: AiConversationDetail }
    >(`/ai/conversations/${encodeURIComponent(conversationId)}`)
    const detail = unwrapObject<AiConversationDetail>(payload, 'conversation')
    if (detail && currentConversationId.value === conversationId) {
      providerObservability.value = detail.provider_observability || null
    }
  } catch {
    // Telemetry is supplementary; a failed refresh must not turn a completed
    // Agent response into a visible chat error.
  }
}

function startNewConversation(): void {
  if (sending.value) return
  stopDiagnosticPoll()
  resetScrollState()
  currentConversationId.value = ''
  messages.value = []
  toolEvents.value = []
  autonomyDrafts.value = []
  resultScope.value = null
  diagnosticRuns.value = []
  providerObservability.value = null
  draft.value = ''
  selectedContextMode.value = AI_CONTEXT_MODE_STANDARD
  lastAppliedContextMode = AI_CONTEXT_MODE_STANDARD
  localStorage.removeItem('ogs:ai-conversation')
  conversationDrawer.value = false
}

/** 切换模型/上下文会开新会话；有正在查看的会话时先向用户确认，取消则回退选项。 */
async function confirmStartNewConversation(): Promise<boolean> {
  if (!currentConversationId.value && !messages.value.length) return true
  try {
    await ElMessageBox.confirm(
      t('ai.confirm.switchMessage'),
      t('ai.confirm.switchTitle'),
      { confirmButtonText: t('ai.confirm.switchConfirm'), cancelButtonText: t('common.action.cancel'), type: 'warning' },
    )
    return true
  } catch {
    return false
  }
}

async function handleProviderChange(code: string): Promise<void> {
  if (code === lastAppliedProvider) return
  if (!(await confirmStartNewConversation())) {
    selectedProvider.value = lastAppliedProvider
    return
  }
  lastAppliedProvider = code
  localStorage.setItem('ogs:ai-provider', code)
  const provider = providers.value.find(item => item.provider_code === code)
  if (currentConversationId.value || messages.value.length) startNewConversation()
  ensureContextModeSupported(provider)
  lastAppliedContextMode = selectedContextMode.value
}

async function handleContextModeChange(value: string | number | boolean | undefined): Promise<void> {
  const mode = value === AI_CONTEXT_MODE_DEEP
    ? AI_CONTEXT_MODE_DEEP
    : AI_CONTEXT_MODE_STANDARD
  if (mode === lastAppliedContextMode) return
  if (!(await confirmStartNewConversation())) {
    selectedContextMode.value = lastAppliedContextMode
    return
  }
  if (currentConversationId.value || messages.value.length) startNewConversation()
  selectedContextMode.value = mode
  lastAppliedContextMode = mode
}

async function ensureConversation(firstMessage: string): Promise<string> {
  if (currentConversationId.value) return currentConversationId.value
  const payload = await aiJsonRequest<AiApiResponse<AiConversation> & { conversation?: AiConversation }>(
    '/ai/conversations',
    {
      method: 'POST',
      body: {
        provider_code: selectedProvider.value,
        title: firstMessage.trim().slice(0, 32),
        context_mode: selectedContextMode.value,
      },
    },
  )
  const conversation = unwrapObject<AiConversation>(payload, 'conversation')
  if (!conversation?.id) throw new Error(t('ai.msg.createConversationFail'))
  currentConversationId.value = conversation.id
  localStorage.setItem('ogs:ai-conversation', conversation.id)
  conversations.value = [conversation, ...conversations.value.filter(item => item.id !== conversation.id)]
  return conversation.id
}

function useExample(text: string): void {
  draft.value = text
  void nextTick(() => sendMessage())
}

async function sendMessage(): Promise<void> {
  const content = draft.value.trim()
  if (!content || !canChat.value) return
  followNewContent = true
  sending.value = true
  draft.value = ''
  activeController = new AbortController()
  try {
    const conversationId = await ensureConversation(content)
    messages.value.push({
      id: uid('user'),
      role: 'user',
      content,
      created_at: nowIso(),
    })
    scrollToBottom(true)
    await postAiStream('/ai/chat', {
      conversation_id: conversationId,
      provider_code: selectedProvider.value,
      message: content,
    }, {
      signal: activeController.signal,
      onEvent: handleSseEvent,
    })
    await refreshProviderObservability(conversationId)
    await refreshConversationList()
  } catch (error) {
    if ((error as Error).name !== 'AbortError') {
      messages.value.push({
        id: uid('error'),
        role: 'assistant',
        content: errorMessage(error, t('ai.msg.requestFail')),
        created_at: nowIso(),
        error: true,
      })
      ElMessage.error(errorMessage(error, t('ai.msg.requestFail')))
    }
  } finally {
    finishStreamingMessage()
    sending.value = false
    activeController = null
    scrollToBottom()
  }
}

async function handleSseEvent(event: AiSseEvent): Promise<void> {
  const data = event.data || {}
  const type = event.type || String(data.type || '')
  if (typeof data.conversation_id === 'string' && !currentConversationId.value) {
    currentConversationId.value = data.conversation_id
    localStorage.setItem('ogs:ai-conversation', data.conversation_id)
  }

  switch (type) {
    case 'assistant.delta':
    case 'message.delta':
      appendAssistantDelta(String(data.delta ?? data.content ?? data.text ?? ''))
      break
    case 'tool.started':
      upsertToolEvent(data, 'running')
      break
    case 'tool.completed':
      upsertToolEvent(data, data.error ? 'error' : 'success')
      updateResultScope(data)
      break
    case 'autonomy.draft_created':
      upsertAutonomyDraft(data)
      break
    case 'diagnostic.started':
    case 'diagnostic_started':
    case 'diagnostic.progress':
    case 'diagnostic_progress':
    case 'diagnostic.evidence':
    case 'diagnostic_evidence':
    case 'diagnostic.completed':
    case 'diagnostic_completed':
    case 'diagnostic.failed':
    case 'diagnostic_failed':
      {
        const run = upsertDiagnosticRun(data)
        if (run && ['diagnostic.failed', 'diagnostic_failed'].includes(type)) {
          const eventStatus = String(data.status || run.status)
          run.status = DIAGNOSTIC_STATUSES.includes(eventStatus as AiDiagnosticStatus)
            ? eventStatus as AiDiagnosticStatus
            : 'failed'
          if (['failed', 'interrupted'].includes(run.status)) {
            run.error = String(data.error || data.message || run.error || t('ai.msg.diagnosticIncomplete'))
          }
          stopDiagnosticPoll()
        }
        if (run && ['diagnostic.completed', 'diagnostic_completed'].includes(type)) {
          if (run.status === 'queued' || run.status === 'running') run.status = 'completed'
          if (data.report && typeof data.report === 'object') {
            run.report = data.report as AiDiagnosticReport
          }
          stopDiagnosticPoll()
        }
      }
      break
    case 'run.failed':
      finishStreamingMessage()
      messages.value.push({
        id: uid('run-error'),
        role: 'assistant',
        content: String(data.message || data.error || t('ai.msg.runFail')),
        created_at: nowIso(),
        error: true,
      })
      break
    case 'run.completed':
      finishStreamingMessage()
      break
  }
  scrollToBottom()
}

function appendAssistantDelta(delta: string): void {
  if (!delta) return
  let message = [...messages.value].reverse().find(item => item.role === 'assistant' && item.streaming)
  if (!message) {
    message = {
      id: uid('assistant'),
      role: 'assistant',
      content: '',
      created_at: nowIso(),
      streaming: true,
    }
    messages.value.push(message)
  }
  message.content += delta
}

function finishStreamingMessage(): void {
  for (const message of messages.value) message.streaming = false
}

function upsertToolEvent(data: Record<string, unknown>, status: AiToolEvent['status']): void {
  const tool = String(data.tool || data.name || 'platform_tool')
  if (isDiagnosticTool(tool) && diagnosticRuns.value.length) return
  const eventId = String(data.tool_call_id || data.id || tool)
  const existing = toolEvents.value.find(item => item.id === eventId)
  const summary = summarizeToolData(data)
  if (existing) {
    if (existing.status !== 'running' && status === 'running') return
    existing.status = status
    existing.summary = summary || existing.summary
    return
  }
  toolEvents.value.push({
    id: eventId,
    tool,
    label: toolLabel(tool),
    status,
    summary,
    created_at: nowIso(),
  })
}

function restoreToolEvents(items: AiToolEvent[]): AiToolEvent[] {
  const restored: AiToolEvent[] = []
  for (const item of items) {
    if (!item.id || !item.tool || !['running', 'success', 'error'].includes(item.status)) continue
    const normalized: AiToolEvent = {
      ...item,
      label: !item.label || item.label === item.tool ? toolLabel(item.tool) : item.label,
    }
    const existingIndex = restored.findIndex(candidate => candidate.id === normalized.id)
    if (existingIndex < 0) {
      restored.push(normalized)
      continue
    }
    const existing = restored[existingIndex]
    if (existing.status !== 'running' && normalized.status === 'running') continue
    restored[existingIndex] = {
      ...existing,
      ...normalized,
      created_at: existing.created_at || normalized.created_at,
    }
  }
  return restored
}

/** 切片 7：引用卡只做展示与跳转，聊天侧不提供启动/审批入口。 */
function normalizeAutonomyDraft(data: Record<string, unknown>): AiAutonomyDraft | null {
  const runId = String(data.run_id || '')
  if (!runId) return null
  return {
    id: String(data.id || ''),
    run_id: runId,
    goal: String(data.goal || ''),
    status: String(data.status || 'draft'),
    mode: String(data.mode || ''),
    host_alias: String(data.host_alias || ''),
    created_at: typeof data.created_at === 'string' ? data.created_at : nowIso(),
  }
}

function upsertAutonomyDraft(data: Record<string, unknown>): void {
  const draft = normalizeAutonomyDraft(data)
  if (!draft) return
  const next = [...autonomyDrafts.value]
  const index = autonomyDrafts.value.findIndex(
    item => item.run_id === draft.run_id,
  )
  if (index < 0) {
    next.push(draft)
  } else {
    next[index] = {
      ...next[index],
      ...draft,
      created_at: next[index].created_at || draft.created_at,
    }
  }
  autonomyDrafts.value = sortAutonomyDrafts(next)
}

function restoreAutonomyDrafts(items: AiAutonomyDraft[]): AiAutonomyDraft[] {
  const restored: AiAutonomyDraft[] = []
  for (const item of items || []) {
    const draft = normalizeAutonomyDraft(item as unknown as Record<string, unknown>)
    if (!draft) continue
    if (!restored.some(candidate => candidate.run_id === draft.run_id)) {
      restored.push(draft)
    }
  }
  return sortAutonomyDrafts(restored)
}

function summarizeToolData(data: Record<string, unknown>): string {
  if (typeof data.summary === 'string') return data.summary
  if (typeof data.message === 'string') return data.message
  if (typeof data.error === 'string') return data.error
  const result = data.result
  if (result && typeof result === 'object') {
    const object = result as Record<string, unknown>
    if (typeof object.summary === 'string') return object.summary
    if (typeof object.total === 'number') return t('ai.tool.resultCount', { n: object.total })
  }
  return ''
}

const TOOL_LABEL_KEYS: Record<string, string> = {
  get_platform_overview: 'ai.tool.labels.getPlatformOverview',
  search_assets: 'ai.tool.labels.searchAssets',
  search_cron_jobs: 'ai.tool.labels.searchCronJobs',
  list_authorized_system_users: 'ai.tool.labels.listAuthorizedSystemUsers',
  search_accounts: 'ai.tool.labels.searchAccounts',
  search_audit_logs: 'ai.tool.labels.searchAuditLogs',
  run_readonly_diagnostic: 'ai.tool.labels.runReadonlyDiagnostic',
  start_diagnostic: 'ai.tool.labels.runReadonlyDiagnostic',
  create_autonomy_draft: 'ai.tool.labels.createAutonomyDraft',
  search_knowledge: 'ai.tool.labels.searchKnowledge',
}

function toolLabel(tool: string): string {
  return t(TOOL_LABEL_KEYS[tool] || 'ai.tool.labels.default')
}

function updateResultScope(data: Record<string, unknown>): void {
  const raw = (data.result_scope || data.scope || data.result) as Record<string, unknown> | undefined
  if (!raw || typeof raw !== 'object') return
  const total = Number(raw.total ?? raw.target_count ?? raw.count)
  if (!Number.isFinite(total)) return
  resultScope.value = {
    result_set_id: String(raw.result_set_id || ''),
    title: typeof raw.title === 'string' ? raw.title : undefined,
    total,
    online: numberOrUndefined(raw.online),
    offline: numberOrUndefined(raw.offline),
    groups: Array.isArray(raw.groups) ? raw.groups.map(String) : undefined,
    sample: Array.isArray(raw.sample) ? raw.sample as Array<Record<string, unknown>> : undefined,
  }
}

async function refreshConversationList(): Promise<void> {
  try {
    const payload = await aiJsonRequest<AiApiResponse<AiConversation[]> & { conversations?: AiConversation[] }>('/ai/conversations')
    conversations.value = unwrapArray<AiConversation>(payload, 'conversations')
  } catch {
    // 对话本身已成功，不因列表刷新失败打断用户。
  }
}

async function cancelDiagnosticRun(run: AiDiagnosticRun): Promise<void> {
  if (!['queued', 'running'].includes(run.status)) return
  try {
    const updated = await cancelDiagnostic(run.id)
    upsertDiagnosticRun(updated as unknown as Record<string, unknown>)
    stopDiagnosticPoll()
    ElMessage.success(t('ai.msg.diagnosticCancelled'))
  } catch (error) {
    ElMessage.error(errorMessage(error, t('ai.msg.cancelDiagnosticFail')))
  }
}

async function openDiagnosticEvidence(run: AiDiagnosticRun): Promise<void> {
  selectedDiagnosticRunId.value = run.id
  evidenceDrawer.value = true
  evidenceLoading.value = true
  diagnosticEvidence.value = []
  selectedDiagnosticReport.value = run.report || null
  const [evidenceResult, reportResult] = await Promise.allSettled([
    getDiagnosticEvidence(run.id),
    getDiagnosticReport(run.id),
  ])
  if (selectedDiagnosticRunId.value !== run.id) return
  if (evidenceResult.status === 'fulfilled') {
    diagnosticEvidence.value = evidenceResult.value
  } else {
    ElMessage.error(errorMessage(evidenceResult.reason, t('ai.msg.loadEvidenceFail')))
  }
  if (reportResult.status === 'fulfilled') {
    selectedDiagnosticReport.value = reportResult.value
    run.report = reportResult.value
  }
  evidenceLoading.value = false
}

function diagnosticSeverityLabel(severity: string): string {
  return {
    info: t('ai.severity.info'),
    warning: t('ai.severity.warning'),
    high: t('ai.severity.high'),
    critical: t('ai.severity.critical'),
  }[severity] || severity
}

function diagnosticSeverityTagType(
  severity: string,
): 'success' | 'warning' | 'danger' | 'info' {
  if (severity === 'critical' || severity === 'high') return 'danger'
  if (severity === 'warning') return 'warning'
  if (severity === 'info') return 'info'
  return 'info'
}

async function openResultDetails(): Promise<void> {
  if (!resultScope.value?.result_set_id) return
  resultPage.value = 1
  resultDrawer.value = true
  await loadResultPage(1)
}

async function loadResultPage(page: number): Promise<void> {
  const resultSetId = resultScope.value?.result_set_id
  if (!resultSetId) return
  resultLoading.value = true
  try {
    const payload = await aiJsonRequest<AiApiResponse<Record<string, unknown>> & { result?: Record<string, unknown> }>(
      `/ai/results/${encodeURIComponent(resultSetId)}?page=${page}&page_size=${resultPageSize}`,
    )
    const result = unwrapObject<Record<string, unknown>>(payload, 'result')
    resultRows.value = Array.isArray(result?.rows)
      ? result.rows.filter(row => row && typeof row === 'object') as Array<Record<string, unknown>>
      : []
    resultPage.value = Number(result?.page || page)
    resultTotal.value = Number(result?.total || 0)
    resultKind.value = String(result?.kind || '')
  } catch (error) {
    ElMessage.error(errorMessage(error, t('ai.msg.loadResultFail')))
  } finally {
    resultLoading.value = false
  }
}

function resultColumnLabel(column: string): string {
  const labels: Record<string, string> = {
    alias: t('ai.columns.alias'),
    host_ip: t('ai.columns.hostIp'),
    group: t('ai.columns.group'),
    online: t('ai.columns.online'),
    configured: t('ai.columns.configured'),
    name: t('ai.columns.name'),
    status: t('ai.columns.status'),
    username: t('ai.columns.username'),
    time: t('ai.columns.time'),
  }
  return labels[column] || column
}

async function deleteConversation(conversation: AiConversation): Promise<void> {
  try {
    await ElMessageBox.confirm(
      t('ai.confirm.deleteMessage', { title: conversation.title || t('ai.conversation.untitled') }),
      t('ai.confirm.deleteTitle'),
      { type: 'warning', confirmButtonText: t('common.action.delete'), cancelButtonText: t('common.action.cancel') },
    )
    await aiJsonRequest(`/ai/conversations/${encodeURIComponent(conversation.id)}`, { method: 'DELETE' })
    conversations.value = conversations.value.filter(item => item.id !== conversation.id)
    if (conversation.id === currentConversationId.value) startNewConversation()
    ElMessage.success(t('ai.msg.deleteDone'))
  } catch (error) {
    if (error !== 'cancel' && error !== 'close') ElMessage.error(errorMessage(error, t('ai.msg.deleteFail')))
  }
}

function handleComposerKeydown(event: KeyboardEvent): void {
  if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return
  event.preventDefault()
  void sendMessage()
}

// —— 迷你 Markdown 渲染（零依赖）：整体先 HTML 转义，再只注入白名单标签，
//    支持 Agent 常用子集：表格 / **加粗** / `行内代码` / ``` 代码块 / - 列表 / # 小标题 ——
function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

function renderInlineMarkdown(value: string): string {
  return value
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
}

function renderMarkdown(raw: string): string {
  const lines = escapeHtml(raw || '').split(/\r?\n/)
  const out: string[] = []
  let listItems: string[] = []
  let codeLines: string[] | null = null
  const flushList = (): void => {
    if (listItems.length) {
      out.push(`<ul>${listItems.join('')}</ul>`)
      listItems = []
    }
  }
  const tableRowCells = (row: string): string[] =>
    row.trim().replace(/^\||\|$/g, '').split('|').map(cell => renderInlineMarkdown(cell.trim()))

  let i = 0
  while (i < lines.length) {
    const line = lines[i]
    if (line.trim().startsWith('```')) {
      if (codeLines) {
        out.push(`<pre>${codeLines.join('\n')}</pre>`)
        codeLines = null
      } else {
        flushList()
        codeLines = []
      }
      i += 1
      continue
    }
    if (codeLines) {
      codeLines.push(line)
      i += 1
      continue
    }
    if (/^\s*\|.*\|\s*$/.test(line)) {
      const block: string[] = []
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
        block.push(lines[i])
        i += 1
      }
      if (block.length >= 2 && /^[\s|:\-]+$/.test(block[1])) {
        flushList()
        const rows = block.filter((_, index) => index !== 1)
        const head = tableRowCells(rows[0]).map(cell => `<th>${cell}</th>`).join('')
        const body = rows.slice(1)
          .map(row => `<tr>${tableRowCells(row).map(cell => `<td>${cell}</td>`).join('')}</tr>`)
          .join('')
        out.push(`<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`)
      } else {
        for (const rawRow of block) out.push(`<p>${renderInlineMarkdown(rawRow)}</p>`)
      }
      continue
    }
    if (/^\s*[-*]\s+/.test(line)) {
      listItems.push(`<li>${renderInlineMarkdown(line.replace(/^\s*[-*]\s+/, ''))}</li>`)
      i += 1
      continue
    }
    flushList()
    if (/^#{1,4}\s+/.test(line)) {
      out.push(`<h4>${renderInlineMarkdown(line.replace(/^#{1,4}\s+/, ''))}</h4>`)
      i += 1
      continue
    }
    if (line.trim()) out.push(`<p>${renderInlineMarkdown(line)}</p>`)
    i += 1
  }
  if (codeLines?.length) out.push(`<pre>${codeLines.join('\n')}</pre>`)
  flushList()
  return out.join('')
}

function formatTime(value?: string): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return new Intl.DateTimeFormat(currentLocale(), { hour: '2-digit', minute: '2-digit' }).format(date)
}

function relativeTime(value?: string): string {
  if (!value) return t('common.time.justNow')
  const delta = Date.now() - new Date(value).getTime()
  if (!Number.isFinite(delta) || delta < 60000) return t('common.time.justNow')
  if (delta < 3600000) return t('common.time.minutesAgo', { n: Math.floor(delta / 60000) })
  if (delta < 86400000) return t('common.time.hoursAgo', { n: Math.floor(delta / 3600000) })
  return t('common.time.daysAgo', { n: Math.floor(delta / 86400000) })
}

function numberOrUndefined(value: unknown): number | undefined {
  const number = Number(value)
  return Number.isFinite(number) ? number : undefined
}

function errorMessage(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback
}

onMounted(async () => {
  await Promise.all([loadProviders(), loadConversations()])
})

onBeforeUnmount(() => {
  activeController?.abort()
  stopDiagnosticPoll()
})
</script>

<style>
.ai-agent-page {
  height: 100%;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

/* —— composer 工具栏：模型与上下文选择贴着输入位，安静版控件 —— */
.provider-select { width: 230px; }
.provider-select :deep(.el-select__wrapper) {
  min-height: 28px;
  padding: 0 8px;
  font-size: 12px;
  background: transparent !important;
  border-radius: 6px;
  box-shadow: none !important;
}
.provider-select :deep(.el-select__wrapper:hover) { background: var(--ogs-bg-elevated) !important; }
.provider-select :deep(.el-select__wrapper.is-focused) {
  background: var(--ogs-bg-elevated) !important;
  box-shadow: 0 0 0 1px var(--ogs-primary) inset !important;
}
.provider-select :deep(.el-select__selected-item),
.provider-select :deep(.el-select__placeholder) {
  font-size: 12px;
  color: var(--ogs-text) !important;
}
.provider-select :deep(.el-select__placeholder.is-transparent) { color: var(--ogs-text-muted) !important; }
.provider-select :deep(.el-select__suffix) { color: var(--ogs-text-muted) !important; }
.provider-status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: #FFA94D;
  font-size: 11px;
  white-space: nowrap;
}
.provider-status i {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--ogs-warning);
}
/* 用 EP 的 CSS 变量覆盖选中态（不受样式加载顺序影响），软底描边而非实心橙 */
.context-mode-toggle {
  --el-radio-button-checked-bg-color: color-mix(in srgb, var(--ogs-primary) 16%, transparent);
  --el-radio-button-checked-text-color: var(--ogs-primary-light);
  --el-radio-button-checked-border-color: var(--ogs-primary);
}
.context-mode-toggle :deep(.el-radio-button__inner) {
  min-height: 28px;
  padding: 5px 10px;
  font-family: var(--ogs-mono);
  font-size: 11.5px;
  color: var(--ogs-text-secondary);
  border-color: var(--ogs-border);
  background: transparent;
  box-shadow: none;
}
.context-mode-toggle :deep(.el-radio-button__original-radio:checked + .el-radio-button__inner),
.context-mode-toggle :deep(.el-radio-button.is-active .el-radio-button__inner) {
  color: var(--ogs-primary-light);
  border-color: var(--ogs-primary);
  background: color-mix(in srgb, var(--ogs-primary) 16%, transparent);
  box-shadow: -1px 0 0 0 var(--ogs-primary);
}
.context-mode-toggle :deep(.el-radio-button__inner:hover) {
  color: var(--ogs-primary-light);
}
.provider-option { display: flex; justify-content: space-between; gap: 20px; font-size: 13px; }
.provider-option-name { display: inline-flex; align-items: center; gap: 7px; }
.provider-option-name svg { flex-shrink: 0; }
.provider-option-detail {
  min-width: 0;
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  line-height: 1.4;
}
.provider-option-model {
  color: var(--ogs-text-secondary);
  font-family: var(--ogs-mono);
  font-size: 13px;
}
.provider-option-detail small {
  color: var(--ogs-warning);
  font-size: 11px;
}

.agent-workspace {
  flex: 1;
  min-height: 0;
  display: grid;
  grid-template-columns: minmax(0, 1fr) 326px;
  overflow: hidden;
  background: var(--ogs-surface);
}
.conversation-panel {
  min-width: 0;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.conversation-head {
  height: 54px;
  flex: 0 0 auto;
  padding: 0 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  border-bottom: 1px solid var(--ogs-border-subtle);
}
.conversation-identity { display: flex; align-items: center; gap: 10px; min-width: 0; }
.agent-mark {
  width: 30px;
  height: 30px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--ogs-radius-sm);
  color: var(--ogs-primary);
  background: var(--ogs-primary-soft);
}
/* —— 橘子图标尺寸 + 思考动画（Claude 风格非匀速旋转 + 呼吸缩放） —— */
.agent-mark .orange-mark { font-size: 18px; transition: transform 0.6s cubic-bezier(0.34, 1.56, 0.64, 1); }
.message-avatar .orange-mark { font-size: 18px; transition: transform 0.6s cubic-bezier(0.34, 1.56, 0.64, 1); }
.thinking-mark .orange-mark { font-size: 17px; }
@keyframes orange-think {
  0%   { transform: rotate(0deg) scale(1); }
  50%  { transform: rotate(180deg) scale(1.06); }
  100% { transform: rotate(360deg) scale(1); }
}
@keyframes orange-glow {
  0%, 100% { box-shadow: 0 0 0 0 rgba(249,115,22,0); }
  50%      { box-shadow: 0 0 12px 3px rgba(249,115,22,0.3); }
}
.agent-mark.thinking .orange-mark,
.message-avatar .orange-mark.thinking,
.thinking-mark .orange-mark.thinking {
  animation: orange-think 1.8s cubic-bezier(0.45, 0, 0.55, 1) infinite;
}
.agent-mark.thinking {
  animation: orange-glow 1.8s ease-in-out infinite;
}
.conversation-title-block { min-width: 0; }
.conversation-kicker {
  display: block;
  margin-bottom: 2px;
  color: var(--ogs-text-tertiary);
  font-size: 10px;
  letter-spacing: 0.08em;
  text-transform: uppercase;
}
.conversation-identity strong {
  display: block;
  max-width: 420px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--ogs-text);
  font-size: 14px;
}
.conversation-head-actions { display: flex; align-items: center; gap: 2px; }
.mobile-context-button { display: none; }

.message-stream {
  flex: 1;
  min-height: 0;
  padding: 26px clamp(22px, 5vw, 68px);
  overflow-y: auto;
}
.stream-loading {
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  color: var(--ogs-text-muted);
  font-size: 13px;
}
.agent-empty {
  max-width: 780px;
  min-height: 100%;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  justify-content: center;
}
.empty-terminal {
  width: max-content;
  max-width: 100%;
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 10px 14px;
  border-radius: var(--ogs-radius);
  background: #18181B;
  color: rgba(255, 255, 255, 0.55);
  box-shadow: var(--ogs-shadow-md);
}
.terminal-led { width: 7px; height: 7px; border-radius: 50%; background: rgba(255,255,255,.22); }
.terminal-led:first-child { background: #ef4444; }
.terminal-led:nth-child(2) { background: #f59e0b; }
.terminal-led:nth-child(3) { background: #10b981; margin-right: 7px; }
.empty-terminal code { font-size: 12px; white-space: nowrap; }
.empty-terminal b { color: var(--ogs-primary-light); font-weight: 600; }
.agent-empty h3 {
  margin-top: 24px;
  color: var(--ogs-text);
  font-size: 22px;
  line-height: 1.25;
  letter-spacing: -0.02em;
}
.agent-empty > p {
  max-width: 600px;
  margin-top: 9px;
  color: var(--ogs-text-secondary);
  font-size: 13px;
  line-height: 1.7;
}
.prompt-grid {
  margin-top: 26px;
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}
.prompt-card {
  min-width: 0;
  padding: 14px;
  display: grid;
  grid-template-columns: 34px minmax(0, 1fr) 16px;
  align-items: center;
  gap: 11px;
  text-align: left;
  color: var(--ogs-text);
  border: 1px solid var(--ogs-border);
  border-radius: var(--ogs-radius);
  background: var(--ogs-bg-elevated);
  cursor: pointer;
  transition: border-color .16s ease, box-shadow .16s ease, transform .16s ease;
}
.prompt-card:hover {
  transform: translateY(-1px);
  border-color: var(--ogs-primary);
  box-shadow: 0 6px 18px var(--ogs-primary-soft);
}
.prompt-card:focus-visible { outline: 3px solid var(--ogs-primary-ring); outline-offset: 2px; }
.prompt-card-icon {
  width: 34px;
  height: 34px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--ogs-radius-sm);
  color: var(--ogs-text-secondary);
  background: var(--ogs-bg-sunken);
  transition: color .16s ease, background-color .16s ease;
}
.prompt-card:hover .prompt-card-icon {
  color: var(--ogs-primary);
  background: var(--ogs-primary-soft);
}
.prompt-card strong { display: block; font-size: 13px; font-weight: 600; }
.prompt-card small {
  display: block;
  margin-top: 4px;
  overflow: hidden;
  color: var(--ogs-text-secondary);
  font-size: 11px;
  line-height: 1.5;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.prompt-arrow { color: var(--ogs-text-muted); }

.timeline-row {
  max-width: 860px;
  margin: 0 auto 22px;
  display: flex;
  align-items: flex-start;
  gap: 11px;
}
.message-avatar {
  width: 30px;
  height: 30px;
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 8px;
  color: var(--ogs-primary);
  background: var(--ogs-primary-soft);
  font-size: 12px;
  font-weight: 700;
}
/* 用户系统头像：填满容器，圆角与容器一致；加载失败时回落为首字符——
   文字用品牌橙（覆盖 EP 默认白字，避免白字落在浅橙容器底上看不清） */
.message-avatar .user-avatar {
  border-radius: 8px;
  background: transparent;
  --el-avatar-text-color: var(--ogs-primary);
}
.message-avatar .user-avatar img { border-radius: 8px; object-fit: cover; }
.is-message:has(.message-body:first-of-type) .message-avatar { background: var(--ogs-bg-sunken); color: var(--ogs-text-secondary); }
.message-body { min-width: 0; max-width: min(720px, 88%); }
.message-body.has-error .message-content {
  border-color: var(--ogs-danger);
  background: var(--ogs-danger-soft);
}
.message-content {
  padding: 12px 14px;
  color: var(--ogs-text);
  background: var(--ogs-bg-sunken);
  border: 1px solid transparent;
  border-left: 2px solid color-mix(in srgb, var(--ogs-text-muted) 45%, transparent);
  border-radius: 3px;
  font-size: 13px;
  line-height: 1.75;
  white-space: pre-wrap;
  word-break: break-word;
}
.message-content code {
  font-family: var(--ogs-mono);
  font-size: 12px;
  padding: 1px 5px;
  border-radius: 3px;
  background: color-mix(in srgb, var(--ogs-primary) 6%, var(--ogs-bg));
  border: 1px solid var(--ogs-border-subtle);
  margin: 0 2px;
}
.message-error-label {
  display: block;
  margin-bottom: 4px;
  color: var(--ogs-danger);
  font-size: 12px;
}
.message-error-details {
  margin-top: 8px;
  color: var(--ogs-text-tertiary);
  font-size: 11px;
}
.message-error-details summary { cursor: pointer; }
.message-error-details code {
  display: block;
  margin-top: 4px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
/* —— Agent 回复的 Markdown 排版 —— */
.md-body { display: block; white-space: normal; }
.md-body p { margin: 0 0 8px; }
.md-body > *:last-child { margin-bottom: 0; }
.md-body h4 { margin: 12px 0 6px; font-size: 13px; color: var(--ogs-text); }
.md-body ul { margin: 0 0 8px; padding-left: 20px; }
.md-body li { margin: 3px 0; }
.md-body table {
  margin: 8px 0;
  border-collapse: collapse;
  font-size: 12px;
  line-height: 1.6;
}
.md-body th, .md-body td {
  padding: 5px 12px;
  border: 1px solid var(--ogs-border);
  text-align: left;
}
.md-body th { background: var(--ogs-bg-elevated); color: var(--ogs-text); font-weight: 600; }
.md-body pre {
  margin: 8px 0;
  padding: 10px 12px;
  overflow-x: auto;
  border-radius: var(--ogs-radius-sm);
  color: rgba(255,255,255,.88);
  background: #18181B;
  font-family: var(--ogs-mono);
  font-size: 11px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}
.md-body pre code { padding: 0; margin: 0; border: 0; background: transparent; font-size: 11px; }
.timeline-row:has(.message-avatar > span) { flex-direction: row-reverse; }
/* 用户气泡：柔和橙底 + 左侧主色边（终端工单感），深浅主题通用 */
.timeline-row:has(.message-avatar > span) .message-content {
  color: var(--ogs-text);
  background: var(--ogs-primary-soft);
  border-color: color-mix(in srgb, var(--ogs-primary) 25%, transparent);
  border-left: 2px solid var(--ogs-primary);
  border-radius: 3px;
}
.stream-caret {
  display: inline-block;
  width: 2px;
  height: 1em;
  margin-left: 3px;
  vertical-align: -2px;
  background: var(--ogs-primary);
  animation: caret 1s steps(1) infinite;
}
/* tool-event：亮色工作日志卡——mono 字体 + 状态色左边框传递终端感（不黑化） */
.tool-event {
  position: relative;
  box-sizing: border-box;
  min-width: 0;
  width: min(720px, calc(100% - 41px));
  margin-left: 41px;
  padding: 11px 13px 11px 14px;
  display: flex;
  align-items: flex-start;
  gap: 10px;
  overflow: hidden;
  border: 1px solid var(--ogs-border);
  border-radius: 8px;
  background: var(--ogs-bg-sunken);
  font-family: var(--ogs-mono);
  transition: border-color .16s ease, background-color .16s ease;
}
.tool-event::before {
  position: absolute;
  inset: 0 auto 0 0;
  width: 3px;
  content: '';
  background: var(--ogs-border);
  transition: background-color .16s ease;
}
.tool-event.status-running {
  border-color: color-mix(in srgb, var(--ogs-info) 30%, var(--ogs-border));
  background: color-mix(in srgb, var(--ogs-info) 4%, var(--ogs-bg-sunken));
}
.tool-event.status-success {
  border-color: color-mix(in srgb, var(--ogs-success) 28%, var(--ogs-border));
  background: var(--ogs-bg-sunken);
}
.tool-event.status-error {
  border-color: color-mix(in srgb, var(--ogs-danger) 32%, var(--ogs-border));
  background: color-mix(in srgb, var(--ogs-danger) 4%, var(--ogs-bg-sunken));
}
.tool-event.status-running::before { background: var(--ogs-info); }
.tool-event.status-success::before { background: var(--ogs-success); }
.tool-event.status-error::before { background: var(--ogs-danger); }
.tool-event-icon {
  width: 18px;
  height: 18px;
  margin-top: 1px;
  display: inline-flex;
  flex: 0 0 18px;
  align-items: center;
  justify-content: center;
  color: var(--ogs-text-muted);
  font-size: 16px;
}
.tool-event.status-running .tool-event-icon { color: var(--ogs-info); }
.tool-event.status-success .tool-event-icon { color: var(--ogs-success); }
.tool-event.status-error .tool-event-icon { color: var(--ogs-danger); }
.tool-event-title { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.tool-event > div { min-width: 0; }
.tool-event-title strong { color: var(--ogs-text); font-size: 12.5px; line-height: 1.4; font-weight: 600; }
.tool-event-state {
  padding: 1px 7px;
  color: var(--ogs-text-muted);
  border-radius: 4px;
  background: var(--ogs-bg-elevated);
  font-size: 10.5px;
  font-weight: 600;
  letter-spacing: 0.06em;
  line-height: 1.5;
}
.status-running .tool-event-state { color: var(--ogs-info); }
.status-success .tool-event-state { color: var(--ogs-success); }
.status-error .tool-event-state { color: var(--ogs-danger); }
.tool-event-title code {
  color: var(--ogs-text-muted);
  font-family: var(--ogs-mono);
  font-size: 11px;
  overflow-wrap: anywhere;
}
.tool-event-technical {
  color: var(--ogs-text-muted);
  font-size: 10px;
}
.tool-event-technical summary { cursor: pointer; }
.tool-event-technical code {
  display: block;
  margin-top: 3px;
  padding: 2px 6px;
  border-radius: 4px;
  background: var(--ogs-bg-elevated);
}
.tool-error-details {
  margin-top: 6px;
  color: var(--ogs-text-tertiary);
  font-size: 10px;
}
.tool-error-details summary { cursor: pointer; }
.tool-error-details code {
  display: block;
  margin-top: 3px;
  padding: 3px 6px;
  color: var(--ogs-text-secondary);
  background: var(--ogs-bg-elevated);
  white-space: pre-wrap;
}
.tool-event p {
  margin-top: 5px;
  overflow-wrap: anywhere;
  color: var(--ogs-text-secondary);
  font-size: 11.5px;
  line-height: 1.55;
}

.thinking-row {
  max-width: 860px;
  margin: 0 auto 20px;
  display: flex;
  align-items: center;
  gap: 6px;
  color: var(--ogs-text-muted);
  font-size: 11px;
}
.thinking-mark {
  width: 28px;
  height: 28px;
  margin-right: 5px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 8px;
  color: var(--ogs-primary);
  background: var(--ogs-primary-soft);
}
.thinking-row i {
  width: 4px;
  height: 4px;
  border-radius: 50%;
  background: var(--ogs-text-muted);
  animation: pulse-dot 1.2s infinite ease-in-out;
}
.thinking-row i:nth-last-child(2) { animation-delay: .15s; }
.thinking-row i:last-child { animation-delay: .3s; }

.composer-shell {
  flex: 0 0 auto;
  padding: 14px clamp(22px, 5vw, 68px) 14px;
  border-top: 1px solid var(--ogs-border-subtle);
  background: var(--ogs-bg-elevated);
}
/* composer：保持浅色容器，终端感交给工具栏控件与发光发送按钮 */
.composer {
  max-width: 860px;
  margin: 0 auto;
  padding: 8px 8px 6px;
  border: 1px solid var(--ogs-border);
  border-radius: 12px;
  background: var(--ogs-bg-sunken);
  transition: border-color .16s, box-shadow .16s;
}
.composer:focus-within {
  border-color: var(--ogs-primary);
  box-shadow: 0 0 0 3px var(--ogs-primary-ring);
}
.composer .el-textarea__inner {
  min-height: 44px !important;
  padding: 6px 8px;
  color: var(--ogs-text) !important;
  background: transparent !important;
  border: 0;
  box-shadow: none !important;
  line-height: 1.6;
  caret-color: var(--ogs-primary);
}
.composer-toolbar {
  margin-top: 4px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}
.composer-controls {
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
/* 发送按钮：橙色实心发光（与审批卡确认按钮同款） */
.send-button {
  flex: 0 0 auto;
  width: 34px;
  height: 34px;
  padding: 0;
  border-radius: 9px;
  background: #F76707 !important;
  border-color: #F76707 !important;
  color: #fff !important;
  box-shadow: 0 2px 10px rgba(247,103,7,0.45);
  transition: background .16s, box-shadow .16s;
}
.send-button:hover {
  background: #E8590C !important;
  border-color: #E8590C !important;
  box-shadow: 0 4px 16px rgba(247,103,7,0.55);
}
.send-button.is-disabled,
.send-button.is-disabled:hover {
  background: rgba(247,103,7,0.22) !important;
  border-color: transparent !important;
  color: rgba(255,255,255,0.55) !important;
  box-shadow: none;
}
.composer-hint {
  max-width: 860px;
  margin: 7px auto 0;
  display: flex;
  justify-content: space-between;
  gap: 12px;
  color: var(--ogs-text-muted);
  font-size: 11px;
}
.composer-hint span:last-child { display: inline-flex; align-items: center; gap: 5px; }
.composer-hint i { width: 6px; height: 6px; border-radius: 50%; background: var(--ogs-warning); }

.context-panel {
  min-width: 0;
  overflow-y: auto;
  border-left: 1px solid var(--ogs-border);
  background: var(--ogs-bg-sunken);
}
.context-content { padding: 16px; }
.context-section { margin-bottom: 16px; }
.context-label {
  display: block;
  margin-bottom: 8px;
  color: var(--ogs-text-muted);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: .02em;
}
.run-state {
  padding: 11px;
  display: flex;
  align-items: center;
  gap: 10px;
  border: 1px solid var(--ogs-border);
  border-radius: var(--ogs-radius-sm);
  background: var(--ogs-bg-elevated);
}
.run-state-dot {
  width: 8px;
  height: 8px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: var(--ogs-success);
  box-shadow: 0 0 0 4px var(--ogs-success-soft);
}
.run-state-dot.busy { background: var(--ogs-info); box-shadow: 0 0 0 4px var(--ogs-info-soft); animation: state-pulse 1.6s infinite; }
.run-state strong { display: block; color: var(--ogs-text); font-size: 12px; }
.run-state span { display: block; max-width: 220px; margin-top: 2px; overflow: hidden; color: var(--ogs-text-muted); font-size: 11px; text-overflow: ellipsis; white-space: nowrap; }
.context-budget {
  padding: 11px;
  border: 1px solid var(--ogs-border);
  border-radius: var(--ogs-radius-sm);
  background: var(--ogs-bg-elevated);
}
.context-budget-copy {
  display: flex;
  align-items: center;
  justify-content: space-between;
  color: var(--ogs-text-secondary);
  font-size: 11px;
}
.context-budget-copy strong {
  color: var(--ogs-text);
  font-family: var(--ogs-mono);
  font-size: 12px;
}
.context-budget-track {
  height: 6px;
  margin-top: 9px;
  overflow: hidden;
  border-radius: 999px;
  background: var(--ogs-bg-sunken);
}
.context-budget-track i {
  height: 100%;
  display: block;
  border-radius: inherit;
  background: var(--ogs-info);
  transition: width .2s ease;
}
.context-budget-track i.warning { background: var(--ogs-warning); }
.context-budget-track i.danger { background: var(--ogs-danger); }
.context-budget-facts {
  margin-top: 10px;
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 5px;
}
.context-budget-facts span {
  color: var(--ogs-text-muted);
  font-size: 11px;
  text-align: center;
}
.context-budget-facts b {
  display: block;
  color: var(--ogs-text);
  font-family: var(--ogs-mono);
  font-size: 13px;
}
.context-budget-warning {
  margin: 10px 0 0;
  padding-top: 9px;
  display: flex;
  align-items: flex-start;
  gap: 6px;
  color: var(--ogs-warning);
  border-top: 1px solid var(--ogs-border-subtle);
  font-size: 11px;
  line-height: 1.5;
}
.context-budget-warning svg { width: 13px; height: 13px; flex: 0 0 auto; margin-top: 1px; }
.scope-card {
  overflow: hidden;
  border: 1px solid var(--ogs-border);
  border-radius: var(--ogs-radius-sm);
  background: var(--ogs-bg-elevated);
}
.scope-total { padding: 13px; display: flex; align-items: baseline; gap: 6px; border-bottom: 1px solid var(--ogs-border-subtle); }
.scope-total span, .scope-total small { color: var(--ogs-text-muted); font-size: 11px; }
.scope-total strong { color: var(--ogs-text); font-family: var(--ogs-mono); font-size: 22px; line-height: 1; }
.scope-stats { padding: 9px 13px; display: flex; gap: 14px; color: var(--ogs-text-secondary); font-size: 11px; }
.scope-stats span { display: inline-flex; align-items: center; gap: 5px; }
.scope-stats i { width: 6px; height: 6px; border-radius: 50%; }
.scope-stats i.online { background: var(--ogs-success); }
.scope-stats i.offline { background: var(--ogs-text-muted); }
.scope-groups { padding: 0 13px 11px; display: flex; gap: 5px; flex-wrap: wrap; }
.scope-groups code { padding: 3px 6px; border-radius: 4px; color: var(--ogs-text-secondary); background: var(--ogs-bg-sunken); font-size: 11px; }
.scope-detail-button {
  width: 100%;
  padding: 9px 13px;
  color: var(--ogs-primary);
  text-align: left;
  border: 0;
  border-top: 1px solid var(--ogs-border-subtle);
  background: transparent;
  cursor: pointer;
  font-size: 11px;
  font-weight: 600;
}
.scope-detail-button:hover { background: var(--ogs-primary-soft); }
.scope-detail-button:focus-visible { outline: 3px solid var(--ogs-primary-ring); outline-offset: -3px; }
.context-empty {
  margin-bottom: 16px;
  padding: 14px;
  color: var(--ogs-text-muted);
  border: 1px dashed var(--ogs-border);
  border-radius: var(--ogs-radius-sm);
  font-size: 12px;
  line-height: 1.6;
}
.safety-note { padding: 12px; display: flex; gap: 9px; border-radius: var(--ogs-radius-sm); border: 1px solid var(--ogs-border); background: var(--ogs-bg-elevated); color: var(--ogs-warning); }
.safety-note > span { flex: 0 0 auto; }
.safety-note > span svg { width: 15px; height: 15px; }
.safety-note p { color: var(--ogs-text-secondary); font-size: 11px; line-height: 1.6; }
.safety-note strong { color: var(--ogs-text); font-size: 12px; }

.drawer-toolbar { margin-bottom: 14px; display: flex; align-items: center; justify-content: space-between; color: var(--ogs-text-muted); font-size: 12px; }
.drawer-empty { min-height: 240px; display: flex; flex-direction: column; align-items: center; justify-content: center; color: var(--ogs-text-muted); text-align: center; }
.drawer-empty > .el-icon { font-size: 30px; }
.drawer-empty p { margin-top: 12px; color: var(--ogs-text); font-size: 13px; font-weight: 600; }
.drawer-empty span { margin-top: 5px; font-size: 12px; }
.conversation-list { display: flex; flex-direction: column; gap: 7px; }
.conversation-item {
  width: 100%;
  padding: 11px;
  display: grid;
  grid-template-columns: 32px minmax(0, 1fr) 28px;
  align-items: center;
  gap: 9px;
  color: var(--ogs-text);
  text-align: left;
  border: 1px solid var(--ogs-border);
  border-radius: 9px;
  background: var(--ogs-bg-elevated);
  cursor: pointer;
}
.conversation-item:hover, .conversation-item.active { border-color: var(--ogs-primary); background: var(--ogs-primary-soft); }
.conversation-item:focus-visible { outline: 3px solid var(--ogs-primary-ring); outline-offset: 2px; }
.conversation-item-icon { width: 30px; height: 30px; display: inline-flex; align-items: center; justify-content: center; border-radius: 7px; color: var(--ogs-primary); background: var(--ogs-primary-soft); }
.conversation-item-body { min-width: 0; }
.conversation-item-body strong, .conversation-item-body small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.conversation-item-body strong { font-size: 13px; }
.conversation-item-body small { margin-top: 4px; color: var(--ogs-text-muted); font-size: 11px; }
.conversation-item-id { display: flex !important; align-items: center; gap: 6px; }
.conversation-item-id :deep(.el-tag) { font-size: 10px; line-height: 16px; }
.conversation-delete { opacity: 0; }
.conversation-item:hover .conversation-delete, .conversation-item:focus-within .conversation-delete { opacity: 1; }
.context-technical {
  margin-top: 3px;
  color: var(--ogs-text-tertiary);
  font-size: 10px;
}
.context-technical summary { cursor: pointer; }
.context-technical code { display: block; margin-top: 2px; overflow-wrap: anywhere; }
.result-drawer-head {
  margin-bottom: 12px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  color: var(--ogs-text-secondary);
  font-size: 12px;
}
.result-pagination { padding-top: 14px; display: flex; justify-content: flex-end; }
.evidence-content { min-height: 260px; }
.evidence-report {
  margin-bottom: 14px;
  padding: 14px;
  border: 1px solid var(--ogs-border);
  border-left: 3px solid var(--ogs-primary);
  border-radius: 9px;
  background: var(--ogs-bg-sunken);
}
.evidence-report-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.evidence-report-head > span:first-child { color: var(--ogs-text); font-size: 12px; font-weight: 700; }
.evidence-report p { margin: 8px 0 0; color: var(--ogs-text-secondary); font-size: 12px; line-height: 1.65; }
.evidence-insufficient {
  margin-top: 10px;
  padding: 8px 10px;
  display: flex;
  align-items: center;
  gap: 7px;
  border-radius: 7px;
  color: #9a3412;
  background: #fff7ed;
  font-size: 11px;
}
.evidence-list { display: flex; flex-direction: column; gap: 8px; }
.evidence-item {
  overflow: hidden;
  border: 1px solid var(--ogs-border);
  border-radius: 9px;
  background: var(--ogs-bg-elevated);
}
.evidence-item summary {
  min-height: 56px;
  padding: 8px 12px;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto 14px;
  align-items: center;
  gap: 10px;
  cursor: pointer;
  list-style: none;
}
.evidence-item summary::-webkit-details-marker { display: none; }
.evidence-item summary:focus-visible { outline: 3px solid var(--ogs-primary-ring); outline-offset: -3px; }
.evidence-kind {
  padding: 3px 5px;
  border-radius: 4px;
  color: var(--ogs-primary);
  background: var(--ogs-primary-soft);
  font-family: var(--ogs-mono);
  font-size: 10px;
  font-weight: 700;
}
.evidence-title { min-width: 0; }
.evidence-title strong,
.evidence-title small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.evidence-title strong { color: var(--ogs-text); font-size: 12px; }
.evidence-title small { margin-top: 4px; color: var(--ogs-text-muted); font-family: var(--ogs-mono); font-size: 11px; }
.evidence-item summary > .el-icon { color: var(--ogs-text-muted); transition: transform .16s ease; }
.evidence-item[open] summary > .el-icon { transform: rotate(90deg); }
.evidence-item pre {
  max-height: 360px;
  margin: 0;
  padding: 14px;
  overflow: auto;
  border-top: 1px solid var(--ogs-border-subtle);
  color: rgba(255,255,255,.9);
  background: #18181B;
  font-family: var(--ogs-mono);
  font-size: 11px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
}

@keyframes caret { 50% { opacity: 0; } }
@keyframes pulse-dot { 0%, 70%, 100% { transform: translateY(0); opacity: .35; } 35% { transform: translateY(-3px); opacity: 1; } }
@keyframes state-pulse { 50% { box-shadow: 0 0 0 7px transparent; } }

@media (max-width: 1180px) {
  .agent-workspace { grid-template-columns: minmax(0, 1fr); }
  .context-panel { display: none; }
  .mobile-context-button { display: inline-flex; }
  .message-stream { padding-inline: clamp(18px, 5vw, 52px); }
}

@media (max-width: 760px) {
  .provider-select { width: 150px; }
  .conversation-head { padding-inline: 14px; }
  .conversation-identity { flex: 1; }
  .conversation-head-actions .head-action-label { display: none; }
  .empty-terminal { width: 100%; box-sizing: border-box; }
  .empty-terminal code { overflow: hidden; text-overflow: ellipsis; }
  .message-stream { padding: 20px 14px; }
  .prompt-grid { grid-template-columns: 1fr; }
  .agent-empty { justify-content: flex-start; padding-top: 28px; }
  .timeline-row { gap: 8px; }
  .message-body { max-width: calc(100% - 38px); }
  .tool-event { width: calc(100% - 38px); margin-left: 38px; }
  .composer-shell { padding: 11px 12px 13px; }
  .composer-hint span:first-child { display: none; }
  .composer-hint { justify-content: flex-end; }
}

@media (prefers-reduced-motion: reduce) {
  .message-stream { scroll-behavior: auto; }
  .prompt-card,
  .stream-caret,
  .thinking-row i,
  .run-state-dot.busy,
  .context-budget-track i,
  .tool-event,
  .tool-event::before,
  .tool-event .is-loading {
    animation: none;
    transition: none;
  }
  .agent-mark.thinking .orange-mark,
  .message-avatar .orange-mark.thinking,
  .thinking-mark .orange-mark.thinking,
  .agent-mark.thinking {
    animation: none;
  }
  .agent-mark.thinking .orange-mark { opacity: 0.75; }
}
</style>
