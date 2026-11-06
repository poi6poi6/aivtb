"""Bound, short-lived snapshots of results read on the official browser page.

This module performs no network requests and never consumes browser credentials.
Visible abbreviated counts are intervals, not exact API statistics.
"""
import copy
import html
import re
import secrets
import time
import unicodedata
from collections import OrderedDict
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR, localcontext
from urllib.parse import parse_qs, urlencode, urlsplit

from .bilibili_media import AUTO_PLAY_MIN_COUNT, MAX_PLAY_COUNT


class BrowserResultError(ValueError):
    def __init__(self, message, *, code='invalid_result', status=400):
        super().__init__(message)
        self.code = code
        self.status = status


def _text(value, maximum, *, required=False):
    if (not isinstance(value, str) or len(value) > maximum
            or any(ord(char) < 32 or ord(char) == 127 for char in value)
            or required and not value.strip()):
        raise BrowserResultError('浏览器结果字段无效，请重新读取官方页面。')
    return value.strip()


def parse_visible_count(value):
    """Return center/exact/lower/upper, with one display unit of uncertainty."""
    unknown = {'play_count': None, 'play_count_exact': False,
               'play_count_lower': None, 'play_count_upper': None}
    if not isinstance(value, str) or len(value) > 64:
        return unknown
    text = unicodedata.normalize('NFKC', value).strip()
    match = re.fullmatch(r'([0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(\.[0-9]+)?\s*([万萬亿億]?)', text)
    if not match or match[2] and not match[3]:
        return unknown
    try:
        digits = match[1].replace(',', '') + (match[2] or '')
        multiplier = {'': 1, '万': 10000, '萬': 10000, '亿': 100000000, '億': 100000000}[match[3]]
        with localcontext() as context:
            context.prec = 80
            center = Decimal(digits) * multiplier
        if not center.is_finite() or not 0 <= center <= MAX_PLAY_COUNT:
            return unknown
        if not match[3]:
            count = int(center)
            return {'play_count': count, 'play_count_exact': True,
                    'play_count_lower': count, 'play_count_upper': count}
        places = len(match[2]) - 1 if match[2] else 0
        unit = Decimal(multiplier).scaleb(-places)
        with localcontext() as context:
            context.prec = 80
            lower = max(0, int((center - unit).to_integral_value(rounding=ROUND_FLOOR)))
            upper = int((center + unit).to_integral_value(rounding=ROUND_CEILING))
        if upper > MAX_PLAY_COUNT:
            return unknown
        return {'play_count': int(center) if center == int(center) else float(center),
                'play_count_exact': False, 'play_count_lower': lower, 'play_count_upper': upper}
    except (InvalidOperation, ValueError, OverflowError):
        return unknown


def _official_url(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme != 'https' or parsed.username is not None or parsed.password is not None
                or parsed.port not in (None, 443) or parsed.fragment):
            raise ValueError()
        return parsed
    except ValueError:
        raise BrowserResultError('必须使用本次请求指定的B站官方页面。') from None


def direct_video_reference(query):
    """Validate an explicit player target, without claiming video availability."""
    value = query.strip() if isinstance(query, str) and len(query) <= 500 else ''
    if re.fullmatch(r'BV[A-Za-z0-9]{10}', value):
        bvid, part = value, 1
    else:
        match = re.fullmatch(r'https://www\.bilibili\.com/video/(BV[A-Za-z0-9]{10})(?:\?p=([1-9][0-9]{0,4}))?', value)
        if match is None or int(match[2] or '1') > 10000:
            raise BrowserResultError('直接播放只接受明确的 BV 号或规范的B站视频链接，请重新选择。',
                                     code='invalid_video_reference')
        bvid, part = match[1], int(match[2] or '1')
    return {'bvid': bvid, 'part': part,
            'url': 'https://www.bilibili.com/video/' + bvid + (f'?p={part}' if part > 1 else '')}


