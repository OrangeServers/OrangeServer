import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { createServer } from 'node:http';
import { createInterface } from 'node:readline';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { SessionManager } from '@earendil-works/pi-coding-agent';

const dir = path.dirname(fileURLToPath(import.meta.url));
const childFile = path.resolve(dir, '../../app/ai/pi/web-child.mjs');
const fakeKey = 'fake-"quoted\\key-selftest';
const systemPrompt = 'SELFTEST trusted parent policy: preserve authorization and evidence.';
const toolSchema = [{ type: 'function', function: {
  name: 'lookup', description: 'Return one fake value',
  parameters: { type: 'object', properties: { x: { type: 'integer' } }, required: ['x'] },
} }];

function chunk(delta, finishReason = null, extra = {}) {
  return { id: 'chatcmpl-selftest', object: 'chat.completion.chunk', created: 1,
    model: 'gpt-4o', choices: [{ index: 0, delta, finish_reason: finishReason }], ...extra };
}

function textContent(content) {
  if (typeof content === 'string') return content;
  return Array.isArray(content) ? content.map(part => part?.text || '').join('') : '';
}

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value === 'object') return Object.fromEntries(
    Object.keys(value).sort().map(key => [key, canonical(value[key])]));
  return value;
}

function signCheckpoint(checkpoint) {
  const payload = { version: checkpoint.version, entries: checkpoint.entries, leafId: checkpoint.leafId };
  return { ...payload, sha256: createHash('sha256').update(JSON.stringify(canonical(payload))).digest('hex') };
}

async function fakeProvider(mode, authCount) {
  const requests = [];
  let finished = false;
  const server = createServer(async (req, res) => {
    let raw = '';
    for await (const part of req) raw += part;
    const body = JSON.parse(raw);
    requests.push({ body, auth: req.headers.authorization,
      authCountAtCall: authCount.count });
    if (mode === 'rate') {
      res.writeHead(429, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: { message: 'rate limited' } }));
      return;
    }
    let chunks;
    if (((mode === 'tool' || mode === 'multiturn' || mode === 'reasoning') && requests.length === 1)
        || (mode === 'twenty-turns' && requests.length <= 20)) {
      chunks = [
        ...(mode === 'reasoning' ? [chunk({ reasoning_content: 'fake-provider-reasoning' })] : []),
        chunk({ tool_calls: [{ index: 0, id: mode === 'twenty-turns' ? `call-${requests.length}` : 'call-1', type: 'function',
          function: { name: 'lookup', arguments: '{"x":1}' } }] }),
        chunk({}, 'tool_calls'),
      ];
    } else if (mode === 'length' || mode === 'repeated-length') {
      chunks = [
        chunk({ content: mode === 'length' && requests.length > 1 ? ' continued.' : 'Truncated answer' }),
        chunk({}, mode === 'length' && requests.length > 1 ? 'stop' : 'length',
          { usage: { prompt_tokens: 10, completion_tokens: 1, total_tokens: 11 } }),
      ];
    } else if (mode === 'compaction' && requests.length === 1) {
      chunks = [chunk({ content: 'Earlier monitoring evidence: load1 is 1.25 at timestamp 1700000000.' }), chunk({}, 'stop')];
    } else if (mode === 'multiturn') {
      chunks = [chunk({ content: requests.length === 2 ? 'First answer.' : 'Second answer.' }), chunk({}, 'stop')];
    } else {
      chunks = [chunk({ content: 'Do' }), chunk({ content: 'ne.' }), chunk({}, 'stop')];
    }
    res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache' });
    for (const item of chunks) res.write(`data: ${JSON.stringify(item)}\n\n`);
    if (mode === 'stream') await new Promise(resolve => setTimeout(resolve, 200));
    finished = true;
    res.end('data: [DONE]\n\n');
  });
  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(0, '127.0.0.1', resolve);
  });
  return {
    requests,
    get finished() { return finished; },
    url: `http://127.0.0.1:${server.address().port}/v1`,
    async close() {
      const closed = new Promise(resolve => server.close(resolve));
      server.closeAllConnections();
      await closed;
    },
  };
}

