<template>
  <div class="knowledge-page">
    <header class="knowledge-hero">
      <div>
        <span>{{ $t('ai.knowledgeManager.eyebrow') }}</span>
        <h1>{{ $t('ai.knowledgeManager.title') }}</h1>
        <p>{{ $t('ai.knowledgeManager.subtitle') }}</p>
      </div>
      <el-button type="primary" :loading="reindexing" @click="reindex">
        <el-icon><Refresh /></el-icon>{{ $t('ai.knowledgeManager.reindex') }}
      </el-button>
    </header>

    <el-card class="knowledge-card" shadow="never" v-loading="loading">
      <template #header>
        <div class="card-title">
          <strong>{{ $t('ai.knowledgeManager.configTitle') }}</strong>
          <span>{{ indexSummary }}</span>
        </div>
      </template>
      <el-form :model="configForm" label-position="top" class="config-grid">
        <el-form-item :label="$t('ai.knowledgeManager.provider')">
          <el-select v-model="configForm.provider_type">
            <el-option value="local" :label="$t('ai.knowledgeManager.local')" />
            <el-option value="openai_compatible" :label="$t('ai.knowledgeManager.remote')" />
          </el-select>
        </el-form-item>
        <template v-if="configForm.provider_type === 'openai_compatible'">
          <el-form-item :label="$t('ai.knowledgeManager.baseUrl')">
            <el-input v-model="configForm.base_url" placeholder="https://api.example.com/v1" />
          </el-form-item>
          <el-form-item :label="$t('ai.knowledgeManager.model')">
            <el-input v-model="configForm.model" />
          </el-form-item>
          <el-form-item :label="$t('ai.knowledgeManager.dimension')">
            <el-input-number v-model="configForm.dimension" :min="1" :max="4096" controls-position="right" />
          </el-form-item>
          <el-form-item :label="$t('ai.knowledgeManager.apiKey')">
            <el-input v-model="configForm.api_key" type="password" show-password :placeholder="keyPlaceholder" />
          </el-form-item>
        </template>
        <div v-else class="local-model">
          <code>BAAI/bge-small-zh-v1.5</code><span>512 dimensions · ONNX</span>
        </div>
        <el-form-item class="config-action">
          <el-button :loading="savingConfig" @click="saveConfig">
            {{ $t('ai.knowledgeManager.saveConfig') }}
          </el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <el-card class="knowledge-card" shadow="never">
      <template #header>
        <div class="card-title">
          <strong>{{ $t('ai.knowledgeManager.docsTitle') }}</strong>
          <el-button type="primary" plain @click="openCreate">{{ $t('ai.knowledgeManager.add') }}</el-button>
        </div>
      </template>
      <el-table :data="documents" v-loading="loading" empty-text=" ">
        <el-table-column prop="title" :label="$t('ai.knowledgeManager.titleLabel')" min-width="220" show-overflow-tooltip />
        <el-table-column :label="$t('ai.knowledgeManager.source')" width="130">
          <template #default="{ row }">{{ $t(`ai.knowledgeManager.${row.source_type}`) }}</template>
        </el-table-column>
        <el-table-column prop="scope" :label="$t('ai.knowledgeManager.scope')" min-width="150" show-overflow-tooltip />
        <el-table-column prop="version" :label="$t('ai.knowledgeManager.version')" width="80" />
        <el-table-column prop="chunk_count" :label="$t('ai.knowledgeManager.chunks')" width="80" />
        <el-table-column :label="$t('ai.knowledgeManager.updated')" width="180">
          <template #default="{ row }">{{ formatTime(row.updated_at) }}</template>
        </el-table-column>
        <el-table-column :label="$t('ai.knowledgeManager.actions')" width="150" fixed="right">
          <template #default="{ row }">
            <el-button v-if="row.source_type === 'runbook'" link type="primary" @click="openEdit(row)">
              {{ $t('ai.knowledgeManager.edit') }}
            </el-button>
            <el-button link type="danger" @click="remove(row)">{{ $t('ai.knowledgeManager.delete') }}</el-button>
          </template>
        </el-table-column>
        <template #empty><p class="empty">{{ $t('ai.knowledgeManager.empty') }}</p></template>
      </el-table>
    </el-card>

    <el-dialog v-model="dialogOpen" :title="dialogTitle" width="min(720px, 92vw)" destroy-on-close>
      <el-form :model="documentForm" label-position="top">
        <el-form-item :label="$t('ai.knowledgeManager.titleLabel')">
          <el-input v-model="documentForm.title" maxlength="128" show-word-limit />
        </el-form-item>
        <el-form-item :label="$t('ai.knowledgeManager.scope')">
          <el-input v-model="documentForm.scope" :placeholder="$t('ai.knowledgeManager.scopeHint')" maxlength="128" />
        </el-form-item>
        <el-form-item :label="$t('ai.knowledgeManager.content')">
          <el-input v-model="documentForm.content" type="textarea" :rows="14" maxlength="1048576" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogOpen = false">{{ $t('ai.knowledgeManager.cancel') }}</el-button>
        <el-button type="primary" :loading="savingDocument" @click="saveDocument">
          {{ $t('ai.knowledgeManager.save') }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { Refresh } from '@element-plus/icons-vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
  createKnowledgeDocument,
  deleteKnowledgeDocument,
  getKnowledgeConfig,
  getKnowledgeDocument,
  listKnowledgeDocuments,
  reindexKnowledge,
  saveKnowledgeConfig,
  updateKnowledgeDocument,
} from '@/api/autonomy'
import { t } from '@/i18n'
import type { KnowledgeDocument, KnowledgeEmbeddingConfig } from '@/types/autonomy'

