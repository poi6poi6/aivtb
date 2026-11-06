"""Run the private test server, with a file-triggered graceful stop."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import time

ROOT = Path(__file__).resolve().parents[1]
FOLDER = ROOT / 'data' / 'minecraft' / 'server'


def main():
    if not (FOLDER / 'eula.txt').exists() or 'eula=true' not in (FOLDER / 'eula.txt').read_text().splitlines():
        raise SystemExit('请先阅读并同意 Minecraft EULA，再将 data/minecraft/server/eula.txt 中的 eula=false 改为 eula=true。')
    # Windows file lock prevents two wrappers racing to start the same world.
    import msvcrt
    with (FOLDER / 'wrapper.lock').open('a+b') as lock:
        lock.seek(0); lock.write(b'0'); lock.flush(); lock.seek(0)
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        java = Path(os.environ['LOCALAPPDATA']) / 'Packages/Microsoft.4297127D64EC6_8wekyb3d8bbwe/LocalCache/Local/runtime/java-runtime-gamma/windows-x64/java-runtime-gamma/bin/java.exe'
        if not java.is_file():
            raise SystemExit('没有找到启动器的 Java 17，请先启动一次 Minecraft 1.20.1。')
        stop = FOLDER / 'stop.request'
        stop.unlink(missing_ok=True)
        with (FOLDER / 'console.log').open('ab') as log:
            process = subprocess.Popen([str(java), '-Xms1G', '-Xmx3G', '-jar', 'server.jar', 'nogui'],
                cwd=FOLDER, stdin=subprocess.PIPE, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW)
            try:
                while process.poll() is None:
                    if stop.exists():
                        process.stdin.write(b'stop\n'); process.stdin.flush()
                        # Allow Minecraft to save normally; do not force-kill a world during saving.
                        process.wait()
                        break
                    time.sleep(0.5)
            finally:
                if process.poll() is None:
                    process.stdin.write(b'stop\n'); process.stdin.flush(); process.wait()
                stop.unlink(missing_ok=True)
        raise SystemExit(process.returncode)


if __name__ == '__main__':
    main()
