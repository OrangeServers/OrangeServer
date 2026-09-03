import { createApp, defineComponent, h } from 'vue'
import { describe, expect, it, vi } from 'vitest'

import AutonomyDraftCard from '@/components/ai/AutonomyDraftCard.vue'

const mocks = vi.hoisted(() => ({ push: vi.fn() }))

vi.mock('@/i18n', () => ({ t: (key: string) => key }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: mocks.push }) }))

const Passthrough = defineComponent({
  setup(_props, { attrs, slots }) {
    return () => h('div', attrs, slots.default?.())
  },
})

describe('AutonomyDraftCard', () => {
  it('shows the selected custom action categories beside the profile', () => {
    const root = document.createElement('div')
    const app = createApp(AutonomyDraftCard, {
      draft: {
        run_id: 'run-1', goal: 'restart nginx', status: 'draft', mode: 'custom',
        action_categories: ['file_read', 'systemd'],
      },
    })
    app.config.globalProperties.$t = (key: string) => key
    app.component('ElIcon', Passthrough)
    app.component('ElTag', Passthrough)
    app.component('ElButton', Passthrough)
    app.mount(root)

    expect(root.textContent).toContain('aiRuns.mode.custom')
    expect(root.textContent).toContain('aiRuns.dialog.category.file_read')
    expect(root.textContent).toContain('aiRuns.dialog.category.systemd')
    app.unmount()
  })

  it('opens a draft in task detail', () => {
    mocks.push.mockClear()
    const root = document.createElement('div')
    const app = createApp(AutonomyDraftCard, {
      draft: { run_id: 'run-1', goal: 'restart nginx', status: 'draft', mode: 'ask' },
    })
    app.config.globalProperties.$t = (key: string) => key
    app.component('ElIcon', Passthrough)
    app.component('ElTag', Passthrough)
    app.component('ElButton', Passthrough)
    app.mount(root)

    Array.from(root.querySelectorAll<HTMLElement>('.draft-actions > div'))
      .find(element => element.textContent?.includes('ai.autonomyDraft.open'))
      ?.click()

    expect(mocks.push).toHaveBeenCalledWith({
      name: 'AiOpsRunDetail',
      params: { runId: 'run-1' },
    })
    app.unmount()
  })
})