def _target(query):
    query = _text(query, 500, required=True)
    bvid = None
    part = 1
    if re.fullmatch(r'(?i:BV)[A-Za-z0-9]{10}', query):
        bvid = 'BV' + query[2:]
    elif re.match(r'(?i)(?:https?://|(?:www\.|m\.)?bilibili\.com/)', query):
        url = query if '://' in query else 'https://' + query
        if url.startswith('http://'):
            url = 'https://' + url[7:]
        parsed = _official_url(url)
        match = re.fullmatch(r'/video/((?i:BV)[A-Za-z0-9]{10})/?', parsed.path)
        if parsed.hostname not in {'www.bilibili.com', 'm.bilibili.com', 'bilibili.com'} or not match:
            raise BrowserResultError('请使用 BV 号、B站视频链接或歌曲关键词。')
        values = parse_qs(parsed.query, keep_blank_values=True)
        parts = values.get('p', ['1'])
        if (len(parts) != 1 or not re.fullmatch(r'[1-9][0-9]{0,4}', parts[0])
                or int(parts[0]) > 10000):
            raise BrowserResultError('视频分集参数无效。')
        part = int(parts[0])
        bvid = 'BV' + match[1][2:]
    if bvid:
        url = 'https://www.bilibili.com/video/' + bvid + (f'?p={part}' if part != 1 else '')
        return {'query': query, 'kind': 'video', 'url': url, 'bvid': bvid, 'part': part}
    if len(query) > 100 or '://' in query:
        raise BrowserResultError('请输入1至100字的歌曲关键词，或有效的B站视频链接。')
    return {'query': query, 'kind': 'search', 'url': 'https://search.bilibili.com/all?' +
            urlencode({'keyword': query, 'order': 'click'}), 'bvid': None, 'part': 1}


def _source_matches(target, page_url):
    parsed = _official_url(_text(page_url, 2048, required=True))
    values = parse_qs(parsed.query, keep_blank_values=True)
    if target['kind'] == 'search':
        valid = (parsed.hostname == 'search.bilibili.com' and parsed.path.rstrip('/') in {'/all', '/video'}
                 and values.get('keyword') == [target['query']] and values.get('order') == ['click'])
    else:
        valid = (parsed.hostname == 'www.bilibili.com'
                 and parsed.path.rstrip('/') == '/video/' + target['bvid']
                 and values.get('p', ['1']) == [str(target['part'])])
    if not valid:
        raise BrowserResultError('结果页面与本次点歌不一致，请重新查询。', code='source_mismatch')


def _normalize_rows(rows, target):
    if not isinstance(rows, list) or len(rows) > 30:
        raise BrowserResultError('浏览器结果数量无效。')
    result, seen = [], set()
    for row in rows:
        if not isinstance(row, dict):
            raise BrowserResultError('浏览器结果格式无效。')
        bvid = _text(row.get('bvid'), 12, required=True)
        if not re.fullmatch(r'BV[A-Za-z0-9]{10}', bvid):
            raise BrowserResultError('浏览器结果的 BV 号无效。')
        if target['kind'] == 'video' and bvid != target['bvid']:
            raise BrowserResultError('视频详情与指定 BV 号不一致。', code='source_mismatch')
        title = _text(row.get('title'), 250, required=True)
        title = html.unescape(re.sub(r'<[^>]*>', '', title)).strip()
        title = _text(title, 250, required=True)
        author = _text(row.get('author', ''), 100)
        duration = _text(row.get('duration', ''), 32)
        play_text = _text(row.get('play_text', ''), 64)
        cover = _text(row.get('cover', ''), 2048)
        if cover.startswith('//'):
            cover = 'https:' + cover
        if cover:
            try:
                parsed = _official_url(cover)
                if not (parsed.hostname or '').endswith('.hdslb.com'):
                    cover = ''
            except BrowserResultError:
                cover = ''
        if bvid in seen:
            continue
        seen.add(bvid)
        result.append({'bvid': bvid, 'title': title, 'author': author, 'duration': duration,
                       'cover': cover, 'url': (target['url'] if target['kind'] == 'video'
                                              else 'https://www.bilibili.com/video/' + bvid),
                       'play_text': play_text, **parse_visible_count(play_text)})
    # Stable order preserves official page ordering for exact ties.
    result.sort(key=lambda row: (row['play_count'] is None, -(row['play_count'] or 0)))
    return result


