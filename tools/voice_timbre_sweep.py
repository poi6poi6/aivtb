"""Offline, controlled reference/beam comparison with a local listening report."""
import html
import json
import os
from pathlib import Path
import random
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / 'IndexTTS2'
sys.path[:0] = [str(PROJECT), str(ROOT)]
os.chdir(PROJECT)
os.environ.setdefault('HF_HUB_OFFLINE', '1')

import numpy as np
import soundfile as sf
import torch
from indextts.infer_v2 import IndexTTS2

OUT = ROOT / 'data/audio/timbre_sweep'
OUT.mkdir(parents=True, exist_ok=True)
reference = ROOT / 'GPT-SoVITS/references/reference.wav'
samples, rate = sf.read(reference, always_2d=True)
mono = samples.mean(axis=1)
window = int(rate * .02)
energy = np.array([np.sqrt(np.mean(mono[i:i+window] ** 2))
                   for i in range(0, len(mono), window)])
quiet = np.flatnonzero(energy < .006) * .02

def boundary(low, high, fallback):
    choices = quiet[(quiet >= low) & (quiet <= high)]
    return float(choices[np.argmin(abs(choices - fallback))]) if len(choices) else fallback

start = boundary(3, 6, 4.5)
end = boundary(12, 14.5, 14)
middle = OUT / 'reference_later.wav'
sf.write(middle, samples[int(start*rate):int(end*rate)], rate, subtype='PCM_16')
refs = {'full':reference, 'opening':ROOT / 'GPT-SoVITS/references/reference-short.wav', 'later':middle}
texts = {'question':'今天想聊点什么？慢慢说，我在听。', 'short':'好，我在听。',
         'long':'今天如果有点累，就先歇一会儿吧。你刚才说的那件事，我还想听听后面怎么样了。'}
jobs = [(ref, 1, 'question') for ref in refs] + [('full', 3, 'question')]
jobs += [(ref, beams, text) for ref, beams in [('full',1),('later',1),('full',3)]
         for text in ['short','long']]
model = IndexTTS2(cfg_path='checkpoints/config.yaml', model_dir='checkpoints',
                 use_fp16=True, use_cuda_kernel=False, use_deepspeed=False)
results = []
for ref, beams, kind in jobs:
    name = f'{ref}_beam{beams}_{kind}'
    random.seed(75); np.random.seed(75); torch.manual_seed(75); torch.cuda.manual_seed_all(75)
    path = OUT / (name + '.wav')
    began = time.perf_counter()
    model.infer(spk_audio_prompt=str(refs[ref]), text=texts[kind], output_path=str(path),
                emo_vector=None, use_random=False, verbose=False, num_beams=beams)
    elapsed = time.perf_counter() - began
    audio, sr = sf.read(path, always_2d=True)
    rms = float(np.sqrt(np.mean(audio ** 2)))
    peak = float(np.max(np.abs(audio)))
    # Comparable playback level, preserving raw output; RMS matching is not LUFS.
    gain = min(.1 / max(rms, 1e-9), .95 / max(peak, 1e-9))
    listening = OUT / (name + '_listen.wav')
    sf.write(listening, audio * gain, sr, subtype='PCM_16')
    results.append(dict(name=name, reference=ref, beams=beams, text=texts[kind],
                        seconds=round(len(audio)/sr,3), synthesis_seconds=round(elapsed,3),
                        rms_dbfs=round(20*np.log10(max(rms,1e-9)),2),
                        peak_dbfs=round(20*np.log10(max(peak,1e-9)),2),
                        clipping_samples=int(np.sum(np.abs(audio)>=.999)),
                        listen=listening.name, raw=path.name))
    (OUT/'results.json').write_text(json.dumps({'crop_seconds':[start,end], 'results':results},
                                             ensure_ascii=False, indent=2), encoding='utf-8')
    print('DONE', name, elapsed, flush=True)

cards = ''.join(f'''<article><h2>{html.escape(r['name'])}</h2><p>{html.escape(r['text'])}</p>
<audio controls preload="none" src="{r['listen']}"></audio>
<p>音频 {r['seconds']} 秒 · 合成 {r['synthesis_seconds']} 秒 · <a href="{r['raw']}">原始录音</a></p></article>'''
                for r in results)
(OUT/'index.html').write_text('''<!doctype html><html lang="zh"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>助手 · 音色对照</title>
<style>body{margin:32px auto;max-width:960px;padding:0 20px;background:#14271f;color:#eee7d4;font:15px/1.8 system-ui}
main{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px}article{padding:20px;background:#243c2f;border-radius:16px}
h2{font-size:16px}a{color:#e7cb92}audio{width:100%}p{color:#c6cfbb}</style>
<h1>助手 · 音色对照</h1><p>full：完整样例；opening：原有开头片段；later：按低能量位置截取的后段。beam1 / beam3：候选搜索参数。</p>
<p>播放器使用 RMS 音量匹配版，也可打开原始录音。未做降噪、变调或变速。所有录音使用参考声音模式及相同随机种子。</p>
<p>重点听：是否像参考声音、尾音是否发虚、短句和长句是否保持同一声线。合成耗时受模型缓存影响，不能当作严格性能基准。参数与声学数据不能代替试听结论。</p><main>'''
                         + cards + '</main></html>', encoding='utf-8')
print('REPORT', OUT/'index.html', flush=True)
