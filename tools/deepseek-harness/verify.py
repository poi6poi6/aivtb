"""Start the official SDK without credentials or model input, then close it."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from brain.deepseek_harness import HarnessRunner


def main():
    runner = HarnessRunner(root=ROOT)
    allowed = {'PATH', 'SystemRoot', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT', 'TEMP', 'TMP'}
    env = {key: value for key, value in os.environ.items() if key in allowed}
    with tempfile.TemporaryDirectory(prefix='vtuber-dsh-check-') as directory:
        patch = Path(directory) / 'bili.patch.yml'
        patch.write_text(json.dumps(runner._patch(), ensure_ascii=False), encoding='utf-8')
        result = subprocess.run(['node', str(runner.folder / 'verify.mjs'), '--handshake', str(patch)],
                                cwd=runner.folder, env=env, timeout=40, check=False,
                                capture_output=True, text=True, encoding='utf-8',
                                **({'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}))
        print(result.stdout, end='')
        if result.returncode:
            print(result.stderr, end='', file=sys.stderr)
        return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
