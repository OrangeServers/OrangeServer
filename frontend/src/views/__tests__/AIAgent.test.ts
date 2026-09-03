import {
  createApp,
  defineComponent,
  h,
  inject,
  nextTick,
  provide,
  ref,
  type InjectionKey,
} from 'vue'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import AIAgent from '@/views/AIAgent.vue'

const mocks = vi.hoisted(() => ({
  aiJsonRequest: vi.fn(),
  postAiStream: vi.fn(),
  messageError: vi.fn(),
  messageSuccess: vi.fn(),
  push: vi.fn(),
}))

vi.mock('@/utils/aiStream', () => ({
  aiJsonRequest: mocks.aiJsonRequest,
  postAiStream: mocks.postAiStream,
}))
vi.mock('@/api/aiDiagnostics', () => ({
  cancelDiagnostic: vi.fn(),
  getDiagnosticEvidence: vi.fn(),
  getDiagnosticReport: vi.fn(),
  getDiagnosticRun: vi.fn(),
}))
vi.mock('@/components/OrangeMark.vue', () => ({
  default: { name: 'OrangeMark', render: () => null },
}))
vi.mock('@/components/ai/DiagnosticRunCard.vue', () => ({
  default: { name: 'DiagnosticRunCard', render: () => null },
}))
vi.mock('@/components/ai/AutonomyDraftCard.vue', () => ({
  default: { name: 'AutonomyDraftCard', render: () => null },
}))
vi.mock('@/store', () => ({
  store: { user: { role: 'admin', username: 'alice', alias: 'Alice', avatar: '' } },
}))
vi.mock('@/i18n', () => ({
  currentLocale: () => 'zh-CN',
  t: (key: string) => key,
}))
vi.mock('vue-router', () => ({
  useRouter: () => ({ push: mocks.push }),
}))
vi.mock('element-plus', () => ({
  ElMessage: { error: mocks.messageError, success: mocks.messageSuccess },
  ElMessageBox: { confirm: vi.fn().mockResolvedValue(undefined) },
}))

const dropdownCommand = Symbol('dropdown-command') as InjectionKey<(value: string) => void>

const Passthrough = defineComponent({
  setup(_props, { attrs, slots }) {
    return () => h('div', attrs, slots.default?.())
  },
})

const InputStub = defineComponent({
  inheritAttrs: false,
  props: { modelValue: { type: String, default: '' }, disabled: Boolean },
  emits: ['update:modelValue', 'keydown'],
  setup(props, { attrs, emit, expose }) {
    const element = ref<HTMLTextAreaElement>()
    expose({ focus: () => element.value?.focus() })
    return () => h('textarea', {
      ...attrs,
      ref: element,
      disabled: props.disabled,
      value: props.modelValue,
      onInput: (event: Event) => emit(
        'update:modelValue',
        (event.target as HTMLTextAreaElement).value,
      ),
      onKeydown: (event: KeyboardEvent) => emit('keydown', event),
    })
  },
})

const ButtonStub = defineComponent({
  inheritAttrs: false,
  props: { disabled: Boolean, loading: Boolean },
  emits: ['click'],
  setup(props, { attrs, emit, slots }) {
    return () => h('button', {
      ...attrs,
      disabled: props.disabled || props.loading,
      onClick: (event: MouseEvent) => emit('click', event),
    }, slots.default?.())
  },
})

const DropdownStub = defineComponent({
  emits: ['command'],
  setup(_props, { attrs, emit, slots }) {
    provide(dropdownCommand, value => emit('command', value))
    return () => h('div', attrs, [slots.default?.(), slots.dropdown?.()])
  },
})

const DropdownItemStub = defineComponent({
  inheritAttrs: false,
  props: { command: { type: String, required: true }, disabled: Boolean },
  setup(props, { attrs, slots }) {
    const command = inject(dropdownCommand)
    return () => h('button', {
      ...attrs,
      disabled: props.disabled,
      onClick: () => command?.(props.command),
    }, slots.default?.())
  },
})

const DialogStub = defineComponent({
  props: { modelValue: Boolean },
  setup(props, { attrs, slots }) {
    return () => props.modelValue
      ? h('div', attrs, [slots.default?.(), slots.footer?.()])
      : null
  },
})

const SelectStub = defineComponent({
  inheritAttrs: false,
  props: { modelValue: { type: String, default: '' }, disabled: Boolean },
  emits: ['update:modelValue', 'change'],
  setup(props, { attrs, emit, slots }) {
    return () => h('select', {
      ...attrs,
      disabled: props.disabled,
      value: props.modelValue,
      onChange: (event: Event) => {
        const value = (event.target as HTMLSelectElement).value
        emit('update:modelValue', value)
        emit('change', value)
      },
    }, slots.default?.())
  },
})

const OptionStub = defineComponent({
  props: {
    value: { type: String, required: true },
    label: { type: String, required: true },
    disabled: Boolean,
  },
  setup(props) {
    return () => h('option', {
      value: props.value,
      disabled: props.disabled,
    }, props.label)
  },
})

