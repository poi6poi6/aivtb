"""Bounded IndexTTS2 emotion mixing that retains the selected reference voice."""
from __future__ import annotations

import math


# Official IndexTTS2 order: happy, angry, sad, afraid, disgusted, melancholic,
# surprised, calm. The legacy full-emotion mode keeps these values unchanged.
EMOTION_VECTORS = {
    '开心': [0.42, 0.0, 0.0, 0.0, 0.0, 0.0, 0.03, 0.18],
    '吐槽': [0.14, 0.06, 0.0, 0.0, 0.04, 0.0, 0.0, 0.28],
    '惊讶': [0.08, 0.0, 0.0, 0.0, 0.0, 0.0, 0.35, 0.12],
    '得意': [0.30, 0.0, 0.0, 0.0, 0.0, 0.0, 0.03, 0.22],
    '委屈': [0.0, 0.0, 0.28, 0.0, 0.0, 0.08, 0.0, 0.22],
    '平静': [0.03, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.40],
}


def index_emotion_options(emotion: str, alpha: float, *,
                          reference_style: bool = True,
                          natural_delivery: bool = False) -> dict:
    alpha = float(alpha)
    if not math.isfinite(alpha):
        raise ValueError('emo_alpha must be finite')
    alpha = max(0.0, min(1.0, alpha))
    if not reference_style:
        return {'emo_vector': list(EMOTION_VECTORS.get(emotion, EMOTION_VECTORS['平静'])),
                'emo_alpha': alpha}
    if not natural_delivery or emotion == '平静' or emotion not in EMOTION_VECTORS:
        return {'emo_vector': None, 'emo_alpha': alpha}
    vector = list(EMOTION_VECTORS[emotion])
    # infer_v2 scales the vector by alpha before mixing with the reference.
    # Keep at least 85% of its reference emotion embedding. This is a mixing
    # bound, not a claim about perceived voice similarity or loudness.
    weight = min(0.15, alpha * 0.22)
    return {'emo_vector': vector, 'emo_alpha': weight / sum(vector)}
