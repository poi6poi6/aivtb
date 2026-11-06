// Offline SDK/tool-surface verification. Never calls run(), prompt(), or a model.
import assert from 'node:assert/strict';
import {mkdtemp, rm} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {DeepSeekHarness} from '@deepseek-ai/dsh-sdk-client';
import {apply, TOOL_NAMES} from './plugin.mjs';

assert.equal(typeof DeepSeekHarness, 'function');
process.env.VTUBER_DSH_BRIDGE = 'http://127.0.0.1:1';
process.env.VTUBER_DSH_TOKEN = 'offline-verification-only';
// Deliberately no credentials. A caller's real key must never reach verification.
delete process.env.DEEPSEEK_API_KEY;
const definitions = [], hooks = new Map(), restrictions = [];
let guard;
const ctx = {
  tools: {register: tool => definitions.push(tool), guard: rule => { guard = rule; }},
  on: (event, hook) => hooks.set(event, hook),
  provide: (name, value) => { assert.equal(name, 'biliBridge'); assert.equal(value, true); },
};
apply(ctx);
assert.deepEqual(definitions.map(tool => tool.name), TOOL_NAMES);
assert.equal(guard({name: 'bili_read'}), undefined);
assert.equal(typeof guard({name: 'bash'}), 'string');
const agent = {ctx: {tools: {
  restrict: value => restrictions.push(value),
  schemas: () => TOOL_NAMES.map(name => ({name})),
}}};
hooks.get('agent/created')({agent});
assert.deepEqual(restrictions, [{allow: TOOL_NAMES}]);
agent.ctx.tools.schemas = () => [...TOOL_NAMES, 'bash'].map(name => ({name}));
assert.throws(() => hooks.get('agent/created')({agent}), /Unexpected model tool surface/);

const calls = [];
globalThis.fetch = async (url, options) => {
  calls.push([url, options]);
  return {ok: true, json: async () => ({ok: true, result: {ok: false, error: '请先读取来源'}})};
};
const signal = new AbortController().signal;
const output = await definitions[0].execute({query: 'rose'}, {signal});
assert.deepEqual(output, {ok: false, error: '请先读取来源'});
assert.equal(calls[0][1].signal, signal);
let proceeded = false;
await hooks.get('agent/pre-step')({signal}, () => { proceeded = true; });
assert.equal(proceeded, true);
assert.match(calls[1][0], /\/step$/);
globalThis.fetch = async () => ({ok: false, json: async () => ({ok: false})});
proceeded = false;
await assert.rejects(hooks.get('agent/pre-step')({signal}, () => { proceeded = true; }));
assert.equal(proceeded, false);
console.log('SDK import and six-tool plugin contracts: PASS');

if (process.argv[2] === '--handshake') {
  const directory = await mkdtemp(join(tmpdir(), 'vtuber-dsh-verify-'));
  const env = {};
  for (const key of ['PATH', 'SystemRoot', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP']) {
    if (process.env[key]) env[key] = process.env[key];
  }
  Object.assign(env, {DEEPSEEK_API_KEY: '', VTUBER_DSH_BRIDGE: process.env.VTUBER_DSH_BRIDGE,
    VTUBER_DSH_TOKEN: process.env.VTUBER_DSH_TOKEN});
  const harness = new DeepSeekHarness({profile: 'sdk-minimal', patches: [process.argv[3]],
    dshHome: join(directory, 'home'), cwd: directory, processCwd: directory, env,
    provider: 'deepseek-official', model: 'deepseek-v4-flash', initializeTimeoutMs: 20000,
    shutdownTimeoutMs: 500, disposeEofGraceMs: 500, disposeGraceMs: 1000});
  try {
    await harness.start(); // initialize only; never creates an input/model request.
    // This creates an idle session/agent and exercises the real agent/created
    // tool-surface guard without enqueuing any prompt or running an agent turn.
    assert.equal(await harness.session('offline-tool-surface-check').getWorkingDirectory(), directory);
    console.log('Official sdk-minimal initialization without credentials: PASS');
  } catch {
    console.error('Official sdk-minimal initialization without credentials: FAILED');
    process.exitCode = 1;
  } finally {
    await harness.close();
    await rm(directory, {recursive: true, force: true});
  }
}
