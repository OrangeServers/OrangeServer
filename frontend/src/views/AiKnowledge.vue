<template>
  <section class="knowledge-page">
    <nav class="knowledge-tabs" :aria-label="$t('ai.knowledgeManager.navigation')">
      <button
        v-for="item in tabs"
        :key="item.key"
        type="button"
        :class="{ active: activeTab === item.key }"
        @click="activeTab = item.key"
      >{{ $t(item.label) }}</button>
      <span>{{ $t(isAdmin ? 'ai.knowledgeManager.adminMode' : 'ai.knowledgeManager.userMode') }}</span>
    </nav>

    <main v-loading="loading" class="knowledge-scroll">
      <div class="knowledge-inner">
        <header class="knowledge-head">
          <div>
            <span>{{ $t('ai.knowledgeManager.eyebrow') }}</span>
            <h1>{{ $t('ai.knowledgeManager.title') }}</h1>
            <p>{{ $t('ai.knowledgeManager.subtitle') }}</p>
          </div>
          <div v-if="isAdmin" class="knowledge-actions">
            <el-button v-if="activeTab === 'sources'" type="primary" @click="openCreate">
              {{ $t('ai.knowledgeManager.add') }}
            </el-button>
            <template v-else-if="activeTab === 'status'">
              <el-button plain @click="configDrawer = true">{{ $t('ai.knowledgeManager.configButton') }}</el-button>
              <el-button type="primary" :loading="reindexing" @click="reindex">
                <el-icon><Refresh /></el-icon>{{ $t('ai.knowledgeManager.reindex') }}
              </el-button>
            </template>
          </div>
        </header>

        <section class="knowledge-state" aria-live="polite">
          <div><span>{{ $t('ai.knowledgeManager.indexState') }}</span><strong>{{ indexStateText }}</strong></div>
          <div><span>{{ $t('ai.knowledgeManager.docsTitle') }}</span><strong>{{ documents.length }}</strong></div>
          <div><span>{{ $t('ai.knowledgeManager.chunks') }}</span><strong>{{ visibleChunkCount }}</strong></div>
          <div><span>Embedding</span><strong>{{ embeddingLabel }}</strong></div>
        </section>

        <section v-if="activeTab === 'sources'" class="knowledge-section">
          <div class="section-heading">
            <div><h2>{{ $t('ai.knowledgeManager.sourcesTitle') }}</h2><p>{{ $t('ai.knowledgeManager.sourcesHint') }}</p></div>
            <span v-if="!isAdmin" class="read-only">{{ $t('ai.knowledgeManager.readOnly') }}</span>
          </div>
          <div v-if="catalogError" class="knowledge-load-error" role="alert">
            <span>{{ catalogError }}</span>
            <el-button size="small" plain @click="load">{{ $t('common.action.retry') }}</el-button>
          </div>
          <div v-if="!documents.length && !catalogError" class="knowledge-empty">{{ $t('ai.knowledgeManager.empty') }}</div>
          <div v-if="documents.length" class="source-list">
            <article v-for="document in documents" :key="document.id" class="source-row">
              <component
                :is="isAdmin ? 'button' : 'div'"
                class="source-primary"
                :type="isAdmin ? 'button' : undefined"
                :aria-label="isAdmin ? $t('ai.knowledgeManager.openDocument', { title: document.title }) : undefined"
                @click="openDocument(document)"
              >
                <span>{{ $t(`ai.knowledgeManager.${document.source_type}`) }}</span>
                <strong>{{ document.title }}</strong>
                <small>{{ document.scope }} · v{{ document.version }}</small>
              </component>
              <div class="source-meta"><span>{{ document.chunk_count }}</span><small>{{ $t('ai.knowledgeManager.chunks') }}</small></div>
              <div class="source-meta"><span>{{ document.indexed ? indexStateLabel('ready') : indexStateLabel('stale') }}</span><small>{{ formatTime(document.updated_at) }}</small></div>
              <div v-if="isAdmin" class="source-actions">
                <el-button v-if="document.source_type === 'runbook'" text type="primary" @click="openDocument(document)">
                  {{ $t('ai.knowledgeManager.edit') }}
                </el-button>
                <el-button text type="danger" @click="remove(document)">{{ $t('ai.knowledgeManager.delete') }}</el-button>
              </div>
            </article>
          </div>
        </section>

        <section v-else-if="activeTab === 'status'" class="knowledge-section index-panel">
          <div class="section-heading"><div><h2>{{ $t('ai.knowledgeManager.indexTitle') }}</h2><p>{{ $t('ai.knowledgeManager.indexHint') }}</p></div></div>
          <dl>
            <div><dt>{{ $t('ai.knowledgeManager.indexState') }}</dt><dd>{{ indexStateText }}</dd></div>
            <div><dt>{{ $t('ai.knowledgeManager.provider') }}</dt><dd>{{ embeddingLabel }}</dd></div>
            <div><dt>{{ $t('ai.knowledgeManager.dimension') }}</dt><dd>{{ config?.dimension || '—' }}</dd></div>
            <div><dt>{{ $t('ai.knowledgeManager.indexedChunks') }}</dt><dd>{{ config?.indexed_chunks ?? visibleChunkCount }}</dd></div>
          </dl>
          <p v-if="!isAdmin" class="index-note">{{ $t('ai.knowledgeManager.userIndexHint') }}</p>
        </section>

        <section v-else class="knowledge-section search-panel">
          <div class="section-heading"><div><h2>{{ $t('ai.knowledgeManager.searchTitle') }}</h2><p>{{ $t('ai.knowledgeManager.searchHint') }}</p></div></div>
          <div class="knowledge-search">
            <el-input
              v-model="searchQuery"
              maxlength="512"
              clearable
              :placeholder="$t('ai.knowledgeManager.searchPlaceholder')"
              @keyup.enter="runSearch"
            />
            <el-button type="primary" :loading="searching" :disabled="!searchQuery.trim()" @click="runSearch">
              {{ $t('ai.knowledgeManager.search') }}
            </el-button>
          </div>
          <p class="search-scope">{{ $t('ai.knowledgeManager.searchScope') }}</p>
          <div v-if="searchFeedback" class="knowledge-empty" role="status">{{ $t(searchFeedback) }}</div>
          <div v-else-if="searchResults.length" class="search-results">
            <article v-for="result in searchResults" :key="`${result.citation_id}:${result.document_id}`">
              <header><span>{{ result.citation_id }}</span><strong>{{ result.title }}</strong><code>{{ scoreText(result.score) }}</code></header>
              <small>{{ result.heading }} · {{ result.scope }} · v{{ result.version }}</small>
              <p>{{ result.excerpt }}</p>
            </article>
          </div>
        </section>
      </div>
    </main>

    <el-drawer
      v-model="configDrawer"
      append-to-body
      size="min(620px, 100vw)"
      :title="$t('ai.knowledgeManager.configTitle')"
    >
      <el-form v-if="isAdmin" :model="configForm" label-position="top" class="config-form">
        <el-form-item :label="$t('ai.knowledgeManager.provider')">
          <el-select v-model="configForm.provider_type">
            <el-option value="local" :label="$t('ai.knowledgeManager.local')" />
            <el-option value="openai_compatible" :label="$t('ai.knowledgeManager.remote')" />
          </el-select>
        </el-form-item>
        <template v-if="configForm.provider_type === 'openai_compatible'">
          <el-form-item :label="$t('ai.knowledgeManager.baseUrl')"><el-input v-model="configForm.base_url" placeholder="https://api.example.com/v1" /></el-form-item>
          <el-form-item :label="$t('ai.knowledgeManager.model')"><el-input v-model="configForm.model" /></el-form-item>
          <el-form-item :label="$t('ai.knowledgeManager.dimension')"><el-input-number v-model="configForm.dimension" :min="1" :max="4096" /></el-form-item>
          <el-form-item :label="$t('ai.knowledgeManager.apiKey')"><el-input v-model="configForm.api_key" type="password" show-password :placeholder="keyPlaceholder" /></el-form-item>
        </template>
        <div v-else class="local-model"><code>BAAI/bge-small-zh-v1.5</code><span>512 dimensions · ONNX</span></div>
        <el-button type="primary" :loading="savingConfig" @click="saveConfig">{{ $t('ai.knowledgeManager.saveConfig') }}</el-button>
      </el-form>
    </el-drawer>

    <el-dialog v-model="dialogOpen" append-to-body :title="dialogTitle" width="min(720px, 94vw)" destroy-on-close>
      <el-form v-if="isAdmin" :model="documentForm" label-position="top" :disabled="documentReadOnly">
        <div v-if="!editingId" class="document-upload">
          <input
            ref="fileInput"
            data-testid="knowledge-file"
            type="file"
            accept=".md,.txt,.pdf,.docx"
            :disabled="previewing"
            :aria-label="$t('ai.knowledgeManager.upload')"
            @change="previewUpload"
          />
          <el-button plain :loading="previewing" @click="fileInput?.click()">
            {{ $t('ai.knowledgeManager.upload') }}
          </el-button>
          <span>{{ $t('ai.knowledgeManager.uploadHint') }}</span>
        </div>
        <p v-if="previewName" class="preview-ready" role="status" aria-live="polite">
          {{ $t('ai.knowledgeManager.previewReady', { name: previewName }) }}
        </p>
        <el-form-item :label="$t('ai.knowledgeManager.titleLabel')"><el-input v-model="documentForm.title" maxlength="128" show-word-limit /></el-form-item>
        <el-form-item :label="$t('ai.knowledgeManager.scope')"><el-input v-model="documentForm.scope" :placeholder="$t('ai.knowledgeManager.scopeHint')" maxlength="128" /></el-form-item>
        <el-form-item :label="$t('ai.knowledgeManager.content')"><el-input v-model="documentForm.content" type="textarea" :rows="14" maxlength="1048576" /></el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogOpen = false">{{ $t(documentReadOnly ? 'ai.knowledgeManager.close' : 'ai.knowledgeManager.cancel') }}</el-button>
        <el-button v-if="!documentReadOnly" type="primary" :loading="savingDocument" :disabled="previewing" @click="saveDocument">{{ $t('ai.knowledgeManager.save') }}</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  createKnowledgeDocument,
  deleteKnowledgeDocument,
  getAIOpsStatus,
  getKnowledgeConfig,
  getKnowledgeDocument,
  listKnowledgeDocuments,
  previewKnowledgeDocument,
  reindexKnowledge,
  saveKnowledgeConfig,
  searchKnowledge,
  updateKnowledgeDocument,
} from '@/api/autonomy'
import { t } from '@/i18n'
import { store } from '@/store'
import type {
  AIOpsStatus,
  KnowledgeDocument,
  KnowledgeEmbeddingConfig,
  KnowledgeIndexState,
  KnowledgeSearchResult,
} from '@/types/autonomy'