async function runChild(mode, provider, authCount, options = {}) {
  const child = spawn(process.execPath, [childFile], {
    cwd: dir,
    stdio: ['pipe', 'pipe', 'pipe'],
    env: {
      PATH: process.env.PATH,
      SystemRoot: process.env.SystemRoot,
      WINDIR: process.env.WINDIR,
      TEMP: process.env.TEMP,
      TMP: process.env.TMP,
      PI_OFFLINE: '1',
    },
  });
  const events = [];
  let stdout = '';
  let stderr = '';
  let protocolError;
  let streamedBeforeProviderFinished = false;
  const lines = createInterface({ input: child.stdout, crlfDelay: Infinity });
  lines.on('line', line => {
    stdout += `${line}\n`;
    try {
      const event = JSON.parse(line);
      assert.ok(event && typeof event === 'object' && !Array.isArray(event));
      events.push(event);
      if (event.type === 'text.delta' && !provider.finished) streamedBeforeProviderFinished = true;
      if (event.type === 'authorize.request') {
        authCount.count += 1;
        child.stdin.write(JSON.stringify({ id: event.id, ok: mode !== 'deny' }) + '\n');
      } else if (event.type === 'tool.request') {
        child.stdin.write(JSON.stringify({ id: event.id, ok: true, result: { value: 42, secret: fakeKey } }) + '\n');
      }
    } catch (error) {
      protocolError = error;
      child.kill();
    }
  });
  child.stderr.setEncoding('utf8').on('data', chunk => { stderr += chunk; });
  child.stdin.on('error', () => {});
  child.stdin.write(JSON.stringify({
    provider: { code: 'openai', model: 'gpt-4o', base_url: provider.url,
      api_key: fakeKey, context_window: 131072, ...options.provider },
    system_prompt: options.systemPrompt || systemPrompt,
    tools: options.tools ?? (mode === 'tool' ? toolSchema : []),
    message: options.message || (mode === 'tool' ? 'Use lookup with x=1, then answer.' : 'Answer briefly.'),
    ...(options.checkpoint !== undefined ? { checkpoint: options.checkpoint } : {}),
    ...(options.history !== undefined ? { history: options.history } : {}),
    ...(options.resetHistory !== undefined ? { reset_history: options.resetHistory } : {}),
  }) + '\n');

  const exited = new Promise(resolve => child.once('close', (code, signal) => resolve({ code, signal })));
  let timer;
  try {
    const timeout = new Promise((_, reject) => {
      timer = setTimeout(() => { child.kill(); reject(new Error('worker timed out')); }, 20000);
    });
    const exit = await Promise.race([exited, timeout]);
    if (protocolError) throw protocolError;
    assert.equal(stdout.includes(fakeKey), false, 'provider key must not appear on stdout');
    assert.equal(stdout.includes(JSON.stringify(fakeKey).slice(1, -1)), false, 'escaped key must not appear on stdout');
    assert.equal(stderr.includes(fakeKey), false, 'provider key must not appear in logs');
    return { ...exit, events, stdout, stderr, streamedBeforeProviderFinished };
  } finally {
    clearTimeout(timer);
    lines.close();
    if (!child.killed && child.exitCode === null) child.kill();
  }
}

async function runCase(mode) {
  const authCount = { count: 0 };
  const provider = await fakeProvider(mode, authCount);
  try {
    const child = await runChild(mode, provider, authCount);
    return { ...child, authCount: authCount.count, requests: provider.requests };
  } finally {
    await provider.close();
  }
}

