import { createApp, defineComponent, h, nextTick } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { AutonomyRun } from '@/types/autonomy'
import AiRuns from '@/views/AiRuns.vue'

const mocks = vi.hoisted(() => ({
  createRun: vi.fn(),
  getStatus: vi.fn(),
  listRuns: vi.fn(),
  getHosts: vi.fn(),
  getSystemUsers: vi.fn(),
  push: vi.fn(),
}))

vi.mock('@/api/autonomy', () => ({
  createAutonomyRun: mocks.createRun,
  getAutonomyStatus: mocks.getStatus,
  listAutonomyRuns: mocks.listRuns,
  listAutonomySystemUsers: mocks.getSystemUsers,
}))
vi.mock('@/api', () => ({
  getHostList: mocks.getHosts,
}))
vi.mock('vue-router', () => ({ useRouter: () => ({ push: mocks.push }) }))
vi.mock('@/i18n', () => ({
  currentLocale: () => 'en-US',
  t: (key: string) => {
    const labels: Record<string, string> = {
      'aiRuns.alerts.unknown': 'Unknown',
      'aiRuns.alerts.signal.firing': 'Firing',
      'aiRuns.alerts.signal.resolved': 'Resolved notification',
      'aiRuns.alerts.signal.unknown': 'Unknown',
      'aiRuns.outcome.none': 'No outcome',
    }
    return labels[key] || key.replace(/^aiRuns\.(status|outcome)\./, '$1:')
  },
}))

const Passthrough = defineComponent({
  setup(_props, { slots }) {
    return () => h('div', [slots.default?.(), slots.footer?.()])
  },
})
const ButtonStub = defineComponent({
  emits: ['click'],
  setup(_props, { emit, slots }) {
    return () => h('button', { onClick: () => emit('click') }, slots.default?.())
  },
})
const AlertStub = defineComponent({
  props: { title: { type: String, default: '' } },
  setup(props, { slots }) {
    return () => h('div', { role: 'alert' }, [props.title, slots.default?.()])
  },
})

function makeRun(overrides: Partial<AutonomyRun> = {}): AutonomyRun {
  return {
    id: 'run-alert-12345678',
    owner: 'admin',
    goal: 'Investigate the alert',
    host_id: 7,
    host_alias: 'prod-web-01',
    system_user_id: 11,
    system_user_alias: 'ops',
    mode: 'ask',
    custom_profile: null,
    status: 'running',
    outcome: null,
    conclusion: null,
    trigger_type: 'alertmanager',
    trigger_ref: 'abcdef1234567890',
    trigger_summary: 'firing: payments-api on asset #7',
    alert_state: 'firing',
    alert_updated_at: '2026-08-29T08:00:07Z',
    revision: 1,
    graph_version: 'v1',
    budget: {},
    latest_event_seq: 0,
    cancel_requested: false,
    started_at: '2026-08-29T08:00:06Z',
    completed_at: null,
    created_at: '2026-08-29T08:00:05Z',
    ...overrides,
  }
}

async function mountAlerts(): Promise<{ app: ReturnType<typeof createApp>; root: HTMLDivElement }> {
  const root = document.createElement('div')
  const app = createApp(AiRuns, { alertsOnly: true })
  for (const name of [
    'ElCheckbox', 'ElCheckboxGroup', 'ElDialog', 'ElEmpty',
    'ElForm', 'ElFormItem', 'ElInput', 'ElInputNumber', 'ElOption', 'ElRadio',
    'ElRadioGroup', 'ElSelect', 'ElTag',
  ]) app.component(name, Passthrough)
  app.component('ElAlert', AlertStub)
  app.component('ElButton', ButtonStub)
  app.directive('loading', () => undefined)
  app.mount(root)
  await Promise.resolve()
  await nextTick()
  return { app, root }
}

async function mountRuns(): Promise<{ app: ReturnType<typeof createApp>; root: HTMLDivElement }> {
  const root = document.createElement('div')
  const app = createApp(AiRuns)
  for (const name of [
    'ElCheckbox', 'ElCheckboxGroup', 'ElDialog', 'ElEmpty',
    'ElForm', 'ElFormItem', 'ElInput', 'ElInputNumber', 'ElOption', 'ElRadio',
    'ElRadioGroup', 'ElSelect', 'ElTag',
  ]) app.component(name, Passthrough)
  app.component('ElAlert', AlertStub)
  app.component('ElButton', ButtonStub)
  app.directive('loading', () => undefined)
  app.mount(root)
  await Promise.resolve()
  await nextTick()
  return { app, root }
}

