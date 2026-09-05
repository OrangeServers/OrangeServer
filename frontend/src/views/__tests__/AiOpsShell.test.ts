import { createApp, defineComponent, h, nextTick } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import AiOpsShell from '@/views/AiOpsShell.vue'

const mocks = vi.hoisted(() => ({
  push: vi.fn(),
  route: {
    name: 'AiOpsWorkbench' as string,
    params: {} as Record<string, string>,
    fullPath: '/ai-ops',
  },
  listRuns: vi.fn(),
}))

vi.mock('vue-router', () => ({
  useRoute: () => mocks.route,
  useRouter: () => ({ push: mocks.push }),
}))
vi.mock('@/api/autonomy', () => ({
  listAutonomyRuns: mocks.listRuns,
}))
vi.mock('@/i18n', () => ({ t: (key: string) => key }))
vi.mock('@/utils/datetime', () => ({ formatTimeRel: () => 'now' }))

const Passthrough = defineComponent({
  setup(_props, { slots }) {
    return () => h('div', slots.default?.())
  },
})
const RouterView = defineComponent({
  setup(_props, { slots }) {
    return () => slots.default?.({ Component: Passthrough })
  },
})

describe('AiOpsShell', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.route.name = 'AiOpsWorkbench'
    mocks.route.params = {}
    mocks.route.fullPath = '/ai-ops'
    mocks.listRuns.mockResolvedValue([{
      id: 'run-1', status: 'failed', goal: 'restart service', trigger_summary: '',
      host_alias: 'host-a', created_at: '2026-08-30T00:00:00Z', started_at: null,
      completed_at: '2026-08-30T00:01:00Z', trigger_type: 'manual',
      outcome: null,
    }])
  })

  it('owns Run details under the task tab without the workbench rail', async () => {
    mocks.route.name = 'AiOpsRunDetail'
    mocks.route.params = { runId: 'run-1' }
    mocks.route.fullPath = '/ai-ops/tasks/run-1'

    const root = document.createElement('div')
    const app = createApp(AiOpsShell)
    app.component('ElIcon', Passthrough)
    app.component('ElButton', Passthrough)
    app.component('ElDrawer', Passthrough)
    app.component('RouterView', RouterView)
    app.mount(root)
    await Promise.resolve()
    await Promise.resolve()
    await nextTick()

    expect(root.querySelector('.aiops-tabs')).toBeNull()
    expect(root.querySelector('.aiops-run-rail')).toBeNull()
    app.unmount()
  })

  it('collapses the desktop task rail and keeps the attention entry visible', async () => {
    const root = document.createElement('div')
    const app = createApp(AiOpsShell)
    app.component('ElIcon', Passthrough)
    app.component('ElButton', Passthrough)
    app.component('ElDrawer', Passthrough)
    app.component('RouterView', RouterView)
    app.mount(root)
    await Promise.resolve()
    await Promise.resolve()
    await nextTick()

    const collapse = root.querySelector<HTMLButtonElement>('[aria-label="ai.ops.rail.collapse"]')
    expect(collapse).not.toBeNull()
    collapse?.click()
    await nextTick()

    expect(root.querySelector('.aiops-body')?.classList.contains('rail-collapsed')).toBe(true)
    expect(root.querySelector('.aiops-run-rail')?.classList.contains('is-collapsed')).toBe(true)
    expect(root.querySelector('.aiops-rail-summary-count')?.textContent?.trim()).toBe('1')
    expect(root.querySelector('.aiops-run-rail .aiops-run-list')).toBeNull()
    expect(root.querySelector('[aria-label="ai.ops.rail.expand"]')).not.toBeNull()

    app.unmount()
  })

  it('turns Alertmanager transport text into an operator-facing task row', async () => {
    mocks.listRuns.mockResolvedValue([{
      id: 'run-alert', status: 'failed', goal: 'Investigate alert',
      trigger_summary: 'firing: payments-api on asset #7', trigger_type: 'alertmanager',
      host_alias: 'prod-web-01', created_at: '2026-08-30T00:00:00Z', started_at: null,
      completed_at: '2026-08-30T00:01:00Z', outcome: null,
    }])

    const root = document.createElement('div')
    const app = createApp(AiOpsShell)
    app.component('ElIcon', Passthrough)
    app.component('ElButton', Passthrough)
    app.component('ElDrawer', Passthrough)
    app.component('RouterView', RouterView)
    app.mount(root)
    await Promise.resolve()
    await Promise.resolve()
    await nextTick()

    const row = root.querySelector('.aiops-run-row')
    expect(row?.textContent).toContain('ai.ops.rail.kind.alertmanager')
    expect(row?.textContent).toContain('ai.ops.rail.alertFiringTitle')
    expect(row?.textContent).toContain('aiRuns.nextStep.failed')
    expect(row?.textContent).not.toContain('firing:')
    expect(row?.textContent).not.toContain('asset #7')

    app.unmount()
  })

  it('uses the current Alertmanager event state in the task rail title', async () => {
    mocks.listRuns.mockResolvedValue([{
      id: 'run-alert', status: 'completed', goal: 'Investigate alert',
      trigger_summary: 'firing: payments-api on asset #7', trigger_type: 'alertmanager',
      alert_state: 'resolved', alert_updated_at: '2026-08-30T00:02:00Z',
      host_alias: 'prod-web-01', created_at: '2026-08-30T00:00:00Z', started_at: null,
      completed_at: '2026-08-30T00:01:00Z', outcome: 'resolved',
    }])

    const root = document.createElement('div')
    const app = createApp(AiOpsShell)
    app.component('ElIcon', Passthrough)
    app.component('ElButton', Passthrough)
    app.component('ElDrawer', Passthrough)
    app.component('RouterView', RouterView)
    app.mount(root)
    await Promise.resolve()
    await Promise.resolve()
    await nextTick()

    expect(root.querySelector('.aiops-run-title')?.textContent)
      .toContain('ai.ops.rail.alertResolvedTitle')
    expect(root.querySelector('.aiops-run-title')?.textContent)
      .not.toContain('ai.ops.rail.alertFiringTitle')
    app.unmount()
  })

  it('opens a rail Run as a task detail', async () => {
    mocks.listRuns.mockResolvedValue([{
      id: 'run-alert', status: 'failed', goal: 'Investigate alert',
      trigger_summary: 'firing: payments-api on asset #7', trigger_type: 'alertmanager',
      host_alias: 'prod-web-01', created_at: '2026-08-30T00:00:00Z', started_at: null,
      completed_at: '2026-08-30T00:01:00Z', outcome: null,
    }])

    const root = document.createElement('div')
    const app = createApp(AiOpsShell)
    app.component('ElIcon', Passthrough)
    app.component('ElButton', Passthrough)
    app.component('ElDrawer', Passthrough)
    app.component('RouterView', RouterView)
    app.mount(root)
    await Promise.resolve()
    await Promise.resolve()
    await nextTick()

    root.querySelector<HTMLButtonElement>('.aiops-run-row')?.click()

    expect(mocks.push).toHaveBeenCalledWith({
      name: 'AiOpsRunDetail',
      params: { runId: 'run-alert' },
    })
    app.unmount()
  })

  it('shows a retryable rail error instead of an empty task list', async () => {
    mocks.listRuns.mockRejectedValue(new Error('network failed'))

    const root = document.createElement('div')
    const app = createApp(AiOpsShell)
    app.component('ElIcon', Passthrough)
    app.component('ElButton', Passthrough)
    app.component('ElDrawer', Passthrough)
    app.component('RouterView', RouterView)
    app.mount(root)
    await Promise.resolve()
    await Promise.resolve()
    await nextTick()

    expect(root.querySelector('.aiops-rail-error')?.textContent)
      .toContain('ai.ops.rail.loadFailed')
    expect(root.textContent).not.toContain('ai.ops.rail.empty')
    app.unmount()
  })
})
