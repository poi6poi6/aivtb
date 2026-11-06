import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from brain.agent_tasks import AgentTasks
from brain.arbiter import Arbiter
from brain.config import AgentTasksConfig, LLMConfig, PlaybackConfig
from brain.events import Event
from brain.playback import SpeechController, SpeechItem
from brain.speech_pipeline import SpeechPipeline

class CoordinationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.queue = asyncio.Queue()
        self.manager = AgentTasks(Path(self.tmp.name), AgentTasksConfig(enabled=True), LLMConfig(), self.queue)

    async def asyncTearDown(self):
        await self.manager.close()
        self.tmp.cleanup()

    async def request(self, key='bili:1'):
        e = Event('danmaku','chat','帮我画一朵花',user='同名观众',viewer_key=key)
        e.ui_id = 123
        await self.manager.handle_event(e)
        return e, self.manager.get(e.task_id)

    def player(self):
        p=SpeechController(PlaybackConfig())
        p.item_valid=self.manager.speech_valid
        p.on_dispatch=self.manager.speech_dispatched
        return p

    def speech(self, e, id=20):
        return SpeechItem(id,'/audio/a.wav',e.agent_reply,'平静',[],
            {'task_id':e.task_id,'task_revision':e.task_revision,'task_notice':e.task_notice,'task_ts':e.ts},10)

    async def test_receipts_coalesce_without_interrupting_or_aggregation(self):
        e,j=await self.request()
        self.assertEqual(j['origin']['turn_id'],123)
        a=Arbiter(danmaku_cooldown_sec=0)
        a.push(e)
        self.manager.approve(j['id'])
        self.manager.update(j,state='running')
        self.manager.update(j,state='succeeded')
        notice=self.queue.get_nowait()
        a.push(notice)
        self.assertFalse(a.has_urgent())
        self.assertEqual(a.summary()['pending'],1)
        a.discard_unforced()
        chosen=a.pick(allow_idle=False)
        self.assertIs(chosen,notice)
        self.assertTrue(self.manager.refresh_receipt(chosen))
        self.assertIn('草稿',chosen.agent_reply)
        self.assertIn('确认草稿后再继续上色',chosen.agent_reply)
        self.assertFalse(chosen.forced)

    async def test_request_identity_query_and_scoped_cancel(self):
        e1,j1=await self.request()
        e2,j2=await self.request('bili:2')
        query=Event('danmaku','chat','刚才那个做好了吗',user='同名观众',viewer_key='bili:1')
        await self.manager.handle_event(query)
        self.assertEqual(query.task_id,j1['id'])
        forged=Event('danmaku','chat','取消任务 '+j2['id'],user='同名观众',viewer_key='bili:1')
        await self.manager.handle_event(forged)
        self.assertEqual(j2['state'],'pending')
        self.assertFalse(forged.task_id)
        cancel=Event('danmaku','chat','取消刚才那个',user='同名观众',viewer_key='bili:1')
        await self.manager.handle_event(cancel)
        self.assertEqual(j1['state'],'cancelled')
        self.assertIn('已取消',cancel.agent_reply)
        self.assertFalse(self.manager.refresh_receipt(e1))
        self.assertIn('已取消',self.manager.context_for(query))

    async def test_two_viewers_concurrent_requests_amend_confirm_and_cancel(self):
        (e1,j1),(e2,j2)=await asyncio.gather(self.request('bili:1'),self.request('bili:2'))
        async def say(key,text):
            e=Event('danmaku','chat',text,user='同名观众',viewer_key=key)
            await self.manager.handle_event(e)
            return e
        preview=await say('bili:1','修改刚才那个任务：改画玫瑰')
        self.assertIn('确认修改任务',preview.agent_reply)
        self.assertEqual(j1['state'],'pending')
        await say('bili:2','确认修改任务')
        self.assertEqual(len(self.manager.jobs),2)
        confirmed=await say('bili:1','确认修改任务')
        child=self.manager.get(confirmed.task_id)
        self.assertEqual(child['parent_id'],j1['id'])
        query=await say('bili:2','任务进度')
        self.assertEqual(query.task_id,j2['id'])
        await say('bili:2','取消刚才那个任务')
        self.assertEqual(j2['state'],'cancelled')
        self.assertEqual(child['state'],'pending')

    async def test_expired_and_superseded_notifications(self):
        e,j=await self.request()
        e.ts=time.time()-301
        self.assertFalse(self.manager.refresh_receipt(e))
        e.ts=time.time()
        self.manager.approve(j['id'])
        self.manager.update(j,state='running')
        self.assertFalse(self.manager.receipt_valid(e))
        self.assertTrue(self.manager.refresh_receipt(e))
        self.assertIn('正在执行',e.agent_reply)

    async def test_tts_inflight_cancellation_never_enqueues_old_audio(self):
        e,j=await self.request()
        started,release=asyncio.Event(),asyncio.Event()
        class Voice:
            async def speak(self,text,emotion):
                started.set(); await release.wait(); return 'a.wav'
        messages=[]
        async def broadcast(m): messages.append(m)
        p=self.player()
        pipe=SpeechPipeline(Voice(),p,broadcast)
        pipe.event_valid=self.manager.receipt_valid
        worker=asyncio.create_task(pipe.run())
        try:
            future=pipe.enqueue(turn_id=1,segment_index=0,text=e.agent_reply,emotion='平静',actions=[],event=e)
            await asyncio.wait_for(started.wait(),1)
            await self.manager.cancel(j['id'])
            release.set()
            self.assertTrue((await asyncio.wait_for(future,1)).cancelled)
            self.assertIsNone(p.current)
            self.assertFalse(any(m['type']=='audio' for m in messages))
        finally:
            worker.cancel(); await asyncio.gather(worker,return_exceptions=True)

    async def test_revalidate_waiting_audio_and_deduplicate_dispatch(self):
        e,j=await self.request()
        self.manager.approve(j['id'])
        self.manager.update(j,state='running')
        self.manager.update(j,state='review')
        self.manager.refresh_receipt(e)
        p=self.player()
        p.enqueue(SpeechItem(1,'a.wav','正在说话','平静',[],{},10))
        p.audio_started(1)
        p.enqueue(self.speech(e))
        self.assertEqual(p.current.id,1)
        self.manager.update(j,state='applied')
        messages=p.audio_finished(1)
        self.assertFalse(any(m['type']=='audio' for m in messages))
        self.manager.refresh_receipt(e)
        messages=p.enqueue(self.speech(e,21))
        self.assertTrue(any(m['type']=='audio' for m in messages))
        self.assertFalse(self.manager.refresh_receipt(e))
        self.assertTrue(self.manager.speech_valid(p.current))
        self.assertFalse(self.manager.speech_valid(self.speech(e,22)))

if __name__=='__main__': unittest.main()
