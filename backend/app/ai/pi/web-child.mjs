import { createInterface } from 'node:readline';
import { createHash } from 'node:crypto';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Type } from 'typebox';
import { InMemoryCredentialStore, InMemoryModelsStore } from '@earendil-works/pi-ai';
import { getModel, getModels, getProviders } from '@earendil-works/pi-ai/compat';
import { createAgentSession, createExtensionRuntime, CURRENT_SESSION_VERSION, ModelRuntime,
  SessionManager, SettingsManager } from '@earendil-works/pi-coding-agent';
import { installAuthorizationGuard } from './authorization-guard.mjs';
import { installLengthRecovery } from './length-recovery.mjs';
import { appendLegacyHistory } from './legacy-history.mjs';

const writeProtocol = process.stdout.write.bind(process.stdout);
process.stdout.write = () => true;
for (const method of ['debug', 'info', 'log', 'warn', 'error']) console[method] = () => {};

let apiKey = '';
let parentClosed = false;
let inputFailure = false;
let initialized = false;
let requestCounter = 0;
const pending = new Map();
let resolveInit;
let rejectInit;
const initFrame = new Promise((resolve, reject) => {
  resolveInit = resolve;
  rejectInit = reject;
});
const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
const idKey = id => `${typeof id}:${String(id)}`;

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === 'object') return Object.fromEntries(
    Object.keys(value).sort().map(key => [key, canonical(value[key])]));
  return value;
}

function checkpointHash(payload) {
  return createHash('sha256').update(JSON.stringify(canonical(payload))).digest('hex');
}

function validateCheckpoint(checkpoint) {
  const fail = () => { throw new Error('invalid checkpoint'); };
  if (!checkpoint || typeof checkpoint !== 'object' || Array.isArray(checkpoint) ||
      Object.keys(checkpoint).sort().join(',') !== 'entries,leafId,sha256,version' ||
      checkpoint.version !== 1 || !Array.isArray(checkpoint.entries) ||
      !/^[0-9a-f]{64}$/.test(checkpoint.sha256 || '')) fail();
  const { version, entries, leafId, sha256 } = checkpoint;
  if (checkpointHash({ version, entries, leafId }) !== sha256) fail();
  const header = entries[0];
  if (!header || header.type !== 'session' || header.version !== CURRENT_SESSION_VERSION ||
      typeof header.id !== 'string' || !header.id || typeof header.timestamp !== 'string' ||
      typeof header.cwd !== 'string' || header.cwd.length > 4096) fail();
  const seen = new Map();
  let roots = 0;
  const isAncestor = (parent, target) => {
    while (parent !== null) {
      if (parent === target) return true;
      parent = seen.get(parent)?.parentId ?? null;
    }
    return false;
  };
  for (const entry of entries.slice(1)) {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry) ||
        typeof entry.id !== 'string' || !entry.id || seen.has(entry.id) ||
        typeof entry.timestamp !== 'string' ||
        !(entry.parentId === null || (typeof entry.parentId === 'string' && seen.has(entry.parentId)))) fail();
    if (entry.parentId === null) roots++;
    if (entry.type === 'message') {
      const message = entry.message;
      if (!message || typeof message !== 'object' || Array.isArray(message) ||
          !['system', 'user', 'assistant', 'toolResult'].includes(message.role) ||
          !Number.isFinite(message.timestamp) ||
          !(typeof message.content === 'string' || Array.isArray(message.content)) ||
          (message.role === 'system' && typeof message.content !== 'string') ||
          (['assistant', 'toolResult'].includes(message.role) && !Array.isArray(message.content))) fail();
      if (Array.isArray(message.content) && message.content.some(block =>
        !block || typeof block !== 'object' || !['text', 'thinking', 'toolCall'].includes(block.type) ||
        (block.type === 'text' && typeof block.text !== 'string') ||
        (block.type === 'thinking' && typeof block.thinking !== 'string') ||
        (block.type === 'toolCall' && (message.role !== 'assistant' || typeof block.id !== 'string' ||
          !block.id || typeof block.name !== 'string' || !block.name ||
          !block.arguments || typeof block.arguments !== 'object' || Array.isArray(block.arguments))))) fail();
    } else if (entry.type === 'compaction') {
      if (typeof entry.summary !== 'string' || !Number.isFinite(entry.tokensBefore) || entry.tokensBefore < 0 ||
          typeof entry.firstKeptEntryId !== 'string' ||
          (entry.firstKeptEntryId !== entry.id && !isAncestor(entry.parentId, entry.firstKeptEntryId))) fail();
      if (entry.systemMessage && (entry.systemMessage.role !== 'system' ||
          typeof entry.systemMessage.content !== 'string')) fail();
    } else if (entry.type === 'context_edit') {
      if (typeof entry.targetId !== 'string' || !isAncestor(entry.parentId, entry.targetId) ||
          !(entry.replacement === null || (entry.replacement && typeof entry.replacement === 'object' &&
            (typeof entry.replacement.content === 'string' || Array.isArray(entry.replacement.content))))) fail();
    } else if (entry.type === 'model_change') {
      if (typeof entry.provider !== 'string' || !entry.provider || typeof entry.modelId !== 'string' || !entry.modelId) fail();
    } else if (entry.type === 'thinking_level_change') {
      if (typeof entry.thinkingLevel !== 'string') fail();
    } else if (entry.type === 'usage') {
      if (!entry.usage || typeof entry.usage !== 'object') fail();
    } else fail();
    seen.set(entry.id, entry);
  }
  if ((seen.size && (roots < 1 || typeof leafId !== 'string' || !seen.has(leafId))) ||
      (!seen.size && leafId !== null)) fail();
  return checkpoint;
}

