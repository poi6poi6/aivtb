"""Exercise the actual Python -> official SDK -> Persona -> Bilibili host loop.

The model and browser are deterministic local fixtures. No credentials, external
model, browser account or publication is used, even if configured on this host.
"""
import asyncio
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from brain.bilibili_activity import BilibiliActivity
from brain.deepseek_harness import HarnessRunner

URL = 'https://www.bilibili.com/video/BV1B7411m7LV'
DECISIONS = [
    dict(type='tool', name='bili_search', arguments=dict(query='玫瑰')),
    dict(type='tool', name='bili_read', arguments=dict(url=URL)),
    dict(type='tool', name='bili_note', arguments=dict(
        source_url=URL, summary='介绍建议使用有排水孔的花盆。', reaction='想记住这个配土提示。')),
    dict(type='tool', name='bili_draft', arguments=dict(
        text='刚读到一段玫瑰养护介绍，花盆排水值得留意。', source_urls=[URL])),
    dict(type='tool', name='bili_diary', arguments=dict(
        text='今天读到玫瑰养护的介绍，想记住花盆排水的建议。', source_urls=[URL])),
    dict(type='tool', name='bili_interaction', arguments=dict(action='like', url=URL, text='')),
    dict(type='finish', text='已根据工具回执保存见闻与草稿。'),
]


class PersonaFixture:
    model_name = 'offline-persona-fixture'
    model_configured = True

    def __init__(self):
        self.inputs = []

    async def activity_completion(self, system, user):
        self.inputs.append(json.loads(user))
        return json.dumps(DECISIONS[len(self.inputs) - 1], ensure_ascii=False)


class BrowserFixture:
    def __init__(self):
        self.calls = []

    def snapshot(self):
        return dict(connected=True, account={})

    def cancel(self):
        pass

    async def call(self, action, payload):
        self.calls.append(action)
        if action == 'search':
            return dict(items=[dict(url=URL, title='玫瑰养护', author='离线作者')])
        if action == 'read':
            return dict(url=URL, description='花盆要有排水孔。', subtitles=[])
        raise AssertionError('This verification must never reach an account or publish action')


async def main():
    persona, browser = PersonaFixture(), BrowserFixture()
    runner = HarnessRunner(root=ROOT, persona=persona, timeout_sec=35)
    assert runner.readiness()['ready'], 'Install the pinned SDK dependencies first'
    assert 'DEEPSEEK_API_KEY' not in runner._environment('http://127.0.0.1:1', 'fixture')
    with tempfile.TemporaryDirectory(prefix='vtuber-persona-host-check-') as directory:
        activity = BilibiliActivity(Path(directory) / 'activity.json', runner,
                                    dict(name='助手', interests=['花园']), broker=browser)
        try:
            activity.run()
            await activity._task
            assert not activity.error, activity.error
            assert len(activity.data['notes']) == 1
            assert len(activity.data['drafts']) == 1
            assert activity.data['drafts'][0]['state'] == 'draft'
            assert activity.data['notes'][0]['evidence_kind'] == 'page_metadata'
            assert browser.calls == ['search', 'read']
            assert len(activity.data['diaries']) == 1
            assert activity.data['interactions'][0]['state'] == 'draft'
            assert len(persona.inputs) == 7
            assert len([m for m in persona.inputs[-1]['messages'] if m['role'] == 'tool']) == 6
            print('Python + official SDK + persona provider + activity host: PASS (7 decisions, 6 tools, no external calls)')
        finally:
            await activity.close()


if __name__ == '__main__':
    asyncio.run(main())
