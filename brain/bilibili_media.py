"""Read-only Bilibili search. Never infer results or accept arbitrary fetch URLs."""
import asyncio
import html
import math
import re
import time
import unicodedata
from collections import OrderedDict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, localcontext
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import aiohttp


MAX_PLAY_COUNT = 2**53 - 1  # The browser must receive an exact JSON integer.
AUTO_PLAY_MIN_COUNT = 200_000


def normalize_play_count(value):
    """Return a trustworthy nonnegative count, not a guessed zero."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if 0 <= value <= MAX_PLAY_COUNT else None
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and 0 <= value <= MAX_PLAY_COUNT and value.is_integer() else None
    if not isinstance(value, str) or len(value) > 64:
        return None
    text = unicodedata.normalize('NFKC', value).strip()
    match = re.fullmatch(r'(\d{1,3}(?:,\d{3})+|[0-9]+)(\.[0-9]+)?\s*([万萬亿億]?)', text, re.ASCII)
    if not match:
        return None
    try:
        with localcontext() as context:
            context.prec = 80  # No rounding of a long, almost-integer API value.
            count = Decimal(match[1].replace(',', '') + (match[2] or ''))
            count *= {'': 1, '万': 10000, '萬': 10000, '亿': 100000000, '億': 100000000}[match[3]]
        if count < 0 or count > MAX_PLAY_COUNT or count != count.to_integral_value():
            return None
        return int(count)
    except (InvalidOperation, ValueError):
        return None


def extract_bvid(query):
    value = query.strip()
    if re.fullmatch(r'(?i:BV)[0-9A-Za-z]{10}', value):
        return 'BV' + value[2:]
    parsed = urlsplit(value)
    if parsed.scheme in ('https', 'http') and parsed.hostname in ('www.bilibili.com', 'bilibili.com', 'm.bilibili.com'):
        match = re.fullmatch(r'/video/((?i:BV)[0-9A-Za-z]{10})/?', parsed.path)
        if match:
            return 'BV' + match[1][2:]
    return None


def normalize_video(payload, bvid):
    if not isinstance(payload, dict):
        raise ValueError('视频详情返回异常，请稍后重试。')
    if payload.get('code') in (-404, 62002):
        raise ValueError('没有找到这个BV号对应的视频，视频可能已删除或不可访问。')
    if type(payload.get('code')) is not int or payload['code'] != 0:
        raise ValueError('暂时无法查询这个BV号，请稍后重试或在B站打开。')
    data = payload.get('data') or {}
    if not isinstance(data, dict) or data.get('bvid') != bvid:
        raise ValueError('视频详情返回异常，请稍后重试。')
    seconds = normalize_play_count(data.get('duration')) or 0
    owner = data.get('owner') if isinstance(data.get('owner'), dict) else {}
    stat = data.get('stat') if isinstance(data.get('stat'), dict) else {}
    return normalize_results({'code': 0, 'data': {'result': [{
        'bvid': bvid, 'title': data.get('title', ''), 'pic': data.get('pic', ''),
        'author': owner.get('name', ''), 'play': stat.get('view'),
        'duration': f'{seconds // 60}:{seconds % 60:02d}',
    }]}})


def normalize_results(payload):
    if not isinstance(payload, dict) or type(payload.get('code')) is not int or payload['code'] != 0:
        raise ValueError('B站暂时拒绝了搜索请求，请稍后重试或打开B站搜索。')
    data = payload.get('data')
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError('B站搜索结果格式异常，请稍后重试。')
    items = data.get('result')
    if items is None:
        items = []
    if not isinstance(items, list):
        raise ValueError('B站搜索结果格式异常，请稍后重试。')
    results = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        bvid = item.get('bvid', '')
        if not isinstance(bvid, str) or not re.fullmatch(r'BV[0-9A-Za-z]{10}', bvid) or bvid in seen:
            continue
        seen.add(bvid)
        cover = item.get('pic')
        if not isinstance(cover, str) or len(cover) > 2048:
            cover = ''
        if cover.startswith('//'):
            cover = 'https:' + cover
        if cover.startswith('http://'):
            cover = 'https://' + cover[7:]
        try:
            parsed = urlsplit(cover)
            if (parsed.scheme != 'https' or not (parsed.hostname or '').endswith('.hdslb.com')
                    or parsed.username or parsed.password or parsed.port not in {None, 443}):
                cover = ''
        except ValueError:
            cover = ''
        results.append({'bvid': bvid,
                        'title': html.unescape(re.sub(r'<[^>]*>', '', str(item.get('title', ''))))[:250],
                        'author': str(item.get('author', ''))[:100],
                        'duration': str(item.get('duration', ''))[:20],
                        'play_count': normalize_play_count(item.get('play')),
                        'cover': cover,
                        'url': 'https://www.bilibili.com/video/' + bvid})
    results.sort(key=lambda item: (item['play_count'] is None, -(item['play_count'] or 0)))
    return results[:12]


class BilibiliSearchError(ValueError):
    """Public, safe failure details; upstream bodies and credentials stay private."""

    def __init__(self, message, *, code, http_status=503, retry_after=None, stage=None, _retryable=False):
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.retry_after = retry_after
        self.stage = stage
        self._retryable = _retryable


class BilibiliSearch:
    def __init__(self, *, total_timeout=25, request_timeout=10, min_interval=2,
                 cache_ttl=60, cache_size=32):
        self.lock = asyncio.Lock()
        self.cached = OrderedDict()
        self.last_request = -math.inf
        self._clock = time.monotonic
        self.total_timeout = total_timeout
        self.request_timeout = request_timeout
        self.min_interval = min_interval
        self.cache_ttl = cache_ttl
        self.cache_size = max(1, cache_size)
        self._session = None
        self._primed = False
        self._inflight = {}
        self._closed = False
        self._close_task = None
        self._limited_until = 0
        self._service_until = 0

    @staticmethod
    def _copy(results):
        return [dict(item) for item in results]

    @staticmethod
    def _closed_error():
        return BilibiliSearchError('搜索服务正在关闭，请稍后重新连接。', code='closed',
                                   http_status=503, stage='lifecycle')

    def _cached(self, query):
        now = self._clock()
        for key, (created, _) in list(self.cached.items()):
            if now - created >= self.cache_ttl:
                self.cached.pop(key, None)
        item = self.cached.get(query)
        if item is not None:
            self.cached.move_to_end(query)
            return self._copy(item[1])
        return None

    async def search(self, query):
        if self._closed:
            raise self._closed_error()
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 100:
            raise BilibiliSearchError('请输入1至100字的搜索词。', code='invalid_response', http_status=400)
        query = query.strip()
        try:
            bvid = extract_bvid(query)
        except ValueError:
            raise BilibiliSearchError('视频链接格式无效，请使用歌名或有效 BV 号。',
                                      code='invalid_response', http_status=400) from None
        query = bvid or query
        cached = self._cached(query)
        if cached is not None:
            return cached
        flight = self._inflight.get(query)
        if flight is None:
            flight = {'task': asyncio.create_task(self._run(query, bvid)), 'waiters': 0}
            self._inflight[query] = flight
        flight['waiters'] += 1
        try:
            return self._copy(await asyncio.shield(flight['task']))
        except asyncio.CancelledError:
            if self._closed:
                raise self._closed_error() from None
            raise
        finally:
            flight['waiters'] -= 1
            if flight['waiters'] == 0:
                if self._inflight.get(query) is flight:
                    self._inflight.pop(query, None)
                if not flight['task'].done():
                    flight['task'].cancel()
                # Drain even if the final waiter was cancelled; no orphan request.
                await asyncio.gather(flight['task'], return_exceptions=True)

    async def _run(self, query, bvid):
        try:
            # This deadline also covers queueing for the global upstream lock.
            deadline = self._clock() + self.total_timeout
            results = await asyncio.wait_for(self._execute(query, bvid, deadline), self.total_timeout)
            if self._closed:
                raise self._closed_error()
            flight = self._inflight.get(query)
            if (flight is None or flight['task'] is not asyncio.current_task()
                    or flight['waiters'] <= 0):
                # Python 3.10 wait_for can return an already-finished result
                # while cancellation is arriving. An abandoned flight must
                # not repopulate the cache or overwrite a replacement flight.
                raise asyncio.CancelledError
            self.cached[query] = (self._clock(), self._copy(results))
            self.cached.move_to_end(query)
            while len(self.cached) > self.cache_size:
                self.cached.popitem(last=False)
            return results
        except asyncio.TimeoutError:
            raise BilibiliSearchError('B站查询超时，还没有取得可确认的结果，请稍后重试。',
                                      code='timeout', http_status=504, stage='search_total') from None
        except (BilibiliSearchError, asyncio.CancelledError):
            raise
        except Exception:
            raise BilibiliSearchError('搜索服务暂时不可用，请稍后重试。', code='service', http_status=503) from None

    async def _execute(self, query, bvid, deadline):
        async with self.lock:
            if self._closed:
                raise self._closed_error()
            if self._clock() < self._limited_until:
                raise BilibiliSearchError('B站正在限制查询频率，请稍后再试。', code='rate_limited',
                                          http_status=429, retry_after=math.ceil(self._limited_until - self._clock()))
            if self._clock() < self._service_until:
                raise BilibiliSearchError('B站服务暂时不可用，请稍后重试。', code='service',
                                          http_status=503, retry_after=math.ceil(self._service_until - self._clock()))
            if self._session is None:
                self._session = aiohttp.ClientSession(
                    headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.bilibili.com/'},
                    timeout=aiohttp.ClientTimeout(total=self.request_timeout,
                                                  connect=min(5, self.request_timeout),
                                                  sock_read=min(8, self.request_timeout)))
            retries = [0]  # One retry for the whole search, not one per HTTP stage.
            if not bvid and not self._primed:
                await self._request('https://www.bilibili.com/', retries=retries, deadline=deadline, json_body=False)
                self._primed = True
            endpoint = ('https://api.bilibili.com/x/web-interface/view' if bvid
                        else 'https://api.bilibili.com/x/web-interface/search/type')
            params = {'bvid': bvid} if bvid else {'search_type': 'video', 'keyword': query, 'order': 'click'}
            payload = await self._request(endpoint, retries=retries, deadline=deadline, params=params)
            try:
                return self._normalize_payload(payload, bvid)
            except BilibiliSearchError as error:
                error.stage = 'detail' if bvid else 'search'
                raise

    async def _request(self, endpoint, *, retries, deadline, params=None, json_body=True):
        while True:
            while True:
                wait = max(self.last_request + self.min_interval, self._service_until) - self._clock()
                if wait <= 0:
                    break
                # Windows timers can wake before the monotonic clock advances.
                await asyncio.sleep(wait)
            if self._closed:
                raise self._closed_error()
            self.last_request = self._clock()
            try:
                return await self._request_once(endpoint, params=params, json_body=json_body)
            except BilibiliSearchError as error:
                if error.stage is None:
                    error.stage = ('homepage' if endpoint == 'https://www.bilibili.com/'
                                   else 'detail' if endpoint.endswith('/view') else 'search')
                if not error._retryable or retries[0] >= 1:
                    raise
                if max(self.last_request + self.min_interval, self._service_until) >= deadline:
                    raise  # Respect Retry-After without hiding the cause behind a later timeout.
                retries[0] += 1

    @staticmethod
    def _retry_after(headers):
        raw = headers.get('Retry-After', '')
        if not isinstance(raw, str) or len(raw) > 100:
            return None
        raw = raw.strip()
        if re.fullmatch(r'[0-9]{1,9}', raw):
            return int(raw)
        try:
            when = parsedate_to_datetime(raw)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            return max(0, math.ceil((when - datetime.now(timezone.utc)).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return None

    async def _request_once(self, endpoint, *, params, json_body):
        try:
            async with self._session.get(endpoint, params=params) as response:
                status = response.status
                if status == 429:
                    delay = max(2, self._retry_after(response.headers) or 0)
                    self._limited_until = self._clock() + delay
                    raise BilibiliSearchError('B站正在限制查询频率，请稍后再试。', code='rate_limited',
                                              http_status=429, retry_after=delay)
                if status in {403, 412}:
                    raise BilibiliSearchError('B站暂时拒绝了访问，请稍后重试或在B站打开。',
                                              code='upstream_rejected', http_status=503)
                if 500 <= status <= 599:
                    delay = self._retry_after(response.headers)
                    if delay is not None:
                        self._service_until = self._clock() + delay
                    raise BilibiliSearchError('B站服务暂时不可用，请稍后重试。', code='service',
                                              http_status=503, retry_after=delay, _retryable=True)
                if status != 200:
                    raise BilibiliSearchError('B站未接受这次查询，请稍后重试或在B站打开。',
                                              code='upstream_rejected', http_status=503)
                if not json_body:
                    # Set-Cookie headers establish the anonymous session. The
                    # homepage's full HTML body is not needed for a search.
                    return None
                try:
                    return await response.json()
                except (aiohttp.ContentTypeError, ValueError, UnicodeError):
                    raise BilibiliSearchError('B站返回了无法解析的数据，请稍后重试。',
                                              code='invalid_response', http_status=502) from None
        except (BilibiliSearchError, asyncio.CancelledError):
            raise
        except asyncio.TimeoutError:
            raise BilibiliSearchError('连接B站超时，还没有取得结果，请稍后重试。',
                                      code='timeout', http_status=504, _retryable=True) from None
        except aiohttp.ClientConnectionError as error:
            retryable = not isinstance(error, (aiohttp.ClientConnectorCertificateError, aiohttp.ClientSSLError))
            raise BilibiliSearchError('无法连接B站，还没有取得结果，请检查网络后重试。',
                                      code='network', http_status=502, _retryable=retryable) from None
        except aiohttp.ClientError:
            raise BilibiliSearchError('B站响应未能完整读取，请稍后重试。',
                                      code='invalid_response', http_status=502) from None

    def _normalize_payload(self, payload, bvid):
        if not isinstance(payload, dict) or type(payload.get('code')) is not int:
            raise BilibiliSearchError('B站返回的数据格式异常，请稍后重试。', code='invalid_response', http_status=502)
        code = payload['code']
        if code != 0:
            if code == -509:
                self._limited_until = self._clock() + 2
                raise BilibiliSearchError('B站正在限制查询频率，请稍后再试。', code='rate_limited',
                                          http_status=429, retry_after=2)
            message = ('没有找到这个BV号对应的视频，视频可能已删除或不可访问。'
                       if bvid and code in {-404, 62002} else 'B站暂时拒绝了查询，请稍后重试或在B站打开。')
            raise BilibiliSearchError(message, code='upstream_rejected', http_status=503)
        data = payload.get('data')
        if not isinstance(data, dict) or (not bvid and not isinstance(data.get('result'), list)):
            raise BilibiliSearchError('B站返回的数据格式异常，请稍后重试。', code='invalid_response', http_status=502)
        try:
            return normalize_video(payload, bvid) if bvid else normalize_results(payload)
        except (ValueError, TypeError, OverflowError):
            raise BilibiliSearchError('B站返回的视频信息异常，请稍后重试。', code='invalid_response', http_status=502) from None

    async def close(self):
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self):
        tasks = [flight['task'] for flight in self._inflight.values()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
        self.cached.clear()
        if self._session is not None:
            await self._session.close()
