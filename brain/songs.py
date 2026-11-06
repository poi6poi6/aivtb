"""Local song catalog. Only explicit files in data/songs are published."""
import hashlib
import re
import unicodedata
from pathlib import Path
from urllib.parse import urlsplit

FORMATS = {'.mp3', '.wav', '.ogg', '.m4a', '.flac', '.webm'}

_GREETING = r'(?:早上好|上午好|中午好|下午好|晚上好|你好(?:呀|啊)?|您好|哈喽|哈啰|嗨|早安|晚安)'
_NAME = r'(?:助手)'
_QUESTION = r'(?:(?:你)?(?:能不能|可不可以|能否|可以))'
_POLITE = r'(?:请(?:你)?|麻烦(?:你)?|帮我|给我|为我)'
_PLAY = r'(?:(?:我)?想(?:要)?(?:听(?:一首|一下)?|点(?:一)?首(?:歌)?|点歌)|点一首歌|点一首|点首歌|点首|点歌|播放|放一下|放一首|放首|放歌|来一首|来首|听一首|听一下)'
_NESTED_PLAY = r'(?:放个|放一首|放首|放一下|播放|来一首|来首)'
_BILI = r'(?:B\s*站|哔哩哔哩|bilibili)'
_PLATFORM = rf'(?:(?:在|去)\s*)?{_BILI}(?:上|里)?(?:的)?\s*'
_BILI_ACTION = rf'(?:{_PLAY}|搜索|搜一下|搜|找|打开|点开|听)'
_QUOTES = {'"': '"', "'": "'", '“': '”', '‘': '’', '《': '》', '「': '」', '『': '』'}
_SEPARATORS = r'[\s,，、:：!！。]*'
_END_MARKS = '。!！?？'
_COURTESY = r'(?:吧|呀|啊|好吗|好不好|可以吗|行吗|谢谢|拜托了|吗)'


def _query_text(value: str) -> str:
    value = value.strip().lstrip(':：').strip().rstrip('。！？!').strip()
    while len(value) >= 2 and _QUOTES.get(value[0]) == value[-1]:
        value = value[1:-1].strip()
    return value


def normalize_song_title(value: str) -> str:
    """A catalog comparison key; preserve the original title and opaque id."""
    if not isinstance(value, str):
        return ''
    value = unicodedata.normalize('NFKC', value).casefold()
    return ''.join(c for c in value if not c.isspace() and not unicodedata.category(c).startswith('P'))


def match_songs(songs: list[dict], query: str) -> list[dict]:
    """Exact normalized titles win; callers must reject ambiguous matches."""
    key = normalize_song_title(_query_text(query)) if isinstance(query, str) else ''
    if not key:
        return []
    exact = [song for song in songs if normalize_song_title(song.get('title', '')) == key]
    return exact or [song for song in songs if key in normalize_song_title(song.get('title', ''))]


def _bilibili_reference(value: str) -> bool:
    if re.fullmatch(r'BV[A-Za-z0-9]{10}', value, re.IGNORECASE | re.ASCII):
        return True
    try:
        url = urlsplit(value if '://' in value else 'https://' + value)
        return (url.scheme in {'http', 'https'} and not url.username and not url.password
                and url.hostname in {'bilibili.com', 'www.bilibili.com', 'm.bilibili.com'}
                and url.port in {None, 80, 443}
                and re.fullmatch(r'/video/(?:BV[A-Za-z0-9]{10}|av\d+)/?', url.path, re.IGNORECASE | re.ASCII) is not None)
    except ValueError:
        return False


def _command_prefix(value: str) -> tuple[str, bool]:
    """Consume only known vocatives/requests, never search within a sentence."""
    question = False
    for _ in range(8):
        prefix = re.match(rf'(?:{_GREETING}|{_NAME}){_SEPARATORS}', value, re.IGNORECASE)
        if prefix:
            value = value[prefix.end():]
            continue
        prefix = re.match(rf'{_QUESTION}\s*', value)
        if prefix:
            question = True
            value = value[prefix.end():]
            continue
        prefix = re.match(rf'{_POLITE}\s*', value)
        if prefix:
            value = value[prefix.end():]
            continue
        break
    return value, question


