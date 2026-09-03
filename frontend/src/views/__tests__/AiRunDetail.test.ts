import { createApp, defineComponent, h, nextTick } from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import AiRunDetail from '@/views/AiRunDetail.vue'

const mocks = vi.hoisted(() => ({
  push: vi.fn(),
  route: { params: { runId: 'run-failed' } },
  getSnapshot: vi.fn(),
  listArtifacts: vi.fn(),
  listEvidence: vi.fn(),
  streamRun: vi.fn(),
}))

vi.mock('vue-router', () => ({
  useRoute: () => mocks.route,
  useRouter: () => ({ push: mocks.push }),
}))
vi.mock('@/i18n', () => ({ t: (key: string) => key }))
vi.mock('@/store', () => ({ store: { user: { role: 'admin' } } }))
vi.mock('@/utils/datetime', () => ({ formatTimeAbs: () => 'time' }))
vi.mock('@/api/autonomy', () => ({
  getAutonomySnapshot: mocks.getSnapshot,
  listAutonomyArtifacts: mocks.listArtifacts,
  listAutonomyEvidence: mocks.listEvidence,
  streamAutonomyRun: mocks.streamRun,
  cancelAutonomyRun: vi.fn(),
  captureRunKnowledge: vi.fn(),
  decideAutonomyStep: vi.fn(),
  getAutonomyArtifact: vi.fn(),
  startAutonomyRun: vi.fn(),
}))

const Passthrough = defineComponent({
  setup(_props, { slots }) {
    return () => h('div', [slots.default?.(), slots.title?.()])
  },
})
const ButtonStub = defineComponent({
  inheritAttrs: false,
  setup(_props, { attrs, slots }) {
    return () => h('button', attrs, slots.default?.())
  },
})

describe('AiRunDetail', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mocks.getSnapshot.mockResolvedValue({
      id: 'run-failed', owner: 'operator', goal: 'restart the service',
      host_id: 1, host_alias: 'host-a', system_user_id: 2, system_user_alias: 'ops',
      mode: 'ask', custom_profile: null, status: 'failed', outcome: null, conclusion: null,
      trigger_type: 'manual', trigger_ref: null, trigger_summary: '', revision: 3,
      graph_version: 'v1', budget: {}, latest_event_seq: 3, cancel_requested: false,
      started_at: '2026-08-30T00:00:00Z', completed_at: '2026-08-30T00:02:00Z',
      created_at: '2026-08-30T00:00:00Z', allowed_operations: [],
      steps: [
        {
          id: 'step-action', run_id: 'run-failed', kind: 'action', status: 'failed', seq: 1,
          summary: 'systemd restart unit=cron', action_digest: '', note: 'exit_code=1', created_at: null,
        },
        {
          id: 'step-verify', run_id: 'run-failed', kind: 'verification', status: 'succeeded', seq: 2,
          summary: 'service.status unit=cron', action_digest: '', note: 'exit_code=0', created_at: null,
        },
      ],
    })
    mocks.listArtifacts.mockResolvedValue([])
    mocks.listEvidence.mockResolvedValue([])
    mocks.streamRun.mockResolvedValue(undefined)
  })

  it('explains a terminal failure instead of promising a future conclusion', async () => {
    const root = document.createElement('div')
    const app = createApp(AiRunDetail)
    for (const name of ['ElAlert', 'ElDrawer', 'ElIcon', 'ElTag', 'ElTooltip']) {
      app.component(name, Passthrough)
    }
    app.component('ElButton', ButtonStub)
    app.directive('loading', () => undefined)
    app.mount(root)
    await Promise.resolve()
    await Promise.resolve()
    await nextTick()

    expect(root.textContent).toContain('ai.ops.next.reviewFailure')
    expect(root.textContent).toContain('aiRuns.detail.conclusion.failedWithoutConclusionTitle')
    expect(root.textContent).toContain('aiRuns.detail.conclusion.failedWithoutConclusionSummary')
    expect(root.textContent).toContain('aiRuns.detail.conclusion.blockingStep')
    expect(root.textContent).toContain('aiRuns.detail.conclusion.verificationNonFinalFact')
    expect(root.textContent).not.toContain('aiRuns.detail.conclusion.pendingSummary')
    expect(root.querySelector('.run-conclusion')?.classList.contains('is-danger')).toBe(true)

    app.unmount()
  })

  it('returns Run details to the task list', async () => {
    const root = document.createElement('div')
    const app = createApp(AiRunDetail)
    for (const name of ['ElAlert', 'ElDrawer', 'ElIcon', 'ElTag', 'ElTooltip']) {
      app.component(name, Passthrough)
    }
    app.component('ElButton', ButtonStub)
    app.directive('loading', () => undefined)
    app.mount(root)
    await Promise.resolve()
    await Promise.resolve()
    await nextTick()

    root.querySelector<HTMLButtonElement>('.run-detail-back button')?.click()

    expect(mocks.push).toHaveBeenCalledWith({ name: 'AiOpsTasks' })
    app.unmount()
  })

  it('shows the server failure reason when a Run failed before creating steps', async () => {
    mocks.getSnapshot.mockResolvedValue({
      ...(await mocks.getSnapshot()),
      steps: [],
      failure_reason: 'provider tool-call contract did not converge',
    })
    const root = document.createElement('div')
    const app = createApp(AiRunDetail)
    for (const name of ['ElAlert', 'ElDrawer', 'ElIcon', 'ElTag', 'ElTooltip']) {
      app.component(name, Passthrough)
    }
    app.component('ElButton', ButtonStub)
    app.directive('loading', () => undefined)
    app.mount(root)
    await Promise.resolve()
    await Promise.resolve()
    await nextTick()

    expect(root.textContent).toContain('aiRuns.detail.conclusion.preflightFailure')
    expect(root.textContent).toContain('provider tool-call contract did not converge')
    expect(root.textContent).not.toContain('aiRuns.detail.conclusion.noFailureDetail')

    app.unmount()
  })

  it('shows a retryable inspector error instead of fake zero evidence', async () => {
    mocks.listArtifacts.mockRejectedValue(new Error('artifact network failed'))
    mocks.listEvidence.mockRejectedValue(new Error('evidence network failed'))

    const root = document.createElement('div')
    const app = createApp(AiRunDetail)
    for (const name of ['ElAlert', 'ElDrawer', 'ElIcon', 'ElTag', 'ElTooltip']) {
      app.component(name, Passthrough)
    }
    app.component('ElButton', ButtonStub)
    app.directive('loading', () => undefined)
    app.mount(root)
    await Promise.resolve()
    await Promise.resolve()
    await nextTick()

    expect(root.querySelector('.run-side-load-error')?.textContent)
      .toContain('aiRuns.detail.sidePanelLoadFailed')
    expect(root.textContent).not.toContain('aiRuns.detail.evidenceEmpty')
    expect(root.textContent).not.toContain('aiRuns.detail.artifactsEmpty')
    expect(root.querySelector('.page-actions')?.textContent).toContain('—')
    app.unmount()
  })
})