type KnowledgeTab = 'sources' | 'status' | 'search'

const tabs: Array<{ key: KnowledgeTab; label: string }> = [
  { key: 'sources', label: 'ai.knowledgeManager.tabs.sources' },
  { key: 'status', label: 'ai.knowledgeManager.tabs.status' },
  { key: 'search', label: 'ai.knowledgeManager.tabs.search' },
]
const isAdmin = computed(() => store.user.role === 'admin')
const activeTab = ref<KnowledgeTab>('sources')
const loading = ref(false)
const savingConfig = ref(false)
const savingDocument = ref(false)
const previewing = ref(false)
const reindexing = ref(false)
const searching = ref(false)
const config = ref<KnowledgeEmbeddingConfig | null>(null)
const opsStatus = ref<AIOpsStatus | null>(null)
const documents = ref<KnowledgeDocument[]>([])
const catalogError = ref('')
const searchResults = ref<KnowledgeSearchResult[]>([])
const searchQuery = ref('')
const searchFeedback = ref('')
const configDrawer = ref(false)
const dialogOpen = ref(false)
const documentReadOnly = ref(false)
const fileInput = ref<HTMLInputElement | null>(null)
const previewName = ref('')
let previewRequestId = 0
const editingId = ref('')
const configForm = reactive({ provider_type: 'local' as 'local' | 'openai_compatible', base_url: '', model: '', dimension: 512, api_key: '' })
const documentForm = reactive({ title: '', scope: 'global', content: '' })

