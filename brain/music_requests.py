"""Persistent intake for music production; no fabricated generation receipts."""
import json
import re
import time
import uuid
from pathlib import Path


def parse_music_request(text):
    match = re.fullmatch(r'(唱歌|翻唱|原创|写歌)[\s：:]+(.+)', text.strip(), re.S)
    if not match:
        return None
    return ('cover' if match[1] in ('唱歌', '翻唱') else 'original', match[2].strip())


class MusicRequests:
    """Requests await real provider setup. Never mark queued work as rendered audio."""
    MAX_HISTORY = 100
    TERMINAL_STATES = {'cancelled', 'ready', 'failed'}

    def __init__(self, path):
        self.path = Path(path)
        self.items = []
        self.cooldowns = {}
        if self.path.exists():
            self.items = json.loads(self.path.read_text(encoding='utf-8'))

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix('.tmp')
        temporary.write_text(json.dumps(self.items, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(self.path)

    def submit(self, mode, query, viewer):
        if mode not in ('cover', 'original') or not query or len(query) > 200:
            return '请使用“唱歌 歌名”或“原创 主题”，内容不超过200字。'
        active = [item for item in self.items if item['state'] not in self.TERMINAL_STATES]
        if any(item['mode'] == mode and item['query'].casefold() == query.casefold() for item in active):
            return '这首歌或主题已在待办列表中，不会重复创建。'
        now = time.time()
        if now - self.cooldowns.get(viewer, 0) < 30:
            return '点歌请求间隔为30秒，请稍后再试。'
        if len(active) >= 20:
            return '音乐待办已满，请等主播处理后再点。'
        reason = ('等待配置助手RVC模型、人声分离和转换运行环境，并提供可用原曲。'
                  if mode == 'cover' else '等待准备歌词与风格；由主播在Suno网页生成并导入成品。')
        self.items.append({'id': uuid.uuid4().hex[:12], 'mode': mode, 'query': query,
                           'state': 'needs_setup', 'detail': reason, 'created': now})
        # Workers keep references to active jobs while awaiting a model/render.
        # Evict only old terminal history, preserving those exact job objects.
        excess = max(0, len(self.items) - self.MAX_HISTORY)
        retained = []
        for item in self.items:
            if excess and item['state'] in self.TERMINAL_STATES:
                excess -= 1
            else:
                retained.append(item)
        self.items = retained
        self.cooldowns[viewer] = now
        self.save()
        return ('翻唱' if mode == 'cover' else '原创') + '请求已记录：' + query + '。' + reason

    def cancel(self, job_id):
        for item in self.items:
            if item['id'] == job_id and item['state'] != 'cancelled':
                item['state'] = 'cancelled'
                item['detail'] = '主播已取消。'
                self.save()
                return True
        return False