async function flush(): Promise<void> {
  await Promise.resolve()
  await Promise.resolve()
  await nextTick()
}

function mountAgent() {
  const root = document.createElement('div')
  document.body.append(root)
  const app = createApp(AIAgent)
  app.config.globalProperties.$t = (key: string) => key
  app.component('ElInput', InputStub)
  app.component('ElButton', ButtonStub)
  app.component('ElDropdown', DropdownStub)
  app.component('ElDropdownMenu', Passthrough)
  app.component('ElDropdownItem', DropdownItemStub)
  app.component('ElDialog', DialogStub)
  app.component('ElSelect', SelectStub)
  app.component('ElOption', OptionStub)
  for (const name of [
    'ElIcon', 'ElRadioGroup', 'ElRadioButton',
    'ElDrawer', 'ElAvatar', 'ElTag', 'ElTable', 'ElTableColumn', 'ElPagination',
  ]) app.component(name, Passthrough)
  app.mount(root)
  return { app, root }
}

function mockEmptyConversationList(): void {
  mocks.aiJsonRequest.mockImplementation((url: string, options?: { method?: string }) => {
    if (url === '/ai/providers') {
      return Promise.resolve({ providers: [{
        provider_code: 'deepseek', name: 'DeepSeek', model: 'deepseek-chat',
        available: true, enabled: true, api_key_configured: true,
      }] })
    }
    if (url === '/ai/conversations' && options?.method === 'POST') {
      return Promise.resolve({ conversation: {
        id: 'conversation-new', title: 'new', provider_code: 'deepseek',
      } })
    }
    if (url === '/ai/conversations') return Promise.resolve({ conversations: [] })
    return Promise.resolve({})
  })
}