const indexState = computed<KnowledgeIndexState | 'unknown'>(() => {
  const value = config.value?.index_state || opsStatus.value?.knowledge_index_state || 'unknown'
  return value as KnowledgeIndexState | 'unknown'
})
const indexStateText = computed(() => indexStateLabel(indexState.value))
const visibleChunkCount = computed(() => documents.value.reduce((total, document) => total + document.chunk_count, 0))
const embeddingLabel = computed(() => {
  if (!isAdmin.value) return t('ai.knowledgeManager.managedEmbedding')
  if (!config.value) return '—'
  return config.value.provider_type === 'local' ? 'bge-small-zh' : (config.value.model || 'OpenAI compatible')
})
const keyPlaceholder = computed(() => config.value?.api_key_configured ? t('ai.knowledgeManager.keySaved') : '')
const dialogTitle = computed(() => t(documentReadOnly.value
  ? 'ai.knowledgeManager.dialogView'
  : (editingId.value ? 'ai.knowledgeManager.dialogEdit' : 'ai.knowledgeManager.dialogAdd')))

function indexStateLabel(state: string): string {
  const known = ['empty', 'ready', 'stale', 'rebuilding', 'error', 'unknown']
  return known.includes(state) ? t(`ai.ops.knowledge.${state}`) : state
}

