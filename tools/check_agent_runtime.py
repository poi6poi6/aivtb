"""Read subscription/model availability without generating a model response."""
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from brain.agent_rpc import codex_client, subscription_status
from brain.codex_runtime import resolve_codex_command


async def main():
    executable = resolve_codex_command('codex')
    if not executable:
        raise RuntimeError('未找到 Codex CLI')
    rpc = await codex_client(executable, ROOT)
    try:
        print(json.dumps(await subscription_status(rpc), ensure_ascii=True))
        if '--tools' in sys.argv:
            skills = await rpc.call('skills/list', {'cwds': [str(ROOT/'data/agent-tasks')]})
            print(json.dumps({'skills': [s['name'] for entry in skills.get('data', []) for s in entry.get('skills', [])]}, ensure_ascii=True))
            servers = await rpc.call('mcpServerStatus/list', {'limit': 100, 'detail': 'toolsAndAuthOnly'})
            print(json.dumps({'mcp': [{'name': s.get('name'), 'tools': list(s.get('tools', {}))} for s in servers.get('data', [])]}, ensure_ascii=True))
    finally:
        await rpc.close()


if __name__ == '__main__':
    asyncio.run(main())
