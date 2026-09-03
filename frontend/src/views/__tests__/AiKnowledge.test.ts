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
  previewDocument: vi.fn(),
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
  previewKnowledgeDocument: mocks.previewDocument,
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
      'data-loading': String(props.loading),
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
    mocks.previewDocument.mockResolvedValue({
      title: 'disk-runbook', content: '# Disk\n\nCheck usage.',
      detected_type: 'markdown', warnings: [],
    })
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

  it('loads an uploaded preview into the existing review form without saving it', async () => {
    const { app, root } = await mountKnowledge()
    Array.from(root.querySelectorAll('button'))
      .find(button => button.textContent?.includes('ai.knowledgeManager.add'))?.click()
    await nextTick()
    const input = root.querySelector<HTMLInputElement>('[data-testid="knowledge-file"]')
    expect(input).not.toBeNull()
    const file = new File(['# Disk'], 'disk-runbook.md', { type: 'text/markdown' })
    Object.defineProperty(input, 'files', { value: [file] })
    input!.dispatchEvent(new Event('change'))
    await Promise.resolve()
    await nextTick()

    expect(mocks.previewDocument).toHaveBeenCalledWith(file)
    expect(mocks.createDocument).not.toHaveBeenCalled()
    expect(root.textContent).toContain('ai.knowledgeManager.previewReady')
    app.unmount()
  })

  it('shows upload progress and reports conversion errors without saving', async () => {
    let rejectPreview!: (error: Error) => void
    mocks.previewDocument.mockReturnValue(new Promise((_resolve, reject) => {
      rejectPreview = reject
    }))
    const { app, root } = await mountKnowledge()
    Array.from(root.querySelectorAll('button'))
      .find(button => button.textContent?.includes('ai.knowledgeManager.add'))?.click()
    await nextTick()
    const input = root.querySelector<HTMLInputElement>('[data-testid="knowledge-file"]')!
    const file = new File(['broken'], 'broken.pdf', { type: 'application/pdf' })
    Object.defineProperty(input, 'files', { value: [file] })
    input.dispatchEvent(new Event('change'))
    await nextTick()

    const upload = Array.from(root.querySelectorAll('button'))
      .find(button => button.textContent?.includes('ai.knowledgeManager.upload'))
    expect(upload?.dataset.loading).toBe('true')

    rejectPreview(new Error('encrypted PDF is not supported'))
    await Promise.resolve()
    await nextTick()

    expect(upload?.dataset.loading).toBe('false')
    expect(mocks.messageError).toHaveBeenCalledWith('encrypted PDF is not supported')
    expect(mocks.createDocument).not.toHaveBeenCalled()
    app.unmount()
  })

  it('ignores an older upload response when a newer preview finishes first', async () => {
    let resolveFirst!: (value: object) => void
    let resolveSecond!: (value: object) => void
    mocks.previewDocument
      .mockReturnValueOnce(new Promise(resolve => { resolveFirst = resolve }))
      .mockReturnValueOnce(new Promise(resolve => { resolveSecond = resolve }))
    const { app, root } = await mountKnowledge()
    Array.from(root.querySelectorAll('button'))
      .find(button => button.textContent?.includes('ai.knowledgeManager.add'))?.click()
    await nextTick()
    const input = root.querySelector<HTMLInputElement>('[data-testid="knowledge-file"]')!

    Object.defineProperty(input, 'files', {
      configurable: true,
      value: [new File(['first'], 'first.md', { type: 'text/markdown' })],
    })
    input.dispatchEvent(new Event('change'))
    Object.defineProperty(input, 'files', {
      configurable: true,
      value: [new File(['second'], 'second.md', { type: 'text/markdown' })],
    })
    input.dispatchEvent(new Event('change'))
    resolveSecond({ title: 'second', content: '# Second', detected_type: 'markdown', warnings: [] })
    await Promise.resolve()
    resolveFirst({ title: 'first', content: '# First', detected_type: 'markdown', warnings: [] })
    await Promise.resolve()
    await nextTick()

    const values = Array.from(root.querySelectorAll<HTMLInputElement>('input'))
      .map(element => element.value)
    expect(values).toContain('second')
    expect(values).not.toContain('first')
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

  it('waits for an asynchronous rebuild to finish before reporting success', async () => {
    vi.useFakeTimers()
    const empty = {
      provider_type: 'local', base_url: '', model: 'BAAI/bge-small-zh-v1.5',
      dimension: 512, api_key_configured: false, model_fingerprint: 'f',
      indexed_fingerprint: null, index_state: 'empty', indexed_chunks: 0,
      created_at: null, updated_at: null,
    }
    mocks.reindex.mockResolvedValue({ ...empty, index_state: 'rebuilding' })
    mocks.getConfig
      .mockResolvedValueOnce(empty)
      .mockResolvedValue({ ...empty, index_state: 'ready', indexed_chunks: 2 })

    const { app, root } = await mountKnowledge()
    try {
      Array.from(root.querySelectorAll('button'))
        .find(button => button.textContent?.includes('ai.knowledgeManager.tabs.status'))?.click()
      await nextTick()
      Array.from(root.querySelectorAll('button'))
        .find(button => button.textContent?.includes('ai.knowledgeManager.reindex'))?.click()
      await Promise.resolve()

      expect(mocks.messageSuccess).not.toHaveBeenCalled()
      await vi.advanceTimersByTimeAsync(1000)
      await nextTick()

      expect(mocks.messageSuccess).toHaveBeenCalledWith('ai.knowledgeManager.indexed')
      expect(root.textContent).toContain('ai.ops.knowledge.ready')
    } finally {
      app.unmount()
      vi.useRealTimers()
    }
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