def _command_query(value: str, *, question=False, platform=False) -> str:
    value = value.strip().lstrip(':：,，').strip().rstrip(_END_MARKS).strip()
    if platform:
        value = re.sub(r'\s*(?:并|然后)(?:播放|打开)$', '', value).strip()
    # Quoting explicitly marks the song boundary. Its own punctuation and 吧
    # belong to the title; only a polite suffix OUTSIDE the quote is removable.
    if value and value[0] in _QUOTES:
        close = value.rfind(_QUOTES[value[0]])
        if close > 0 and re.fullmatch(rf'{_SEPARATORS}(?:{_COURTESY})?', value[close + 1:]):
            value = value[1:close].strip()
            while len(value) >= 2 and _QUOTES.get(value[0]) == value[-1]:
                value = value[1:-1].strip()
            return value
    value = re.sub(rf'[\s,，]+{_COURTESY}$', '', value).strip()
    if question:
        # A capability-request prefix makes 吗 an utterance suffix. Bare titles
        # such as 走吧 and 你好吗 otherwise remain intact.
        suffix = re.search(r'(?:可以吗|好吗|好不好|行吗|吗)$', value)
        if suffix and suffix.start() > 0:
            value = value[:suffix.start()].strip()
    return value


def _query_is_discussion(value: str) -> bool:
    # A quoted title may legitimately contain any of these words.
    if value and value[0] in _QUOTES:
        close = value.rfind(_QUOTES[value[0]])
        if close > 0:
            tail = value[close + 1:].strip().rstrip(_END_MARKS).strip()
            if re.fullmatch(rf'{_SEPARATORS}(?:{_COURTESY})?', tail):
                return False
            value = tail
    return bool(re.search(r'(?:是什么意思|什么意思|是什么|会怎么样|会怎样|怎么样|怎么用|如何使用|怎么操作|的意思|的功能|功能介绍|这个功能|了吗|了没|过吗)[。!！?？]*$', value)
                or re.match(r'(?:功能|这个功能|你(?:说|讲|聊)|关于)', value)
                or re.search(r'(?:^|[,，;；]|但是|不过|然后|但)\s*(?:现在|先|暂时|还是|请)?\s*(?:(?:不要|别|不用|不想)(?:再)?(?:播放|放歌|点歌|听歌|放个|放一首|放首|放一下|放(?=$|[了吧]))|取消(?:播放|点歌)|(?:稍后|等会)(?:再)?(?:播放|放歌|点歌))', value))


def _platform_command_query(value: str, *, question=False) -> str | None:
    """Remove one extra spoken play request, never recursively rewrite titles."""
    value = value.strip().lstrip(':：,，').strip().rstrip(_END_MARKS).strip()
    # A platform request can contain a second verb: B站点歌 / 给我放个 / 歌名.
    # An explicitly quoted title always bypasses this interpretation.
    request = re.match(rf'^(?:{_POLITE}\s*){{0,3}}{_NESTED_PLAY}\s*[:：,，]?\s*(.*)$', value)
    if not request:
        guarded = re.sub(rf'^(?:{_POLITE}\s*){{1,3}}', '', value)
        return None if _query_is_discussion(guarded) else _command_query(value, question=question, platform=True)
    title = request.group(1).strip()
    if _query_is_discussion(title):
        return None
    query = _command_query(title, question=question, platform=True)
    quoted = bool(title and title[0] in _QUOTES)
    separated_courtesy = bool(re.search(rf'[\s,，]+{_COURTESY}$', title))
    # Only this extra imperative frame licenses an unseparated final particle.
    # Short titles such as 走吧, quoted text, and opaque video links stay intact.
    if (not quoted and not separated_courtesy and not _bilibili_reference(query)
            and len(query) > 2 and query.endswith('吧')):
        query = query[:-1].rstrip()
    return query