function applyConfig(value: KnowledgeEmbeddingConfig): void {
  config.value = value
  Object.assign(configForm, {
    provider_type: value.provider_type,
    base_url: value.base_url,
    model: value.model,
    dimension: value.dimension,
    api_key: '',
  })
}

function formatTime(value: string | null): string {
  return value ? new Date(value).toLocaleString() : '—'
}

function scoreText(value: number | null): string {
  return value === null ? '—' : value.toFixed(2)
}

async function load(): Promise<void> {
  loading.value = true
  const secondaryLoad = Promise.allSettled([
    getAIOpsStatus(),
    isAdmin.value ? getKnowledgeConfig() : Promise.resolve(null),
  ] as const)
  try {
    documents.value = await listKnowledgeDocuments()
    catalogError.value = ''
  } catch (error) {
    catalogError.value = t('ai.knowledgeManager.loadFailed')
    ElMessage.error(error instanceof Error ? error.message : catalogError.value)
  } finally {
    loading.value = false
  }

  const [statusResult, configResult] = await secondaryLoad
  if (statusResult.status === 'fulfilled') opsStatus.value = statusResult.value
  else ElMessage.error(statusResult.reason instanceof Error
    ? statusResult.reason.message
    : t('ai.knowledgeManager.loadFailed'))
  if (!isAdmin.value) return
  if (configResult.status === 'fulfilled' && configResult.value) applyConfig(configResult.value)
  else ElMessage.error(configResult.status === 'rejected' && configResult.reason instanceof Error
    ? configResult.reason.message
    : t('ai.knowledgeManager.loadFailed'))
}

