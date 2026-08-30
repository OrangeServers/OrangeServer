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
    mocks.search.mockResolvedValue([])
  })

  async function mountKnowledge(): Promise<{ app: ReturnType<typeof createApp>; root: HTMLDivElement }> {
    const root = document.createElement('div')
    const app = createApp(AiKnowledge)
    app.config.globalProperties.$t = (key: string) => key
    for (const name of [
      'ElCard', 'ElForm', 'ElFormItem', 'ElSelect', 'ElOption', 'ElInput',
      'ElInputNumber', 'ElButton', 'ElIcon', 'ElTable', 'ElDialog', 'ElDrawer',
    ]) app.component(name, Passthrough)
    app.component('ElTableColumn', Empty)
    app.directive('loading', () => undefined)
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
})
