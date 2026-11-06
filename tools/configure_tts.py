"""Interactive TTS selection and a read-only Fish Speech service check."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path
import sys
import tempfile
from urllib.parse import urlsplit
import uuid

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from brain.config import TTSConfig, load_dotenv

FIELDS = {'backend', 'fish_speech_url', 'fish_speech_reference_id',
          'fish_speech_ref_audio_path', 'fish_speech_prompt_text'}
BACKENDS = {'indextts2', 'gpt_sovits', 'fish_speech', 'mock'}


def fish_url(value: str) -> str:
    value = value.strip().rstrip('/')
    try:
        url = urlsplit(value)
        valid = (url.scheme in {'http', 'https'} and url.hostname and not url.username
                 and not url.password and not url.query and not url.fragment and not url.path)
        _ = url.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('Fish Speech 请填写 HTTP(S) 服务根地址，不含 /v1/tts、凭据或查询参数。')
    return value


def settings_document(path: Path):
    original = path.read_bytes()
    text = original.decode('utf-8-sig')
    doc = yaml.compose(text)
    if not isinstance(doc, yaml.MappingNode):
        raise ValueError('配置必须是 YAML 映射。')
    roots = [key.value for key, _ in doc.value]
    if len(set(roots)) != len(roots):
        raise ValueError('配置含重复顶层键，请先手动整理。')
    sections = [value for key, value in doc.value if key.value == 'tts']
    if len(sections) != 1 or not isinstance(sections[0], yaml.MappingNode) or sections[0].flow_style:
        raise ValueError('请先在配置中建立普通的 tts: 配置段。')
    node = sections[0]
    names = [key.value for key, _ in node.value]
    if len(set(names)) != len(names) or '<<' in names:
        raise ValueError('tts 段含重复键或 YAML 合并，请先手动整理。')
    raw = yaml.safe_load(text)
    if not isinstance(raw.get('tts'), dict):
        raise ValueError('tts 配置必须是映射。')
    return original, text, node, raw


def save_settings(path: Path, updates: dict) -> Path:
    """Edit only selected scalar tokens, preserving comments and other sections."""
    if not updates or set(updates) - FIELDS:
        raise ValueError('不支持的语音设置字段。')
    if updates.get('backend') not in BACKENDS:
        raise ValueError('请选择支持的语音后端。')
    updates = dict(updates)
    original, text, node, raw = settings_document(path)
    combined = {**asdict(TTSConfig()), **raw['tts'], **updates}
    if combined['backend'] == 'fish_speech':
        updates['fish_speech_url'] = fish_url(combined['fish_speech_url'])
        if not combined['fish_speech_reference_id'].strip():
            audio = combined['fish_speech_ref_audio_path'].strip()
            transcript = combined['fish_speech_prompt_text'].strip()
            if bool(audio) != bool(transcript):
                raise ValueError('参考音频与逐字转写必须成对填写。')
            if audio:
                reference = Path(audio)
                if not reference.is_absolute():
                    reference = path.parent.parent / reference
                if not reference.is_file():
                    raise ValueError('参考音频不存在，请检查本机文件路径。')
    edits = []
    remaining = dict(updates)
    newline = '\r\n' if '\r\n' in text else '\n'
    for key, value in node.value:
        if key.value not in remaining:
            continue
        if (not isinstance(value, yaml.ScalarNode) or value.start_mark.index < key.end_mark.index
                or text[value.start_mark.index:value.end_mark.index].lstrip().startswith(('&', '!'))):
            raise ValueError('待修改字段含别名、标签或复杂结构，请手动编辑。')
        replacement = json.dumps(remaining.pop(key.value), ensure_ascii=False)
        old_scalar = text[value.start_mark.index:value.end_mark.index]
        if value.style in ('|', '>') and '#' in old_scalar.splitlines()[0]:
            replacement += '  #' + old_scalar.splitlines()[0].split('#', 1)[1]
        # Literal/folded YAML scalars include their terminating newline.
        if old_scalar.endswith('\n'):
            replacement += newline
        edits.append((value.start_mark.index, value.end_mark.index, replacement))
    if remaining:
        insert = ''.join(f'  {key}: {json.dumps(value, ensure_ascii=False)}{newline}'
                         for key, value in remaining.items())
        index = node.end_mark.index
        # A final scalar may be at EOF without a terminating newline.
        if index and text[index - 1] != '\n':
            insert = newline + insert
        edits.append((index, index, insert))
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    expected = {**raw, 'tts': {**raw['tts'], **updates}}
    if yaml.safe_load(text) != expected:
        raise ValueError('无法在保留其他配置的情况下更新，请手动编辑 tts 段。')
    encoded = (b'\xef\xbb\xbf' if original.startswith(b'\xef\xbb\xbf') else b'') + text.encode('utf-8')
    if path.read_bytes() != original:
        raise ValueError('配置已被其他程序更改，请重新打开语音设置。')
    backup = path.with_name(path.name + '.tts-' + datetime.now().strftime('%Y%m%d-%H%M%S')
                            + '-' + uuid.uuid4().hex[:8] + '.bak')
    backup.write_bytes(original)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.tts-', suffix='.tmp', delete=False) as out:
            temporary = Path(out.name)
            out.write(encoded)
            out.flush()
            os.fsync(out.fileno())
        if path.read_bytes() != original:
            raise ValueError('配置已被其他程序更改，请重新打开语音设置。')
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return backup


def check_service(cfg: dict, *, client=None) -> str:
    url = fish_url(cfg.get('fish_speech_url', TTSConfig().fish_speech_url))
    key_name = cfg.get('fish_speech_api_key_env', TTSConfig().fish_speech_api_key_env)
    token = os.environ.get(key_name, '').strip()
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    owned = client is None
    client = client or httpx.Client(timeout=5, trust_env=False, follow_redirects=False)
    try:
        response = client.get(url + '/v1/health', headers=headers)
        if response.status_code in (401, 403):
            raise ValueError('Fish Speech 鉴权失败，请检查服务端与本地配置的环境变量。')
        if response.status_code != 200:
            raise ValueError(f'Fish Speech 健康检查失败（HTTP {response.status_code}）。')
        try:
            valid = response.json().get('status') == 'ok'
        except (ValueError, AttributeError):
            valid = False
        if not valid:
            raise ValueError('该地址未返回 Fish Speech 的健康状态，请核对服务和端口。')
        return url
    except httpx.RequestError:
        raise ValueError('无法连接 Fish Speech。请先启动独立的官方 API 服务，详见 docs/fish-speech.md。') from None
    finally:
        if owned:
            client.close()


def configure(path: Path, *, ask=input, tell=print) -> bool:
    _, _, _, raw = settings_document(path)
    cfg = {**asdict(TTSConfig()), **raw['tts']}
    tell('语音模型设置（保存后重启主服务生效；当前模型：' + str(cfg['backend']) + '）')
    tell('  1 Fish Speech（Drama 3 / S2-Pro）  2 IndexTTS2  3 GPT-SoVITS  4 mock（无真实声音）  0 返回')
    selected = ask('选择语音模型：').strip()
    if selected in {'', '0'}:
        return False
    choices = {'1': 'fish_speech', '2': 'indextts2', '3': 'gpt_sovits', '4': 'mock'}
    if selected not in choices:
        raise ValueError('请选择菜单中的数字。')
    updates = {'backend': choices[selected]}
    if updates['backend'] == 'fish_speech':
        url = ask(f"服务根地址 [回车保留 {cfg['fish_speech_url']}]：").strip()
        updates['fish_speech_url'] = url or cfg['fish_speech_url']
        tell('音色：1 保留已配置音色 / 2 使用服务端参考 ID / 3 本机参考音频和转写 / 4 随机音色')
        mode = ask('选择音色方式 [1]：').strip() or '1'
        if mode == '2':
            reference_id = ask('服务端已保存的参考 ID：').strip()
            if not reference_id:
                raise ValueError('参考 ID 不能为空。')
            updates.update(fish_speech_reference_id=reference_id, fish_speech_ref_audio_path='', fish_speech_prompt_text='')
        elif mode == '3':
            audio = ask('本机参考音频路径（可拖入文件）：').strip().strip('"')
            transcript = ask('该音频的逐字转写：').strip()
            if not audio or not transcript:
                raise ValueError('参考音频与逐字转写都不能为空。')
            updates.update(fish_speech_reference_id='', fish_speech_ref_audio_path=audio, fish_speech_prompt_text=transcript)
        elif mode == '4':
            updates.update(fish_speech_reference_id='', fish_speech_ref_audio_path='', fish_speech_prompt_text='')
        elif mode != '1':
            raise ValueError('请选择菜单中的音色方式。')
        tell('Fish Speech 使用外部已启动的官方服务；没有参考音色时，声音可能每次不同。')
    if ask('保存选择？[y/N]：').strip().lower() not in {'y', 'yes', '是'}:
        tell('未修改设置。')
        return False
    backup = save_settings(path, updates)
    tell('已保存。请停止并重新启动主服务；仅重复点击正式运行会复用旧进程。')
    tell('原配置备份：' + str(backup))
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='只检查配置的 Fish Speech 健康接口，不生成音频')
    parser.add_argument('--settings', type=Path, default=ROOT / 'config/settings.yaml')
    args = parser.parse_args()
    try:
        if args.check:
            load_dotenv()
            _, _, _, raw = settings_document(args.settings)
            url = check_service(raw['tts'])
            print('Fish Speech 服务可用：' + url + '（健康检查通过，尚未合成试听）')
        else:
            configure(args.settings)
        return 0
    except (EOFError, KeyboardInterrupt):
        print('\n已取消，设置未保存。')
        return 0
    except (OSError, ValueError, yaml.YAMLError) as exc:
        # YAML parser messages may include unrelated secret-bearing lines.
        print('语音设置失败：' + ('配置格式有误，请检查 settings.yaml。' if isinstance(exc, yaml.YAMLError) else str(exc)))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
