"""Google's public Gemini API transport for persona replies.

Only assistant content is yielded. Provider errors are deliberately not echoed:
requests carry an API key, and upstream error bodies may contain private input.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import re
import time
from urllib.request import proxy_bypass_environment

import httpx

from .antigravity_network import with_windows_proxy


_ENDPOINT = 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions'
_HOST = 'generativelanguage.googleapis.com'
_MAX_EVENT = 1024 * 1024
_MAX_OUTPUT = 256 * 1024
MODEL_ERROR_CODES = frozenset({
    'model_timeout', 'model_connection', 'model_access', 'model_quota',
    'model_unavailable', 'model_response',
})


class GeminiChatError(RuntimeError):
    """A credential-safe error suitable for the local status panel."""

    def __init__(self, message: str, *, code: str = 'model_response'):
        super().__init__(message)
        self.code = code if isinstance(code, str) and code in MODEL_ERROR_CODES else 'model_response'


def _status_error(status: int) -> GeminiChatError:
    if status in (401, 403):
        return GeminiChatError('Google API 密钥或模型访问权限不可用，请检查密钥和权限。', code='model_access')
    if status == 429:
        return GeminiChatError('Google API 配额不足或请求过于频繁，请检查 API 额度后重试。', code='model_quota')
    if status == 404:
        return GeminiChatError('Google API 指定模型不可用，请检查模型名称和访问权限。', code='model_unavailable')
    if status >= 500:
        return GeminiChatError('Google API 服务暂时不可用，请稍后重试。', code='model_unavailable')
    return GeminiChatError('Google API 未接受本次请求，请检查模型配置。')


def _proxy_options() -> dict:
    """Respect explicit HTTPX environment routing before Windows settings.

