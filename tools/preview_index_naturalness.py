"""Generate finite, same-text IndexTTS2 A/B previews; never reads chat or .env.

Run with the IndexTTS2 environment through the launcher's ASCII drive mapping.
The model runs locally. MockTransport bridges the real app client to inference,
so no listening socket, API calls, or persistent GPU service is needed.
"""
from __future__ import annotations

import asyncio
from dataclasses import fields
import html
import json
import os
from pathlib import Path
import random
import sys
import time

# resolve() would undo SUBST and break SentencePiece on a CJK workspace path.
ROOT = Path(os.path.abspath(__file__)).parents[1]
PROJECT = ROOT / 'IndexTTS2'
sys.path[:0] = [str(PROJECT), str(ROOT)]
os.environ.setdefault('HF_HUB_OFFLINE', '1')
os.environ.setdefault('TRANSFORMERS_OFFLINE', '1')
os.chdir(PROJECT)

import numpy as np
import soundfile as sf
import torch
import yaml
from indextts.infer_v2 import IndexTTS2

# Load model dependencies first. Only the app's missing HTTP client dependencies
# use this fallback; do not install packages into or alter the model environment.
sys.path.append(str(ROOT / '.venv' / 'Lib' / 'site-packages'))
import httpx
from brain.config import TTSConfig
from brain.index_expression import index_emotion_options
from brain.speech_text import SentenceChunker
from brain.tts import IndexTTS2TTS


CASES = [
    ('reaction', '反应与前后句衔接',
     '啊，原来是这样！我刚才还以为你要卖个关子呢，你这么一说，前面那件事也就说得通了。',
     ['惊讶', '吐槽']),
    ('calm', '平静语气与完整句子',
     '你说的那种总觉得有件事还没做完的感觉，我大概明白，先别急着把今晚也安排满。',
     ['平静']),
]


def chunks(text, natural):
    chunker = SentenceChunker(max_chars=96, first_clause_chars=0 if natural else 24,
                             join_spoken_leadins=True)
    # Fixed incremental delivery reproduces streaming chunk boundaries.
    output = []
    for offset in range(0, len(text), 3):
        output.extend(chunker.feed(text[offset:offset + 3]))
    return output + chunker.flush()


async def main():
    output = ROOT / 'data' / 'voice-previews' / time.strftime('index-naturalness-%Y%m%d-%H%M%S')
    output.mkdir(parents=True, exist_ok=False)
    settings = yaml.safe_load((ROOT / 'config' / 'settings.yaml').read_text(encoding='utf-8'))
    allowed = {item.name for item in fields(TTSConfig)}
    options = {key: value for key, value in settings['tts'].items() if key in allowed}
    print('OUTPUT', output, flush=True)
    print('Loading local IndexTTS2 for four finite audition clips...', flush=True)
    model = IndexTTS2(cfg_path='checkpoints/config.yaml', model_dir='checkpoints',
                     use_fp16=True, use_cuda_kernel=False, use_deepspeed=False)
    results = []
    for case_index, (name, label, text, emotions) in enumerate(CASES):
        for natural in (False, True):
            mode = 'after' if natural else 'before'
            folder = output / (name + '_' + mode)
            folder.mkdir()
            cfg = TTSConfig(**{**options, 'out_dir': str(folder),
                               'indextts2_natural_delivery': natural})
            voice = IndexTTS2TTS(cfg)
            segments = []

            async def synthesize(request):
                payload = json.loads(request.content)
                seed = 7500 + case_index * 100 + len(segments)
                random.seed(seed)
                np.random.seed(seed)
                torch.manual_seed(seed)
                torch.cuda.manual_seed_all(seed)
                emotion_options = index_emotion_options(
                    payload['emotion'], payload['emo_alpha'],
                    reference_style=payload['reference_style'],
                    natural_delivery=payload['natural_delivery'])
                raw = folder / f'raw_{len(segments):02d}.wav'
                began = time.perf_counter()
                model.infer(spk_audio_prompt=payload['ref_audio_path'], text=payload['text'],
                            output_path=str(raw), **emotion_options, use_random=False,
                            verbose=False, num_beams=payload['num_beams'])
                segments.append({key: value for key, value in payload.items() if key != 'request_id'} |
                                {'seed': seed, 'emotion_options': emotion_options,
                                 'raw': str(raw.relative_to(output)),
                                 'synthesis_seconds': round(time.perf_counter() - began, 3)})
                return httpx.Response(200, content=raw.read_bytes(), headers={'Content-Type': 'audio/wav'})

            voice._client = httpx.AsyncClient(transport=httpx.MockTransport(synthesize))
            rendered = []
            sample_rate = None
            try:
                for index, sentence in enumerate(chunks(text, natural)):
                    path = await voice.speak_segment(sentence, emotions[min(index, len(emotions) - 1)],
                                                     turn_id=name, segment_index=index)
                    audio, rate = sf.read(path, always_2d=True)
                    if sample_rate is not None and rate != sample_rate:
                        raise ValueError('segment sample rates differ')
                    if not len(audio) or not np.isfinite(audio).all():
                        raise ValueError('invalid audio')
                    sample_rate = rate
                    rendered.append(audio)
            finally:
                await voice.close()
            joined = np.concatenate(rendered, axis=0)
            target = output / f'{name}_{mode}.wav'
            # Keep model pauses and dynamics. No added gaps, breaths, pitch/speed,
            # or per-segment loudness changes; raw segments are also retained.
            sf.write(target, joined, sample_rate, subtype='PCM_16')
            results.append({'case': name, 'label': label, 'mode': mode, 'text': text,
                            'audio': target.name, 'seconds': round(len(joined) / sample_rate, 3),
                            'clipping_samples': int(np.sum(np.abs(joined) >= .999)),
                            'segments': segments})
            (output / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
            print('DONE', name, mode, results[-1]['seconds'], 'seconds', flush=True)

    cards = ''.join(f'<article><h2>{html.escape(item["label"])} · {"调整后" if item["mode"] == "after" else "调整前"}</h2>'
                    f'<p>{html.escape(item["text"])}</p><audio controls preload="metadata" src="{item["audio"]}"></audio>'
                    f'<p>{item["seconds"]} 秒</p></article>' for item in results)
    (output / 'index.html').write_text('''<!doctype html><html lang="zh"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>语音表达对照试听</title>
<style>body{max-width:1000px;margin:32px auto;padding:0 20px;background:#14271f;color:#eee7d4;font:16px/1.7 system-ui}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:18px}
article{padding:22px;background:#243c2f;border-radius:16px}h2{font-size:18px}audio{width:100%}p{color:#c6cfbb}</style>
<h1>语音表达对照试听</h1><p>同一句文本、原有音色，同一段序号固定随机种子。比较语气反应、句子衔接，以及是否仍像你选的声音。</p>
<p>这组是预设试听文本，不来自聊天记录。未添加呼吸音、变调、变速或后期音量调整。成功合成不能代替你的听感判断。</p><main>'''
                                               + cards + '</main></html>', encoding='utf-8')
    print('REPORT', output / 'index.html', flush=True)


if __name__ == '__main__':
    asyncio.run(main())