describe('AIAgent composer', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    localStorage.clear()
    document.body.innerHTML = ''
    mocks.postAiStream.mockResolvedValue(undefined)
  })

  it('fills and focuses a real task prompt without sending it', async () => {
    mockEmptyConversationList()
    const { app, root } = mountAgent()
    await flush()

    root.querySelector<HTMLButtonElement>('.prompt-card')?.click()
    await flush()

    const input = root.querySelector<HTMLTextAreaElement>('[aria-label="ai.composer.inputAria"]')
    expect(input?.value).toBe('ai.prompts.inspectText')
    expect(document.activeElement).toBe(input)
    expect(mocks.postAiStream).not.toHaveBeenCalled()
    app.unmount()
  })

  it('shows independent compact model and context selectors', async () => {
    mocks.aiJsonRequest.mockImplementation((url: string) => {
      if (url === '/ai/providers') return Promise.resolve({ providers: [{
        provider_code: 'siliconflow', name: 'SiliconFlow',
        model: 'deepseek-ai/DeepSeek-V4-Flash', context_window_tokens: 262144,
        available: true, enabled: true, api_key_configured: true,
      }] })
      if (url === '/ai/conversations') return Promise.resolve({ conversations: [] })
      return Promise.resolve({})
    })
    const { app, root } = mountAgent()
    await flush()

    const model = root.querySelector<HTMLSelectElement>('.provider-select')
    const context = root.querySelector<HTMLSelectElement>('.context-mode-select')
    expect(model?.selectedOptions[0]?.textContent).toBe('DeepSeek V4 Flash')
    expect(context?.value).toBe('standard_256k')
    expect([...context!.options].map(option => option.textContent)).toEqual(['256K'])
    expect(context?.getAttribute('placement')).toBe('top-start')
    expect(root.querySelector('.context-mode-toggle')).toBeNull()
    app.unmount()
  })

  it('creates every new conversation with the safe ask profile', async () => {
    mockEmptyConversationList()
    const { app, root } = mountAgent()
    await flush()

    root.querySelector<HTMLButtonElement>('.prompt-card')?.click()
    await flush()
    root.querySelector<HTMLButtonElement>('.send-button')?.click()
    await flush()

    expect(mocks.aiJsonRequest).toHaveBeenCalledWith('/ai/conversations', expect.objectContaining({
      method: 'POST',
      body: expect.objectContaining({ autonomy_mode: 'ask', autonomy_profile: undefined }),
    }))
    app.unmount()
  })

  it('saves a profile switch and rolls the visible choice back on failure', async () => {
    let failAuto = false
    mocks.aiJsonRequest.mockImplementation((url: string, options?: { method?: string }) => {
      if (url === '/ai/providers') return Promise.resolve({ providers: [{
        provider_code: 'deepseek', model: 'deepseek-chat', available: true,
        enabled: true, api_key_configured: true,
      }] })
      if (url === '/ai/conversations') return Promise.resolve({ conversations: [{
        id: 'conversation-1', title: 'existing', provider_code: 'deepseek',
        autonomy_mode: 'ask', autonomy_profile: null,
      }] })
      if (url === '/ai/conversations/conversation-1' && !options?.method) {
        return Promise.resolve({ conversation: {
          id: 'conversation-1', title: 'existing', provider_code: 'deepseek',
          autonomy_mode: 'ask', autonomy_profile: null,
        } })
      }
      if (url === '/ai/conversations/conversation-1' && options?.method === 'PATCH') {
        if (failAuto) return Promise.reject(new Error('save failed'))
        return Promise.resolve({ conversation: {
          id: 'conversation-1', autonomy_mode: 'ai_review', autonomy_profile: null,
        } })
      }
      return Promise.resolve({})
    })
    const { app, root } = mountAgent()
    await flush()

    root.querySelector<HTMLButtonElement>('[data-autonomy-mode="ai_review"]')?.click()
    await flush()
    expect(root.querySelector('.autonomy-mode-trigger')?.textContent)
      .toContain('aiRuns.mode.ai_review')

    failAuto = true
    root.querySelector<HTMLButtonElement>('[data-autonomy-mode="auto"]')?.click()
    await flush()
    expect(root.querySelector('.autonomy-mode-trigger')?.textContent)
      .toContain('aiRuns.mode.ai_review')
    expect(mocks.messageError).toHaveBeenCalledWith('save failed')
    app.unmount()
  })

  it('blocks sending until a profile switch is persisted', async () => {
    let finishPatch: ((value: unknown) => void) | undefined
    const pendingPatch = new Promise(resolve => { finishPatch = resolve })
    mocks.aiJsonRequest.mockImplementation((url: string, options?: { method?: string }) => {
      if (url === '/ai/providers') return Promise.resolve({ providers: [{
        provider_code: 'deepseek', model: 'deepseek-chat', available: true,
        enabled: true, api_key_configured: true,
      }] })
      if (url === '/ai/conversations') return Promise.resolve({ conversations: [{
        id: 'conversation-1', title: 'existing', provider_code: 'deepseek',
        autonomy_mode: 'ask', autonomy_profile: null,
      }] })
      if (url === '/ai/conversations/conversation-1' && !options?.method) {
        return Promise.resolve({ conversation: {
          id: 'conversation-1', title: 'existing', provider_code: 'deepseek',
          autonomy_mode: 'ask', autonomy_profile: null,
        } })
      }
      if (url === '/ai/conversations/conversation-1' && options?.method === 'PATCH') {
        return pendingPatch
      }
      return Promise.resolve({})
    })
    const { app, root } = mountAgent()
    await flush()

    root.querySelector<HTMLButtonElement>('.prompt-card')?.click()
    root.querySelector<HTMLButtonElement>('[data-autonomy-mode="ai_review"]')?.click()
    await nextTick()
    const send = root.querySelector<HTMLButtonElement>('.send-button')
    expect(send?.disabled).toBe(true)
    send?.click()
    expect(mocks.postAiStream).not.toHaveBeenCalled()

    finishPatch?.({ conversation: {
      id: 'conversation-1', autonomy_mode: 'ai_review', autonomy_profile: null,
    } })
    await flush()
    expect(send?.disabled).toBe(false)
    app.unmount()
  })

  it('requires and persists at least one custom action category', async () => {
    mockEmptyConversationList()
    const { app, root } = mountAgent()
    await flush()

    root.querySelector<HTMLButtonElement>('[data-autonomy-mode="custom"]')?.click()
    await nextTick()
    const confirm = root.querySelector<HTMLButtonElement>('.custom-profile-confirm')
    expect(confirm?.disabled).toBe(true)

    root.querySelector<HTMLInputElement>('[data-action-category="systemd"]')?.click()
    await nextTick()
    expect(confirm?.disabled).toBe(false)
    confirm?.click()
    await nextTick()

    root.querySelector<HTMLButtonElement>('.prompt-card')?.click()
    await nextTick()
    root.querySelector<HTMLButtonElement>('.send-button')?.click()
    await flush()
    expect(mocks.aiJsonRequest).toHaveBeenCalledWith('/ai/conversations', expect.objectContaining({
      body: expect.objectContaining({
        autonomy_mode: 'custom',
        autonomy_profile: { action_categories: ['systemd'] },
      }),
    }))
    app.unmount()
  })

  it('restores a custom profile from conversation history', async () => {
    mocks.aiJsonRequest.mockImplementation((url: string) => {
      if (url === '/ai/providers') return Promise.resolve({ providers: [{
        provider_code: 'deepseek', model: 'deepseek-chat', available: true,
        enabled: true, api_key_configured: true,
      }] })
      if (url === '/ai/conversations') return Promise.resolve({ conversations: [{
        id: 'conversation-custom', title: 'custom', provider_code: 'deepseek',
      }] })
      if (url === '/ai/conversations/conversation-custom') {
        return Promise.resolve({ conversation: {
          id: 'conversation-custom', title: 'custom', provider_code: 'deepseek',
          autonomy_mode: 'custom',
          autonomy_profile: { action_categories: ['file_read', 'systemd'] },
        } })
      }
      return Promise.resolve({})
    })
    const { app, root } = mountAgent()
    await flush()

    expect(root.querySelector('.autonomy-mode-trigger')?.textContent)
      .toContain('aiRuns.mode.custom')
    app.unmount()
  })
})
