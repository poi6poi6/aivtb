"""Fixed host entry point for release activation/recovery; no arbitrary commands."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from brain.upgrade_release import LocalRuntime, ReleaseController, read_json, workspace_id, configured_health_url, release_policy


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--release-id')
    parser.add_argument('--recover', action='store_true')
    args = parser.parse_args()
    if not args.release_id and not args.recover:
        parser.error('a fixed release ID or --recover is required')
    journal = read_json(ROOT / 'data/upgrades/release.json')
    if journal.get('workspace_id') != workspace_id(ROOT):
        raise ValueError('Release belongs to another workspace')
    allowed = journal.get('allowed_paths', []) if args.recover else release_policy(ROOT, manual=journal.get('manual') is True)
    controller = ReleaseController(ROOT, allowed_paths=allowed,
        runtime=LocalRuntime(ROOT, configured_health_url(ROOT)))
    result = controller.run(args.release_id, recover=args.recover)
    print('release state:', result['state'])
    return 0 if result['state'] in {'verified', 'rolled_back', 'cancelled'} else 1


if __name__ == '__main__':
    raise SystemExit(main())
