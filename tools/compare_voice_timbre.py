"""Generate a reproducible local pair, changing only emotion guidance."""
import os
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
PROJECT = ROOT / 'IndexTTS2'
sys.path.insert(0, str(PROJECT))
sys.path.insert(0, str(ROOT))
os.chdir(PROJECT)
os.environ.setdefault('HF_HUB_OFFLINE', '1')

import numpy as np
import torch
from indextts.infer_v2 import IndexTTS2
from indextts2_server import EMOTION_VECTORS

output = ROOT / 'data' / 'audio' / 'voice_comparison'
output.mkdir(parents=True, exist_ok=True)
tts = IndexTTS2(cfg_path='checkpoints/config.yaml', model_dir='checkpoints',
                use_fp16=True, use_cuda_kernel=False, use_deepspeed=False)
text = '今天想聊点什么？慢慢说，我在听。'
reference = ROOT / 'GPT-SoVITS' / 'references' / 'reference.wav'
for name, vector in [('A_fixed_emotion', EMOTION_VECTORS['平静']), ('B_reference_style', None)]:
    random.seed(75)
    np.random.seed(75)
    torch.manual_seed(75)
    torch.cuda.manual_seed_all(75)
    tts.infer(spk_audio_prompt=str(reference), text=text,
              output_path=str(output / (name + '.wav')), emo_vector=vector,
              emo_alpha=0.51, use_random=False, verbose=False, num_beams=1)
    print('SAVED', name, flush=True)
