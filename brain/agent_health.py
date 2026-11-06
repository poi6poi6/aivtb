"""Small, credential-safe diagnostics for the configured DeepSeek endpoint."""
import os
from urllib.parse import urlsplit
import httpx


def error_kind(message):
    text=str(message).lower()
    if '401' in text or 'authentication' in text or 'api key' in text: return 'authentication'
    if '402' in text or 'balance' in text or '额度' in text: return 'quota'
    if '429' in text or 'rate limit' in text: return 'rate_limit'
    if 'timeout' in text or 'timed out' in text: return 'timeout'
    if 'connect' in text or 'fetch' in text or 'network' in text or 'enotfound' in text: return 'network'
    return 'execution'


async def probe(llm):
    if not llm.api_key:
        return {'ok':False,'category':'authentication','detail':'未配置 DeepSeek 密钥'}
    for direct in (False,True):
        try:
            async with httpx.AsyncClient(timeout=10,trust_env=not direct) as client:
                r=await client.get(llm.base_url.rstrip('/')+'/models',headers={'Authorization':'Bearer '+llm.api_key})
            return {'ok':r.status_code==200,'category':'connected' if r.status_code==200 else error_kind(str(r.status_code)),
                    'detail':'DeepSeek 接口可达' if r.status_code==200 else f'DeepSeek 返回 HTTP {r.status_code}',
                    'direct':direct}
        except (httpx.HTTPError,OSError) as exc:
            if direct:
                return {'ok':False,'category':error_kind(type(exc).__name__),
                        'detail':'DeepSeek 连接失败：'+type(exc).__name__}


def dispatch_env(env,llm,result):
    if result.get('direct'):
        host=urlsplit(llm.base_url).hostname
        for name in ('NO_PROXY','no_proxy'):
            env[name]=','.join(filter(None,[env.get(name,''),host]))
    return env