describe('AiRuns alert view', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.getStatus.mockResolvedValue({ enabled: true, ready: true })
    mocks.listRuns.mockResolvedValue([])
    mocks.getHosts.mockResolvedValue({ host_list_msg: [] })
    mocks.getSystemUsers.mockResolvedValue([{ id: 11, alias: 'ops' }])
  })

  it('loads owner-scoped credential options without the admin list endpoint', async () => {
    const { app, root } = await mountRuns()
    const create = Array.from(root.querySelectorAll('button'))
      .find(element => element.textContent?.trim() === 'aiRuns.create')
    create?.click()
    await Promise.resolve()
    await nextTick()

    expect(mocks.getHosts).toHaveBeenCalledOnce()
    expect(mocks.getSystemUsers).toHaveBeenCalledOnce()
    app.unmount()
  })

  it('groups similar alerts and keeps older Runs reachable', async () => {
    mocks.listRuns.mockResolvedValue([
      makeRun(),
      makeRun({ id: 'run-alert-older', created_at: '2026-08-28T08:00:05Z' }),
      makeRun({ id: 'manual-run', trigger_type: 'manual' }),
    ])

    const { app, root } = await mountAlerts()
    const cards = root.querySelectorAll('.alert-card')
    const text = cards[0]?.textContent || ''

    expect(cards).toHaveLength(1)
    expect(text).toContain('Firing')
    expect(text).toContain('payments-api')
    expect(text).toContain('status:running')
    expect(text).toContain('No outcome')
    expect(root.querySelector('.alert-facts')).toBeNull()
    expect(root.querySelectorAll('.alert-history-row')).toHaveLength(1)
    expect(root.querySelector('.runs-groups')).toBeNull()
    app.unmount()
  })

  it('opens Runs in the task detail', async () => {
    mocks.listRuns.mockResolvedValue([makeRun()])
    const alerts = await mountAlerts()
    alerts.root.querySelector<HTMLButtonElement>('.alert-card')?.click()
    expect(mocks.push).toHaveBeenCalledWith({
      name: 'AiOpsRunDetail',
      params: { runId: 'run-alert-12345678' },
    })
    alerts.app.unmount()

    mocks.push.mockClear()
    mocks.listRuns.mockResolvedValue([makeRun({ trigger_type: 'manual' })])
    const tasks = await mountRuns()
    tasks.root.querySelector<HTMLButtonElement>('.runs-row')?.click()
    expect(mocks.push).toHaveBeenCalledWith({
      name: 'AiOpsRunDetail',
      params: { runId: 'run-alert-12345678' },
    })
    tasks.app.unmount()
  })

  it('uses the latest Alertmanager event instead of the newest Run creation', async () => {
    mocks.listRuns.mockResolvedValue([
      makeRun({
        id: 'newer-run',
        created_at: '2026-08-30T08:00:00Z',
        alert_updated_at: '2026-08-30T08:01:00Z',
      }),
      makeRun({
        id: 'older-run-later-event',
        created_at: '2026-08-29T08:00:00Z',
        alert_updated_at: '2026-08-30T09:00:00Z',
        alert_state: 'resolved',
      }),
    ])

    const { app, root } = await mountAlerts()
    const card = root.querySelector('.alert-card')

    expect(card?.classList.contains('is-alert-resolved')).toBe(true)
    expect(card?.querySelector('.alert-run-id')?.textContent).toContain('older-ru')
    expect(root.querySelectorAll('.alert-history-row')).toHaveLength(1)
    app.unmount()
  })

  it('keeps the Alertmanager signal separate from the linked Run outcome', async () => {
    mocks.listRuns.mockResolvedValue([
      makeRun({
        id: 'resolved-signal',
        trigger_summary: 'firing: billing-api on asset #7',
        alert_state: 'resolved',
      }),
      makeRun({
        id: 'resolved-outcome',
        trigger_summary: 'firing: ledger-api on asset #7',
        alert_state: 'firing',
        outcome: 'resolved',
        status: 'completed',
      }),
    ])

    const { app, root } = await mountAlerts()
    const resolvedSignal = root.querySelector('.is-alert-resolved')
    const firingSignal = root.querySelector('.is-alert-firing')

    expect(resolvedSignal?.querySelector('.alert-signal')?.textContent).toContain('Resolved notification')
    expect(firingSignal?.querySelector('.alert-signal')?.textContent).toContain('Firing')
    expect(firingSignal?.querySelector('.alert-run-tags')?.textContent).toContain('outcome:resolved')
    app.unmount()
  })

  it('keeps loaded alerts visible when a refresh fails', async () => {
    mocks.listRuns
      .mockResolvedValueOnce([makeRun()])
      .mockRejectedValueOnce(new Error('network failed'))

    const { app, root } = await mountAlerts()
    const refresh = Array.from(root.querySelectorAll('button'))
      .find(element => element.textContent?.trim() === 'common.action.refresh')
    refresh?.click()
    await Promise.resolve()
    await Promise.resolve()
    await nextTick()

    expect(mocks.listRuns).toHaveBeenCalledTimes(2)
    expect(root.querySelector('.alert-card')).not.toBeNull()
    expect(root.querySelector('[role="alert"]')?.textContent).toContain('network failed')
    app.unmount()
  })
})