async function runSearch(): Promise<void> {
  const query = searchQuery.value.trim()
  if (!query) return
  searching.value = true
  searchResults.value = []
  searchFeedback.value = ''
  try {
    const response = await searchKnowledge(query, 8)
    if (config.value) config.value = { ...config.value, index_state: response.index_state }
    if (opsStatus.value) opsStatus.value = { ...opsStatus.value, knowledge_index_state: response.index_state }
    searchResults.value = response.results || []
    if (!searchResults.value.length) {
      searchFeedback.value = response.index_state === 'ready'
        ? 'ai.knowledgeManager.searchEmpty'
        : 'ai.knowledgeManager.searchIndexNotReady'
    }
  } catch (error) {
    searchFeedback.value = error !== null && typeof error === 'object'
      && 'status' in error && error.status === 403
      ? 'ai.knowledgeManager.searchForbidden'
      : 'ai.knowledgeManager.searchFailed'
  } finally {
    searching.value = false
  }
}

async function saveConfig(): Promise<void> {
  if (!isAdmin.value) return
  savingConfig.value = true
  try {
    applyConfig(await saveKnowledgeConfig({ ...configForm }))
    configDrawer.value = false
    ElMessage.success(t('ai.knowledgeManager.saved'))
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : t('ai.knowledgeManager.loadFailed'))
  } finally {
    savingConfig.value = false
  }
}

function openCreate(): void {
  if (!isAdmin.value) return
  previewRequestId += 1
  previewing.value = false
  editingId.value = ''
  documentReadOnly.value = false
  Object.assign(documentForm, { title: '', scope: 'global', content: '' })
  previewName.value = ''
  dialogOpen.value = true
}

async function previewUpload(event: Event): Promise<void> {
  if (!isAdmin.value) return
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (!file) return
  previewName.value = ''
  previewing.value = true
  const requestId = ++previewRequestId
  try {
    const preview = await previewKnowledgeDocument(file)
    if (requestId !== previewRequestId || !dialogOpen.value) return
    Object.assign(documentForm, { title: preview.title, content: preview.content })
    previewName.value = file.name
  } catch (error) {
    if (requestId === previewRequestId && dialogOpen.value) {
      ElMessage.error(error instanceof Error ? error.message : t('ai.knowledgeManager.previewFailed'))
    }
  } finally {
    if (requestId === previewRequestId) previewing.value = false
    input.value = ''
  }
}

async function openDocument(document: KnowledgeDocument): Promise<void> {
  if (!isAdmin.value) return
  previewRequestId += 1
  previewing.value = false
  previewName.value = ''
  loading.value = true
  try {
    const detail = await getKnowledgeDocument(document.id)
    editingId.value = detail.id
    documentReadOnly.value = detail.source_type !== 'runbook'
    Object.assign(documentForm, { title: detail.title, scope: detail.scope, content: detail.content || '' })
    dialogOpen.value = true
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : t('ai.knowledgeManager.loadFailed'))
  } finally {
    loading.value = false
  }
}

async function saveDocument(): Promise<void> {
  if (!isAdmin.value || documentReadOnly.value) return
  savingDocument.value = true
  try {
    if (editingId.value) await updateKnowledgeDocument(editingId.value, documentForm)
    else await createKnowledgeDocument(documentForm)
    dialogOpen.value = false
    ElMessage.success(t('ai.knowledgeManager.saved'))
    await load()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : t('ai.knowledgeManager.loadFailed'))
  } finally {
    savingDocument.value = false
  }
}

async function remove(document: KnowledgeDocument): Promise<void> {
  if (!isAdmin.value) return
  try {
    await ElMessageBox.confirm(t('ai.knowledgeManager.deleteConfirm', { title: document.title }))
  } catch {
    return
  }
  try {
    await deleteKnowledgeDocument(document.id)
    ElMessage.success(t('ai.knowledgeManager.deleted'))
    await load()
  } catch (error) {
    ElMessage.error(error instanceof Error
      ? error.message
      : t('ai.knowledgeManager.deleteFailed'))
  }
}

