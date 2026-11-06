"""Six offline voice examples through the current production delivery planner.

Run with the IndexTTS2 environment via the launcher's ASCII workspace alias.
This finite process changes neither production settings nor the reference voice.
"""
from __future__ import annotations

import asyncio
from dataclasses import fields
import hashlib
import json
import os
from pathlib import Path
import random
import sys
import time

ROOT = Path(os.path.abspath(__file__)).parents[1]
PROJECT = ROOT / 'IndexTTS2'
sys.path[:0] = [str(PROJECT), str(ROOT)]
os.environ['HF_HUB_OFFLINE'] = '1'
os.environ['TRANSFORMERS_OFFLINE'] = '1'
os.chdir(PROJECT)

import numpy as np
import soundfile as sf
import torch
import yaml
from indextts.infer_v2 import IndexTTS2

sys.path.append(str(ROOT / '.venv' / 'Lib' / 'site-packages'))
import httpx
from brain.config import TTSConfig
from brain.index_expression import index_emotion_options
from brain.tts import IndexTTS2TTS, index_voice_profile


CASES = [
    ('chat_1', '日常聊天', '这首歌的前奏挺好听，轻轻一响，房间里的气氛都变了。', '平静'),
    ('chat_2', '日常聊天', '这游戏比我想的还好玩，刚才那一下，我差点跟着你欢呼了。', '平静'),
    ('explain_1', '认真解释', '先把麦克风打开，再说一句短的，确认能听见，我们就继续。', '平静'),
    ('explain_2', '认真解释', '关键是先找到卡住的那一步，一次只改一个地方，才知道哪里起了作用。', '平静'),
    ('tease_1', '轻吐槽', '你说再看五分钟，我都听见三次了，这五分钟还挺会续费的。', '吐槽'),
    ('tease_2', '轻吐槽', '计划写得这么漂亮，执行的时候，你可别只负责给自己鼓掌。', '吐槽'),
]


async def main():
    source = ROOT / 'config' / 'settings.yaml'
    original_settings = source.read_bytes()
    raw = yaml.safe_load(original_settings)['tts']
    allowed = {item.name for item in fields(TTSConfig)}
    options = {key: value for key, value in raw.items() if key in allowed}
    cfg = TTSConfig(**options)
    if cfg.backend != 'indextts2':
        raise ValueError('Expected the currently selected IndexTTS2 backend')
    for _, _, text, _ in CASES:
        reference, _ = index_voice_profile(cfg, text)
        if not Path(reference).is_file():
            raise FileNotFoundError(reference)
    output = ROOT / 'data' / 'voice-previews' / time.strftime('current-delivery-%Y%m%d-%H%M%S')
    output.mkdir(parents=True, exist_ok=False)
    print('OUTPUT', output, flush=True)
    print('Loading local model for six current-preset examples...', flush=True)
    model = IndexTTS2(cfg_path='checkpoints/config.yaml', model_dir='checkpoints',
                      use_fp16=True, use_cuda_kernel=False, use_deepspeed=False)
    report = {
        'preset': cfg.indextts2_delivery_preset,
        'expressive_teasing': cfg.indextts2_expressive_teasing,
        'postprocessing': 'existing client edge trim; no normalization, pitch or speed changes',
        'results': [],
    }
    grouped = {}
    for index, (name, label, text, emotion) in enumerate(CASES):
        folder = output / (name + '_source')
        folder.mkdir()
        voice = IndexTTS2TTS(TTSConfig(**{**options, 'out_dir': str(folder)}))
        record = {'name': name, 'label': label, 'text': text, 'input_emotion': emotion}

        async def synthesize(request):
            payload = json.loads(request.content)
            seed = 20261020 + index
            random.seed(seed)
            np.random.seed(seed)
            torch.manual_seed(seed)
            torch.cuda.manual_seed_all(seed)
            emotion_options = index_emotion_options(
                payload['emotion'], payload['emo_alpha'],
                reference_style=payload['reference_style'], natural_delivery=payload['natural_delivery'])
            reference = Path(payload['ref_audio_path'])
            temperature = payload.get('temperature', .8)
            raw_path = folder / 'raw.wav'
            started = time.perf_counter()
            model.infer(spk_audio_prompt=str(reference), text=payload['text'],
                        output_path=str(raw_path), **emotion_options, use_random=False,
                        verbose=False, num_beams=payload['num_beams'], temperature=temperature)
            record.update(seed=seed, reference=reference.name,
                          reference_sha256=hashlib.sha256(reference.read_bytes()).hexdigest(),
                          emotion=payload['emotion'], input_alpha=payload['emo_alpha'],
                          emotion_options=emotion_options, temperature=temperature,
                          num_beams=payload['num_beams'],
                          synthesis_seconds=round(time.perf_counter() - started, 3))
            return httpx.Response(200, content=raw_path.read_bytes(), headers={'Content-Type': 'audio/wav'})

        voice._client = httpx.AsyncClient(transport=httpx.MockTransport(synthesize))
        try:
            rendered = await voice.speak(text, emotion)
        finally:
            await voice.close()
        audio, rate = sf.read(rendered, always_2d=True)
        if not len(audio) or not np.isfinite(audio).all() or not 1 < len(audio) / rate < 45:
            raise ValueError('Invalid audition audio')
        target = output / (name + '.wav')
        target.write_bytes(Path(rendered).read_bytes())
        record.update(audio=target.name, seconds=round(len(audio) / rate, 3), sample_rate=rate,
                      peak=float(np.max(np.abs(audio))), rms=float(np.sqrt(np.mean(audio ** 2))))
        report['results'].append(record)
        (output / 'results.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        grouped.setdefault(name.rsplit('_', 1)[0], []).append((audio, rate))
        print('DONE', name, record['seconds'], 'seconds', flush=True)
    for name, clips in grouped.items():
        (first, rate), (second, second_rate) = clips
        if rate != second_rate or first.shape[1] != second.shape[1]:
            raise ValueError('Inconsistent audio formats within a group')
        joined = np.concatenate([first, np.zeros((round(.9 * rate), first.shape[1])), second])
        sf.write(output / (name + '_examples.wav'), joined, rate, subtype='PCM_16')
    if source.read_bytes() != original_settings:
        print('NOTE: Settings changed externally during generation; captured settings were used.', flush=True)
    print('COMPLETE', output, flush=True)


if __name__ == '__main__':
    asyncio.run(main())
