"""One explicit, small end-to-end generation probe. Uses subscription Astra."""
import asyncio
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from brain import config
from brain.agent_tasks import AgentTasks


async def main():
    config.load_dotenv()
    s=config.load_settings()
    manager=AgentTasks(ROOT,s.agent_tasks,s.llm)
    try:
        kind = 'image' if '--image' in sys.argv else 'desktop' if '--desktop' in sys.argv else 'code'
        prompt = {'code': '验证独立开发链：在 workspace/tools/agent_probe.py 添加纯函数 greeting() 返回 avatar-astra-ok，在 workspace/tests/test_agent_probe.py 编写并运行 unittest 验证。不修改其他源码。按代码任务要求交付 implementation.md。',
                  'image': '验证绘图链：使用当前套餐内置图像工具生成一张简单的绿色庭院插画，保存为任务目录里的 garden.png。不使用付费 API、不用 SVG 或脚本代替图像生成。没有内置工具则如实报告。',
                  'desktop': '只读验证桌面工具：列出当前可用的窗口标题，把结果写到当前任务目录 windows.txt。不得点击、输入、关闭或切换窗口。没有桌面工具则如实报告。'}[kind]
        job=manager.submit(prompt, kind, owner=True)
        while job['state'] not in {'review','succeeded','failed','blocked','cancelled'}:
            print(json.dumps({k:job.get(k) for k in ('id','state','detail','approvals')},ensure_ascii=True),flush=True)
            if job['state']=='waiting_approval':
                # Never approve on behalf of a user from a smoke test.
                raise RuntimeError('Probe requires user approval; inspect the task console')
            await asyncio.sleep(2)
        print(json.dumps(job,ensure_ascii=True),flush=True)
        if job['state'] not in {'review', 'succeeded'}:
            raise RuntimeError('End-to-end probe did not complete')
        if kind == 'code':
            assert job.get('code_checks', {}).get('can_apply'), 'Code review checks did not pass'
    finally:
        await manager.close()


if __name__=='__main__':
    asyncio.run(main())