function makeCheckpoint(manager) {
  const payload = JSON.parse(JSON.stringify(redact({ version: 1,
    entries: [manager.getHeader(), ...manager.getEntries()], leafId: manager.getLeafId() })));
  const checkpoint = { ...payload, sha256: checkpointHash(payload) };
  return validateCheckpoint(checkpoint);
}

function redact(value) {
  return typeof value === 'string' && apiKey
    ? value.split(apiKey).join('[redacted]').split(JSON.stringify(apiKey).slice(1, -1)).join('[redacted]')
    : Array.isArray(value) ? value.map(redact)
      : value && typeof value === 'object' ? Object.fromEntries(Object.entries(value).map(([k, v]) => [redact(k), redact(v)]))
        : value;
}

function emit(event) {
  writeProtocol(`${JSON.stringify(redact(event))}\n`);
}

function rejectPending(error) {
  for (const waiter of pending.values()) waiter.reject(error);
  pending.clear();
}

function failInput() {
  inputFailure = true;
  rejectPending(new Error('invalid parent response'));
}

input.on('line', line => {
  if (!initialized) {
    initialized = true;
    try {
      resolveInit(JSON.parse(line));
    } catch { rejectInit(new Error('invalid init')); }
    return;
  }
  let response;
  try { response = JSON.parse(line); } catch { failInput(); return; }
  if (!response || typeof response !== 'object' || Array.isArray(response) || response.id === undefined) {
    failInput();
    return;
  }
  const key = idKey(response.id);
  const waiter = pending.get(key);
  if (!waiter) { failInput(); return; }
  pending.delete(key);
  waiter.resolve(response);
});

input.on('close', () => {
  parentClosed = true;
  if (!initialized) rejectInit(new Error('parent closed'));
  rejectPending(new Error('parent closed'));
});

function requestParent(type, fields = {}, id = `authorize-${++requestCounter}`) {
  if (parentClosed || inputFailure) return Promise.reject(new Error('parent unavailable'));
  const key = idKey(id);
  if (pending.has(key)) return Promise.reject(new Error('duplicate request id'));
  return new Promise((resolve, reject) => {
    pending.set(key, { resolve, reject });
    emit({ ...fields, type, id });
  });
}

