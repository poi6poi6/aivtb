"""Serialized, cancellable local RVC jobs; only completed WAVs reach the player."""
import asyncio
import json
import os
import shutil
import wave
from pathlib import Path

from .songs import catalog


class RVCBackend:
    def __init__(self, root, requests, publish):
        self.root = Path(root)
        self.requests = requests
        self.publish = publish
        self.process = None

    def missing(self):
        try:
            cfg = json.loads((self.root / 'config/rvc.json').read_text(encoding='utf-8'))
            if not isinstance(cfg, dict):
                raise ValueError()
        except (OSError, ValueError):
            return ['有效的config/rvc.json配置']
        paths = {'RVC运行环境': self.root / 'RVC/.venv/Scripts/python.exe',
                 'FFmpeg': self.root / 'RVC/bin/ffmpeg.exe',
                 'HuBERT模型': self.root / 'RVC/assets/hubert_base/pytorch_model.bin',
                 'RMVPE模型': self.root / 'RVC/assets/rmvpe/rmvpe.pt'}
        model = cfg.get('model', '')
        paths['助手RVC模型'] = self.root / model if model else self.root / 'RVC/assets/weights/UNCONFIGURED.pth'
        if cfg.get('index'):
            paths['RVC索引'] = self.root / cfg['index']
        missing = [name for name, path in paths.items() if not path.is_file()]
        if not (self.root / 'RVC/assets/setup-ready.json').is_file():
            missing.append('RVC依赖及分离模型验证')
        return missing

    def source(self, query):
        folder = self.root / 'data/music/sources'
        return next((p for p in folder.glob('*') if p.is_file() and not p.is_symlink()
                     and p.suffix.lower() in {'.wav', '.mp3', '.flac', '.m4a', '.ogg'}
                     and p.stem.casefold() == query.casefold()), None)

    async def status(self, job, state, detail):
        job.update(state=state, detail=detail)
        self.requests.save()
        await self.publish({'type': 'music_status', 'text': job['query'] + '：' + detail})

    async def stop_process(self):
        if self.process and self.process.returncode is None:
            if os.name == 'nt':
                killer = await asyncio.create_subprocess_exec('taskkill.exe', '/PID', str(self.process.pid), '/T', '/F',
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, creationflags=0x08000000)
                await killer.wait()
            else:
                self.process.kill()
            await self.process.wait()
        self.process = None

    async def render(self, job, source):
        work = self.root / 'data/music/work' / job['id']
        work.mkdir(parents=True, exist_ok=True)
        output = work / 'cover.wav'
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        await self.status(job, 'processing', '正在分离人声并转换，首次制作需要等待。')
        try:
            with (work / 'render.log').open('w', encoding='utf-8') as log:
                self.process = await asyncio.create_subprocess_exec(
                    str(self.root / 'RVC/.venv/Scripts/python.exe'),
                    str(self.root / 'tools/rvc_cover.py'), '--source', str(source),
                    '--work', str(work), '--output', str(output), cwd=str(self.root),
                    env=env, stdout=log, stderr=log,
                    **({'creationflags': 0x08000000} if os.name == 'nt' else {}))
                deadline = asyncio.get_running_loop().time() + 1800
                while self.process.returncode is None:
                    if job['state'] == 'cancelled':
                        await self.stop_process()
                        return
                    if asyncio.get_running_loop().time() > deadline:
                        raise TimeoutError('render timeout')
                    await asyncio.sleep(.5)
                if self.process.returncode != 0:
                    raise RuntimeError('render failed')
            with wave.open(str(output), 'rb') as audio:
                if audio.getnframes() < audio.getframerate():
                    raise ValueError('empty output')
            if job['state'] == 'cancelled':
                return
            folder = self.root / 'data/songs'
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / ('RVC-' + job['id'] + '.wav')
            shutil.copyfile(output, target)
            song = next(s for s in catalog(folder) if s['title'] == target.stem)
            song['title'] = '助手翻唱 · ' + job['query']
            await self.status(job, 'ready', '翻唱已制作完成，已发送到播放队列。')
            job['song'] = song
            self.requests.save()
            await self.publish({'type': 'song_request', 'song': song})
        except asyncio.CancelledError:
            await self.status(job, 'needs_setup', '服务停止，制作中断；下次启动可重试。')
            raise
        except Exception:
            await self.status(job, 'failed', '制作失败，请检查该任务的本地render.log。')
        finally:
            await self.stop_process()

    async def run(self):
        for job in self.requests.items:
            if job['state'] == 'processing':
                job['state'] = 'needs_setup'
        while True:
            for job in list(self.requests.items):
                if job['mode'] != 'cover' or job['state'] not in {'needs_setup', 'waiting_source'}:
                    continue
                missing = self.missing()
                source = self.source(job['query'])
                state = 'needs_setup' if missing else 'waiting_source'
                detail = '缺少：' + '、'.join(missing) if missing else '等待同名原曲放入data/music/sources。'
                if not missing and source:
                    await self.render(job, source)
                elif job['detail'] != detail or job['state'] != state:
                    await self.status(job, state, detail)
            await asyncio.sleep(3)
