import assert from 'node:assert/strict';
import test from 'node:test';
import {apply, PersonaAdapter, projectMessages, decisionChunks} from '../tools/deepseek-harness/persona-provider.mjs';

const message = {role: 'user', content: [{type: 'text', text: '花园'}]};
const endpoint = 'http://127.0.0.1:1234';

test('projects only public message content and keeps tool correlation', () => {
  const projected = projectMessages([
    {...message, id: 'private-id', source: {replayState: 'private'}},
    {role: 'assistant', content: [{type: 'reasoning', text: 'private reasoning'},
      {type: 'tool-call', id: 'call1', name: 'bili_search', arguments: '{"query":"花园"}', private: true}]},
    {role: 'tool', source: {kind: 'tool'}, toolCallId: 'call1', content: [{type: 'text', text: 'result'}]},
  ]);
  assert.deepEqual(projected, [message,
    {role: 'assistant', content: [{type: 'tool-call', id: 'call1', name: 'bili_search', arguments: '{"query":"花园"}'}]},
    {role: 'tool', content: [{type: 'text', text: 'result'}], toolCallId: 'call1'}]);
  for (const bad of [[], [{...message, content: [{type: 'image', url: 'private'}]}],
    [{role: 'tool', content: []}], [{role: 'system', content: [{type: 'text', text: 'x'.repeat(900001)}]}]]) {
    assert.throws(() => projectMessages(bad), /主人格模型桥接失败/);
  }
});

test('one decision yields correlated native tool blocks or a final text block', () => {
  const chunks = decisionChunks({type: 'tool', name: 'bili_search', arguments: {query: '花园'}});
  assert.equal(chunks[1].id, chunks[2].block.id);
  assert.equal(chunks[1].argumentsDelta, '{"query":"花园"}');
  assert.deepEqual(chunks.at(-1), {type: 'finish', reason: {kind: 'tool-calls'}});
  const answer = decisionChunks({type: 'finish', text: '阅读结束。'});
  assert.deepEqual(answer[2].block, {type: 'text', text: '阅读结束。'});
  assert.equal(answer.at(-1).reason.kind, 'stop');
  for (const bad of [{type: 'tool', name: 'publish', arguments: {}},
    {type: 'tool', name: 'bili_search', arguments: []}, {type: 'finish', text: ''},
    {type: 'finish', text: 'done', extra: true}]) assert.throws(() => decisionChunks(bad));
});

test('registers the official adapter route and initialization service', () => {
  const saved = [process.env.VTUBER_DSH_BRIDGE, process.env.VTUBER_DSH_TOKEN];
  process.env.VTUBER_DSH_BRIDGE = endpoint;
  process.env.VTUBER_DSH_TOKEN = 'test-token';
  let registration, service;
  try {
    apply({llm: {registerAdapter: (...args) => { registration = args; }},
      provide: (...args) => { service = args; }});
    assert.deepEqual(registration[0], ['vtuber-persona']);
    assert.ok(registration[1] instanceof PersonaAdapter);
    assert.deepEqual(service, ['personaProvider', true]);
  } finally {
    for (const [index, name] of ['VTUBER_DSH_BRIDGE', 'VTUBER_DSH_TOKEN'].entries()) {
      if (saved[index] === undefined) delete process.env[name]; else process.env[name] = saved[index];
    }
  }
});

test('sends only projected history to authenticated localhost and forwards cancellation', async () => {
  const fetch = globalThis.fetch;
  const signal = new AbortController().signal;
  let seen;
  globalThis.fetch = async (url, options) => {
    seen = {url, options};
    return {ok: true, text: async () => JSON.stringify({ok: true, result: {type: 'finish', text: '完成'}})};
  };
  try {
    const chunks = [];
    for await (const chunk of new PersonaAdapter(endpoint, 'test-token').stream({
      messages: [message], signal, sessionId: 'private', tools: ['private'], model: 'main-persona',
    })) chunks.push(chunk);
    assert.equal(seen.url, endpoint + '/model');
    assert.equal(seen.options.signal, signal);
    assert.equal(seen.options.redirect, 'error');
    assert.equal(seen.options.headers.Authorization, 'Bearer test-token');
    assert.deepEqual(JSON.parse(seen.options.body), {messages: [message]});
    assert.equal(chunks.at(-1).reason.kind, 'stop');
  } finally { globalThis.fetch = fetch; }
});

test('bridge and malformed response failures never expose raw server details', async () => {
  const fetch = globalThis.fetch;
  const adapter = new PersonaAdapter(endpoint, 'test-token');
  try {
    for (const response of [{ok: false, text: async () => 'secret'},
      {ok: true, text: async () => 'secret'}, {ok: true, text: async () => JSON.stringify({ok: true, result: {type: 'tool', name: 'publish', arguments: {}}})}]) {
      globalThis.fetch = async () => response;
      await assert.rejects(async () => { for await (const chunk of adapter.stream({messages: [message]})) {} },
        error => error.message === '主人格模型桥接失败。');
    }
    globalThis.fetch = async () => { throw new Error('secret endpoint credential'); };
    await assert.rejects(async () => { for await (const chunk of adapter.stream({messages: [message]})) {} },
      error => error.message === '主人格模型桥接失败。');
  } finally { globalThis.fetch = fetch; }
});

test('an aborted request never calls the host or emits a decision', async () => {
  const fetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = async () => { calls += 1; throw new Error('unexpected'); };
  const controller = new AbortController();
  controller.abort();
  try {
    await assert.rejects(async () => {
      for await (const chunk of new PersonaAdapter(endpoint, 'test-token').stream({messages: [message], signal: controller.signal})) {}
    }, error => error.name === 'AbortError');
    assert.equal(calls, 0);
  } finally { globalThis.fetch = fetch; }
});

test('cancellation during the model bridge aborts the request without emitting a tool', async () => {
  const fetch = globalThis.fetch;
  const controller = new AbortController();
  let entered;
  const fetched = new Promise(resolve => { entered = resolve; });
  globalThis.fetch = async (_url, options) => {
    assert.equal(options.signal, controller.signal);
    entered();
    return new Promise((_resolve, reject) => {
      options.signal.addEventListener('abort', () => reject(new Error('secret request detail')), {once: true});
    });
  };
  const stream = new PersonaAdapter(endpoint, 'test-token').stream({messages: [message], signal: controller.signal});
  try {
    const pending = stream.next();
    await fetched;
    controller.abort();
    await assert.rejects(pending, error => error.name === 'AbortError' && !error.message.includes('secret'));
  } finally { await stream.return(); globalThis.fetch = fetch; }
});
