// Persist a bounded, value-free transport summary. Never accept message text,
// IDs, credentials, URLs, exception messages or stacks into this projection.
import {mkdirSync, renameSync, writeFileSync} from 'node:fs';
import {dirname} from 'node:path';
import {fileURLToPath} from 'node:url';

export const STATUS_PATH = fileURLToPath(new URL('../../data/qq/transport-status.json', import.meta.url));
const EVENTS = new Set(['starting', 'binding', 'connecting', 'ready', 'resumed', 'disconnected',
  'stopped', 'sdk_error', 'raw_event', 'inbound', 'rejected', 'forwarded', 'bridge_failed',
  'no_reply', 'voice_failed', 'send_attempted', 'send_result', 'receipt_recorded', 'receipt_failed']);
const REASONS = new Set(['aborted', 'non_private', 'non_chat', 'bot_sender', 'sender_id', 'message_id', 'group_id', 'not_mentioned',
  'target_mismatch', 'content_type', 'empty_content', 'content_length', 'duplicate', 'queue_full',
  'host_no_reply', 'host_reply_invalid', 'host_reply_mode', 'prepare', 'commands', 'events', 'message', 'receipt',
  'start_failed', 'gateway_error', 'sdk_log', 'sent', 'failed', 'unknown']);
const KINDS = new Set(['c2c', 'group', 'guild', 'dm']);
const TYPES = new Set(['string', 'number', 'boolean', 'object', 'undefined', 'function', 'symbol', 'bigint']);
const RAW_EVENTS = new Set(['FRIEND_ADD', 'FRIEND_DEL', 'C2C_MSG_REJECT', 'C2C_MSG_RECEIVE',
  'GROUP_ADD_ROBOT', 'GROUP_DEL_ROBOT', 'GROUP_MSG_REJECT', 'GROUP_MSG_RECEIVE']);
const COUNTERS = ['ready', 'resumed', 'disconnected', 'sdk_error', 'raw_event', 'inbound', 'rejected',
  'forwarded', 'bridge_failed', 'no_reply', 'voice_failed', 'send_attempted', 'send_result', 'receipt_recorded', 'receipt_failed'];
const FIELD_TYPES = ['sender_type', 'message_type', 'content_type'];
const FIELD_LENGTHS = ['sender_length', 'message_length', 'content_length'];
const FIELD_BOOLEANS = ['sender_valid', 'message_valid', 'target_matches', 'msg_id_matches',
  'sender_is_bot', 'has_attachments', 'has_elements', 'group_valid', 'mentioned'];

// Only construction-time trusted code chooses a path; an event can never change it.
export function createDiagnosticWriter(destination = STATUS_PATH, {now = Date.now} = {}) {
  const temporary = `${destination}.${process.pid}.tmp`;
  const state = {schema: 1, process_id: process.pid, updated_at: 0, phase: 'starting',
    counts: Object.fromEntries(COUNTERS.map(key => [key, 0])), last_event: {event: 'starting'}};
  return function diagnostic(value) {
    try {
      if (!value || !EVENTS.has(value.event)) return;
      const last = {event: value.event};
      if (REASONS.has(value.reason)) last.reason = value.reason;
      if ('kind' in value) last.kind = KINDS.has(value.kind) ? value.kind : 'other';
      if ('raw_event' in value) last.raw_event = RAW_EVENTS.has(value.raw_event) ? value.raw_event : 'other';
      if (value.fields && typeof value.fields === 'object') {
        const fields = {};
        for (const key of FIELD_TYPES) if (TYPES.has(value.fields[key])) fields[key] = value.fields[key];
        for (const key of FIELD_LENGTHS) {
          const length = value.fields[key];
          if (Number.isSafeInteger(length) && length >= 0) fields[key] = Math.min(length, 65536);
        }
        for (const key of FIELD_BOOLEANS) if (typeof value.fields[key] === 'boolean') fields[key] = value.fields[key];
        last.fields = fields;
      }
      if (Object.hasOwn(state.counts, value.event)) state.counts[value.event] = Math.min(state.counts[value.event] + 1, 2147483647);
      if (['starting', 'binding', 'connecting', 'disconnected', 'stopped'].includes(value.event)) state.phase = value.event;
      else if (value.event === 'ready' || value.event === 'resumed') state.phase = 'connected';
      const timestamp = now();
      if (Number.isSafeInteger(timestamp) && timestamp >= 0) state.updated_at = timestamp;
      state.last_event = last;
      // Preserve the latest validation details after forwarding/sending overwrites last_event.
      if (value.event === 'inbound') state.last_inbound = last;
      if (value.event === 'rejected') state.last_rejection = last;
      if (['bridge_failed', 'sdk_error', 'receipt_failed', 'voice_failed'].includes(value.event)) state.last_error = last;
      mkdirSync(dirname(destination), {recursive: true});
      writeFileSync(temporary, JSON.stringify(state), {encoding: 'utf8', mode: 0o600});
      renameSync(temporary, destination);
    } catch { /* A diagnostics write must never interrupt chat or expose raw errors. */ }
  };
}
