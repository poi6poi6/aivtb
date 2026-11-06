// Official provider seam: the host's existing Persona owns every model request.
import {randomUUID} from 'node:crypto';
import {LlmAdapter, LlmError, attributionHeaders} from '@deepseek-ai/dsh-llm';

export const name = 'vtuber-persona-provider';
export const inject = ['llm'];
export const PROVIDER = 'vtuber-persona';
const TOOLS = new Set(['bili_search', 'bili_read', 'bili_note', 'bili_draft', 'bili_diary', 'bili_interaction']);
const failed = () => new LlmError('主人格模型桥接失败。', 'PERSONA_BRIDGE_FAILED');
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const exactKeys = (value, keys) => object(value) && Object.keys(value).sort().join(',') === [...keys].sort().join(',');

// Project only public conversation content. No session metadata, credentials,
// private replay state, reasoning, signal or arbitrary SDK objects cross here.
export function projectMessages(messages) {
  if (!Array.isArray(messages) || !messages.length || messages.length > 100) throw failed();
  const projected = messages.map(message => {
    if (!object(message) || !['system', 'developer', 'user', 'assistant', 'tool'].includes(message.role)
        || !Array.isArray(message.content)) throw failed();
    const content = [];
    for (const block of message.content) {
      if (!object(block)) throw failed();
      if (block.type === 'reasoning') continue;
      if (block.type === 'text' && typeof block.text === 'string') {
        content.push({type: 'text', text: block.text});
      } else if (block.type === 'tool-call' && message.role === 'assistant'
          && typeof block.id === 'string' && block.id && TOOLS.has(block.name)
          && typeof block.arguments === 'string') {
        content.push({type: 'tool-call', id: block.id, name: block.name, arguments: block.arguments});
      } else throw failed();
    }
    const result = {role: message.role, content};
    if (message.role === 'tool') {
      if (typeof message.toolCallId !== 'string' || !message.toolCallId) throw failed();
      result.toolCallId = message.toolCallId;
    }
    return result;
  });
  if (Buffer.byteLength(JSON.stringify({messages: projected}), 'utf8') > 240000) throw failed();
  return projected;
}

export function decisionChunks(decision) {
  if (exactKeys(decision, ['type', 'text']) && decision.type === 'finish'
      && typeof decision.text === 'string' && decision.text.trim() && decision.text.length <= 2000) {
    return [
      {type: 'block-start', index: 0, blockType: 'text'},
      {type: 'text-delta', index: 0, text: decision.text},
      {type: 'block-end', index: 0, block: {type: 'text', text: decision.text}},
      {type: 'finish', reason: {kind: 'stop'}},
    ];
  }
  if (!exactKeys(decision, ['type', 'name', 'arguments']) || decision.type !== 'tool'
      || !TOOLS.has(decision.name) || !object(decision.arguments)) throw failed();
  const args = JSON.stringify(decision.arguments);
  if (Buffer.byteLength(args, 'utf8') > 16000) throw failed();
  const id = randomUUID();
  return [
    {type: 'block-start', index: 0, blockType: 'tool-call'},
    {type: 'tool-call-delta', index: 0, id, name: decision.name, argumentsDelta: args},
    {type: 'block-end', index: 0, block: {type: 'tool-call', id, name: decision.name, arguments: args}},
    {type: 'finish', reason: {kind: 'tool-calls'}},
  ];
}

export class PersonaAdapter extends LlmAdapter {
  constructor(endpoint, token) {
    super();
    if (!/^http:\/\/127\.0\.0\.1:\d+$/.test(endpoint || '') || !token) throw failed();
    this.endpoint = endpoint;
    this.token = token;
  }
  providerInfo(provider) { return {id: provider, name: '当前主人格模型'}; }
  async resolveModel(provider, model) {
    return {provider, id: model, name: model, inputModalities: ['text']};
  }
  async *stream(options) {
    try {
      options.signal?.throwIfAborted();
      const response = await fetch(this.endpoint + '/model', {
        method: 'POST', redirect: 'error', signal: options.signal,
        headers: {...attributionHeaders(), 'Content-Type': 'application/json',
          Authorization: `Bearer ${this.token}`},
        body: JSON.stringify({messages: projectMessages(options.messages)}),
      });
      if (!response.ok) throw failed();
      const raw = await response.text();
      if (Buffer.byteLength(raw, 'utf8') > 64000) throw failed();
      const envelope = JSON.parse(raw);
      if (!exactKeys(envelope, ['ok', 'result']) || envelope.ok !== true) throw failed();
      const chunks = decisionChunks(envelope.result);
      for (const chunk of chunks) {
        options.signal?.throwIfAborted();
        yield chunk;
      }
    } catch (error) {
      if (options.signal?.aborted) throw new DOMException('人格阅读任务已取消。', 'AbortError');
      throw failed();
    }
  }
}

export function apply(ctx) {
  ctx.llm.registerAdapter([PROVIDER], new PersonaAdapter(
    process.env.VTUBER_DSH_BRIDGE, process.env.VTUBER_DSH_TOKEN));
  ctx.provide('personaProvider', true);
}