async function main() {
  const longAuth = { count: 0 };
  const longProvider = await fakeProvider('twenty-turns', longAuth);
  try {
    const longRun = await runChild('tool', longProvider, longAuth);
    assert.equal(longRun.code, 0);
    assert.equal(longProvider.requests.length, 21);
    assert.equal(longRun.events.filter(event => event.type === 'tool.request').length, 20);
    assert.equal(longAuth.count, 21);
  } finally {
    await longProvider.close();
  }
  const legacyAuth = { count: 0 };
  const legacyProvider = await fakeProvider('plain', legacyAuth);
  try {
    const legacy = await runChild('plain', legacyProvider, legacyAuth, { history: [
      { role: 'user', content: 'Earlier synthetic question' },
      { role: 'assistant', content: 'Earlier synthetic answer' },
    ] });
    assert.equal(legacy.code, 0);
    const sent = legacyProvider.requests[0].body.messages;
    assert.ok(sent.some(item => item.content === 'Earlier synthetic question'));
    assert.ok(sent.some(item => item.content === 'Earlier synthetic answer'));
  } finally {
    await legacyProvider.close();
  }
  const unknownAuth = { count: 0 };
  const unknownProvider = await fakeProvider('plain', unknownAuth);
  try {
    const unknown = await runChild('plain', unknownProvider, unknownAuth,
      { provider: { code: 'deepseek', model: 'deepseek-reasoner', extra_body: { temperature: 0.2 } } });
    assert.equal(unknown.code, 0, 'configured OpenAI-compatible models must not require a Pi catalog entry');
    assert.equal(unknownProvider.requests[0].body.model, 'deepseek-reasoner');
    assert.equal(unknownProvider.requests[0].body.temperature, 0.2);
    assert.equal('max_tokens' in unknownProvider.requests[0].body, false,
      `unexpected SDK max_tokens=${unknownProvider.requests[0].body.max_tokens}`);
    assert.equal('max_completion_tokens' in unknownProvider.requests[0].body, false,
      `unexpected SDK max_completion_tokens=${unknownProvider.requests[0].body.max_completion_tokens}`);
  } finally {
    await unknownProvider.close();
  }
  const reasoningAuth = { count: 0 };
  const reasoningProvider = await fakeProvider('reasoning', reasoningAuth);
  try {
    const reasoning = await runChild('tool', reasoningProvider, reasoningAuth,
      { provider: { code: 'deepseek', model: 'deepseek-reasoner' } });
    assert.equal(reasoning.code, 0);
    const assistant = reasoningProvider.requests[1].body.messages.find(message => message.role === 'assistant' && message.tool_calls);
    assert.equal(assistant.reasoning_content, 'fake-provider-reasoning');
    assert.equal(reasoning.events.find(event => event.type === 'tool.request')?.reasoning_content,
      'fake-provider-reasoning');
    assert.equal(reasoning.events.filter(event => !['final', 'checkpoint', 'tool.request'].includes(event.type))
      .some(event => JSON.stringify(event).includes('fake-provider-reasoning')), false);
    assert.equal(reasoning.events.find(event => event.type === 'final')?.text.includes('fake-provider-reasoning'), false);
  } finally {
    await reasoningProvider.close();
  }
  const success = await runCase('tool');
  assert.equal(success.code, 0);
  assert.equal(success.authCount, 2);
  assert.equal(success.requests.length, 2);
  assert.ok(success.requests.every((request, index) =>
    request.auth === `Bearer ${fakeKey}` && request.authCountAtCall === index + 1));
  assert.ok(success.requests[0].body.messages.some(message =>
    message.role === 'system' && textContent(message.content).includes(systemPrompt)));
  assert.ok(success.requests[1].body.messages.some(message =>
    message.role === 'tool' && message.content.includes('42')));
  assert.deepEqual(success.events.filter(event => event.type === 'tool.request').map(event =>
    ({ id: event.id, name: event.name, arguments: event.arguments })), [
      { id: 'tool-call-1', name: 'lookup', arguments: { x: 1 } },
    ]);
  assert.ok(success.events.some(event => event.type === 'tool_execution_start' && event.id === 'call-1'));
  assert.ok(success.events.some(event => event.type === 'tool_execution_end' && event.id === 'call-1' && event.ok));
  const progress = success.events.find(event => event.type === 'checkpoint');
  assert.ok(progress?.checkpoint?.entries?.some(entry => entry.type === 'message'
    && entry.message.role === 'toolResult' && entry.message.toolCallId === 'call-1'),
  'a completed tool turn must be recoverable before the final answer');
  const resumedAuth = { count: 0 };
  const resumedProvider = await fakeProvider('plain', resumedAuth);
  try {
    const resumed = await runChild('resume', resumedProvider, resumedAuth,
      { checkpoint: progress.checkpoint, tools: [], message: 'Continue from the saved result.' });
    assert.equal(resumed.code, 0);
    assert.equal(resumed.events.some(event => event.type === 'tool.request'), false,
      'resuming a completed tool turn must not automatically execute the tool again');
    assert.ok(resumedProvider.requests[0].body.messages.some(item => item.role === 'tool'
      && textContent(item.content).includes('42')));
  } finally {
    await resumedProvider.close();
  }
  assert.equal(success.events.filter(event => event.type === 'text.delta').map(event => event.text).join(''), 'Done.');
  assert.ok(success.events.findIndex(event => event.type === 'text.delta') < success.events.findIndex(event => event.type === 'final'));
  assert.deepEqual(success.events.filter(event => event.type === 'final').map(event => event.text), ['Done.']);
  assert.ok(success.events.find(event => event.type === 'final')?.checkpoint?.entries);
  assert.equal(JSON.stringify(success.requests[1].body).includes(fakeKey), false);

  const stream = await runCase('stream');
  assert.equal(stream.code, 0);
  assert.equal(stream.streamedBeforeProviderFinished, true, 'text must reach the parent before the provider finishes');

  const compactAuth = { count: 0 };
  const compactProvider = await fakeProvider('compaction', compactAuth);
  try {
    const history = Array.from({ length: 30 }, (_, i) => [
      { role: 'user', content: `Monitoring evidence ${i}: ` + 'load1=1.25 timestamp=1700000000; '.repeat(1400) },
      { role: 'assistant', content: 'Monitoring observation recorded.' },
    ]).flat();
    const compactRun = await runChild('compaction', compactProvider, compactAuth,
      { history, provider: { context_window: 262144 }, message: 'Summarize the recorded monitoring evidence.' });
    assert.equal(compactRun.code, 0, JSON.stringify(compactRun.events.filter(event => event.type === 'error')));
    assert.ok(compactRun.events.some(event => event.type === 'compaction_end' && event.ok));
    assert.ok(compactRun.events.find(event => event.type === 'final')?.checkpoint?.entries
      .some(entry => entry.type === 'compaction'));
    assert.ok(compactProvider.requests.at(-1).body.messages.some(message =>
      textContent(message.content).includes('Earlier monitoring evidence: load1 is 1.25')));
  } finally {
    await compactProvider.close();
  }

  const denied = await runCase('deny');
  assert.notEqual(denied.code, 0);
  assert.equal(denied.authCount, 1);
  assert.equal(denied.requests.length, 0, 'denied authorization must prevent the model request');
  assert.ok(denied.events.some(event => event.type === 'error' && event.code === 'authorization_denied'));
  assert.equal(denied.events.some(event => event.type === 'final'), false);

  const length = await runCase('length');
  assert.equal(length.code, 0);
  assert.equal(length.authCount, 2);
  assert.equal(length.requests.length, 2);
  assert.equal(length.events.find(event => event.type === 'final')?.text, 'Truncated answer continued.');
  const repeatedLength = await runCase('repeated-length');
  assert.notEqual(repeatedLength.code, 0);
  assert.equal(repeatedLength.requests.length, 2, 'identical truncated responses must not repeat forever');
  assert.equal(repeatedLength.events.some(event => event.type === 'final'), false);

  const rate = await runCase('rate');
  assert.notEqual(rate.code, 0);
  assert.equal(rate.requests.length, 1, 'provider retry must not bypass authorization');
  assert.equal(rate.events.some(event => event.type === 'final'), false);

  let multiModelRequests = 0;
  const authCount = { count: 0 };
  const provider = await fakeProvider('multiturn', authCount);
  try {
    const first = await runChild('tool', provider, authCount, { checkpoint: null });
    assert.equal(first.code, 0);
    const checkpoint = first.events.find(event => event.type === 'final')?.checkpoint;
    assert.ok(checkpoint?.entries?.length);
    assert.equal(first.events.find(event => event.type === 'final')?.contextUsage?.contextWindow, 131072);
    const currentPrompt = 'SELFTEST updated parent policy: use only current tools.';
    const second = await runChild('resume', provider, authCount,
      { checkpoint, systemPrompt: currentPrompt, tools: [], message: 'What happened before?' });
    assert.equal(second.code, 0);
    assert.equal(second.events.some(event => event.type === 'tool.request'), false);
    assert.deepEqual(second.events.filter(event => event.type === 'final').map(event => event.text), ['Second answer.']);
    const resumedBody = provider.requests[2].body;
    assert.ok(resumedBody.messages.some(item => textContent(item.content).includes('First answer.')));
    assert.ok(resumedBody.messages.some(item => item.role === 'tool' && textContent(item.content).includes('42')));
    const systemText = resumedBody.messages.filter(item => item.role === 'system').map(item => textContent(item.content)).join('\n');
    assert.ok(systemText.includes(currentPrompt));
    assert.equal(systemText.includes(systemPrompt), false, 'stale system prompt must not survive restore');
    assert.equal((resumedBody.tools || []).some(item => item.function?.name === 'lookup'), false);
    assert.equal(authCount.count, 3);

    const manager = SessionManager.inMemory(dir, undefined, structuredClone(checkpoint.entries));
    manager.branch(checkpoint.leafId);
    manager.appendCompaction('Synthetic summary: lookup returned 42.', null, 42);
    const compacted = signCheckpoint({ version: 1, entries: [manager.getHeader(), ...manager.getEntries()],
      leafId: manager.getLeafId() });
    const resumedCompact = await runChild('resume', provider, authCount,
      { checkpoint: compacted, systemPrompt: currentPrompt, tools: [] });
    assert.equal(resumedCompact.code, 0);
    assert.equal(resumedCompact.events.some(event => event.type === 'tool.request'), false);
    assert.ok(provider.requests[3].body.messages.some(item => textContent(item.content).includes('Synthetic summary: lookup returned 42.')));
    const compactSystem = provider.requests[3].body.messages.filter(item => item.role === 'system')
      .map(item => textContent(item.content)).join('\n');
    assert.ok(compactSystem.includes(currentPrompt));
    assert.equal(compactSystem.includes(systemPrompt), false);
    assert.equal((provider.requests[3].body.tools || []).some(item => item.function?.name === 'lookup'), false);

    const branch = SessionManager.inMemory(dir, undefined, structuredClone(checkpoint.entries));
    const earlier = branch.getEntries().find(entry => entry.type === 'message' && entry.message.role === 'user').id;
    branch.branch(earlier);
    const branched = signCheckpoint({ version: 1, entries: [branch.getHeader(), ...branch.getEntries()], leafId: earlier });
    const resumedBranch = await runChild('resume', provider, authCount, { checkpoint: branched, tools: [] });
    assert.equal(resumedBranch.code, 0);
    assert.equal(provider.requests[4].body.messages.some(item => textContent(item.content).includes('First answer.')), false,
      'restore must use the saved leaf, not the last entry');

    const longHistory = SessionManager.inMemory(dir);
    for (let i = 0; i < 280; i++) {
      longHistory.appendMessage({ role: 'user', content: `evidence-${i}:` + 'x'.repeat(10000), timestamp: i + 1 });
    }
    const mainLeaf = longHistory.getLeafId();
    longHistory.branch(longHistory.getEntries()[10].id);
    const oldBranch = longHistory.appendMessage({ role: 'user', content: 'fake historical branch', timestamp: 500 });
    longHistory.branch(mainLeaf);
    longHistory.appendCompaction('Prior evidence remains in the authorized store.', null, 100000);
    const longCheckpoint = signCheckpoint({ version: 1,
      entries: [longHistory.getHeader(), ...longHistory.getEntries()], leafId: longHistory.getLeafId() });
    assert.ok(Buffer.byteLength(JSON.stringify(longCheckpoint)) > 2 * 1024 * 1024);
    const longResume = await runChild('resume', provider, authCount,
      { checkpoint: longCheckpoint, systemPrompt: currentPrompt, tools: [] });
    assert.equal(longResume.code, 0, `compacted long history must remain resumable: ${JSON.stringify(longResume.events.filter(e => e.type === 'error'))}`);
    const savedLong = longResume.events.find(event => event.type === 'final')?.checkpoint;
    const restoredLong = SessionManager.inMemory(dir, undefined, savedLong.entries);
    assert.ok(restoredLong.getEntry(oldBranch), 'large checkpoint must preserve historical branch');
    restoredLong.branch(oldBranch);
    assert.ok(JSON.stringify(restoredLong.buildSessionContext()).includes('fake historical branch'));

    const corrupt = structuredClone(checkpoint);
    corrupt.entries[0].id += '-changed';
    const badLeaf = signCheckpoint({ ...structuredClone(checkpoint), leafId: 'missing-leaf' });
    const badParent = structuredClone(checkpoint);
    badParent.entries[1].parentId = badParent.entries[1].id;
    const badMessage = structuredClone(checkpoint);
    badMessage.entries.find(entry => entry.type === 'message' && entry.message.role === 'assistant').message.content = 'invalid';
    const oversized = structuredClone(checkpoint);
    oversized.entries[0].cwd = 'x'.repeat(1100000);
    for (const invalid of [corrupt, badLeaf, signCheckpoint(badParent), signCheckpoint(badMessage),
      { ...checkpoint, unhashed: true }, signCheckpoint(oversized)]) {
      const before = provider.requests.length;
      const rejected = await runChild('resume', provider, authCount, { checkpoint: invalid });
      assert.notEqual(rejected.code, 0);
      assert.ok(rejected.events.some(event => event.type === 'error' && event.code === 'invalid_checkpoint'));
      assert.equal(rejected.events.some(event => event.type === 'authorize.request' || event.type === 'final'), false);
      assert.equal(provider.requests.length, before);
    }
    const reset = await runChild('resume', provider, authCount,
      { checkpoint, resetHistory: true, tools: [], message: 'Start a new investigation.' });
    assert.equal(reset.code, 0);
    const resetMessages = provider.requests.at(-1).body.messages;
    assert.equal(resetMessages.some(item => textContent(item.content).includes('First answer.')), false);
    assert.equal(resetMessages.some(item => item.role === 'tool'), false);
    const resetCheckpoint = reset.events.find(event => event.type === 'final').checkpoint;
    assert.ok(resetCheckpoint.entries.length > checkpoint.entries.length,
      'the prior session branch must remain available without being replayed');
    multiModelRequests = provider.requests.length;
  } finally {
    await provider.close();
  }

  process.stdout.write(JSON.stringify({
    ok: true,
    scenarios: ['twenty-tool-turns', 'custom-provider', 'deepseek-tool-reasoning', 'tool/auth/final-stop', 'authorization-denied', 'length-continuation', 'streamed-text', 'no-unguarded-retry', 'checkpoint-restart', 'auto-compaction-256k', 'compaction-restart', 'large-history-branch', 'invalid-checkpoints'],
    modelRequests: longProvider.requests.length + unknownProvider.requests.length + reasoningProvider.requests.length + success.requests.length + stream.requests.length + compactProvider.requests.length + denied.requests.length + length.requests.length + repeatedLength.requests.length + rate.requests.length + multiModelRequests,
    provider: 'loopback fake OpenAI-compatible server',
  }) + '\n');
}

main().catch(error => {
  process.stderr.write(`self-test failed: ${String(error?.message || error).split(fakeKey).join('[redacted]')}\n`);
  process.exitCode = 1;
});
