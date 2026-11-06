"""Standalone local task console; no livestream, Minecraft, TTS or tunnel."""
import asyncio
from pathlib import Path
import sys
from aiohttp import web

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from brain import config
from brain.agent_tasks import AgentTasks
from brain.webui import WebUI
from brain.task_daemon import TaskDaemon


def app():
    config.load_dotenv()
    settings=config.load_settings()
    ui=WebUI(config.WebUIConfig(host='127.0.0.1',port=8085),asyncio.Queue(),str(ROOT/'data/audio'),{})
    ui.agent_tasks=AgentTasks(ROOT,settings.agent_tasks,settings.llm)
    ui.task_daemon=TaskDaemon(ui.agent_tasks)
    ui.agent_tasks.verification_url=f'http://127.0.0.1:{settings.webui.port}/runtime-health'
    from brain.tool_registry import build_registry
    ui.tool_registry=build_registry(ui,live=False)
    ui.agent_tasks.tool_registry=ui.tool_registry
    async def cleanup(app):
        ticker=asyncio.create_task(ui.task_daemon.tick())
        token_file=ui.agent_tasks.folder/'service.token'
        token_file.write_text(ui.task_daemon.token,'utf-8')
        yield
        ticker.cancel()
        await asyncio.gather(ticker,return_exceptions=True)
        await ui.agent_tasks.close()
        token_file.unlink(missing_ok=True)
    result=web.Application(client_max_size=16384)
    result.cleanup_ctx.append(cleanup)
    result.router.add_get('/',ui._tasks_page)
    result.router.add_get('/tasks',ui._tasks_page)
    result.router.add_get('/task-service-health',ui.task_daemon.health)
    result.router.add_post('/task-rpc',ui.task_daemon.rpc)
    result.router.add_get('/drawing',ui._drawing_page)
    result.router.add_get('/drawing-data/{job_id}',ui._drawing_data)
    result.router.add_get('/drawing-state/{job_id}',ui._drawing_state)
    result.router.add_get('/tasks-status',ui._tasks_status)
    result.router.add_post('/tasks-action',ui._tasks_action)
    result.router.add_get('/task-artifact/{job_id}/{name:.*}',ui._task_artifact)
    result.router.add_static('/static/',ROOT/'brain/static')
    return result


async def main():
    application=app()
    runner=web.AppRunner(application)
    stop=ROOT/'data/agent-tasks/stop.request'
    try:
        await runner.setup()
        await web.TCPSite(runner,'127.0.0.1',8085).start()
        stop.unlink(missing_ok=True)
        print('Task console: http://127.0.0.1:8085/tasks',flush=True)
        while not stop.exists():
            await asyncio.sleep(.5)
    finally:
        await runner.cleanup()
        stop.unlink(missing_ok=True)


if __name__=='__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