Passing ``proxy`` bypasses HTTPX's NO_PROXY mounts. Check NO_PROXY explicitly
for the single fixed host before adding a Windows proxy; never mutate os.environ.
    """
    original = dict(os.environ)
    routed = with_windows_proxy(original)
    explicit = [value for key, value in original.items()
                if key.upper() in {'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY'}]
    if explicit:
        # urllib otherwise falls back to Windows registry settings when every
        # explicit proxy is empty, defeating a user's deliberate direct route.
        return {'trust_env': any(value.strip() for value in explicit)}
    proxy = routed.get('HTTPS_PROXY')
    if not proxy:
        return {'trust_env': True}
    no_proxy = original.get('no_proxy', original.get('NO_PROXY', ''))
    if (any(part.strip() == '*' for part in no_proxy.split(','))
            or (no_proxy and proxy_bypass_environment(_HOST, {'no': no_proxy}))):
        # Prevent urllib/HTTPX from independently importing registry settings.
        return {'trust_env': False}
    return {'proxy': proxy, 'trust_env': True}


class GeminiChat:
    def __init__(self, *, api_key_env: str, model: str, effort: str = 'low',
                 max_tokens: int = 4096, timeout_sec: float = 45):
        self.api_key_env = api_key_env
        self.model = model
        self.effort = effort
        self.max_tokens = max_tokens
        self.timeout_sec = float(timeout_sec)
        if (not isinstance(api_key_env, str) or not api_key_env
                or not isinstance(model, str)
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', model)
                or effort not in {'low', 'medium', 'high'}
                or isinstance(max_tokens, bool) or not isinstance(max_tokens, int)
                or max_tokens < 1 or not math.isfinite(self.timeout_sec)
                or self.timeout_sec <= 0):
            raise ValueError('无效的 Google API 主人格配置')
        self._client: httpx.AsyncClient | None = None
        self._closed = False

    @property
    def available(self) -> bool:
        return bool(os.environ.get(self.api_key_env, '').strip()) and not self._closed

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout_sec), follow_redirects=False,
                **_proxy_options(),
            )
        return self._client

    async def complete(self, system_prompt: str, user_prompt: str, *,
                       max_tokens: int | None = None, timeout_sec: float | None = None,
                       json_reply: bool = False) -> str:
        """Return one complete assistant message for non-streaming channels.

        Use the provider's final message rather than assembling speech chunks.
        Other fields (including reasoning and thought summaries) are never part
        of the returned text. Content-level output checks belong to the caller.
        """
        if self._closed:
            raise GeminiChatError('Google API 会话已经关闭。', code='model_unavailable')
        key = os.environ.get(self.api_key_env, '').strip()
        if not key:
            raise GeminiChatError('尚未配置 Google API 密钥。', code='model_access')
        limit = self.max_tokens if max_tokens is None else max_tokens
        if type(limit) is not int or limit < 1:
            raise GeminiChatError('Google API 回复长度配置无效。')
        timeout = self.timeout_sec if timeout_sec is None else timeout_sec
        if (type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0):
            raise GeminiChatError('Google API 回复超时配置无效。')
        if type(json_reply) is not bool:
            raise GeminiChatError('Google API 回复格式配置无效。')
        payload = {
            'model': self.model,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_prompt},
            ],
            'stream': False,
            'max_tokens': limit,
            # The compatibility API rejects reasoning_effort together with any
            # thinking_config, even one containing only include_thoughts.
            'extra_body': {'google': {'thinking_config': {
                'thinking_level': self.effort, 'include_thoughts': False}}},
        }
        if json_reply:
            # Only the QQ caller opts in. Other completion and speech callers
            # keep their existing format; the caller validates the JSON again.
            payload['response_format'] = {
                'type': 'json_schema',
                'json_schema': {
                    'name': 'qq_final_reply', 'strict': True,
                    'schema': {
                        'type': 'object',
                        'properties': {'reply': {
                            'type': 'string',
                            'description': 'Only the final message addressed to the user. No analysis, reasoning, drafts, headings or alternate versions.',
                        }},
                        'required': ['reply'], 'additionalProperties': False,
                    },
                },
            }

        async def request_final():
            # HTTP-level streaming bounds the JSON body without requesting a
            # model token stream, and lets us reject errors before reading them.
            async with self._http_client().stream(
                'POST', _ENDPOINT, json=payload,
                headers={'Authorization': 'Bearer ' + key, 'Accept': 'application/json'},
                timeout=httpx.Timeout(timeout),
            ) as response:
                if response.status_code != 200:
                    raise _status_error(response.status_code)
                if response.headers.get('content-type', '').split(';', 1)[0].strip() != 'application/json':
                    raise GeminiChatError('Google API 返回的不是完整文字回复。')
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > _MAX_EVENT:
                        raise GeminiChatError('Google API 返回内容过长，已停止本次回复。')
                    body.extend(chunk)
                try:
                    event = json.loads(body)
                except (ValueError, UnicodeError, RecursionError):
                    raise GeminiChatError('Google API 完整回复格式异常，未能完成本次回复。') from None
                if not isinstance(event, dict):
                    raise GeminiChatError('Google API 完整回复格式异常，未能完成本次回复。')
                if 'error' in event:
                    code = event['error'].get('code') if isinstance(event['error'], dict) else None
                    raise _status_error(code if type(code) is int else 400)
                choices = event.get('choices')
                if (not isinstance(choices, list) or len(choices) != 1
                        or not isinstance(choices[0], dict)
                        or type(choices[0].get('index')) is not int or choices[0]['index'] != 0):
                    raise GeminiChatError('Google API 返回了异常的完整回复。')
                choice = choices[0]
                if choice.get('finish_reason') == 'length':
                    raise GeminiChatError('Google API 回复达到长度上限，未完整生成，请增加输出额度或缩短输入。')
                if choice.get('finish_reason') != 'stop':
                    raise GeminiChatError('Google API 未能正常完成本次角色回复。')
                message = choice.get('message')
                if not isinstance(message, dict) or message.get('role') != 'assistant':
                    raise GeminiChatError('Google API 返回了异常的完整回复。')
                if message.get('tool_calls') or message.get('function_call'):
                    raise GeminiChatError('Google API 返回了工具调用，已停止本次角色回复。')
                text = message.get('content')
                if not isinstance(text, str):
                    raise GeminiChatError('Google API 返回了无法使用的内容格式。')
                if not text.strip():
                    raise GeminiChatError('Google API 没有返回文字正文，请重试。')
                if len(text) > _MAX_OUTPUT:
                    raise GeminiChatError('Google API 返回内容过长，已停止本次回复。')
                return text

        try:
            return await asyncio.wait_for(request_final(), timeout=timeout)
        except (httpx.TimeoutException, asyncio.TimeoutError):
            raise GeminiChatError('Google API 回复超时，请检查代理和网络后重试。', code='model_timeout') from None
        except (httpx.RequestError, OSError):
            raise GeminiChatError('Google API 无法连接，请检查代理和网络后重试。', code='model_connection') from None
        except (ValueError, UnicodeError, ImportError):
            raise GeminiChatError('Google API 网络或回复配置异常，请检查代理和模型设置。') from None

    async def stream(self, system_prompt: str, user_prompt: str):
        if self._closed:
            raise GeminiChatError('Google API 会话已经关闭。', code='model_unavailable')
        key = os.environ.get(self.api_key_env, '').strip()
        if not key:
            raise GeminiChatError('尚未配置 Google API 密钥。', code='model_access')
        payload = {
            'model': self.model,
            'messages': [
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_prompt},
            ],
            'stream': True,
            'reasoning_effort': self.effort,
            'max_tokens': self.max_tokens,
        }
        deadline = time.monotonic() + self.timeout_sec
        has_text = False
        finished = False
        received_done = False
        output_size = 0
        try:
            async with self._http_client().stream(
                'POST', _ENDPOINT, json=payload,
                headers={'Authorization': 'Bearer ' + key, 'Accept': 'text/event-stream'},
            ) as response:
                if response.status_code != 200:
                    raise _status_error(response.status_code)
                if response.headers.get('content-type', '').split(';', 1)[0].strip() != 'text/event-stream':
                    raise GeminiChatError('Google API 返回的不是语音对话所需的流式回复。')
                lines = response.aiter_lines().__aiter__()
                data_lines: list[str] = []
                event_size = 0
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise asyncio.TimeoutError
                    try:
                        line = await asyncio.wait_for(lines.__anext__(), timeout=remaining)
                    except StopAsyncIteration:
                        break
                    event_size += len(line)
                    if event_size > _MAX_EVENT:
                        raise GeminiChatError('Google API 返回内容过长，已停止本次回复。')
                    if line:
                        if line.startswith('data:'):
                            data_lines.append(line[5:].lstrip(' '))
                        continue
                    event_size = 0
                    if not data_lines:
                        continue
                    data = '\n'.join(data_lines)
                    data_lines.clear()
                    if data.strip() == '[DONE]':
                        received_done = True
                        break
                    try:
                        event = json.loads(data)
                    except (ValueError, UnicodeError):
                        raise GeminiChatError('Google API 流式回复格式异常，未能完成本次回复。') from None
                    if not isinstance(event, dict):
                        raise GeminiChatError('Google API 流式回复格式异常，未能完成本次回复。')
                    if 'error' in event:
                        code = event['error'].get('code') if isinstance(event['error'], dict) else None
                        raise _status_error(code if isinstance(code, int) else 400)
                    choices = event.get('choices')
                    if not isinstance(choices, list):
                        raise GeminiChatError('Google API 流式回复格式异常，未能完成本次回复。')
                    for choice in choices:
                        if not isinstance(choice, dict) or choice.get('index', 0) != 0:
                            raise GeminiChatError('Google API 返回了异常的多路回复。')
                        delta = choice.get('delta', {})
                        if not isinstance(delta, dict):
                            raise GeminiChatError('Google API 流式回复格式异常，未能完成本次回复。')
                        if delta.get('tool_calls') or delta.get('function_call'):
                            raise GeminiChatError('Google API 返回了工具调用，已停止本次角色回复。')
                        text = delta.get('content')
                        if text is not None and not isinstance(text, str):
                            raise GeminiChatError('Google API 返回了无法朗读的内容格式。')
                        reason = choice.get('finish_reason')
                        if reason == 'length':
                            raise GeminiChatError('Google API 回复达到长度上限，未完整生成，请增加输出额度或缩短输入。')
                        if reason and reason != 'stop':
                            raise GeminiChatError('Google API 未能正常完成本次角色回复。')
                        if text:
                            if finished:
                                raise GeminiChatError('Google API 流式回复顺序异常，已停止本次回复。')
                            output_size += len(text)
                            if output_size > _MAX_OUTPUT:
                                raise GeminiChatError('Google API 返回内容过长，已停止本次回复。')
                            has_text = has_text or bool(text.strip())
                            yield text
                        if reason == 'stop':
                            finished = True
                if not received_done or not finished:
                    raise GeminiChatError('Google API 连接提前结束，回复未完整接收，请重试。')
                if not has_text:
                    raise GeminiChatError('Google API 没有返回可朗读的正文，请重试。')
        except (httpx.TimeoutException, asyncio.TimeoutError):
            raise GeminiChatError('Google API 回复超时，请检查代理和网络后重试。', code='model_timeout') from None
        except (httpx.RequestError, OSError):
            raise GeminiChatError('Google API 无法连接，请检查代理和网络后重试。', code='model_connection') from None
        except (ValueError, UnicodeError, ImportError):
            raise GeminiChatError('Google API 网络或回复配置异常，请检查代理和模型设置。') from None

    async def close(self):
        self._closed = True
        if self._client is not None:
            await self._client.aclose()