def _selection(rows, target):
    if not rows:
        return None, 'empty'
    if target['kind'] == 'video':
        return target['bvid'], 'explicit_video'
    known = [row for row in rows if row['play_count'] is not None]
    if not known:
        return None, 'missing_play_count'
    if len(known) != len(rows):
        return None, 'uncertain_play_count'
    first = rows[0]
    if first['play_count_upper'] < AUTO_PLAY_MIN_COUNT:
        return None, 'below_play_threshold'
    if first['play_count_lower'] < AUTO_PLAY_MIN_COUNT:
        return None, 'below_play_threshold'
    if any(first['play_count_lower'] < other['play_count_upper'] for other in rows[1:]):
        return None, 'uncertain_play_count'
    return first['bvid'], 'highest_play'


class BilibiliBrowser:
    def __init__(self, *, clock=time.monotonic, pending_ttl=180, snapshot_ttl=600, capacity=32):
        self._clock = clock
        self.pending_ttl, self.snapshot_ttl = pending_ttl, snapshot_ttl
        self.capacity = max(1, capacity)
        self._pending, self._snapshots = OrderedDict(), OrderedDict()

    def _prune(self):
        now = self._clock()
        for entries in (self._pending, self._snapshots):
            for key in tuple(entries):
                if entries[key]['expires'] <= now:
                    entries.pop(key)

    def _put(self, entries, key, value):
        while len(entries) >= self.capacity:
            entries.popitem(last=False)
        entries[key] = value

    def request(self, query):
        self._prune()
        target = _target(query)
        request_id = secrets.token_urlsafe(32)
        self._put(self._pending, request_id, {**target, 'expires': self._clock() + self.pending_ttl})
        return {key: value for key, value in {**target, 'request_id': request_id}.items()
                if key in {'request_id', 'query', 'url', 'kind'}}

    def submit(self, request_id, page_url, rows):
        self._prune()
        request_id = _text(request_id, 80, required=True)
        target = self._pending.pop(request_id, None)
        if target is None:
            raise BrowserResultError('这次浏览器查询已过期、取消或提交，请重新查询。', code='expired_request', status=410)
        _source_matches(target, page_url)
        rows = _normalize_rows(rows, target)
        auto_bvid, reason = _selection(rows, target)
        result_id = secrets.token_urlsafe(32)
        snapshot = {'query': target['query'], 'result_id': result_id, 'results': rows,
                    'auto_bvid': auto_bvid, 'selection_reason': reason}
        self._put(self._snapshots, result_id, {'expires': self._clock() + self.snapshot_ttl,
                                             'request_id': request_id, 'snapshot': snapshot})
        return copy.deepcopy(snapshot)

    def snapshot(self, result_id, query):
        self._prune()
        entry = self._snapshots.get(result_id) if isinstance(result_id, str) and len(result_id) <= 80 else None
        if entry is None or entry['snapshot']['query'] != query:
            raise BrowserResultError('还没有可确认的浏览器结果，结果可能已过期或取消，请重新查询。',
                                     code='expired_result', status=410)
        return copy.deepcopy(entry['snapshot'])

    def cancel(self, *, request_id=None, result_id=None):
        self._prune()
        if request_id is None and result_id is None:
            raise BrowserResultError('缺少要取消的浏览器查询。')
        if request_id is not None:
            request_id = _text(request_id, 80, required=True)
        if result_id is not None:
            result_id = _text(result_id, 80, required=True)
        self._pending.pop(request_id, None)
        self._snapshots.pop(result_id, None)
        for key, entry in tuple(self._snapshots.items()):
            if request_id is not None and entry['request_id'] == request_id:
                self._snapshots.pop(key)
        return {'ok': True}

    def close(self):
        self._pending.clear()
        self._snapshots.clear()
