"""Build the local, unpacked browser helper download without external tools."""
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED
import json


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'tools' / 'bilibili-browser-extension'
OUTPUT = ROOT / 'brain' / 'static' / 'downloads' / 'bilibili-browser-extension.zip'


def build():
    manifest = json.loads((SOURCE / 'manifest.json').read_text(encoding='utf-8'))
    if manifest.get('manifest_version') != 3:
        raise ValueError('Expected Manifest V3 extension')
    files = sorted(path for path in SOURCE.rglob('*')
                   if path.is_file() and path.suffix in {'.js', '.json', '.md', '.png', '.svg'})
    if not files:
        raise ValueError('Extension files are missing')
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(OUTPUT, 'w', ZIP_DEFLATED) as archive:
        for path in files:
            archive.write(path, path.relative_to(SOURCE).as_posix())
    print(json.dumps({'output': str(OUTPUT), 'version': manifest['version'],
                      'files': [p.relative_to(SOURCE).as_posix() for p in files]}, ensure_ascii=False))


if __name__ == '__main__':
    build()
