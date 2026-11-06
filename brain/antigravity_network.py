"""Share the user's enabled Windows HTTP proxy with the official CLI."""
from __future__ import annotations

from ipaddress import IPv6Address
import os
from urllib.parse import urlsplit


def _windows_proxy_settings() -> tuple[bool, str]:
    if os.name != 'nt':
        return False, ''
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r'Software\Microsoft\Windows\CurrentVersion\Internet Settings') as key:
            enabled = winreg.QueryValueEx(key, 'ProxyEnable')[0]
            server = winreg.QueryValueEx(key, 'ProxyServer')[0]
        return bool(enabled), server if isinstance(server, str) else ''
    except OSError:
        return False, ''


def _proxy_url(value: str) -> str:
    value = value.strip()
    if not value or any(character.isspace() for character in value):
        return ''
    candidate = value if '://' in value else 'http://' + value
    try:
        parsed = urlsplit(candidate)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname
                or parsed.port is None or parsed.port == 0
                or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment):
            return ''
        if parsed.netloc.startswith('['):
            IPv6Address(parsed.hostname)
    except ValueError:
        return ''
    return candidate


def with_windows_proxy(env: dict[str, str]) -> dict[str, str]:
    """Preserve explicit routing, including sandbox proxies; never change globals.

    Only static HTTP proxy settings are supported. PAC scripts and SOCKS-only
    settings are left alone instead of guessing a different connection route.
    """
    result = dict(env)
    if any(key.upper() in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY') for key in result):
        return result
    enabled, server = _windows_proxy_settings()
    if not enabled or not server:
        return result
    if '=' in server:
        entries = {}
        for entry in server.split(';'):
            protocol, separator, value = entry.partition('=')
            if separator and protocol.strip().lower() in ('http', 'https'):
                entries[protocol.strip().lower()] = _proxy_url(value)
    else:
        proxy = _proxy_url(server)
        entries = {'http': proxy, 'https': proxy}
    for protocol, proxy in entries.items():
        if proxy:
            result[protocol.upper() + '_PROXY'] = proxy
    return result
