import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from brain.agent_tasks import AgentTasks
from brain.config import AgentTasksConfig, LLMConfig
from brain.events import Event
from brain.task_intent import IntentDecision
from brain.arbiter import Arbiter


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.manager=AgentTasks(Path(self.tmp.name),AgentTasksConfig(enabled=True),LLMConfig())
        self.manager.intent_classifier.classify=AsyncMock(return_value=IntentDecision(action='chat'))

    async def asyncTearDown(self):
        await self.manager.close()
        self.tmp.cleanup()

    def job(self,prompt='画庭院',kind='drawing',key='a'):
        return self.manager.submit(prompt,kind,user='同名观众',requester='danmaku:'+key)

    def event(self,text,key='a'):
        return Event('danmaku','chat',text,user='同名观众',viewer_key=key)

    async def resolve(self,text,decision,key='a'):
        self.manager.intent_classifier.classify.return_value=decision
        event=self.event(text,key)
        await self.manager.handle_event(event)
        await self.manager.resolve_task_intent(event)
        return event

    async def test_semantic_call_deferred_until_attention_selection(self):
        self.job()
        event=self.event('背景换成夜晚')
        await self.manager.handle_event(event)
        self.assertTrue(event.task_intent_pending)
        self.manager.intent_classifier.classify.assert_not_awaited()
        await self.manager.resolve_task_intent(event)
        self.manager.intent_classifier.classify.assert_awaited_once()

    async def test_natural_revision_preserves_original_until_confirmation(self):
        job=self.job()
        event=await self.resolve('背景换成夜晚',IntentDecision(action='amend',target_id=job['id'],kind='drawing'))
        self.assertIn('确认',event.agent_reply)
        self.assertEqual(job['state'],'pending')
        confirm=self.event('就这么改')
        await self.manager.handle_event(confirm)
        self.assertEqual(job['state'],'cancelled')
        child=self.manager.get(confirm.task_id)
        self.assertEqual(child['parent_id'],job['id'])
        self.assertIn('背景换成夜晚',child['prompt'])
        self.assertEqual(child['state'],'pending')

    async def test_ambiguous_cancel_selection_is_viewer_scoped(self):
        first=self.job('画花');self.manager.approve(first['id']);self.manager.update(first,state='running')
        second=self.job('画庭院')
        event=await self.resolve('先别画了',IntentDecision(action='cancel',ambiguous=True,candidates=(first['id'],second['id'])))
        self.assertIn('哪一项',event.agent_reply)
        foreign=self.event('2','b');await self.manager.handle_event(foreign)
        self.assertEqual(second['state'],'pending')
        choice=self.event('第二个');await self.manager.handle_event(choice)
        self.assertEqual(second['state'],'cancelled')
        self.assertEqual(first['state'],'running')

    async def test_resume_cancelled_work_creates_reviewable_new_revision(self):
        job=self.job();await self.manager.cancel(job['id'])
        event=await self.resolve('继续刚才那个',IntentDecision(action='resume',target_id=job['id']))
        self.assertIn('修订',event.agent_reply)
        self.assertEqual(len(self.manager.jobs),1)
        confirm=self.event('确认继续');await self.manager.handle_event(confirm)
        self.assertEqual(self.manager.get(confirm.task_id)['parent_id'],job['id'])

    async def test_foreign_target_stale_target_and_classifier_failure_never_mutate(self):
        foreign=self.job(key='b');own=self.job()
        e=await self.resolve('先别画了',IntentDecision(action='cancel',target_id=foreign['id']))
        self.assertFalse(e.task_id)
        self.assertEqual(foreign['state'],'pending')
        async def delayed(text,jobs):
            await self.manager.cancel(own['id'])
            return IntentDecision(action='resume',target_id=own['id'])
        self.manager.intent_classifier.classify.side_effect=delayed
        e=self.event('继续刚才那个');await self.manager.handle_event(e);await self.manager.resolve_task_intent(e)
        self.assertIn('状态已变化',e.agent_reply)
        self.assertEqual(len(self.manager.jobs),2)
        self.manager.intent_classifier.classify.side_effect=None
        e=await self.resolve('背景换成夜晚',IntentDecision(reason='timeout'))
        self.assertIn('还没有更改',e.agent_reply)
        self.assertEqual(len(self.manager.jobs),2)

    async def test_semantic_submission_is_approval_gated_and_preserves_request(self):
        text='能不能替我做一张庭院海报'
        e=await self.resolve(text,IntentDecision(action='submit',kind='image',instruction='not trusted'))
        job=self.manager.get(e.task_id)
        self.assertEqual(job['prompt'],text)
        self.assertEqual(job['state'],'pending')
        self.assertEqual(job['requester'],'danmaku:a')

    async def test_intent_message_is_not_merged_into_another_chat(self):
        self.job()
        modify=self.event('背景换成夜晚');await self.manager.handle_event(modify)
        plain=self.event('你好')
        # Exercise ordinary-chat aggregation first without depending on the
        # resolution of the wall clock while constructing the two fixtures.
        plain.ts = modify.ts - .01
        arbiter=Arbiter(danmaku_cooldown_sec=0)
        arbiter.push(plain);arbiter.push(modify)
        self.assertIs(arbiter.pick(False),plain)
        self.assertEqual(plain.related_messages,[])
        self.assertIs(arbiter.pick(False),modify)

    async def test_empty_kind_candidates_never_expand_to_other_kinds(self):
        job=self.job('给网页加时钟','code')
        e=await self.resolve('先别画了',IntentDecision(action='cancel',kind='drawing',ambiguous=True,candidates=()))
        self.assertIn('没有找到',e.agent_reply)
        self.assertNotIn('时钟',e.agent_reply)
        await self.manager.handle_event(self.event('第一个'))
        self.assertEqual(job['state'],'pending')

    async def test_new_preview_beats_older_inflight_classification(self):
        job=self.job()
        async def delayed(text,jobs):
            newer=self.event('修改刚才那个任务：改成红色')
            await self.manager.handle_event(newer)
            return IntentDecision(action='amend',target_id=job['id'],kind='drawing')
        self.manager.intent_classifier.classify.side_effect=delayed
        event=self.event('背景换成夜晚')
        await self.manager.handle_event(event);await self.manager.resolve_task_intent(event)
        self.assertIn('最新预览',event.agent_reply)
        self.assertEqual(job['amend_preview']['impact']['instruction'],'改成红色')

    async def test_declining_preview_invalidates_legacy_confirmation_too(self):
        job=self.job()
        await self.resolve('背景换成夜晚',IntentDecision(action='amend',target_id=job['id'],kind='drawing'))
        await self.manager.handle_event(self.event('算了'))
        self.assertNotIn('amend_preview',job)
        await self.manager.handle_event(self.event('确认修改任务'))
        self.assertEqual(len(self.manager.jobs),1)
        self.assertEqual(job['state'],'pending')

    async def test_references_and_capability_questions_do_not_use_submit_fastpath(self):
        self.job()
        for text in ('帮我画一张刚才那幅的夜景版','请帮我写代码是不是很慢'):
            event=self.event(text);await self.manager.handle_event(event)
            self.assertTrue(event.task_intent_pending)
            self.assertEqual(len(self.manager.jobs),1)
        self.manager.intent_classifier.classify.assert_not_awaited()


if __name__=='__main__':unittest.main()
