import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from brain.agent_tasks import AgentTasks
from brain.config import AgentTasksConfig, LLMConfig, WebUIConfig
from brain.drawing_session import DrawingSessions
from brain.task_actions import task_action
from brain.webui import WebUI
from brain.events import Event

DRAWING = {'version':1,'width':1000,'height':750,'background':'#fffaf0','title':'草稿',
           'strokes':[{'phase':'轮廓','color':'#123456','width':3,'duration':2000,
                       'points':[[10,10,.5],[100,100,1]]}]}


class DrawingWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = AgentTasksConfig(enabled=True)
        self.manager = AgentTasks(self.root, self.cfg, LLMConfig())
        self.manager._enqueue_submission = lambda job: None  # No external executor in protocol tests.
        self.now = 10.
        self.manager.drawing_sessions = DrawingSessions(self.manager, clock=lambda:self.now)
        self.job = self.manager.submit('画一只猫', 'drawing', owner=True)
        self.ready(self.job)

    def ready(self, job, title='草稿'):
        folder = self.manager.folder / job['id']
        folder.mkdir(exist_ok=True)
        (folder / 'drawing.json').write_text(json.dumps({**DRAWING,'title':title}), 'utf-8')
        self.manager.update(job, state='running')
        self.manager.update(job, state='succeeded', artifacts=['drawing.json'])

    async def asyncTearDown(self):
        await self.manager.close()
        self.temp.cleanup()

    async def test_sketch_edit_then_confirm_final_preserves_previous_artifact(self):
        self.assertEqual(self.job['drawing_stage'], 'sketch')
        preview = await self.manager.amend(self.job['id'], '眼睛小一点')
        revision = await self.manager.amend(self.job['id'], '眼睛小一点', token=preview['token'])
        self.assertEqual(revision['drawing_stage'], 'sketch')
        self.assertEqual(revision['drawing_root'], self.job['id'])
        self.ready(revision, '改过的草稿')
        preview = await self.manager.finish_drawing(revision['id'], '换成夜景')
        self.assertEqual(len(self.manager.jobs), 2, 'preview must not start another task')
        final = await self.manager.finish_drawing(revision['id'], '换成夜景', token=preview['token'])
        self.assertEqual(final['drawing_stage'], 'final')
        self.assertIn('眼睛小一点', final['prompt'])
        self.assertIn('换成夜景', final['prompt'])
        self.assertEqual(json.loads((self.manager.folder / self.job['id'] / 'drawing.json').read_text('utf-8'))['title'], '草稿')
        with self.assertRaises(ValueError):
            await self.manager.finish_drawing(revision['id'], '换成夜景', token=preview['token'])

    async def test_two_pages_share_timeline_and_stale_commands_cannot_rewind_it(self):
        state = await task_action(self.manager, {'action':'drawing_state','id':self.job['id']})
        command = {'action':'drawing_playback','id':self.job['id'], **state['playback'],
                   'version':state['drawing']['version'],'operation':'play'}
        await task_action(self.manager, command)
        self.now += .75
        second_page = self.manager.drawing_sessions.state(self.job['id'])
        self.assertEqual(second_page['playback']['position'], 750)
        with self.assertRaises(ValueError):
            await task_action(self.manager, {**command,'operation':'restart'})
        paused = await task_action(self.manager, {**command,**second_page['playback'],'operation':'pause'})
        self.now += 2
        self.assertEqual(self.manager.drawing_sessions.state(self.job['id'])['playback']['position'], 750)
        self.assertFalse(paused['playback']['playing'])

    async def test_chat_can_request_coloring_and_still_requires_confirmation(self):
        event = Event('companion', 'chat', '草稿可以，继续上色，换成夜景', user='你')
        event.owner_control = True
        await self.manager.handle_event(event)
        self.assertIn('确认修改任务', event.agent_reply)
        self.assertEqual(len(self.manager.jobs), 1)
        confirm = Event('companion', 'chat', '确认修改任务', user='你')
        confirm.owner_control = True
        await self.manager.handle_event(confirm)
        self.assertEqual(self.manager.jobs[-1]['drawing_stage'], 'final')
        self.assertEqual(self.manager.jobs[-1]['parent_id'], self.job['id'])
        with self.assertRaises(ValueError):
            await self.manager.finish_drawing(self.job['id'], requester='danmaku:someone-else')

    async def test_next_stage_switches_both_pages_and_rejects_old_version(self):
        old = self.manager.drawing_sessions.state(self.job['id'])
        preview = await self.manager.finish_drawing(self.job['id'])
        final = await self.manager.finish_drawing(self.job['id'], token=preview['token'])
        waiting = self.manager.drawing_sessions.state(self.job['id'])
        self.assertEqual(waiting['drawing']['job_id'], self.job['id'])
        self.assertEqual(waiting['latest']['id'], final['id'])
        self.ready(final, '夜景成稿')
        updated = self.manager.drawing_sessions.state(final['id'])
        self.assertEqual(updated, self.manager.drawing_sessions.state(self.job['id']))
        self.assertEqual(updated['drawing']['stage'], 'final')
        self.assertFalse(updated['playback']['playing'])
        with self.assertRaises(ValueError):
            self.manager.drawing_sessions.control(self.job['id'], {**old['playback'], 'version':old['drawing']['version'],'operation':'play'})

    async def test_routes_and_report_are_read_only_and_enforce_same_origin(self):
        ui = WebUI(WebUIConfig(), asyncio.Queue(), str(self.root), {})
        ui.agent_tasks = self.manager
        app = web.Application()
        app.router.add_get('/drawing-state/{job_id}', ui._drawing_state)
        app.router.add_get('/drawing-data/{job_id}', ui._drawing_data)
        app.router.add_post('/tasks-action', ui._tasks_action)
        app.router.add_get('/experience-report', ui._experience_report)
        async with TestClient(TestServer(app)) as client:
            r = await client.get('/drawing-state/'+self.job['id'])
            self.assertEqual(r.status, 200)
            self.assertEqual((await r.json())['drawing']['stage'], 'sketch')
            report = await client.get('/experience-report')
            self.assertNotIn('prompt', await report.text())
            blocked = await client.post('/tasks-action', json={'action':'finish_drawing','id':self.job['id']}, headers={'Origin':'https://foreign.invalid'})
            self.assertEqual(blocked.status, 403)
        self.assertEqual(len(self.manager.jobs), 1)

    async def test_restart_retains_stages_but_does_not_resume_canvas(self):
        state = self.manager.drawing_sessions.state(self.job['id'])
        self.manager.drawing_sessions.control(self.job['id'], {**state['playback'],'version':state['drawing']['version'],'operation':'play'})
        await self.manager.close()
        self.manager = AgentTasks(self.root, self.cfg, LLMConfig())
        state = await task_action(self.manager, {'action':'drawing_state','id':self.job['id']})
        self.assertFalse(state['playback']['playing'])
        self.assertEqual(state['drawing']['stage'], 'sketch')

    async def test_failed_final_retry_receives_last_successful_sketch(self):
        preview = await self.manager.finish_drawing(self.job['id'])
        final = await self.manager.finish_drawing(self.job['id'], token=preview['token'])
        self.manager.update(final, state='failed', detail='测试中断')
        preview = await self.manager.amend(final['id'], '重新完成上色')
        retry = await self.manager.amend(final['id'], '重新完成上色', token=preview['token'])
        self.manager.update(retry, state='running')
        folder = self.manager.folder / retry['id']; folder.mkdir()
        (folder / 'drawing.json').write_text(json.dumps(DRAWING), 'utf-8')
        rpc = AsyncMock(); rpc.events = asyncio.Queue()
        rpc.call.side_effect = [{'thread':{'id':'thread'}}, {'turn':{'id':'turn'}}, {}]
        result = {'completed':True,'summary':'已生成','artifacts':['drawing.json'],'verification':'本地测试回执'}
        rpc.events.put_nowait({'method':'item/completed','params':{'threadId':'thread','item':{'type':'agentMessage','text':json.dumps(result)}}})
        rpc.events.put_nowait({'method':'turn/completed','params':{'threadId':'thread','turn':{'status':'completed'}}})
        await self.manager.execute_astra(rpc, retry, folder)
        base = json.loads((folder/'drawing-base.json').read_text('utf-8'))
        self.assertEqual(base, DRAWING)
        prompt = rpc.call.await_args_list[1].args[1]['input'][0]['text']
        self.assertIn('drawing-base.json', prompt)
        self.assertIn('第二阶段', prompt)
