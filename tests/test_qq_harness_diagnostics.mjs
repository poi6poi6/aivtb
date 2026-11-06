import assert from 'node:assert/strict';
import {existsSync, mkdtempSync, readFileSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {basename, dirname, join, resolve} from 'node:path';
import {createDiagnosticWriter, STATUS_PATH} from '../tools/qq-harness/diagnostics.mjs';

assert.equal(basename(STATUS_PATH), 'transport-status.json');
assert.equal(basename(dirname(STATUS_PATH)), 'qq');
const folder = mkdtempSync(join(tmpdir(), 'vtuber-qq-diagnostics-'));
const destination = join(folder, 'status.json');
try {
  const diagnostic = createDiagnosticWriter(destination, {now: () => 123456});
  diagnostic({event: 'starting'});
  diagnostic({event: 'ready'});
  diagnostic({event: 'inbound', kind: 'secret-kind', credentials: 'private-credentials',
    text: 'private-message', message_id: 'private-id', fields: {
      sender_type: 'string', message_type: 'private-type', content_type: 'string', sender_length: 32,
      message_length: 100000, content_length: -1, sender_valid: true, message_valid: false,
      target_matches: 'private-target', msg_id_matches: false, unexpected: 'private-value',
    }});
  diagnostic({event: 'rejected', reason: 'message_id', message: 'private-rejection'});
  diagnostic({event: 'sdk_error', reason: 'gateway_error', error: new Error('private-error')});
  diagnostic({event: 'raw_event', raw_event: 'private-event-name', reason: 'private-reason'});
  for (let index = 0; index < 100; index++) diagnostic({event: 'forwarded'});
  diagnostic({event: 'private-event', text: 'private-message'});
  const serialized = readFileSync(destination, 'utf8');
  const status = JSON.parse(serialized);
  assert(!serialized.includes('private-'));
  assert.equal(status.schema, 1);
  assert.equal(status.updated_at, 123456);
  assert.equal(status.phase, 'connected');
  assert.equal(status.counts.forwarded, 100);
  assert.equal(status.last_inbound.kind, 'other');
  assert.deepEqual(status.last_inbound.fields, {sender_type: 'string', content_type: 'string',
    sender_length: 32, message_length: 65536, sender_valid: true, message_valid: false, msg_id_matches: false});
  assert.equal(status.last_rejection.reason, 'message_id');
  assert.equal(status.last_error.reason, 'gateway_error');
  assert(serialized.length < 4096, 'summary stays bounded without event history');
  assert(!existsSync(`${destination}.${process.pid}.tmp`), 'atomic replacement consumes the temporary file');
  diagnostic({event: 'voice_failed', reason: 'prepare', error: new Error('private-upload-error')});
  diagnostic({event: 'no_reply', reason: 'host_reply_mode', mode: 'private-mode'});
  diagnostic({event: 'send_result', reason: 'failed'});
  const voiceSerialized = readFileSync(destination, 'utf8');
  const voiceStatus = JSON.parse(voiceSerialized);
  assert.equal(voiceStatus.counts.voice_failed, 1);
  assert.deepEqual(voiceStatus.last_error, {event: 'voice_failed', reason: 'prepare'});
  assert(!voiceSerialized.includes('private-'));
  diagnostic({event: 'inbound', kind: 'group', fields: {mentioned: true, group_valid: true},
    group_id: 'private-group', peer_id: 'private-member'});
  diagnostic({event: 'rejected', reason: 'not_mentioned', content: 'private-group-message'});
  const groupSerialized = readFileSync(destination, 'utf8');
  const groupStatus = JSON.parse(groupSerialized);
  assert.deepEqual(groupStatus.last_inbound.fields, {group_valid: true, mentioned: true});
  assert.equal(groupStatus.last_rejection.reason, 'not_mentioned');
  assert(!groupSerialized.includes('private-'));
  const broken = createDiagnosticWriter(join(destination, 'impossible.json'));
  assert.doesNotThrow(() => broken({event: 'starting'}), 'diagnostics IO failure cannot break the channel');
  console.log('QQ diagnostics: bounded allowlist, no private values, atomic snapshot, isolated IO failures: PASS');
} finally {
  const target = resolve(folder);
  assert.equal(dirname(target), resolve(tmpdir()));
  assert(basename(target).startsWith('vtuber-qq-diagnostics-'));
  rmSync(target, {recursive: true, force: true});
}