function validateInit(init) {
  const provider = init?.provider;
  if (!provider || typeof provider.code !== 'string' || !provider.code ||
      typeof provider.model !== 'string' || !provider.model ||
      typeof provider.base_url !== 'string' || !provider.base_url ||
      typeof provider.api_key !== 'string' || !provider.api_key ||
      !Number.isSafeInteger(provider.context_window) || provider.context_window < 1 ||
      (provider.extra_body !== undefined && (!provider.extra_body || typeof provider.extra_body !== 'object' || Array.isArray(provider.extra_body))) ||
      typeof init.system_prompt !== 'string' || !init.system_prompt.trim() ||
      typeof init.message !== 'string' || !Array.isArray(init.tools) ||
      (init.reset_history !== undefined && typeof init.reset_history !== 'boolean') ||
      (init.history !== undefined && !Array.isArray(init.history))) throw new Error('invalid init');
  const names = new Set();
  for (const item of init.tools) {
    const fn = item?.function;
    if (item?.type !== 'function' || typeof fn?.name !== 'string' || !/^[A-Za-z0-9_-]{1,64}$/.test(fn.name) ||
        !fn.parameters || typeof fn.parameters !== 'object' || Array.isArray(fn.parameters) ||
        names.has(fn.name)) throw new Error('invalid init');
    names.add(fn.name);
  }
  const baseUrl = new URL(provider.base_url);
  if (!['http:', 'https:'].includes(baseUrl.protocol)) throw new Error('invalid init');
  return { provider, schemas: init.tools, systemPrompt: init.system_prompt, message: init.message,
    checkpoint: init.checkpoint ?? null, history: init.history ?? [], resetHistory: init.reset_history === true };
}

let session;
let authorizationDenied = false;
let failureCode = 'invalid_init';

