"""Expiring, exact-video-and-part evidence received from the local browser.

This store has no network or credential access. Full subtitle text stays in the
host snapshot; clients receive only the bounded reading receipt.
"""
import copy
import secrets
import time
from collections import OrderedDict

from .bilibili_browser import BrowserResultError, direct_video_reference


REASONS = {
    'ready': '已读取本分P字幕。',
    'none': '这个分P没有可用字幕，不能据此评价视频内容。',
    'unavailable': '字幕暂时无法读取；请检查登录状态或稍后重试。',
    'blocked': 'B站暂时限制读取，请在正常页面处理验证后重试。',
    'login_required': '读取字幕需要登录，请先在浏览器登录B站。',
    'part_unavailable': '未能确认所选分P，未使用其他分P的字幕。',
}


def _text(value, maximum, *, multiline=False):
    if (not isinstance(value, str) or len(value) > maximum
            or any((ord(c) < 32 and not (multiline and c in '\n\r\t')) or ord(c) == 127 for c in value)):
        raise BrowserResultError('字幕证据字段无效，请重新读取。')
    return value.strip()


class BilibiliVideoEvidence:
    def __init__(self, *, clock=time.monotonic, pending_ttl=90, snapshot_ttl=600, capacity=32):
        self.clock = clock
        self.pending_ttl, self.snapshot_ttl, self.capacity = pending_ttl, snapshot_ttl, capacity
        self.pending, self.snapshots = OrderedDict(), OrderedDict()

    def _prune(self):
        now = self.clock()
        for entries in (self.pending, self.snapshots):
            for key, value in tuple(entries.items()):
                if value['expires'] <= now:
                    entries.pop(key)

    def _put(self, entries, key, value):
        while len(entries) >= self.capacity:
            entries.popitem(last=False)
        entries[key] = value

    def request(self, query):
        self._prune()
        target = direct_video_reference(query)
        request_id = secrets.token_urlsafe(32)
        target = {**target, 'query': query, 'request_id': request_id}
        self._put(self.pending, request_id, {'target': target, 'expires': self.clock() + self.pending_ttl})
        return copy.deepcopy(target)

    @staticmethod
    def receipt(snapshot):
        return copy.deepcopy({key: value for key, value in snapshot.items()
                              if key not in {'subtitles', 'description'}})

    def submit(self, request_id, evidence):
        self._prune()
        request_id = _text(request_id, 80)
        pending = self.pending.pop(request_id, None)
        if pending is None:
            raise BrowserResultError('这次字幕读取已过期或取消，请重新读取。', code='expired_request', status=410)
        target = pending['target']
        if (not isinstance(evidence, dict) or evidence.get('url') != target['url']
                or evidence.get('bvid') != target['bvid'] or type(evidence.get('video_part')) is not int
                or evidence['video_part'] != target['part']):
            raise BrowserResultError('字幕与所选视频或分P不一致，未采用本次结果。')
        status = evidence.get('subtitle_status')
        if not isinstance(status, str) or status not in REASONS:
            raise BrowserResultError('字幕读取状态无效。')
        lines = evidence.get('subtitles')
        if not isinstance(lines, list) or len(lines) > 2000:
            raise BrowserResultError('字幕内容无效或过长。')
        lines = [_text(line, 12000, multiline=True) for line in lines]
        if any(not line for line in lines) or len('\n'.join(lines)) > 12000:
            raise BrowserResultError('字幕内容无效或过长。')
        kind, truncated = evidence.get('evidence_kind'), evidence.get('subtitle_truncated')
        if type(truncated) is not bool:
            raise BrowserResultError('字幕完整性状态无效。')
        if status == 'ready':
            if (not lines or kind != ('subtitle_excerpt' if truncated else 'full_subtitles')
                    or type(evidence.get('video_cid')) is not int or evidence['video_cid'] <= 0):
                raise BrowserResultError('无法确认本分P的字幕完整性，请重新读取。')
        elif lines or kind != 'page_metadata' or truncated:
            raise BrowserResultError('未取得字幕时不能把其他文本当作字幕。')
        now = time.time()
        snapshot = {'evidence_id': secrets.token_urlsafe(32), 'query': target['query'],
                    'url': target['url'], 'bvid': target['bvid'], 'part': target['part'],
                    'title': _text(evidence.get('title', ''), 300),
                    'part_title': _text(evidence.get('part_title', ''), 300),
                    'description': _text(evidence.get('description', ''), 5000, multiline=True),
                    'subtitles': lines, 'evidence_kind': kind, 'subtitle_status': status,
                    'subtitle_reason': ('字幕超过读取上限，仅使用前段内容。' if truncated else REASONS[status]),
                    'subtitle_truncated': truncated,
                    'subtitle_language': _text(evidence.get('subtitle_language', ''), 30),
                    'subtitle_count': len(lines), 'read_at': now, 'expires_at': now + self.snapshot_ttl}
        self._put(self.snapshots, snapshot['evidence_id'], {'snapshot': snapshot, 'request_id': request_id,
                                                         'expires': self.clock() + self.snapshot_ttl})
        return self.receipt(snapshot)

    def snapshot(self, evidence_id, query):
        self._prune()
        target = direct_video_reference(query)
        entry = self.snapshots.get(evidence_id) if isinstance(evidence_id, str) and len(evidence_id) <= 80 else None
        if entry is None or entry['snapshot']['url'] != target['url']:
            raise BrowserResultError('字幕证据已过期、取消或不属于当前视频，请重新读取。', code='expired_result', status=410)
        return copy.deepcopy(entry['snapshot'])

    def cancel(self, *, request_id=None, evidence_id=None):
        self._prune()
        if request_id is None and evidence_id is None:
            raise BrowserResultError('缺少要取消的字幕读取。')
        if request_id is not None:
            request_id = _text(request_id, 80)
        if evidence_id is not None:
            evidence_id = _text(evidence_id, 80)
        self.pending.pop(request_id, None)
        self.snapshots.pop(evidence_id, None)
        for key, value in tuple(self.snapshots.items()):
            if request_id is not None and value['request_id'] == request_id:
                self.snapshots.pop(key)
        return {'ok': True}

    def close(self):
        self.pending.clear()
        self.snapshots.clear()
