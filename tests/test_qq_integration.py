"""Cross-language QQ host checks; no external account or model request."""
import asyncio
import io
import json
import shutil
import subprocess
import os
from pathlib import Path
import tempfile
import unittest
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestServer

from brain.config import PersonaConfig, PersonaLLMConfig, LLMConfig
from brain.persona import Persona
from brain.qq_channel import QQChannel


ROOT = Path(__file__).resolve().parents[1]
SIMULATOR = r"""
import {QQTransport, createHost} from '__TRANSPORT__';
import {createInterface} from 'node:readline';
import assert from 'node:assert/strict';
const lines = createInterface({input: process.stdin});
const [line] = await (async () => { for await (const line of lines) return [line]; })();
const input = JSON.parse(line);
const sent = [];
const uploads = [];
class Bot {
  constructor() {
    this.handlers = {};
    this.tokenManager = {getAccessToken: async () => 'fixture-token'};
    this.apiClient = {request: async (token, method, path, data) => {
      assert.equal(token, 'fixture-token'); assert.equal(method, 'POST');
      assert.equal(path, input.group ? '/v2/groups/TEST_GROUP/files' : '/v2/users/OWNER/files');
      assert.equal(data.file_type, 3); assert.equal(data.file_name, 'reply.wav');
      assert.equal(data.srv_send_msg, false);
      const audio = Buffer.from(data.file_data, 'base64');
      assert.equal(audio.toString('ascii', 0, 4), 'RIFF');
      assert.equal(audio.toString('ascii', 8, 12), 'WAVE');
      uploads.push(audio.length);
      return {file_info: 'fixture-uploaded-voice'};
    }};
    this.mediaApi = {sendMediaMessage: async (scope, targetId, fileInfo, credentials, options) => {
      assert.equal(fileInfo, 'fixture-uploaded-voice');
      sent.push({target: {scope, targetId, msgId: options.msgId}, voice: true});
      return {id: 'ROBOT1.0_voice-' + sent.length + '!'};
    }};
  }
  getCredentials() { return {appId: '123', clientSecret: 'fixture-secret'}; }
  on(name, handler) { this.handlers[name] = handler; }
  async start() { this.handlers.ready(); }
  stop() {}
  async sendText(target, text) { sent.push({target, text}); return {id: 'ROBOT1.0_sent-' + sent.length + '!'}; }
}
const transport = new QQTransport({QQBot: Bot, host: createHost(input.endpoint, input.token),
  startQrConnect() { throw new Error('No real QR is allowed in this test'); }});
try {
  await transport.command({id:'test-connect', type:'connect', credentials:{appId:'123', appSecret:'fixture-secret'}});
  await transport.events;
  const message = (id, text, peer='OWNER') => ({kind:'c2c', senderId:peer, messageId:id, content:text,
    replyTarget:{scope:'c2c', targetId:peer, msgId:id}});
  const feed = message => transport.inbound(transport.bot, '123', message, transport.connection.signal);
  if (input.group) {
    const groupMessage = (id, text, peer='MEMBER') => ({kind:'group', rawEventType:'GROUP_AT_MESSAGE_CREATE',
      senderId:peer, groupOpenid:'TEST_GROUP', messageId:id, content:text,
      replyTarget:{scope:'group',targetId:'TEST_GROUP',msgId:id}});
    await feed({...groupMessage('ordinary', '<@123> FAKE_AT_NO_REPLY'), rawEventType:'GROUP_MESSAGE_CREATE'});
    await feed(groupMessage('group-first!', '<@123> GROUP_MEMBER_ONE_PRIVATE_CONTEXT'));
    await feed(groupMessage('group-first!', '<@123> GROUP_MEMBER_ONE_PRIVATE_CONTEXT'));
    await feed(groupMessage('group-second!', '<@123> GROUP_MEMBER_TWO_QUESTION', 'SECOND_MEMBER'));
    await feed(message('unpaired-private', 'PRIVATE_UNPAIRED_SHOULD_NOT_ENTER'));
    assert.equal(sent.length, 2);
    assert(sent.every(item => item.target.scope === 'group' && item.target.targetId === 'TEST_GROUP'));
    assert.equal(sent[0].target.msgId, 'group-first!');
    assert.equal(sent[1].target.msgId, 'group-second!');
    assert.equal(uploads.length, input.reply_mode === 'voice' ? 2 : 0);
    process.stdout.write(JSON.stringify({ok:true,sent:sent.length}));
  } else {
  await feed(message('ROBOT1.0_pair!', input.code));
  await feed(message('ROBOT1.0_first!', '我们聊聊明天的散步计划'));
  await feed(message('ROBOT1.0_first!', '我们聊聊明天的散步计划'));
  await feed(message('stranger', 'PRIVATE_OTHER', 'OTHER'));
  await feed(message('ROBOT1.0_second!', '这个话题先到这里'));
  assert.equal(sent.length, 3);
  assert.equal(sent[1].target.msgId, 'ROBOT1.0_first!');
  assert.equal(sent[2].target.msgId, 'ROBOT1.0_second!');
  assert(sent.every(item => item.target.targetId === 'OWNER'));
  if (input.reply_mode === 'voice') {
    assert.equal(uploads.length, 2);
    assert.equal(sent[0].voice, undefined, 'pairing is always text');
    assert.equal(sent[1].voice, true); assert.equal(sent[2].voice, true);
  } else { assert.equal(uploads.length, 0); }
  process.stdout.write(JSON.stringify({ok:true,sent:sent.length}));
  }
} finally { transport.stop(); }
"""


