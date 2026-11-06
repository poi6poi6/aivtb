"""Start the local executor once, without owning its lifetime."""
import asyncio
import os
from pathlib import Path
import subprocess
import httpx


async def ensure_task_service(root, url='http://127.0.0.1:8085'):
    root = Path(root).resolve()
    async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
        async def ready():
            try:
                response = await client.get(url + '/task-service-health')
                return response.status_code == 200 and response.json().get('service') == 'avatar-tasks'
            except (httpx.HTTPError, ValueError):
                return False
        if await ready():
            return True
        # The service also holds the manager lock; racing startup cannot create
        # two executors. Logs and all artifacts use the canonical project path.
        folder = root / 'data/agent-tasks'
        folder.mkdir(parents=True, exist_ok=True)
        flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS if os.name == 'nt' else 0
        with (folder / 'console.log').open('ab') as out, (folder / 'console-error.log').open('ab') as err:
            subprocess.Popen([str(root / '.venv/Scripts/python.exe'), '-u', str(root / 'tools/run_agent_console.py')],
                cwd=root, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                creationflags=flags, start_new_session=os.name != 'nt')
        for _ in range(30):
            if await ready():
                return True
            await asyncio.sleep(.2)
        return False
