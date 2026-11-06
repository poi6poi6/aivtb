"""Offline subscription/web-receipt tests; no live search or account access."""
import asyncio
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from brain.astra_trends import (AstraTrends, AstraTrendsConfig, AstraTrendsError,
                                INSTRUCTIONS, MODEL, PROVIDER, _parse_topics)
from brain.codex_runtime import CodexNotFoundError


NOW = datetime(2026, 10, 29, 4, 0, tzinfo=timezone.utc)
URL = 'https://www.toei-anim.co.jp/news/20261029/series.html'


def topic(**changes):
    value = {'title': '动画制作花絮公开', 'category': 'anime',
             'summary': '发行方公开了本周的新制作花絮，介绍角色动作设计。',
             'talking_point': '你更喜欢夸张的动作还是细腻的小表情？',
             'published_at': '2026-10-29T01:00:00Z', 'non_political': True,
             'sources': [{'title': '动画制作团队公开花絮', 'url': URL,
                          'published_at': '2026-10-29T01:00:00Z'}]}
    value.update(changes)
    return value


class FakeRPC:
    def __init__(self, *, account='chatgpt', model=MODEL, remaining=80,
                 mode='live', status='completed', output=None, stall=False,
                 receipts=None, returned_model=MODEL, repair=None):
        self.account, self.model, self.remaining = account, model, remaining
        self.mode, self.status, self.stall = mode, status, stall
        self.returned_model = returned_model
        self.output = json.dumps({'topics': [topic()]}, ensure_ascii=False) if output is None else output
        self.receipts = receipts if receipts is not None else [
            {'type': 'webSearch', 'action': {'type': 'search', 'query': '动画 最近72小时'}},
            {'type': 'webSearch', 'action': {'type': 'openPage', 'url': URL}},
        ]
        self.events = asyncio.Queue()
        self.calls, self.sent = [], []
        self.closed = False
        self.folder = None
        self.repair = repair or {'output': '{"topics":[]}', 'receipts': []}
        self.turn_count = 0
        self.current_turn = 'turn1'

    async def call(self, method, params=None, **kwargs):
        self.calls.append((method, params))
        if method == 'account/read':
            return {'account': {'type': self.account, 'planType': 'fixture'}}
        if method == 'model/list':
            return {'data': [{'model': self.model}], 'nextCursor': None}
        if method == 'account/rateLimits/read':
            return {'rateLimits': {'primary': {'usedPercent': 100 - self.remaining}}
                    if self.remaining is not None else {}}
        if method == 'config/read':
            return {'config': {'web_search': self.mode, 'mcp_servers': {'private.files': {}},
                               'plugins': {'private@plugin': {}},
                               'hooks': {'PreToolUse': [{'hooks': [{'command': 'private'}]}]}}}
        if method == 'thread/start':
            self.folder = Path(params['cwd'])
            assert self.folder.is_dir() and not list(self.folder.iterdir())
            return {'thread': {'id': 'thread1'}, 'model': self.returned_model,
                    'modelProvider': PROVIDER}
        if method == 'turn/start':
            self.turn_count += 1
            self.current_turn = 'turn' + str(self.turn_count)
            plan = self.repair if self.turn_count > 1 else {
                'output': self.output, 'receipts': self.receipts, 'stall': self.stall, 'status': self.status}
            if not plan.get('stall', False):
                for message in plan.get('stale', []):
                    await self.events.put(message)
                for receipt in plan.get('receipts', []):
                    await self.emit('item/completed', item=receipt)
                await self.emit('item/completed', item={
                    'type': 'agentMessage', 'phase': 'analysis', 'text': 'hidden private reasoning'})
                await self.emit('item/completed', item={
                    'type': 'agentMessage', 'phase': 'final_answer', 'text': plan['output']})
                await self.emit('turn/completed', turn={
                    'id': self.current_turn, 'status': plan.get('status', 'completed'),
                    'error': {'message': 'private provider diagnostics'} if plan.get('status') == 'failed' else None})
            return {'turn': {'id': self.current_turn}}
        return {}

    async def emit(self, method, **data):
        await self.events.put({'method': method, 'params': {
            'threadId': 'thread1', 'turnId': self.current_turn, **data}})

    async def send(self, message):
        self.sent.append(message)

    async def close(self):
        self.closed = True


class AstraTrendsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.searcher = AstraTrends()
        self.addAsyncCleanup(self.searcher.close)
        resolver = patch('brain.astra_trends.resolve_codex_command', return_value='fixture/codex.exe')
        self.resolver = resolver.start()
        self.addCleanup(resolver.stop)

    async def run_rpc(self, rpc, **kwargs):
        with patch('brain.astra_trends.codex_client', return_value=rpc):
            return await self.searcher.collect(now=NOW, **kwargs)

    async def test_live_search_exact_model_plan_guard_and_opened_sources(self):
        rpc = FakeRPC()
        result = await self.run_rpc(rpc, categories=['anime'])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['title'], topic()['title'])
        self.assertNotIn('hidden', str(result))
        self.assertTrue(rpc.closed)
        self.assertFalse(rpc.folder.exists())
        calls = dict(rpc.calls)
        thread, turn = calls['thread/start'], calls['turn/start']
        self.assertEqual(thread['model'], MODEL)
        self.assertEqual(thread['modelProvider'], 'openai')
        self.assertTrue(thread['ephemeral'])
        self.assertEqual(thread['sandbox'], 'read-only')
        self.assertEqual(thread['approvalPolicy'], 'never')
        self.assertEqual(thread['config']['web_search'], 'live')
        self.assertFalse(thread['config']['features.shell_tool'])
        self.assertFalse(thread['config']['features.apps'])
        self.assertFalse(thread['config']['features.browser_use'])
        self.assertEqual(thread['config']['project_doc_max_bytes'], 0)
        self.assertFalse(thread['config']['mcp_servers']['private.files']['enabled'])
        self.assertFalse(thread['config']['plugins']['private@plugin']['enabled'])
        self.assertEqual(thread['config']['hooks']['PreToolUse'], [])
        self.assertEqual(turn['sandboxPolicy'], {'type': 'readOnly', 'networkAccess': False})
        request = json.loads(turn['input'][0]['text'])
        self.assertEqual(set(request), {'current_time_utc', 'categories', 'lookback_hours', 'max_topics'})
        self.assertEqual(request['categories'], ['anime'])
        self.assertEqual(turn['outputSchema']['properties']['topics']['maxItems'], 6)
        self.assertTrue(self.searcher.snapshot()['last_search_verified'])
        self.assertEqual(self.searcher.snapshot()['last_search_at'], NOW.isoformat())

    async def test_no_completed_search_cannot_be_claimed_as_fresh(self):
        for receipts in ([], [{'type': 'webSearch', 'action': {'type': 'openPage', 'url': URL}}],
                         [{'type': 'webSearch', 'status': 'failed', 'action': {'type': 'search'}}]):
            with self.subTest(receipts=receipts):
                with self.assertRaises(AstraTrendsError) as error:
                    await self.run_rpc(FakeRPC(receipts=receipts))
                self.assertEqual(error.exception.code, 'search_unavailable')
                self.assertFalse(self.searcher.snapshot()['last_search_verified'])

    async def test_each_source_needs_successful_open_url_from_same_turn(self):
        cases = [
            [{'type': 'webSearch', 'action': {'type': 'search'}}],
            [{'type': 'webSearch', 'action': {'type': 'search'}},
             {'type': 'webSearch', 'action': {'type': 'openPage', 'url': 'turn1search0'}}],
            [{'type': 'webSearch', 'action': {'type': 'search'}},
             {'type': 'webSearch', 'status': 'failed', 'action': {'type': 'openPage', 'url': URL}}],
        ]
        for receipts in cases:
            with self.subTest(receipts=receipts):
                self.assertEqual(await self.run_rpc(FakeRPC(receipts=receipts)), [])

    async def test_foreign_web_search_receipt_does_not_authorize_this_turn(self):
        rpc = FakeRPC(receipts=[])
        await rpc.events.put({'method': 'item/completed', 'params': {
            'threadId': 'other', 'turnId': 'turn1',
            'item': {'type': 'webSearch', 'action': {'type': 'search'}}}})
        with self.assertRaises(AstraTrendsError) as error:
            await self.run_rpc(rpc)
        self.assertEqual(error.exception.code, 'search_unavailable')

    async def test_unsafe_stale_unrequested_and_unopened_topics_are_dropped(self):
        samples = [topic(non_political=False), topic(summary='这是一个关于总统大选的政治隐喻梗。'),
                   topic(published_at='2026-10-20T01:00:00Z'),
                   topic(published_at='2026-10-30T01:00:00Z'),
                   topic(category='finance'),
                   topic(sources=[dict(topic()['sources'][0], url='https://another.example.com/news')])]
        for value in samples:
            with self.subTest(value=value):
                rpc = FakeRPC(output=json.dumps({'topics': [value]}, ensure_ascii=False))
                self.assertEqual(await self.run_rpc(rpc, categories=['anime']), [])

    async def test_one_bad_topic_does_not_escape_or_discard_an_independent_safe_topic(self):
        values = [topic(summary='讨论外交政策与战争。'), topic()]
        result = await self.run_rpc(FakeRPC(output=json.dumps({'topics': values}, ensure_ascii=False)))
        self.assertEqual(len(result), 1)
        self.assertNotIn('战争', str(result))

    async def test_extra_fields_duplicates_drafts_and_oversized_output_rejected(self):
        raws = ['Thinking Process: some draft', '{"topics": [], "topics": []}',
                json.dumps({'topics': [], 'thinking': 'hidden'}),
                json.dumps({'topics': [dict(topic(), extra='hidden')]}), 'a' * 32769,
                '{"topics":NaN}']
        for output in raws:
            with self.subTest(output=output[:60]):
                with self.assertRaises(AstraTrendsError) as error:
                    await self.run_rpc(FakeRPC(output=output))
                self.assertEqual(error.exception.code, 'model_response')
                self.assertNotIn('hidden', str(error.exception))

    async def test_api_auth_missing_model_or_wrong_model_rejected(self):
        for kwargs in ({'account': 'apiKey'}, {'model': 'some-other-model'},
                       {'returned_model': 'some-other-model'}):
            with self.subTest(kwargs=kwargs):
                rpc = FakeRPC(**kwargs)
                with self.assertRaises(AstraTrendsError) as error:
                    await self.run_rpc(rpc)
                self.assertEqual(error.exception.code, 'model_access')
                self.assertNotIn('turn/start', dict(rpc.calls))
                self.assertTrue(rpc.closed)

    async def test_low_or_unknown_quota_stops_before_thread(self):
        for remaining in (9, 0, None):
            rpc = FakeRPC(remaining=remaining)
            with self.subTest(remaining=remaining):
                with self.assertRaises(AstraTrendsError) as error:
                    await self.run_rpc(rpc)
                self.assertEqual(error.exception.code, 'model_quota')
                self.assertNotIn('thread/start', dict(rpc.calls))

    async def test_managed_disabled_or_cached_search_is_not_silently_accepted(self):
        for mode in ('disabled', 'cached', None):
            with self.subTest(mode=mode):
                rpc = FakeRPC(mode=mode)
                with self.assertRaises(AstraTrendsError) as error:
                    await self.run_rpc(rpc)
                self.assertEqual(error.exception.code, 'search_unavailable')
                self.assertNotIn('thread/start', dict(rpc.calls))

    async def test_unexpected_tool_request_denied_and_turn_stopped(self):
        rpc = FakeRPC(stall=True)
        await rpc.events.put({'id': 'tool1', 'method': 'item/commandExecution/requestApproval',
                              'params': {'threadId': 'thread1'}})
        with self.assertRaises(AstraTrendsError) as error:
            await self.run_rpc(rpc)
        self.assertEqual(error.exception.code, 'tool_not_allowed')
        self.assertIn('error', rpc.sent[0])
        self.assertIn('turn/interrupt', dict(rpc.calls))
        self.assertTrue(rpc.closed)

    async def test_disallowed_tool_items_fail_closed(self):
        for kind in ('commandExecution', 'mcpToolCall', 'dynamicToolCall', 'fileChange',
                     'imageView', 'collabToolCall', 'unknownFutureTool'):
            with self.subTest(kind=kind):
                rpc = FakeRPC(receipts=[{'type': kind}])
                with self.assertRaises(AstraTrendsError) as error:
                    await self.run_rpc(rpc)
                self.assertEqual(error.exception.code, 'tool_not_allowed')

    async def test_failed_turn_does_not_expose_partial_result_or_provider_error(self):
        with self.assertRaises(AstraTrendsError) as error:
            await self.run_rpc(FakeRPC(status='failed'))
        self.assertEqual(error.exception.code, 'model_response')
        self.assertNotIn('private', str(error.exception))
        self.assertFalse(self.searcher.snapshot()['last_search_verified'])

    async def test_busy_close_and_cancel_stop_pending_work(self):
        rpc = FakeRPC(stall=True)
        with patch('brain.astra_trends.codex_client', return_value=rpc):
            task = asyncio.create_task(self.searcher.collect(now=NOW))
            while rpc.folder is None:
                await asyncio.sleep(0)
            with self.assertRaises(AstraTrendsError) as error:
                await self.searcher.collect(now=NOW)
            self.assertEqual(error.exception.code, 'model_busy')
            await self.searcher.close()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(rpc.closed)
        self.assertFalse(rpc.folder.exists())
        self.assertEqual(self.searcher.snapshot()['state'], 'closed')

    async def test_timeout_interrupts_and_closes_rpc(self):
        searcher = AstraTrends(AstraTrendsConfig(timeout_sec=1))
        self.addAsyncCleanup(searcher.close)
        rpc = FakeRPC(stall=True)
        with patch('brain.astra_trends.codex_client', return_value=rpc):
            with self.assertRaises(AstraTrendsError) as error:
                await searcher.collect(now=NOW)
        self.assertEqual(error.exception.code, 'model_timeout')
        self.assertTrue(rpc.closed)
        self.assertIn('turn/interrupt', dict(rpc.calls))

    async def test_missing_client_safe_error_and_temp_cleanup(self):
        with patch('brain.astra_trends.codex_client', side_effect=CodexNotFoundError('private/path')) as start:
            with self.assertRaises(AstraTrendsError) as error:
                await self.searcher.collect(now=NOW)
        self.assertEqual(error.exception.code, 'codex_not_found')
        self.assertNotIn('private', str(error.exception))
        self.assertFalse(Path(start.call_args.args[1]).exists())

    async def test_invalid_inputs_and_disabled_do_not_start_rpc(self):
        with patch('brain.astra_trends.codex_client') as start:
            for categories in ([], ['politics'], ['anime', 'anime'], 'anime', [None]):
                with self.assertRaises(AstraTrendsError):
                    await self.searcher.collect(categories, now=NOW)
            for now in ('not-a-date', datetime(2026, 10, 29), 1793240000):
                with self.assertRaises(AstraTrendsError):
                    await self.searcher.collect(now=now)
            disabled = AstraTrends(AstraTrendsConfig(enabled=False))
            with self.assertRaises(AstraTrendsError):
                await disabled.collect(now=NOW)
            await disabled.close()
            start.assert_not_called()

    async def test_snapshot_discovery_does_not_claim_verified_search(self):
        self.assertTrue(self.searcher.snapshot()['available'])
        self.assertFalse(self.searcher.snapshot()['last_search_verified'])
        self.resolver.return_value = None
        state = self.searcher.snapshot()
        self.assertFalse(state['available'])
        self.assertEqual(state['state'], 'codex_not_found')
        self.assertNotIn('fixture', str(state))

    async def test_host_instructions_explicitly_exclude_political_crossovers(self):
        for text in ('政治隐喻', '地缘政治', '金融', '最近72小时', '不提供荐股'):
            self.assertIn(text, INSTRUCTIONS)

    async def test_safe_diagnostics_distinguish_no_candidates_from_rejected_sources(self):
        await self.run_rpc(FakeRPC(output='{"topics":[]}'))
        empty = self.searcher.snapshot()['diagnostics']
        self.assertEqual((empty['outcome'], empty['candidate_count'], empty['accepted_count']),
                         ('no_candidates', 0, 0))
        receipts = [{'type': 'webSearch', 'action': {'type': 'search', 'query': 'PRIVATE QUERY'}},
                    {'type': 'webSearch', 'action': {'type': 'openPage', 'url': 'turnPRIVATEsearch0'}}]
        self.assertEqual(await self.run_rpc(FakeRPC(receipts=receipts)), [])
        rejected = self.searcher.snapshot()['diagnostics']
        self.assertEqual(rejected['outcome'], 'all_rejected')
        self.assertEqual(rejected['search_count'], 1)
        self.assertEqual(rejected['open_page_count'], 1)
        self.assertEqual(rejected['opened_url_count'], 0)
        self.assertEqual(rejected['unresolved_open_page_count'], 1)
        self.assertEqual(rejected['candidate_count'], 1)
        self.assertEqual(rejected['accepted_count'], 0)
        self.assertEqual(rejected['rejected']['unopened_source'], 1)
        self.assertNotIn('PRIVATE', str(rejected))
        self.assertNotIn('https', str(rejected))

    async def test_diagnostics_counts_only_fixed_reasons_and_cannot_be_mutated_by_ui(self):
        values = [topic(), topic(), topic(summary='总统选举相关的不合格话题。'),
                  topic(category='finance'),
                  topic(sources=[dict(topic()['sources'][0], url='https://example.com/unopened')])]
        await self.run_rpc(FakeRPC(output=json.dumps({'topics': values}, ensure_ascii=False)), categories=['anime'])
        diagnostics = self.searcher.snapshot()['diagnostics']
        self.assertEqual(diagnostics['outcome'], 'accepted')
        self.assertEqual(diagnostics['candidate_count'], 5)
        self.assertEqual(diagnostics['accepted_count'], 1)
        self.assertEqual(diagnostics['rejected'], {'invalid_topic': 1, 'category': 1,
                                                  'unopened_source': 1, 'duplicate': 1, 'repair_scope': 0})
        diagnostics['rejected']['invalid_topic'] = 999
        self.assertEqual(self.searcher.snapshot()['diagnostics']['rejected']['invalid_topic'], 1)

    async def test_failed_and_invalid_responses_never_look_like_empty_search_results(self):
        rpc = FakeRPC(receipts=[{'type': 'webSearch', 'status': 'failed',
                                'action': {'type': 'search', 'query': 'PRIVATE QUERY'}}])
        with self.assertRaises(AstraTrendsError):
            await self.run_rpc(rpc)
        diagnostics = self.searcher.snapshot()['diagnostics']
        self.assertEqual(diagnostics['outcome'], 'failed')
        self.assertEqual(diagnostics['failed_web_action_count'], 1)
        self.assertEqual(diagnostics['search_count'], 0)
        with self.assertRaises(AstraTrendsError):
            await self.run_rpc(FakeRPC(output='PRIVATE DRAFT'))
        diagnostics = self.searcher.snapshot()['diagnostics']
        self.assertEqual(diagnostics['outcome'], 'invalid_response')
        self.assertEqual(diagnostics['failed_web_action_count'], 0, 'new attempt resets previous counters')
        self.assertNotIn('PRIVATE', str(diagnostics))

    async def test_parser_optional_diagnostic_dict_is_backward_compatible(self):
        diagnostic = {}
        self.assertEqual(_parse_topics('{"topics":[]}', categories=['anime'], now=NOW,
                                       opened=set(), diagnostics=diagnostic), [])
        self.assertEqual(diagnostic['outcome'], 'no_candidates')
        self.assertEqual(diagnostic['candidate_count'], 0)

    async def test_repeated_cancel_and_close_wait_for_owned_rpc_cleanup(self):
        rpc = FakeRPC(stall=True)
        entered, release = asyncio.Event(), asyncio.Event()
        interrupted = []

        async def slow_close():
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                interrupted.append(True)
                raise
            rpc.closed = True

        rpc.close = slow_close
        with patch('brain.astra_trends.codex_client', return_value=rpc):
            task = asyncio.create_task(self.searcher.collect(now=NOW))
            while rpc.folder is None:
                await asyncio.sleep(0)
            task.cancel()
            await asyncio.wait_for(entered.wait(), 1)
            task.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            closing = asyncio.create_task(self.searcher.close())
            await asyncio.sleep(0)
            self.assertFalse(closing.done(), 'service close must retain ownership of slow cleanup')
            self.assertFalse(rpc.closed)
            self.assertEqual(interrupted, [])
            release.set()
            await asyncio.wait_for(closing, 1)
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(rpc.closed)
        self.assertFalse(rpc.folder.exists())
        self.assertEqual(interrupted, [])

    async def test_repeated_cancel_during_initialization_does_not_cancel_startup_cleanup_twice(self):
        entered, cleaning, release, finished = (asyncio.Event() for _ in range(4))
        folder = None
        interrupted = []

        async def startup(command, path, **kwargs):
            nonlocal folder
            folder = Path(path)
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                try:
                    await release.wait()
                except asyncio.CancelledError:
                    interrupted.append(True)
                    raise
                finished.set()

        with patch('brain.astra_trends.codex_client', side_effect=startup):
            task = asyncio.create_task(self.searcher.collect(now=NOW))
            await entered.wait()
            task.cancel()
            await asyncio.wait_for(cleaning.wait(), 1)
            task.cancel()
            await asyncio.sleep(0)
            closing = asyncio.create_task(self.searcher.close())
            await asyncio.sleep(0)
            self.assertFalse(closing.done())
            release.set()
            await asyncio.wait_for(closing, 1)
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(finished.is_set())
        self.assertFalse(folder.exists())
        self.assertEqual(interrupted, [])

    async def test_single_repair_opens_missing_sources_in_same_thread_and_keeps_verified_topics(self):
        second_url = 'https://www.toei-anim.co.jp/news/20261029/character.html'
        second = topic(title='角色动作设计公开', sources=[dict(topic()['sources'][0], url=second_url)])
        rpc = FakeRPC(output=json.dumps({'topics': [topic(), second]}, ensure_ascii=False), repair={
            'output': json.dumps({'topics': [second]}, ensure_ascii=False),
            'receipts': [{'type': 'webSearch', 'action': {'type': 'openPage', 'url': second_url}}]})
        result = await self.run_rpc(rpc, categories=['anime'])
        self.assertEqual({row['title'] for row in result}, {topic()['title'], second['title']})
        self.assertEqual(rpc.turn_count, 2)
        self.assertEqual(sum(name == 'thread/start' for name, _ in rpc.calls), 1)
        turns = [params for name, params in rpc.calls if name == 'turn/start']
        self.assertEqual({turn['threadId'] for turn in turns}, {'thread1'})
        self.assertEqual({turn['model'] for turn in turns}, {MODEL})
        self.assertTrue(all(turn['sandboxPolicy'] == {'type': 'readOnly', 'networkAccess': False} for turn in turns))
        data = json.loads(turns[1]['input'][0]['text'].split('\n', 1)[1])
        self.assertEqual([row['title'] for row in data['candidates']], [second['title']])
        self.assertNotIn('id', data['candidates'][0])
        diagnostics = self.searcher.snapshot()['diagnostics']
        self.assertEqual(diagnostics['repair_count'], 1)
        self.assertEqual(diagnostics['search_count'], 1)
        self.assertEqual(diagnostics['open_page_count'], 2)
        self.assertEqual(diagnostics['candidate_count'], 3, 'counts are cumulative across both model outputs')
        self.assertEqual(diagnostics['accepted_count'], 2)
        self.assertEqual(dict(rpc.calls)['turn/interrupt']['turnId'], 'turn2')

    async def test_repair_has_four_source_budget_and_never_receives_invalid_candidates(self):
        values = [topic(title=f'动画花絮{i}', sources=[dict(topic()['sources'][0],
                  url=f'https://www.toei-anim.co.jp/news/20261029/series-{i}.html')]) for i in range(5)]
        values.append(topic(summary='总统选举热点'))
        rpc = FakeRPC(output=json.dumps({'topics': values}, ensure_ascii=False),
                      receipts=[{'type': 'webSearch', 'action': {'type': 'search'}}])
        self.assertEqual(await self.run_rpc(rpc), [])
        turns = [params for name, params in rpc.calls if name == 'turn/start']
        self.assertEqual(len(turns), 2)
        data = json.loads(turns[1]['input'][0]['text'].split('\n', 1)[1])
        urls = {source['url'] for row in data['candidates'] for source in row['sources']}
        self.assertEqual(len(urls), 4)
        self.assertNotIn('总统', str(data))

    async def test_repair_without_open_receipts_does_not_loop_or_accept_candidates(self):
        output = json.dumps({'topics': [topic()]}, ensure_ascii=False)
        rpc = FakeRPC(receipts=[{'type': 'webSearch', 'action': {'type': 'search'}}],
                      repair={'output': output, 'receipts': []})
        self.assertEqual(await self.run_rpc(rpc), [])
        self.assertEqual(rpc.turn_count, 2)
        diagnostics = self.searcher.snapshot()['diagnostics']
        self.assertEqual(diagnostics['repair_count'], 1)
        self.assertEqual(diagnostics['rejected']['unopened_source'], 2)
        self.assertEqual(diagnostics['outcome'], 'all_rejected')

    async def test_repair_rejects_new_topics_changed_sources_politics_and_stale_dates(self):
        other_url = 'https://www.toei-anim.co.jp/news/20261029/other.html'
        variants = [topic(title='没有提供的另一个候选'),
                    topic(sources=[dict(topic()['sources'][0], url=other_url)]),
                    topic(summary='这段内容涉及地缘政治。'),
                    topic(published_at='2026-10-01T00:00:00Z')]
        for value in variants:
            with self.subTest(value=value):
                rpc = FakeRPC(receipts=[{'type': 'webSearch', 'action': {'type': 'search'}}], repair={
                    'output': json.dumps({'topics': [value]}, ensure_ascii=False),
                    'receipts': [{'type': 'webSearch', 'action': {'type': 'openPage', 'url': url}}
                                 for url in (URL, other_url)]})
                self.assertEqual(await self.run_rpc(rpc), [])
                self.assertEqual(rpc.turn_count, 2)

    async def test_repair_ignores_late_prior_turn_receipts_and_text(self):
        stale = [{'method': 'item/completed', 'params': {
            'threadId': 'thread1', 'turnId': 'turn1', 'item': {
                'type': 'webSearch', 'action': {'type': 'openPage', 'url': URL}}}},
            {'method': 'item/completed', 'params': {
                'threadId': 'thread1', 'item': {
                    'type': 'webSearch', 'action': {'type': 'openPage', 'url': URL}}}}]
        rpc = FakeRPC(receipts=[{'type': 'webSearch', 'action': {'type': 'search'}}], repair={
            'output': json.dumps({'topics': [topic()]}, ensure_ascii=False),
            'receipts': [], 'stale': stale})
        self.assertEqual(await self.run_rpc(rpc), [])
        self.assertEqual(self.searcher.snapshot()['diagnostics']['open_page_count'], 0)

    async def test_repair_failure_preserves_only_first_turn_verified_topic(self):
        missing = topic(title='缺少打开回执的花絮', sources=[dict(topic()['sources'][0],
                        url='https://www.toei-anim.co.jp/news/20261029/missing.html')])
        for repair in ({'output': '{"topics":[]}', 'receipts': [], 'status': 'failed'},
                       {'output': 'PRIVATE DRAFT', 'receipts': []}):
            with self.subTest(repair=repair):
                rpc = FakeRPC(output=json.dumps({'topics': [topic(), missing]}, ensure_ascii=False), repair=repair)
                result = await self.run_rpc(rpc)
                self.assertEqual([row['title'] for row in result], [topic()['title']])
                self.assertEqual(self.searcher.snapshot()['diagnostics']['repair_failed_count'], 1)

    async def test_repair_shares_total_timeout_and_cleans_current_turn_on_cancel(self):
        for action in ('timeout', 'cancel'):
            with self.subTest(action=action):
                searcher = AstraTrends(AstraTrendsConfig(timeout_sec=1))
                self.addAsyncCleanup(searcher.close)
                rpc = FakeRPC(receipts=[{'type': 'webSearch', 'action': {'type': 'search'}}],
                              repair={'stall': True})
                with patch('brain.astra_trends.codex_client', return_value=rpc):
                    task = asyncio.create_task(searcher.collect(now=NOW))
                    while rpc.turn_count < 2:
                        await asyncio.sleep(0)
                    if action == 'cancel':
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    else:
                        with self.assertRaises(AstraTrendsError) as error:
                            await task
                        self.assertEqual(error.exception.code, 'model_timeout')
                self.assertTrue(rpc.closed)
                self.assertEqual(dict(rpc.calls)['turn/interrupt']['turnId'], 'turn2')
                self.assertEqual(searcher.snapshot()['diagnostics']['repair_count'], 1)


if __name__ == '__main__':
    unittest.main()
