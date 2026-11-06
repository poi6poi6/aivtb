import {QQBot} from '@tencent-connect/qqbot-nodejs';
import {startQrConnect} from '@tencent-connect/qqbot-connector';
import {QQTransport, createHost} from './transport.mjs';
import {createDiagnosticWriter} from './diagnostics.mjs';
import {resolve} from 'node:path';

export const name = 'vtuber-qq-channel';
export function apply(ctx) {
  // The host supplies this runtime's workspace, never an inbound QQ message.
  // Keep diagnostics beside that workspace so isolated tests cannot overwrite live status.
  const diagnostic = createDiagnosticWriter(resolve(process.cwd(), '..', 'transport-status.json'));
  diagnostic({event: 'starting'});
  const channel = new QQTransport({QQBot, startQrConnect,
    diagnostic,
    host: createHost(process.env.VTUBER_QQ_BRIDGE, process.env.VTUBER_QQ_TOKEN)});
  ctx.on('dispose', () => channel.stop());
  void channel.run();
  ctx.provide('qqBridge', true);
}
