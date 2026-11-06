// JSONL runner; never invoke harness.run(), an agent, or any model.
import {createInterface} from 'node:readline';
import {mkdir, writeFile} from 'node:fs/promises';
import {dirname, isAbsolute, join} from 'node:path';
import {fileURLToPath, pathToFileURL} from 'node:url';
import {createRequire} from 'node:module';
import {validBridge} from './transport.mjs';

const emit = value => process.stdout.write(JSON.stringify(value) + '\n');
// Third-party errors may include URLs/credentials; expose only fixed status codes.
for (const key of ['log', 'info', 'warn', 'error', 'debug', 'trace']) console[key] = () => {};
process.stderr.write = (_value, encoding, callback) => { (typeof encoding === 'function' ? encoding : callback)?.(); return true; };
const folder = dirname(fileURLToPath(import.meta.url));
const requireDsh = createRequire(join(folder, '..', 'deepseek-harness', 'package.json'));
const lines = createInterface({input: process.stdin, crlfDelay: Infinity});
let harness, started = false, stopping = false, stopPromise;
function stop() {
  if (stopPromise) return stopPromise;
  stopping = true;
  stopPromise = (async () => {
    try { await harness?.close(); } catch { emit({type: 'error', code: 'shutdown_failed'}); }
    finally { lines.close(); }
  })();
  return stopPromise;
}
async function start(message) {
  try {
    validBridge(message.endpoint, message.token);
    if (!isAbsolute(message.home) || !isAbsolute(message.workspace)) throw new Error('Invalid directories');
    const env = {};
    for (const key of ['PATH', 'SystemRoot', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP']) {
      if (process.env[key]) env[key] = process.env[key];
    }
    Object.assign(env, {VTUBER_QQ_BRIDGE: message.endpoint, VTUBER_QQ_TOKEN: message.token});
    await mkdir(message.home, {recursive: true});
    await mkdir(message.workspace, {recursive: true});
    const disabled = ['llm-deepseek', 'tool-fs', 'tool-working-directory', 'persistent-bash',
      'persistent-pwsh', 'terminal-bash', 'terminal-pwsh', 'mcp-resources', 'compaction-basic',
      'image-offload', 'llm-retry'];
    const patch = [...disabled.map(id => ({id, disabled: true})),
      {id: 'agent-loop', config: {agents: []}},
      {id: 'sdk-jsonrpc-server', inject: ['sdkAppStartup', 'loader', 'qqBridge']},
      {insert: [{id: 'qq-bridge', name: join(folder, 'plugin.mjs')}]},
    ];
    const patchPath = join(message.home, 'qq.patch.yml');
    await writeFile(patchPath, JSON.stringify(patch));
    if (stopping) return;
    const {DeepSeekHarness} = await import(pathToFileURL(requireDsh.resolve('@deepseek-ai/dsh-sdk-client')).href);
    if (stopping) return;
    harness = new DeepSeekHarness({profile: 'sdk-minimal', patches: [patchPath], dshHome: message.home,
      cwd: message.workspace, processCwd: message.workspace, env,
      initializeTimeoutMs: 30000, requestTimeoutMs: 30000,
      shutdownTimeoutMs: 1000, disposeEofGraceMs: 500, disposeGraceMs: 1000});
    await harness.start();
    if (stopping) return;
    emit({type: 'ready'});
    const notifications = harness.client.subscribe();
    try { for await (const _ of notifications) { /* No model/session is created. */ } }
    finally { notifications.close(); }
    if (!stopping) throw new Error('Runtime exited');
  } catch { if (!stopping) emit({type: 'error', code: 'runtime_failed'}); }
  finally { await stop(); }
}
process.on('SIGTERM', () => { void stop(); });
process.on('SIGINT', () => { void stop(); });
lines.on('close', () => { void stop(); });
for await (const line of lines) {
  let message;
  try { if (line.length > 8192) throw new Error(); message = JSON.parse(line); }
  catch { emit({type: 'error', code: 'protocol'}); await stop(); break; }
  if (message.type === 'stop' || message.type === 'cancel') { await stop(); break; }
  if (started || message.type !== 'start') { emit({type: 'error', code: 'protocol'}); await stop(); break; }
  started = true;
  void start(message);
}
await stop();
