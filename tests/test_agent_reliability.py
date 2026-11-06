import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from brain.agent_tasks import AgentTasks
from brain.code_review import CodeReview, is_test_command
from brain.config import AgentTasksConfig,LLMConfig,PlaybackConfig
from brain.playback import SpeechController,SpeechItem
from brain.tool_registry import ToolRegistry

class ReliabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.events=asyncio.Queue()
        self.m=AgentTasks(self.root,AgentTasksConfig(enabled=True),LLMConfig(),self.events)
    async def asyncTearDown(self):
        await self.m.close();self.tmp.cleanup()
    def job(self):
        return self.m.submit('test','research',owner=False,user='viewer',requester='danmaku:bili:1',origin={'source':'danmaku'})
    async def test_amend_creates_linked_scoped_revision_and_suppresses_old_notice(self):
        old=self.job()
        self.m.approve(old['id']);self.m.update(old,state='running')
        self.m.update(old,state='succeeded')
        notice=self.events.get_nowait()
        with self.assertRaises(ValueError): await self.m.amend(old['id'],'change',requester='danmaku:bili:2')
        preview=await self.m.amend(old['id'],'change',requester='danmaku:bili:1')
        child=await self.m.amend(old['id'],'change',requester='danmaku:bili:1',token=preview['token'])
        self.assertEqual(child['parent_id'],old['id'])
        self.assertEqual(child['state'],'pending')
        self.assertFalse(self.m.refresh_receipt(notice))
        self.assertIn('change',child['prompt'])
    async def test_notification_failure_retries_once_and_finished_is_separate(self):
        j=self.job();self.m.approve(j['id']);self.m.update(j,state='running');self.m.update(j,state='review')
        self.events.get_nowait()
        p=SpeechController(PlaybackConfig());p.item_valid=self.m.speech_valid
        p.on_dispatch=self.m.speech_dispatched;p.on_state=self.m.speech_state
        def item(id): return SpeechItem(id,'/a.wav','done','平静',[],{'task_id':j['id'],'task_revision':j['revision'],'task_notice':True},10)
        p.enqueue(item(1));self.assertEqual(j['delivery']['state'],'dispatched')
        p.audio_started(1);self.assertEqual(j['delivery']['state'],'playing')
        p.audio_finished(1,'error');self.assertEqual(j['delivery']['state'],'failed')
        j['delivery']['retry_at']=1;self.m.notification_tick()
        retry=self.events.get_nowait();self.assertTrue(self.m.refresh_receipt(retry))
        p.enqueue(item(2));p.audio_started(2);p.audio_finished(2,'error')
        self.assertNotIn('retry_at',j['delivery']);self.assertEqual(j['delivery']['failures'],2)
        self.m.notification_tick();self.assertTrue(self.events.empty())
        # A new task state gets a new delivery lifecycle.
        self.m.update(j,state='applied');p.enqueue(item(3));p.audio_started(3);p.audio_finished(3)
        self.assertEqual(j['delivery']['state'],'played')
    async def test_budget_and_remote_work_during_foreground_activity(self):
        j=self.job();self.m.cfg.max_tool_calls=1
        self.m.spend(j,'tool_calls')
        with self.assertRaises(RuntimeError):self.m.spend(j,'tool_calls')
        self.m.resource_busy=lambda:{'local_gpu':'语音正在使用显卡', 'desktop':'游戏控制已开启'}
        self.m.execute=AsyncMock();self.m.approve(j['id'])
        await asyncio.wait_for(self.m.queue.join(),1)
        self.m.execute.assert_awaited_once_with(j)

    async def test_amend_preview_does_not_cancel_and_rejects_stale_confirmation(self):
        j=self.job()
        p=await self.m.amend(j['id'],'change',requester=j['requester'])
        self.assertEqual(j['state'],'pending')
        self.assertEqual(len(self.m.jobs),1)
        self.m.approve(j['id']);self.m.update(j,state='running');self.m.update(j,state='succeeded')
        with self.assertRaisesRegex(ValueError,'重新预览'):
            await self.m.amend(j['id'],'change',requester=j['requester'],token=p['token'])
        self.assertEqual(len(self.m.jobs),1)

    async def test_manual_acceptance_requires_current_boot_and_records_failure_honestly(self):
        review,report=self.applied()
        health={'scope':'main','ready':True,'boot_id':'current','code':report['source_version']}
        client=AsyncMock();client.__aenter__.return_value=client
        client.get.return_value=SimpleNamespace(raise_for_status=lambda:None,json=lambda:health)
        with patch('httpx.AsyncClient',return_value=client):
            with self.assertRaises(ValueError):await self.m.accept_code('code','click','sound','silence',False)
            await self.m.verify_code('code')
            result=await self.m.accept_code('code','click','sound','silence',False)
            self.assertFalse(result['acceptance']['passed'])
            self.assertEqual(result['acceptance']['source'],'owner_manual')
            health['boot_id']='different'
            with self.assertRaises(ValueError):await self.m.accept_code('code','click','sound','sound',True)
        self.assertEqual(len(self.m.get('code')['acceptance']),1)
    async def test_restart_does_not_replay_failed_notification(self):
        j=self.job();self.m.approve(j['id']);self.m.update(j,state='running');self.m.update(j,state='succeeded')
        j['delivery']={'state':'playing','revision':j['revision'],'retry_at':1}
        self.m.save();await self.m.close()
        self.m=AgentTasks(self.root,AgentTasksConfig(enabled=True),LLMConfig(),asyncio.Queue())
        restored=self.m.get(j['id'])
        self.assertNotIn('retry_at',restored['delivery'])
        self.assertEqual(restored['delivery']['state'],'failed')
        self.m.notification_tick();self.assertTrue(self.m.event_queue.empty())
    async def test_registry_schema_permission_and_readiness(self):
        r=ToolRegistry();handler=AsyncMock(return_value='receipt')
        r.add('test','test',{'action':{'type':'string','required':True,'enum':['observe']}},handler)
        with self.assertRaises(ValueError):await r.call('test',{'action':'observe'})
        with self.assertRaises(ValueError):await r.call('test',{'action':'delete'},owner=True)
        handler.assert_not_called()
        result=await r.call('test',{'action':'observe'},owner=True)
        self.assertTrue(result['ok']);self.assertEqual(result['result'],'receipt')
    def applied(self):
        folder=self.m.folder/'code';folder.mkdir()
        review=CodeReview(self.root,folder,'code');copy=review.prepare()
        (copy/'tools').mkdir();(copy/'tools/new.py').write_text('x=1\n')
        report=review.inspect([{'test':True,'command':'python -m unittest tests.test_new','cwd':str(review.copy),'exit_code':0,'source_before':review.version(),'source_after':review.version(),'output':'Ran 1 test in 0.001s\nOK\n'}])
        review.apply(review.public()['token'])
        self.m.jobs.append({'id':'code','kind':'code','state':'applied','user':'you','revision':1})
        return review,report
    async def test_verified_requires_main_snapshot_and_bad_version_rolls_back(self):
        review,report=self.applied()
        response=SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'scope':'main','ready':True,'boot_id':'new','code':{'tools/new.py':report['changes'][0]['after']}})
        client=AsyncMock();client.__aenter__.return_value=client;client.get.return_value=response
        with patch('httpx.AsyncClient',return_value=client):
            self.assertTrue((await self.m.verify_code('code'))['verified'])
        self.assertEqual(self.m.get('code')['state'],'verified')
        self.m.rollback_code('code');self.assertFalse((self.root/'tools/new.py').exists())
    async def test_bad_health_preserves_files_and_waits_for_verification(self):
        self.applied()
        response=SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'scope':'main','ready':True,'code':{}})
        client=AsyncMock();client.__aenter__.return_value=client;client.get.return_value=response
        with patch('httpx.AsyncClient',return_value=client):
            result=await self.m.verify_code('code')
        self.assertFalse(result['verified'])
        self.assertEqual(self.m.get('code')['state'],'applied')
        self.assertTrue((self.root/'tools/new.py').exists())
    async def test_rollback_conflict_preserves_user_changes(self):
        review,_=self.applied();(self.root/'tools/new.py').write_text('x=99\n')
        with self.assertRaises(ValueError):self.m.rollback_code('code')
        self.assertEqual((self.root/'tools/new.py').read_text(),'x=99\n')
    async def test_real_test_output_beats_quoted_report_commands(self):
        folder=self.root/'task';folder.mkdir();r=CodeReview(self.root,folder,'test-output');copy=r.prepare()
        (copy/'tools').mkdir();(copy/'tools/x.py').write_text('x=1\n')
        report_command="Set-Content -LiteralPath 'report.txt' -Value 'python -m unittest tests.test_x'"
        self.assertFalse(is_test_command(report_command))
        report=r.inspect([
          {'test':True,'command':'python -m unittest tests.test_x','exit_code':1,'output':'UnicodeDecodeError'},
          {'test':True,'command':'venv/python -m unittest tests.test_x','cwd':str(r.copy),'exit_code':0,'source_before':r.version(),'source_after':r.version(),'output':'Ran 2 tests in 0.001s\nOK\n'},
          {'test':is_test_command(report_command),'command':report_command,'exit_code':0,'output':''}])
        self.assertTrue(report['tests_passed'])

if __name__=='__main__':unittest.main()