@unittest.skipUnless(shutil.which('node'), 'Node is needed for the QQ bridge')
class QQIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.memory = Mock(viewer_note=Mock(return_value='本人历史'))
        self.prompts = []
        async def stream(system, user):
            self.prompts.append((system, user))
            yield '好，我们就从这里接着聊。' if len(self.prompts) == 1 else '好，这个话题先放下。'
        self.backend = Mock(available=True, complete=None, stream=stream, close=AsyncMock())
        with patch('brain.gemini_chat.GeminiChat', return_value=self.backend):
            self.persona = Persona(PersonaConfig(name='助手'), LLMConfig(), self.memory,
                                   persona_llm=PersonaLLMConfig(backend='gemini_api'))
        self.persona._recent_replies.append('PUBLIC_ONLY_MARKER')
        self.channel = QQChannel(Path(temporary.name), self.persona)
        self.addAsyncCleanup(self.channel.close)

    async def test_node_transport_python_host_persona_and_sent_receipt(self):
        await self.exercise_bridge('text')

    async def test_node_voice_download_upload_receipt_and_private_history(self):
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(b'\x00\x01' * 2400)
        voice = SimpleNamespace(available=True, render=AsyncMock(return_value=buffer.getvalue()), close=AsyncMock())
        self.channel.voice = voice
        await self.channel.action({'action': 'reply_mode', 'reply_mode': 'voice'})
        await self.exercise_bridge('voice')
        self.assertEqual(voice.render.await_count, 2)
        self.assertFalse(self.channel._pending)
        self.assertIn('作为 QQ 语音读给对方听', self.prompts[0][0])

    async def test_group_mentions_route_without_private_pairing_or_memory(self):
        await self.exercise_bridge('text', group=True)

    async def test_group_voice_uses_private_download_then_group_upload_and_receipt(self):
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(24000)
            wav.writeframes(b'\x00\x01' * 2400)
        self.channel.voice = SimpleNamespace(available=True, render=AsyncMock(return_value=buffer.getvalue()), close=AsyncMock())
        await self.channel.action({'action':'reply_mode','reply_mode':'voice'})
        await self.exercise_bridge('voice', group=True)
        self.assertEqual(self.channel.voice.render.await_count, 2)

    async def exercise_bridge(self, reply_mode, group=False):
        self.channel.credentials = {'appId': '123', 'appSecret': 'fixture-secret'}
        await self.channel.event({'type': 'connected', 'app_id': '123'})
        state = await self.channel.action({'action': 'pair', 'owner_name': '你'})
        self.channel._token = 'local-only-integration-token'
        server = TestServer(self.channel._application())
        await server.start_server()
        self.addAsyncCleanup(server.close)
        code = SIMULATOR.replace('__TRANSPORT__', (ROOT / 'tools/qq-harness/transport.mjs').as_uri())
        process = await asyncio.create_subprocess_exec('node', '--input-type=module', '-e', code,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
        try:
            payload = {'endpoint': str(server.make_url('/')).rstrip('/'),
                       'token': self.channel._token, 'code': state['pair_code'], 'reply_mode': reply_mode, 'group':group}
            stdout, stderr = await asyncio.wait_for(process.communicate(json.dumps(payload).encode() + b'\n'), 20)
            self.assertEqual(process.returncode, 0, stderr.decode(errors='replace'))
            self.assertEqual(json.loads(stdout), {'ok': True, 'sent': 2 if group else 3})
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        self.assertEqual(len(self.prompts), 2)
        self.assertIn('助手', self.prompts[0][0])
        if group:
            self.assertIn('QQ 群聊', self.prompts[0][0])
            for forbidden in ('PUBLIC_ONLY_MARKER', 'PRIVATE_UNPAIRED_SHOULD_NOT_ENTER', 'FAKE_AT_NO_REPLY', '本人历史'):
                self.assertNotIn(forbidden, str(self.prompts))
            self.assertNotIn('GROUP_MEMBER_ONE_PRIVATE_CONTEXT', self.prompts[1][1])
            self.memory.viewer_note.assert_not_called()
            self.assertIsNone(self.channel.state['owner'])
            self.assertIsNone(self.channel._session)
            self.assertFalse((self.channel.folder / 'conversation.json').exists())
            paths = list((self.channel.folder / 'groups').glob('*.json'))
            self.assertEqual(len(paths), 2)
            for path in paths:
                history = json.loads(path.read_text('utf-8'))
                self.assertEqual(len(history['history']), 1)
                self.assertTrue(history['history'][0]['reply'])
            self.assertFalse(self.channel._pending)
            return
        self.assertIn('QQ 一对一文字私聊', self.prompts[0][0])
        self.assertIn('明天的散步计划', self.prompts[1][1])
        self.assertNotIn('PUBLIC_ONLY_MARKER', str(self.prompts))
        self.assertNotIn('PRIVATE_OTHER', str(self.prompts))
        self.assertEqual(list(self.persona._recent_replies), ['PUBLIC_ONLY_MARKER'])
        self.memory.record.assert_not_called()
        history = json.loads((self.channel.folder / 'conversation.json').read_text('utf-8'))
        self.assertEqual(len(history['history']), 2)
        self.assertTrue(all(row['reply'] for row in history['history']))

    async def test_host_starts_actual_dsh_without_binding_or_model_call(self):
        # Resolve installed code from the repository, but keep every data path temporary.
        self.channel.root = ROOT
        if not self.channel.readiness():
            self.skipTest('Pinned QQ and DSH dependencies are not installed')
        await self.channel._start_runtime()
        self.assertTrue(self.channel._ready.done())
        self.assertIsNone(self.channel._process.returncode)
        self.assertFalse(self.channel.connected)
        self.assertTrue(self.channel._commands.empty())
        self.assertFalse(self.prompts)
        await self.channel.close()
        self.assertIsNone(self.channel._process)
        self.assertIsNone(self.channel._server)


if __name__ == '__main__':
    unittest.main()