async function reindex(): Promise<void> {
  if (!isAdmin.value) return
  reindexing.value = true
  try {
    applyConfig(await reindexKnowledge())
    for (let attempt = 0; config.value?.index_state === 'rebuilding' && attempt < 60; attempt += 1) {
      await new Promise(resolve => window.setTimeout(resolve, 1000))
      applyConfig(await getKnowledgeConfig())
    }
    if (config.value?.index_state === 'ready') ElMessage.success(t('ai.knowledgeManager.indexed'))
    else ElMessage.error(t('ai.knowledgeManager.loadFailed'))
    await load()
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : t('ai.knowledgeManager.loadFailed'))
  } finally {
    reindexing.value = false
  }
}

onMounted(load)
</script>

<style scoped>
.knowledge-page { height: 100%; min-height: 0; display: grid; grid-template-rows: 50px minmax(0, 1fr); background: var(--ogs-bg); }
.knowledge-tabs { display: flex; align-items: stretch; padding: 0 18px; border-bottom: 1px solid var(--ogs-border); background: var(--ogs-surface); }
.knowledge-tabs button { position: relative; padding: 0 14px; border: 0; color: var(--ogs-text-secondary); background: transparent; cursor: pointer; font-size: 12px; font-weight: 600; }
.knowledge-tabs button::after { position: absolute; right: 14px; bottom: -1px; left: 14px; height: 2px; content: ''; background: transparent; }
.knowledge-tabs button.active { color: var(--ogs-text); }
.knowledge-tabs button.active::after { background: var(--ogs-primary); }
.knowledge-tabs > span { margin-left: auto; align-self: center; color: var(--ogs-text-muted); font: 10px var(--ogs-mono); }
.knowledge-scroll { min-height: 0; overflow-y: auto; }
.knowledge-inner { width: min(100%, 1180px); margin: 0 auto; padding: 30px clamp(16px, 4vw, 42px) 54px; box-sizing: border-box; }
.knowledge-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 24px; }
.knowledge-head > div:first-child > span { color: var(--ogs-primary); font: 700 10px var(--ogs-mono); letter-spacing: .12em; text-transform: uppercase; }
.knowledge-head h1 { margin: 7px 0 5px; color: var(--ogs-text); font-size: 27px; }
.knowledge-head p { max-width: 720px; margin: 0; color: var(--ogs-text-secondary); font-size: 13px; line-height: 1.65; }
.knowledge-actions { display: flex; gap: 8px; }
.knowledge-state { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); margin-top: 24px; border: 1px solid var(--ogs-border); background: var(--ogs-surface); }
.knowledge-state div { min-width: 0; padding: 13px 15px; border-right: 1px solid var(--ogs-border); }
.knowledge-state div:last-child { border: 0; }
.knowledge-state span { display: block; color: var(--ogs-text-muted); font-size: 10px; }
.knowledge-state strong { display: block; margin-top: 7px; overflow: hidden; color: var(--ogs-text); font: 650 13px var(--ogs-mono); text-overflow: ellipsis; white-space: nowrap; }
.knowledge-section { margin-top: 28px; }
.section-heading { display: flex; align-items: flex-end; justify-content: space-between; gap: 16px; margin-bottom: 12px; }
.section-heading h2 { margin: 0; color: var(--ogs-text); font-size: 16px; }
.section-heading p { margin: 5px 0 0; color: var(--ogs-text-muted); font-size: 12px; }
.read-only { color: var(--ogs-text-muted); font: 10px var(--ogs-mono); }
.source-list { border-top: 1px solid var(--ogs-border); }
.source-row { min-width: 0; display: grid; grid-template-columns: minmax(220px, 1fr) 100px 190px auto; align-items: center; gap: 18px; padding: 15px 4px; border-bottom: 1px solid var(--ogs-border); }
.source-primary { min-width: 0; padding: 0; border: 0; color: inherit; background: transparent; text-align: left; }
button.source-primary { cursor: pointer; }
button.source-primary:hover strong { color: var(--ogs-primary); }
button.source-primary:focus-visible { outline: 2px solid var(--ogs-primary); outline-offset: 4px; }
.source-primary > span { color: var(--ogs-primary); font: 700 9px var(--ogs-mono); letter-spacing: .08em; text-transform: uppercase; }
.source-primary strong, .source-primary small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.source-primary strong { margin-top: 4px; color: var(--ogs-text); font-size: 13px; }
.source-primary small, .source-meta small { margin-top: 4px; color: var(--ogs-text-muted); font-size: 10px; }
.source-meta span, .source-meta small { display: block; }
.source-meta span { color: var(--ogs-text-secondary); font: 11px var(--ogs-mono); }
.source-actions { display: flex; }
.knowledge-load-error { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px 14px; border: 1px solid var(--el-color-danger-light-5); color: var(--el-color-danger); background: var(--el-color-danger-light-9); font-size: 12px; }
.knowledge-empty { padding: 40px 12px; border-top: 1px solid var(--ogs-border); color: var(--ogs-text-muted); text-align: center; font-size: 12px; }
.index-panel dl { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); margin: 0; border: 1px solid var(--ogs-border); background: var(--ogs-surface); }
.index-panel dl div { padding: 16px; border-right: 1px solid var(--ogs-border); border-bottom: 1px solid var(--ogs-border); }
.index-panel dt { color: var(--ogs-text-muted); font-size: 11px; }
.index-panel dd { margin: 7px 0 0; color: var(--ogs-text); font: 13px var(--ogs-mono); }
.index-note, .search-scope { color: var(--ogs-text-muted); font-size: 11px; }
.knowledge-search { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 9px; max-width: 820px; }
.search-results { max-width: 820px; margin-top: 18px; border-top: 1px solid var(--ogs-border); }
.search-results article { padding: 17px 2px; border-bottom: 1px solid var(--ogs-border); }
.search-results header { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 9px; }
.search-results header span { color: var(--ogs-primary); font: 700 10px var(--ogs-mono); }
.search-results header strong { color: var(--ogs-text); font-size: 13px; }
.search-results header code { color: var(--ogs-text-muted); font-size: 10px; }
.search-results small { display: block; margin-top: 5px; color: var(--ogs-text-muted); font-size: 10px; }
.search-results p { margin: 9px 0 0; color: var(--ogs-text-secondary); font-size: 12px; line-height: 1.7; }
.config-form { max-width: 520px; }
.config-form :deep(.el-select), .config-form :deep(.el-input-number) { width: 100%; }
.local-model { display: flex; flex-direction: column; gap: 5px; margin-bottom: 20px; color: var(--ogs-text-muted); font-size: 12px; }
.local-model code { color: var(--ogs-text); }
.document-upload { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; padding: 12px; border: 1px dashed var(--ogs-border); }
.document-upload input { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); }
.document-upload span, .preview-ready { color: var(--ogs-text-muted); font-size: 11px; }
.preview-ready { margin: 0 0 14px; color: var(--el-color-success); }

@media (max-width: 760px) {
  .knowledge-inner { padding: 22px 12px 42px; }
  .knowledge-head { flex-direction: column; }
  .knowledge-actions { width: 100%; }
  .knowledge-actions .el-button { flex: 1; }
  .knowledge-state { grid-template-columns: 1fr 1fr; }
  .knowledge-state div:nth-child(2) { border-right: 0; }
  .knowledge-state div:nth-child(-n+2) { border-bottom: 1px solid var(--ogs-border); }
  .source-row { grid-template-columns: minmax(0, 1fr) auto; gap: 10px; }
  .source-row .source-meta:first-of-type { display: none; }
  .source-actions { grid-column: 1 / -1; }
}
@media (max-width: 520px) {
  .knowledge-tabs { padding: 0 4px; }
  .knowledge-tabs button { flex: 1; padding-inline: 5px; }
  .knowledge-tabs > span { display: none; }
  .knowledge-state { grid-template-columns: 1fr; }
  .knowledge-state div { border-right: 0; border-bottom: 1px solid var(--ogs-border); }
  .knowledge-search { grid-template-columns: 1fr; }
  .index-panel dl { grid-template-columns: 1fr; }
  .document-upload { align-items: flex-start; flex-direction: column; }
}
</style>