try {
  const { provider, schemas, systemPrompt, message, checkpoint, history, resetHistory } = validateInit(await initFrame);
  apiKey = provider.api_key;
  failureCode = 'invalid_checkpoint';
  if (checkpoint !== null) validateCheckpoint(checkpoint);
  failureCode = 'model_not_in_catalog';
  const directoryModel = getModel(provider.code, provider.model) || getProviders()
    .flatMap(name => getModels(name)).find(item => item.id === provider.model);
  const maxTokens = Number.isSafeInteger(directoryModel?.maxTokens) && directoryModel.maxTokens > 0
    ? directoryModel.maxTokens : provider.context_window;

  failureCode = 'session_failed';
  const runtime = await ModelRuntime.create({
    credentials: new InMemoryCredentialStore(),
    modelsStore: new InMemoryModelsStore(),
    modelsPath: null,
    allowModelNetwork: false,
    refreshOnCreate: false,
  });
  const runtimeProvider = 'orangeserver';
  runtime.registerProvider(runtimeProvider, {
    baseUrl: provider.base_url,
    api: 'openai-completions',
    models: [{
      id: provider.model,
      name: directoryModel?.name || provider.model,
      reasoning: directoryModel?.reasoning ?? false,
      input: ['text'],
      contextWindow: provider.context_window,
      maxTokens,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    }],
  });
  await runtime.setRuntimeApiKey(runtimeProvider, apiKey);
  delete provider.api_key;
  const model = runtime.getModel(runtimeProvider, provider.model);
  if (!model) throw new Error('Pi runtime model unavailable');

  const dir = path.dirname(fileURLToPath(import.meta.url));
  const manager = SessionManager.inMemory(dir, undefined, checkpoint?.entries);
  if (checkpoint) {
    if (checkpoint.leafId === null) manager.resetLeaf();
    else if (manager.getLeafId() !== checkpoint.leafId) manager.branch(checkpoint.leafId);
  }
  if (resetHistory) manager.resetLeaf();
  if (history.length) appendLegacyHistory(manager, history, model);
  const tools = schemas.map(({ function: fn }) => ({
    name: fn.name,
    label: fn.name,
    description: fn.description || fn.name,
    parameters: Type.Unsafe(fn.parameters),
    execute: async (toolCallId, args) => {
      const id = `tool-${toolCallId}`;
      const assistant = session.messages.filter(item => item.role === 'assistant').at(-1);
      const reasoning = assistant?.content?.filter(item => item.type === 'thinking')
        .map(item => item.thinking).join('') || '';
      const response = await requestParent('tool.request', {
        name: fn.name,
        arguments: args,
        ...(reasoning ? { reasoning_content: reasoning } : {}),
      }, id);
      const result = response.ok === true ? response.result ?? null
        : { error: typeof response.error === 'string' ? response.error.slice(0, 200) : 'tool unavailable' };
      const text = JSON.stringify(redact(result));
      return { content: [{ type: 'text', text }], details: {} };
    },
  }));
  const resourceLoader = {
    getExtensions: () => ({ extensions: [], errors: [], runtime: createExtensionRuntime() }),
    getSkills: () => ({ skills: [], diagnostics: [] }),
    getPrompts: () => ({ prompts: [], diagnostics: [] }),
    getThemes: () => ({ themes: [], diagnostics: [] }),
    getAgentsFiles: () => ({ agentsFiles: [] }),
    getSystemPrompt: () => systemPrompt,
    getSystemPromptSource: () => undefined,
    getAppendSystemPrompt: () => [],
    getAppendSystemPromptSources: () => [],
    extendResources: () => {},
    reload: async () => {},
  };

  ({ session } = await createAgentSession({
    cwd: dir,
    agentDir: dir,
    model,
    modelRuntime: runtime,
    resourceLoader,
    noTools: 'all',
    tools: tools.map(tool => tool.name),
    customTools: tools,
    sessionManager: manager,
    settingsManager: SettingsManager.inMemory({ cacheWarming: 'off', retry: { enabled: false, provider: { maxRetries: 0 } },
      compaction: { enabled: true } }),
  }));
  session.setActiveToolsByName(tools.map(tool => tool.name));
  if (!directoryModel || (provider.extra_body && Object.keys(provider.extra_body).length)) {
    const previous = session.agent.onPayload;
    session.agent.onPayload = async (payload, currentModel) => {
      const body = { ...(await previous?.(payload, currentModel) ?? payload) };
      if (!directoryModel) {
        delete body.max_tokens;
        delete body.max_completion_tokens;
      }
      return { ...body, ...provider.extra_body };
    };
  }
  const activeTools = session.getActiveToolNames();
  if (activeTools.length !== tools.length || activeTools.some(name => !tools.some(tool => tool.name === name))) {
    throw new Error('unexpected active tool');
  }

  const truncatedParts = [];
  session.subscribe(event => {
    if (event.type === 'message_update' && event.assistantMessageEvent.type === 'text_delta') {
      writeProtocol(`${JSON.stringify({ type: 'text.delta', text: event.assistantMessageEvent.delta })}\n`);
    } else if (event.type === 'message_end' && event.message?.role === 'assistant' && event.message.usage) {
      const usage = event.message.usage;
      if (event.message.stopReason === 'length' && !event.message.content?.some(item => item.type === 'toolCall')) {
        truncatedParts.push(event.message.content?.filter(item => item.type === 'text').map(item => item.text).join('') || '');
      }
      emit({ type: 'usage', input: usage.input, output: usage.output,
        cacheRead: usage.cacheRead, cacheWrite: usage.cacheWrite, stopReason: event.message.stopReason });
    } else if (event.type.startsWith('tool_execution_')) {
      emit({ type: event.type, id: event.toolCallId, name: event.toolName,
        ...(event.type === 'tool_execution_end' ? { ok: !event.isError } : {}) });
    } else if (event.type === 'compaction_start') {
      emit({ type: event.type, reason: event.reason });
    } else if (event.type === 'compaction_end') {
      emit({ type: event.type, reason: event.reason,
        ok: !!event.result && !event.errorMessage && !event.aborted, aborted: event.aborted });
    } else if (event.type === 'turn_end' && event.message?.content?.some(block => block.type === 'toolCall')) {
      emit({ type: 'checkpoint', checkpoint: makeCheckpoint(manager), contextUsage: session.getContextUsage() });
    }
  });

  installLengthRecovery(session.agent);
  installAuthorizationGuard(session.agent, async () => {
    const response = await requestParent('authorize.request');
    authorizationDenied = response.ok !== true;
    return !authorizationDenied;
  });
  await session.prompt(message);
  await session.waitForIdle();
  if (authorizationDenied) failureCode = 'authorization_denied';
  const last = session.messages.filter(item => item.role === 'assistant').at(-1);
  if (authorizationDenied || last?.stopReason !== 'stop') {
    if (!authorizationDenied) failureCode = 'incomplete_response';
    throw new Error('model request did not complete');
  }
  failureCode = 'invalid_checkpoint';
  const nextCheckpoint = makeCheckpoint(manager);
  const lastText = last.content?.filter(item => item.type === 'text').map(item => item.text).join('') || '';
  const final = { type: 'final', text: truncatedParts.join('') + lastText, checkpoint: nextCheckpoint,
    contextUsage: session.getContextUsage() };
  writeProtocol(`${JSON.stringify(final)}\n`);
} catch {
  emit({ type: 'error', code: authorizationDenied ? 'authorization_denied'
    : inputFailure ? 'protocol_error' : parentClosed ? 'parent_closed' : failureCode });
  process.exitCode = 1;
} finally {
  try { session?.dispose(); } catch {}
  input.close();
  process.stdin.pause();
}
