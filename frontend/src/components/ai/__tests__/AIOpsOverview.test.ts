import { createApp, defineComponent, h, nextTick } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import AIOpsOverview from '@/components/ai/AIOpsOverview.vue'

const mocks = vi.hoisted(() => ({
  getStatus: vi.fn(),
  push: vi.fn(),
}))

vi.mock('@/api/autonomy', () => ({ getAIOpsStatus: mocks.getStatus }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: mocks.push }) }))
vi.mock('@/i18n', () => ({ t: (key: string) => key }))

const ButtonStub = defineComponent({
  emits: ['click'],
  setup(_props, { emit, slots }) {
    return () => h('button', { onClick: () => emit('click') }, slots.default?.())
  },
})
const IconStub = defineComponent({
  setup(_props, { slots }) { return () => h('span', slots.default?.()) },
})

describe('AIOpsOverview', () => {
  beforeEach(() => {
    mocks.getStatus.mockReset()
    mocks.push.mockReset()
  })

  it('renders authoritative alert counts and opens the selected run', async () => {
    mocks.getStatus.mockResolvedValue({
      enabled: true,
      configured: true,
      checkpoint_ready: true,
      worker_ready: true,
      ready: true,
      reason: 'ready',
      worker_pool: 'prefork',
      worker_concurrency_configured: 2,
      worker_concurrency_observed: 2,
      web_worker_class: 'gevent',
      autonomy_pool: 'prefork',
      autonomy_concurrency: 2,
      active_runs: 1,
      queued_runs: 1,
      knowledge_index_state: 'not_configured',
      alertmanager_configured: true,
      prometheus_configured: true,
      pending_alerts: [{
        id: 'run-alert',
        goal: 'Investigate service down',
        trigger_summary: 'nginx down',
        host_alias: 'example-host',
        status: 'queued',
      }],
      running_runs: [],
      recent_conclusions: [],
    })
    const root = document.createElement('div')
    const app = createApp(AIOpsOverview)
    app.config.globalProperties.$t = (key: string) => key
    app.component('ElButton', ButtonStub)
    app.component('ElIcon', IconStub)
    app.mount(root)

    await Promise.resolve()
    await nextTick()
    expect(root.textContent).toContain('nginx down')
    expect(root.textContent).toContain('1')

    ;(root.querySelector('.ops-list button') as HTMLButtonElement).click()
    expect(mocks.push).toHaveBeenCalledWith('/ai-runs/run-alert')
    app.unmount()
  })
})
