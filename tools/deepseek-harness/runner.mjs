// JSONL carrier around the official SDK; stdout contains only our protocol.
import {createInterface} from 'node:readline';
import {DeepSeekHarness} from '@deepseek-ai/dsh-sdk-client';

const emit = value => process.stdout.write(JSON.stringify(value) + '\n');
const lines = createInterface({input: process.stdin, crlfDelay: Infinity});
let harness, started = false, stopping = false;
async function stop() {
  if (stopping) return;
  stopping = true;
  try { await harness?.close(); } finally { lines.close(); }
}
process.on('SIGTERM', () => { void stop(); });
process.on('SIGINT', () => { void stop(); });
lines.on('close', () => { if (!stopping) void stop(); });
for await (const line of lines) {
  let message;
  try { message = JSON.parse(line); } catch { emit({type: 'error', code: 'protocol'}); break; }
  if (message.type === 'cancel') { await stop(); break; }
  if (started || message.type !== 'start') { emit({type: 'error', code: 'protocol'}); break; }
  started = true;
  // Don't await here: stdin cancellation must still be processed during run().
  void (async () => {
    try {
      harness = new DeepSeekHarness({
        profile: 'sdk-minimal', patches: [message.patch], dshHome: message.home,
        cwd: message.workspace, processCwd: message.workspace, env: {...process.env},
        provider: message.provider || 'deepseek-official', model: message.model, maxTokens: 2048,
        initializeTimeoutMs: 30000, requestTimeoutMs: message.timeout_ms,
        shutdownTimeoutMs: 500, disposeEofGraceMs: 500, disposeGraceMs: 1000,
      });
      const result = await harness.run(JSON.stringify({task: 'Browse within the stated limits, record grounded notes, and optionally save a draft. Do not publish.', context: message.context}));
      const ending = result.events.filter(event => event.type === 'turn/end').at(-1);
      if (ending?.data?.reason?.kind !== 'completed') throw new Error('Incomplete turn');
      if (!stopping) emit({type: 'final', content: result.finalResponse});
    } catch {
      // Never forward SDK stderr or raw errors, which may contain auth/request data.
      if (!stopping) emit({type: 'error', code: 'runtime_failed'});
    } finally { await stop(); }
  })();
}
await stop();
