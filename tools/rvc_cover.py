"""Run inside RVC/.venv; isolate GPU inference from the companion process."""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RVC = ROOT / 'RVC'


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True)
    parser.add_argument('--work', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    cfg = json.loads((ROOT / 'config/rvc.json').read_text(encoding='utf-8'))
    work = Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)
    sys.path.insert(0, str(RVC))
    sys.path.insert(0, str(RVC / 'tools'))
    os.chdir(RVC)
    os.environ['PATH'] = str(RVC / 'bin') + os.pathsep + os.environ.get('PATH', '')
    import numpy as np
    import av
    import soundfile as sf
    from scipy.signal import resample_poly
    from math import gcd
    from pymss import create_separator

    with av.open(args.source) as source_audio:
        if source_audio.duration is None or not 0 < source_audio.duration / av.time_base <= 600:
            raise ValueError('原曲需有可读取的时长，且不超过10分钟')

    print('STAGE:separating', flush=True)
    stems = {name: str(work / name) for name in ('vocals', 'instrument')}
    with create_separator(cfg['separator_model'], model_dir=str(RVC / 'assets/pymss_weights'),
                          device='cuda', output_format='wav', store_dirs=stems) as separator:
        if not separator.process_folder(args.source):
            raise RuntimeError('人声分离未产出音轨')
    paths = {}
    for name, folder in stems.items():
        files = list(Path(folder).glob('*.wav'))
        if len(files) != 1:
            raise RuntimeError('分离音轨缺失或不唯一：' + name)
        paths[name] = files[0]
    print('STAGE:converting', flush=True)
    from infer.cli import main as convert
    model = Path(cfg['model'])
    if not model.is_absolute():
        model = ROOT / model
    converted = work / 'converted.wav'
    argv = ['--model', str(model), '--input', str(paths['vocals']), '--output', str(converted),
            '--pitch', str(int(cfg['pitch'])), '--f0-method', 'rmvpe', '--overwrite',
            '--index-rate', str(float(cfg['index_rate']) if cfg.get('index') else 0)]
    if cfg.get('index'):
        index = Path(cfg['index'])
        argv += ['--index', str(index if index.is_absolute() else ROOT / index)]
    if convert(argv) != 0 or not converted.exists():
        raise RuntimeError('RVC没有产出有效人声')
    print('STAGE:mixing', flush=True)
    vocal, sr = sf.read(converted, always_2d=True)
    backing, target_sr = sf.read(paths['instrument'], always_2d=True)
    if sr != target_sr:
        divisor = gcd(sr, target_sr)
        vocal = resample_poly(vocal, target_sr // divisor, sr // divisor, axis=0)
    if abs(len(vocal) - len(backing)) > target_sr * 2:
        raise RuntimeError('转换人声和伴奏长度偏差超过两秒')
    if vocal.shape[1] == 1 and backing.shape[1] > 1:
        vocal = np.repeat(vocal, backing.shape[1], axis=1)
    if vocal.shape[1] != backing.shape[1]:
        raise RuntimeError('混音声道不匹配')
    vocal = np.pad(vocal[:len(backing)], ((0, max(0, len(backing)-len(vocal))), (0, 0)))
    mixed = vocal + backing
    if not np.isfinite(mixed).all() or not len(mixed):
        raise RuntimeError('无效音频')
    peak = np.max(np.abs(mixed))
    if peak < 1e-6:
        raise RuntimeError('混音结果无声')
    mixed *= min(1, 0.95 / peak)
    sf.write(args.output, mixed, target_sr, subtype='PCM_16')
    print('STAGE:ready', flush=True)


if __name__ == '__main__':
    main()
