// Real official SDK loop against deterministic localhost fakes, with no keys.
import assert from 'node:assert/strict';
import {createServer} from 'node:http';
import {mkdtemp, rm, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join, dirname} from 'node:path';
import {fileURLToPath} from 'node:url';
import {DeepSeekHarness} from '@deepseek-ai/dsh-sdk-client';

const folder = dirname(fileURLToPath(import.meta.url));
const token = 'offline-persona-loop-verification-only';
const url = 'https://www.bilibili.com/video/BV1xx411c7mD/';
const decisions = [
  {type: 'tool', name: 'bili_search', arguments: {query: '花园'}},
  {type: 'tool', name: 'bili_read', arguments: {url}},
  {type: 'tool', name: 'bili_note', arguments: {source_url: url, summary: '简介介绍花园。', reaction: '想多了解花园。'}},
  {type: 'tool', name: 'bili_draft', arguments: {text: '今天读到了花园的介绍。', source_urls: [url]}},
  {type: 'tool', name: 'bili_diary', arguments: {text: '想记住读到的花园介绍。', source_urls: [url]}},
  {type: 'tool', name: 'bili_interaction', arguments: {action: 'like', url, text: ''}},
  {type: 'finish', text: '已根据实际工具回执保存见闻与草稿。'},
];
const toolCalls = [], modelMessages = [], errors = [];
let steps = 0;
const server = createServer(async (request, response) => {
  try {
    assert.equal(request.method, 'POST');
    assert.equal(request.headers.authorization, `Bearer ${token}`);
    let raw = '';
    for await (const chunk of request) raw += chunk;
    const body = JSON.parse(raw);
    let result;
    if (request.url === '/model') {
      assert.deepEqual(Object.keys(body), ['messages']);
      assert.match(request.headers['user-agent'], /deepseek-harness/);
      assert.ok(Array.isArray(body.messages));
      for (const message of body.messages) {
        assert.ok(Object.keys(message).every(key => ['role', 'content', 'toolCallId'].includes(key)));
        assert.ok(message.content.every(block => block.type === 'text' || block.type === 'tool-call'));
      }
      assert.equal(body.messages.filter(message => message.role === 'tool').length, toolCalls.length);
      modelMessages.push(body.messages);
      result = decisions[modelMessages.length - 1];
      assert.ok(result, 'The loop must stop after the final answer');
    } else if (request.url === '/step') {
      steps += 1;
      assert.ok(steps <= decisions.length);
      result = {};
    } else if (request.url === '/tool') {
      assert.deepEqual(body, {name: decisions[toolCalls.length].name,
        arguments: decisions[toolCalls.length].arguments});
      toolCalls.push(body.name);
      result = {ok: true, url, title: '花园', description: '视频简介介绍了花园。', saved: true};
    } else assert.fail('Unexpected bridge path');
    response.writeHead(200, {'Content-Type': 'application/json'});
    response.end(JSON.stringify({ok: true, result}));
  } catch (error) {
    errors.push(error);
    response.writeHead(400, {'Content-Type': 'application/json'});
    response.end(JSON.stringify({ok: false}));
  }
});

const directory = await mkdtemp(join(tmpdir(), 'vtuber-persona-verify-'));
let harness;
try {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const endpoint = `http://127.0.0.1:${server.address().port}`;
  const env = {};
  for (const key of ['PATH', 'SystemRoot', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP']) {
    if (process.env[key]) env[key] = process.env[key];
  }
  Object.assign(env, {VTUBER_DSH_BRIDGE: endpoint, VTUBER_DSH_TOKEN: token});
  const disabled = ['llm-deepseek', 'tool-fs', 'tool-working-directory', 'persistent-bash',
    'persistent-pwsh', 'terminal-bash', 'terminal-pwsh', 'mcp-resources',
    'compaction-basic', 'image-offload', 'llm-retry'];
  const patch = [...disabled.map(id => ({id, disabled: true})),
    {id: 'tools', config: {mode: 'native'}},
    {id: 'agent-loop', config: {agents: [], maxParallelToolCalls: 1}},
    {id: 'system-prompt', config: {includeHarnessIdentity: false, includeRuntimeContext: false,
      personaPrefix: 'Offline deterministic reading test.'}},
    {id: 'sdk-jsonrpc-server', inject: ['sdkAppStartup', 'loader', 'biliBridge', 'personaProvider'],
      config: {maxTokensAsSuccess: false}},
    {insert: [
      {id: 'bili-bridge', name: join(folder, 'plugin.mjs')},
      {id: 'persona-provider', name: join(folder, 'persona-provider.mjs')},
    ]},
  ];
  const patchPath = join(directory, 'persona.patch.yml');
  await writeFile(patchPath, JSON.stringify(patch));
  harness = new DeepSeekHarness({profile: 'sdk-minimal', patches: [patchPath],
    dshHome: join(directory, 'home'), cwd: directory, processCwd: directory, env,
    provider: 'vtuber-persona', model: 'existing-persona-model', maxTokens: 2048,
    initializeTimeoutMs: 20000, requestTimeoutMs: 20000,
    shutdownTimeoutMs: 500, disposeEofGraceMs: 500, disposeGraceMs: 1000});
  const result = await harness.run('Read the example page, save a note, and prepare a draft.');
  if (errors.length) throw errors[0];
  assert.equal(result.events.filter(event => event.type === 'turn/end').at(-1)?.data?.reason?.kind, 'completed');
  assert.equal(result.finalResponse, decisions.at(-1).text);
  assert.deepEqual(toolCalls, ['bili_search', 'bili_read', 'bili_note', 'bili_draft', 'bili_diary', 'bili_interaction']);
  assert.equal(modelMessages.length, 7);
  assert.equal(steps, 7);
  assert.equal(modelMessages.at(-1).filter(message => message.role === 'tool').length, 6);
  console.log('Official SDK persona provider loop: 7 local model decisions, 6 tools, no provider keys: PASS');
} finally {
  await harness?.close();
  await new Promise(resolve => server.close(resolve));
  await rm(directory, {recursive: true, force: true});
}
