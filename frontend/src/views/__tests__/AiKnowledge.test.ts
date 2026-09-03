import { createApp, defineComponent, h, nextTick } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import AiKnowledge from '@/views/AiKnowledge.vue'

const mocks = vi.hoisted(() => ({
  user: { role: 'admin' },
  getConfig: vi.fn(),
  getStatus: vi.fn(),
  listDocuments: vi.fn(),
  search: vi.fn(),
  saveConfig: vi.fn(),
  createDocument: vi.fn(),
  getDocument: vi.fn(),
  updateDocument: vi.fn(),
  deleteDocument: vi.fn(),
  reindex: vi.fn(),
  confirm: vi.fn(),
  messageError: vi.fn(),
  messageSuccess: vi.fn(),
}))

vi.mock('element-plus', () => ({
  ElMessage: {
    error: mocks.messageError,
    success: mocks.messageSuccess,
  },
  ElMessageBox: { confirm: mocks.confirm },
}))

vi.mock('@/api/autonomy', () => ({
  getKnowledgeConfig: mocks.getConfig,
  getAIOpsStatus: mocks.getStatus,
  listKnowledgeDocuments: mocks.listDocuments,
  searchKnowledge: mocks.search,
  saveKnowledgeConfig: mocks.saveConfig,
  createKnowledgeDocument: mocks.createDocument,
  getKnowledgeDocument: mocks.getDocument,
  updateKnowledgeDocument: mocks.updateDocument,
  deleteKnowledgeDocument: mocks.deleteDocument,
  reindexKnowledge: mocks.reindex,
}))
vi.mock('@/i18n', () => ({ t: (key: string) => key }))
vi.mock('@/store', () => ({ store: { user: mocks.user } }))

const Passthrough = defineComponent({
  setup(_props, { slots }) {
    return () => h('div', [slots.header?.(), slots.default?.(), slots.footer?.(), slots.empty?.()])
  },
})
const InputStub = defineComponent({
  props: { modelValue: { type: String, default: '' } },
  emits: ['update:modelValue', 'keyup'],
  setup(props, { emit }) {
    return () => h('input', {
      value: props.modelValue,
      onInput: (event: Event) => emit('update:modelValue', (event.target as HTMLInputElement).value),
      onKeyup: (event: KeyboardEvent) => emit('keyup', event),
    })
  },
})
const ButtonStub = defineComponent({
  props: { disabled: Boolean, loading: Boolean },
  emits: ['click'],
  setup(props, { emit, slots }) {
    return () => h('button', {
      disabled: props.disabled,
      onClick: (event: MouseEvent) => emit('click', event),
    }, slots.default?.())
  },
})
const Empty = defineComponent({ setup: () => () => h('span') })

