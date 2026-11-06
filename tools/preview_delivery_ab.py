"""Finite local same-text auditions, with no chat input or production edits.

Use the IndexTTS2 Python environment through an ASCII workspace alias on Windows.
The configured voice/profile, generation seed and text stay equal within a pair.
B is an experimental candidate, not a claim of controllable word-level prosody.
"""
from __future__ import annotations

import asyncio
from dataclasses import fields
import hashlib
import html
import json
import os
from pathlib import Path
import random
import sys
import time

ROOT = Path(os.path.abspath(__file__)).parents[1]
PROJECT = ROOT / "IndexTTS2"
sys.path[:0] = [str(PROJECT), str(ROOT)]
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"
os.chdir(PROJECT)

import numpy as np
import soundfile as sf
import torch
import yaml
from indextts.infer_v2 import IndexTTS2

sys.path.append(str(ROOT / ".venv" / "Lib" / "site-packages"))
import httpx
from brain.config import TTSConfig
from brain.index_expression import index_emotion_options
from brain.tts import IndexTTS2TTS


CASES = [
    ("chat", "日常聊天", "今天这段旋律挺舒服的，前面轻轻铺开，后半段反而更耐听。", "平静",
     "轻微混入开心情绪，试听轻松感与音色是否合适"),
    ("explain", "认真解释", "先把最重要的一件事做完，剩下的慢慢排，不用同时顾着所有事。", "平静",
     "只把采样 temperature 从 0.8 调到 0.9，试听句内节奏；不是逐字重音控制"),
    ("tease", "轻吐槽", "你这计划排得挺满，连休息都要预约，倒也很有你的风格。", "吐槽",
     "只略增吐槽情绪混合，试听能否听出调侃而不变尖刻"),
]


