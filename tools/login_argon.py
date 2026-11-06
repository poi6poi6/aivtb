"""Launch official OAuth interactively with the same network as persona calls."""
from __future__ import annotations

from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brain.antigravity_chat import _subscription_env


def main() -> int:
    executable = ROOT / 'data' / 'argon-runtime' / 'bin' / 'agy.exe'
    workspace = ROOT / 'data' / 'argon-runtime' / 'chat'
    if not executable.is_file():
        print('Official Antigravity CLI is missing from data/argon-runtime/bin.', file=sys.stderr)
        return 1
    workspace.mkdir(parents=True, exist_ok=True)
    env = _subscription_env()
    has_proxy = any(key.upper() in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY') and value
                    for key, value in env.items())
    print('Starting official Antigravity CLI.', flush=True)
    print('Proxy enabled for this CLI session.' if has_proxy
          else 'No HTTP proxy configured for this CLI session.', flush=True)
    print('Choose Google OAuth. Paste the NEW code only at its authorization-code prompt.', flush=True)
    try:
        # Inherit the user's terminal, so the OAuth code goes straight to the
        # official CLI. This wrapper never reads or stores authorization codes.
        return subprocess.call([str(executable)], cwd=workspace, env=env)
    except KeyboardInterrupt:
        return 130
    except OSError:
        print('Could not start the official CLI. Check its installation.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