describe('AiKnowledge', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.user.role = 'admin'
    mocks.getConfig.mockResolvedValue({
      provider_type: 'local', base_url: '', model: 'BAAI/bge-small-zh-v1.5',
      dimension: 512, api_key_configured: false, model_fingerprint: 'f',
      indexed_fingerprint: null, index_state: 'empty', indexed_chunks: 0,
      created_at: null, updated_at: null,
    })
    mocks.getStatus.mockResolvedValue({ knowledge_index_state: 'empty' })
    mocks.listDocuments.mockResolvedValue([])
    mocks.search.mockResolvedValue({ results: [], count: 0, index_state: 'empty' })
    mocks.confirm.mockResolvedValue(undefined)
  })

  async function mountKnowledge(): Promise<{ app: ReturnType<typeof createApp>; root: HTMLDivElement }> {
    const root = document.createElement('div')
    const app = createApp(AiKnowledge)
    app.config.globalProperties.$t = (key: string) => key
    for (const name of [
      'ElCard', 'ElForm', 'ElFormItem', 'ElSelect', 'ElOption',
      'ElInputNumber', 'ElIcon', 'ElTable', 'ElDialog', 'ElDrawer',
    ]) app.component(name, Passthrough)
    app.component('ElInput', InputStub)
    app.component('ElButton', ButtonStub)
    app.component('ElTableColumn', Empty)
    app.directive('loading', {
      mounted: (element, binding) => { element.dataset.loading = String(binding.value) },
      updated: (element, binding) => { element.dataset.loading = String(binding.value) },
    })
    app.mount(root)
    await Promise.resolve()
    await nextTick()
    return { app, root }
  }

  it('loads admin configuration and the knowledge catalog', async () => {
    const { app, root } = await mountKnowledge()

    expect(mocks.getConfig).toHaveBeenCalledOnce()
    expect(mocks.getStatus).toHaveBeenCalledOnce()
    expect(mocks.listDocuments).toHaveBeenCalledOnce()
    expect(root.textContent).toContain('BAAI/bge-small-zh-v1.5')
    app.unmount()
  })

  it('shows knowledge sources without waiting for the operations status', async () => {
    let resolveStatus!: (value: { knowledge_index_state: string }) => void
    mocks.getStatus.mockReturnValue(new Promise((resolve) => { resolveStatus = resolve }))
    mocks.listDocuments.mockResolvedValue([{
      id: 'doc-1', title: 'Disk runbook', source_type: 'runbook', source_ref: null,
      scope: 'global', content_sha256: 'abc', version: 1, approved: true,
      indexed: true, chunk_count: 2, created_by: 'admin', created_at: null,
      updated_at: null,
    }])

    const { app, root } = await mountKnowledge()
    await Promise.resolve()
    await nextTick()

    expect(root.querySelector<HTMLElement>('.knowledge-scroll')?.dataset.loading).toBe('false')
    expect(root.textContent).toContain('Disk runbook')
    expect(mocks.getConfig).toHaveBeenCalledOnce()

    resolveStatus({ knowledge_index_state: 'ready' })
    await Promise.resolve()
    app.unmount()
  })

  it('keeps embedding configuration read-only for a normal user', async () => {
    mocks.user.role = 'user'
    const { app, root } = await mountKnowledge()

    expect(mocks.getConfig).not.toHaveBeenCalled()
    expect(mocks.getStatus).toHaveBeenCalledOnce()
    expect(mocks.listDocuments).toHaveBeenCalledOnce()
    expect(root.textContent).toContain('ai.knowledgeManager.userMode')
    expect(root.textContent).not.toContain('ai.knowledgeManager.add')
    app.unmount()
  })

  it('shows a retryable catalog error instead of an empty knowledge base', async () => {
    mocks.listDocuments.mockRejectedValue(new Error('network failed'))

    const { app, root } = await mountKnowledge()
    await Promise.resolve()
    await nextTick()

    expect(root.querySelector('[role="alert"]')?.textContent)
      .toContain('ai.knowledgeManager.loadFailed')
    expect(root.textContent).not.toContain('ai.knowledgeManager.empty')
    app.unmount()
  })

  it('reports a document delete failure and keeps the loaded source visible', async () => {
    mocks.listDocuments.mockResolvedValue([{
      id: 'doc-1', title: 'Disk runbook', source_type: 'runbook', source_ref: null,
      scope: 'global', content_sha256: 'abc', version: 1, approved: true,
      indexed: true, chunk_count: 2, created_by: 'admin', created_at: null,
      updated_at: null,
    }])
    mocks.deleteDocument.mockRejectedValue(new Error('delete failed'))

    const { app, root } = await mountKnowledge()
    const removeButton = Array.from(root.querySelectorAll('button'))
      .find(button => button.textContent?.includes('ai.knowledgeManager.delete'))
    removeButton?.click()
    await Promise.resolve()
    await Promise.resolve()
    await nextTick()

    expect(mocks.messageError).toHaveBeenCalledWith('delete failed')
    expect(root.textContent).toContain('Disk runbook')
    expect(mocks.listDocuments).toHaveBeenCalledOnce()
    app.unmount()
  })

  it.each([
    ['request failure', 'ready', 'ready', new Error('network failed'), 'ai.knowledgeManager.searchFailed'],
    ['missing permission', 'ready', 'ready', Object.assign(new Error('forbidden'), { status: 403 }), 'ai.knowledgeManager.searchForbidden'],
    ['index changed while searching', 'ready', 'rebuilding', null, 'ai.knowledgeManager.searchIndexNotReady'],
    ['zero matches', 'ready', 'ready', null, 'ai.knowledgeManager.searchEmpty'],
  ])('distinguishes %s from other empty search outcomes', async (
    _name, loadedIndexState, responseIndexState, error, expected,
  ) => {
    mocks.user.role = 'user'
    mocks.getStatus.mockResolvedValue({ knowledge_index_state: loadedIndexState })
    mocks.search.mockResolvedValue({ results: [], count: 0, index_state: responseIndexState })
    if (error) mocks.search.mockRejectedValue(error)

    const { app, root } = await mountKnowledge()
    const searchTab = Array.from(root.querySelectorAll('button'))
      .find(button => button.textContent?.includes('ai.knowledgeManager.tabs.search'))
    searchTab?.click()
    await nextTick()

    const input = root.querySelector('input')
    expect(input).not.toBeNull()
    input!.value = 'disk full'
    input!.dispatchEvent(new Event('input'))
    await nextTick()
    const searchButton = Array.from(root.querySelectorAll('button'))
      .find(button => button.textContent?.includes('ai.knowledgeManager.search'))
    searchButton?.click()
    await Promise.resolve()
    await nextTick()

    for (const key of [
      'ai.knowledgeManager.searchFailed',
      'ai.knowledgeManager.searchForbidden',
      'ai.knowledgeManager.searchIndexNotReady',
      'ai.knowledgeManager.searchEmpty',
    ]) expect(root.textContent?.includes(key)).toBe(key === expected)
    app.unmount()
  })
})