async def main():
    output = ROOT / "data" / "voice-previews" / time.strftime("delivery-ab-%Y%m%d-%H%M%S")
    output.mkdir(parents=True, exist_ok=False)
    settings = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
    allowed = {field.name for field in fields(TTSConfig)}
    options = {k: v for k, v in settings["tts"].items() if k in allowed}
    if options.get("backend") != "indextts2":
        raise ValueError("This audition expects the currently selected IndexTTS2 backend")
    print("OUTPUT", output, flush=True)
    print("Loading local model for six clips; offline, no service socket...", flush=True)
    model = IndexTTS2(cfg_path="checkpoints/config.yaml", model_dir="checkpoints",
                      use_fp16=True, use_cuda_kernel=False, use_deepspeed=False)
    report = {"method": "same text, same reference, same beam setting and fixed seed per pair",
              "postprocessing": "existing edge silence trim only; no normalization, pitch or speed edit",
              "results": []}
    for case_index, (name, label, text, emotion, change) in enumerate(CASES):
        pair_audio = []
        pair_rate = None
        for variant in ("A", "B"):
            folder = output / f"{name}_{variant}_source"
            folder.mkdir()
            # Keep A/B reproducible after B becomes the production preset.
            cfg = TTSConfig(**{**options, "out_dir": str(folder),
                               "indextts2_delivery_preset": "reference"})
            voice = IndexTTS2TTS(cfg)
            record = {"case": name, "label": label, "variant": variant, "text": text,
                      "change": "参考预设 A" if variant == "A" else change}

            async def synthesize(request):
                payload = json.loads(request.content)
                seed = 20261019 + case_index
                random.seed(seed)
                np.random.seed(seed)
                torch.manual_seed(seed)
                torch.cuda.manual_seed_all(seed)
                effective = dict(payload)
                temperature = .8
                if variant == "B":
                    if name == "chat":
                        # Same speaker embedding; modest positive emotion mix.
                        effective.update(emotion="开心", emo_alpha=.60 * .92)
                    elif name == "explain":
                        # The installed model supports this generation kwarg;
                        # the production HTTP adapter does not expose it yet.
                        temperature = .9
                    elif name == "tease":
                        effective["emo_alpha"] = .65
                emotion_options = index_emotion_options(
                    effective["emotion"], effective["emo_alpha"],
                    reference_style=effective["reference_style"],
                    natural_delivery=effective["natural_delivery"])
                reference = Path(effective["ref_audio_path"])
                if not reference.is_absolute():
                    reference = ROOT / reference
                if not reference.is_file():
                    raise FileNotFoundError(reference)
                raw = folder / "raw.wav"
                began = time.perf_counter()
                model.infer(spk_audio_prompt=str(reference), text=effective["text"],
                            output_path=str(raw), **emotion_options, use_random=False,
                            verbose=False, num_beams=effective["num_beams"], temperature=temperature)
                record.update({"seed": seed, "reference": reference.name,
                               "reference_sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
                               "emotion_options": emotion_options, "emotion": effective["emotion"],
                               "temperature": temperature, "num_beams": effective["num_beams"],
                               "synthesis_seconds": round(time.perf_counter()-began, 3)})
                return httpx.Response(200, content=raw.read_bytes(), headers={"Content-Type":"audio/wav"})

            voice._client = httpx.AsyncClient(transport=httpx.MockTransport(synthesize))
            try:
                rendered = await voice.speak(text, emotion)
            finally:
                await voice.close()
            audio, rate = sf.read(rendered, always_2d=True)
            if not len(audio) or not np.isfinite(audio).all() or not 1 < len(audio)/rate < 45:
                raise ValueError("Invalid or unreasonable audition audio")
            if pair_rate is not None and pair_rate != rate:
                raise ValueError("Inconsistent sample rates")
            pair_rate = rate
            pair_audio.append(audio)
            target = output / f"{name}_{variant}.wav"
            target.write_bytes(Path(rendered).read_bytes())
            record.update({"audio": target.name, "seconds": round(len(audio)/rate,3),
                           "sample_rate": rate, "peak": float(np.max(np.abs(audio))),
                           "rms": float(np.sqrt(np.mean(audio**2))),
                           "clipping_samples": int(np.sum(np.abs(audio)>=.999))})
            report["results"].append(record)
            (output / "results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print("DONE", name, variant, record["seconds"], "seconds", flush=True)
        # A comparison gap belongs to the audition player, not synthesized speech.
        pair = np.concatenate([pair_audio[0], np.zeros((round(.9*pair_rate), pair_audio[0].shape[1])), pair_audio[1]])
        sf.write(output / f"{name}_AB.wav", pair, pair_rate, subtype="PCM_16")

    cards = []
    for name, label, text, _, change in CASES:
        cards.append(f'<article><h2>{html.escape(label)}</h2><p>{html.escape(text)}</p>'
                     f'<p>先 A，再 B；中间留 0.9 秒比较间隔。</p>'
                     f'<audio controls preload="metadata" src="{name}_AB.wav"></audio>'
                     f'<details><summary>单独重听与本组调整</summary><p>A：参考预设</p>'
                     f'<audio controls src="{name}_A.wav"></audio><p>B：{html.escape(change)}</p>'
                     f'<audio controls src="{name}_B.wav"></audio></details></article>')
    (output / "index.html").write_text('''<!doctype html><html lang="zh"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>三种说话方式 · A/B试听</title>
<style>body{max-width:900px;margin:30px auto;padding:0 22px;background:#14271f;color:#eee7d4;font:16px/1.7 system-ui}article{padding:22px;margin:18px 0;background:#243c2f;border-radius:16px}audio{width:100%}p{color:#d0d7c9}summary{cursor:pointer}h2{font-size:20px}</style>
<h1>三种说话方式 · A/B试听</h1><p>A 为参考预设，B 为表达预设的候选。每组文字、参考音频和种子相同；此工具不会修改正式设置。</p>
<p>重点听：语调是否自然、重点是否清楚、声线是否仍合适。B 不一定更好。没有插入呼吸、变速、变调或调整音量。</p>
''' + "".join(cards) + "</html>", encoding="utf-8")
    print("REPORT", output / "index.html", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
