import { createApp, defineComponent, h, nextTick } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import AiKnowledge from '@/views/AiKnowledge.vue'

const mocks = vi.hoisted(() => ({
  getConfig: vi.fn(),
  listDocuments: vi.fn(),
  saveConfig: vi.fn(),
  createDocument: vi.fn(),
  getDocument: vi.fn(),
  updateDocument: vi.fn(),
  deleteDocument: vi.fn(),
  reindex: vi.fn(),
}))

vi.mock('@/api/autonomy', () => ({
  getKnowledgeConfig: mocks.getConfig,
  listKnowledgeDocuments: mocks.listDocuments,
  saveKnowledgeConfig: mocks.saveConfig,
  createKnowledgeDocument: mocks.createDocument,
  getKnowledgeDocument: mocks.getDocument,
  updateKnowledgeDocument: mocks.updateDocument,
  deleteKnowledgeDocument: mocks.deleteDocument,
  reindexKnowledge: mocks.reindex,
}))
vi.mock('@/i18n', () => ({ t: (key: string) => key }))

const Passthrough = defineComponent({
  setup(_props, { slots }) {
    return () => h('div', [slots.header?.(), slots.default?.(), slots.footer?.(), slots.empty?.()])
  },
})
const Empty = defineComponent({ setup: () => () => h('span') })

describe('AiKnowledge', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.getConfig.mockResolvedValue({
      provider_type: 'local', base_url: '', model: 'BAAI/bge-small-zh-v1.5',
      dimension: 512, api_key_configured: false, model_fingerprint: 'f',
      indexed_fingerprint: null, index_state: 'empty', indexed_chunks: 0,
      created_at: null, updated_at: null,
    })
    mocks.listDocuments.mockResolvedValue([])
  })

  it('loads the reviewed configuration and document list', async () => {
    const root = document.createElement('div')
    const app = createApp(AiKnowledge)
    app.config.globalProperties.$t = (key: string) => key
    for (const name of [
      'ElCard', 'ElForm', 'ElFormItem', 'ElSelect', 'ElOption', 'ElInput',
      'ElInputNumber', 'ElButton', 'ElIcon', 'ElTable', 'ElDialog',
    ]) app.component(name, Passthrough)
    app.component('ElTableColumn', Empty)
    app.directive('loading', () => undefined)
    app.mount(root)
    await Promise.resolve()
    await nextTick()

    expect(mocks.getConfig).toHaveBeenCalledOnce()
    expect(mocks.listDocuments).toHaveBeenCalledOnce()
    expect(root.textContent).toContain('BAAI/bge-small-zh-v1.5')
    app.unmount()
  })
})