const loading = ref(false)
const savingConfig = ref(false)
const savingDocument = ref(false)
const reindexing = ref(false)
const config = ref<KnowledgeEmbeddingConfig | null>(null)
const documents = ref<KnowledgeDocument[]>([])
const dialogOpen = ref(false)
const editingId = ref('')
const configForm = reactive({ provider_type: 'local' as 'local' | 'openai_compatible', base_url: '', model: '', dimension: 512, api_key: '' })
const documentForm = reactive({ title: '', scope: 'global', content: '' })

const indexSummary = computed(() => t('ai.knowledgeManager.state', {
  state: t(`ai.ops.knowledge.${config.value?.index_state || 'unknown'}`),
  n: config.value?.indexed_chunks ?? 0,
}))
const keyPlaceholder = computed(() => config.value?.api_key_configured ? t('ai.knowledgeManager.keySaved') : '')
const dialogTitle = computed(() => t(editingId.value ? 'ai.knowledgeManager.dialogEdit' : 'ai.knowledgeManager.dialogAdd'))

function applyConfig(value: KnowledgeEmbeddingConfig): void {
  config.value = value
  configForm.provider_type = value.provider_type
  configForm.base_url = value.base_url
  configForm.model = value.model
  configForm.dimension = value.dimension
  configForm.api_key = ''
}

function formatTime(value: string | null): string {
  return value ? new Date(value).toLocaleString() : '—'
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const [nextConfig, nextDocuments] = await Promise.all([getKnowledgeConfig(), listKnowledgeDocuments()])
    applyConfig(nextConfig)
    documents.value = nextDocuments
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : t('ai.knowledgeManager.loadFailed'))
  } finally {
    loading.value = false
  }
}

async function saveConfig(): Promise<void> {
  savingConfig.value = true
  try {
    applyConfig(await saveKnowledgeConfig({ ...configForm }))
    ElMessage.success(t('ai.knowledgeManager.saved'))
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : t('ai.knowledgeManager.loadFailed'))
  } finally {
    savingConfig.value = false
  }
}

function openCreate(): void {
  editingId.value = ''
  Object.assign(documentForm, { title: '', scope: 'global', content: '' })
  dialogOpen.value = true
}

async function openEdit(document: KnowledgeDocument): Promise<void> {
  loading.value = true
  try {
    const detail = await getKnowledgeDocument(document.id)
    editingId.value = detail.id
    Object.assign(documentForm, { title: detail.title, scope: detail.scope, content: detail.content || '' })
    dialogOpen.value = true
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : t('ai.knowledgeManager.loadFailed'))
  } finally {
    loading.value = false
  }
}

async function saveDocument(): Promise<void> {
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
  await ElMessageBox.confirm(t('ai.knowledgeManager.deleteConfirm', { title: document.title }))
  await deleteKnowledgeDocument(document.id)
  ElMessage.success(t('ai.knowledgeManager.deleted'))
  await load()
}

async function reindex(): Promise<void> {
  reindexing.value = true
  try {
    applyConfig(await reindexKnowledge())
    ElMessage.success(t('ai.knowledgeManager.indexed'))
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
.knowledge-page { padding: 28px; }
.knowledge-hero { display: flex; justify-content: space-between; align-items: flex-start; gap: 24px; max-width: 1280px; margin: 0 auto 18px; }
.knowledge-hero span { color: var(--ogs-primary); font: 600 11px/1 var(--ogs-mono); letter-spacing: .12em; text-transform: uppercase; }
.knowledge-hero h1 { margin: 8px 0 6px; color: var(--ogs-text); font-size: 28px; }
.knowledge-hero p { max-width: 780px; margin: 0; color: var(--ogs-text-secondary); font-size: 13px; line-height: 1.6; }
.knowledge-card { max-width: 1280px; margin: 0 auto 16px; border-color: var(--ogs-border); background: var(--ogs-surface); }
.card-title { display: flex; justify-content: space-between; align-items: center; gap: 16px; color: var(--ogs-text); }
.card-title span { color: var(--ogs-text-secondary); font-size: 12px; }
.config-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 0 16px; align-items: end; }
.config-grid :deep(.el-form-item) { margin-bottom: 10px; }
.local-model { align-self: center; display: flex; flex-direction: column; gap: 5px; padding-bottom: 11px; color: var(--ogs-text-secondary); font-size: 12px; }
.local-model code { color: var(--ogs-text); }
.config-action { align-self: end; }
.empty { padding: 30px 0; color: var(--ogs-text-muted); }
@media (max-width: 800px) {
  .knowledge-page { padding: 18px 12px; }
  .knowledge-hero { align-items: stretch; flex-direction: column; }
  .knowledge-hero .el-button { width: 100%; }
  .config-grid { grid-template-columns: 1fr; }
  .card-title { align-items: flex-start; flex-direction: column; }
}
</style>