def parse_song_command(text: str) -> dict | None:
    """Parse explicit playback commands without catalog, network, or LLM access.

    The public contract mirrors SongDeck.parseCommand: mode and an original-case
    query. BV suffixes are case sensitive and must never use the catalog key.
    """
    if not isinstance(text, str):
        return None
    raw = unicodedata.normalize('NFKC', text).strip().rstrip(_END_MARKS).strip()
    value = _query_text(raw)
    if _bilibili_reference(value):
        return {'mode': 'bilibili', 'query': value}
    if raw.startswith('《') and raw.endswith('》'):
        return None  # A book/song title is not a quoted instruction.
    value, question = _command_prefix(value)
    if _bilibili_reference(value):
        return {'mode': 'bilibili', 'query': value}
    control = _command_query(value, question=question)
    for mode, pattern in (
        ('stop', r'(?:停止播放|停止音乐|停止唱歌|停止歌曲|停止点歌|停歌|取消点歌|取消播放|别放了)'),
        ('next', r'(?:下一首|切歌)'),
        ('open', r'(?:打开歌单|查看歌单|本地歌单|点个歌|唱首歌|唱一首歌)'),
    ):
        if re.fullmatch(pattern, control, re.IGNORECASE):
            return {'mode': mode, 'query': ''}
    # An explicit platform request is distinct from an ordinary mention of B站.
    desire = re.match(rf'^(?:我)?想(?:要)?(?={_PLATFORM})', value, re.IGNORECASE)
    if desire:
        value = value[desire.end():]
    bili = re.fullmatch(rf'{_PLATFORM}{_BILI_ACTION}\s*[:：]?\s*(.*)', value, re.IGNORECASE)
    if bili:
        query = _platform_command_query(bili.group(1), question=question)
        return None if query is None else {'mode': 'bilibili', 'query': query}
    play = re.fullmatch(_PLAY + r'\s*[:：]?\s*(.*)', value, re.IGNORECASE)
    if not play:
        return None
    raw_query = play.group(1).strip().lstrip(':：,，').strip()
    if _query_is_discussion(raw_query):
        return None
    platform = re.fullmatch(rf'{_PLATFORM}({_BILI_ACTION})?\s*[:：]?\s*(.*)', raw_query, re.IGNORECASE)
    if platform:
        action, body = platform.groups()
        # The outer local verb already establishes 点歌; retain an inner play
        # verb so this ordering gets the same particle handling as B站点歌放… .
        candidate = f'{action} {body}' if action and re.fullmatch(_NESTED_PLAY, action) else body
        query = _platform_command_query(candidate, question=question)
        return None if query is None else {'mode': 'bilibili', 'query': query}
    query = _command_query(raw_query, question=question)
    if _bilibili_reference(query):
        return {'mode': 'bilibili', 'query': query}
    return {'mode': 'local' if query else 'open', 'query': query}


def catalog(folder: Path):
    root = folder.resolve()
    result = []
    if not root.is_dir():
        return result
    for path in sorted(root.iterdir(), key=lambda p: p.name.casefold()):
        if path.is_symlink() or not path.is_file() or path.suffix.lower() not in FORMATS:
            continue
        if path.resolve().parent != root:
            continue
        song_id = hashlib.sha256(path.name.encode('utf-8')).hexdigest()[:24]
        result.append({'id': song_id, 'title': path.stem, 'url': '/songs/' + song_id})
    return result


def resolve_song(folder: Path, song_id: str):
    for song in catalog(folder):
        if song['id'] == song_id:
            for path in folder.iterdir():
                if hashlib.sha256(path.name.encode('utf-8')).hexdigest()[:24] == song_id:
                    return path
    return None
