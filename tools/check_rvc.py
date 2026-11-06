"""Verify installed offline runtime and base models; does not claim a character model exists."""
import json
import os
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
rvc = root / 'RVC'
os.chdir(rvc)
os.environ['PATH'] = str(rvc / 'bin') + os.pathsep + os.environ.get('PATH', '')
sys.path.insert(0, str(rvc))
sys.path.insert(0, str(rvc / 'tools'))
import torch
from infer.hubert import load_hubert_model
from infer.rmvpe import RMVPE
from infer.vc.modules import VC
from pymss import create_separator

assert torch.cuda.is_available(), 'CUDA unavailable'
print('GPU:', torch.cuda.get_device_name(), flush=True)
x = torch.ones(4, device='cuda')
assert (x * 2).sum().item() == 8
hubert = load_hubert_model('cuda', True)
del hubert
pitch = RMVPE(str(rvc / 'assets/rmvpe/rmvpe.pt'), is_half=True, device='cuda')
del pitch
torch.cuda.empty_cache()
cfg = json.loads((root/'config/rvc.json').read_text(encoding='utf-8'))
with create_separator(cfg['separator_model'], model_dir=str(rvc/'assets/pymss_weights'), device='cuda',
                      output_format='wav', store_dirs=str(root/'data/music/validation')) as separator:
    print('Separation model loaded', flush=True)
if len(sys.argv) > 1:
    stems = {name:str(root/'data/music/validation'/name) for name in ('vocals', 'instrument')}
    with create_separator(cfg['separator_model'], model_dir=str(rvc/'assets/pymss_weights'), device='cuda',
                          output_format='wav', store_dirs=stems) as separator:
        assert separator.process_folder(str(Path(sys.argv[1]).resolve()))
    for folder in stems.values():
        assert list(Path(folder).glob('*.wav')), 'Missing stem: ' + folder
(rvc/'assets/setup-ready.json').write_text(json.dumps({'torch':torch.__version__, 'gpu':torch.cuda.get_device_name(),
    'rvc_revision':'81eed5e8f68b6bed1789f682fe78cdd324495afc', 'separator':cfg['separator_model']}), encoding='utf-8')
print('RVC base runtime verified; character conversion still requires the configured .pth model.', flush=True)
