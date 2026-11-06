"""Conservative identity and fact keys; no generated claim is evidence."""
from __future__ import annotations

import hashlib
import re

from .events import Event
from .viewer_identity import viewer_identity


def record_owner(record: dict) -> str:
    owner = str(record.get('owner_key') or '')
    if owner.startswith(('account:', 'local:', 'unkeyed:')) and len(owner.rsplit(':', 1)[-1]) == 64:
        return owner
    source = record.get('original_source') or record.get('source') or {}
    if not isinstance(source, dict):
        return ''
    channel = str(source.get('source') or '')
    key = str(source.get('viewer_key') or '')
    subject = str(record.get('subject', ''))
    name = str(source.get('user') or (subject[2:] if subject.startswith('观众') else ''))
    if owner and ':' in owner:
        # Older releases used source:platform-id. Never bind by nickname.
        channel, key = owner.split(':', 1)
    if not channel:
        return ''  # Unknown-provenance archives stay quarantined.
    return viewer_identity(Event(channel, 'chat', '', user=name, viewer_key=key))


def owner_matches(owner: str, event: Event) -> bool:
    return bool(owner and (owner == viewer_identity(event) or (
        event.viewer_key and owner == f'{event.source}:{event.viewer_key}'
    )))


def fingerprint(value: str) -> str:
    normalized = re.sub(r'[\s。！!？?，,；;]', '', value.lower())
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()[:20]


def favorite_category(value: str) -> str:
    for category, markers in (
        ('dessert', ('蛋糕', '甜点', '甜食', '布丁', '冰淇淋', '巧克力', '饼干')),
        ('music', ('歌曲', '音乐', '歌手', '乐队', '唱歌')),
        ('drink', ('咖啡', '拿铁', '红茶', '绿茶', '奶茶', '饮料', '饮品', '果汁')),
        ('fruit', ('苹果', '香蕉', '草莓', '葡萄', '西瓜', '水果')),
        ('game', ('游戏', '原神', '我的世界', 'minecraft')),
        ('sport', ('足球', '篮球', '跑步', '游泳', '运动')),
    ):
        if any(marker in value.lower() for marker in markers):
            return category
    # Unknown categories are not assumed to be mutually exclusive.
    return 'unspecified:' + fingerprint(value)


def fact_field(kind: str, predicate: str, value: str) -> str:
    if kind == 'viewer_fact':
        if predicate == '最喜欢':
            return 'favorite:' + favorite_category(value)
        if predicate in {'工作城市', '工作地点'}:
            return 'work-city'
        if predicate in {'居住城市', '居住地'}:
            return 'home-city'
        preference = re.fullmatch(r'对(.+)的偏好', predicate)
        if preference:
            return 'preference:' + fingerprint(preference.group(1))
    return fingerprint(predicate + ':' + value)


def stable_slot(owner: str, kind: str, predicate: str, value: str, scope: str) -> str:
    return f'fact:{owner}:{kind}:{scope}:{fact_field(kind, predicate, value)}'


def timeline_branch(event: Event | None = None, memory: dict | None = None) -> str:
    owner = viewer_identity(event) if event is not None else record_owner(memory or {})
    return 'viewer:' + owner if owner else ''


def supported_excerpt(value: str, source: str, evidence: str = '') -> str:
    """Return an original clause, preserving its subject and negation.

    This deliberately rejects paraphrases; lexical similarity cannot establish
    entailment. A short quoted span is expanded to its complete source clause.
    """
    value = value.strip(' 。！!？?，,；;')
    evidence = evidence.strip()
    if re.search(r'如果|假如|假设|要是|会不会|是不是|能不能|[？?]', source):
        return ''
    if not value or value not in source or (evidence and (len(evidence) > 160 or evidence not in source)):
        return ''
    for clause in re.split(r'[，,。！!？?；;\n]', source):
        if value not in clause or (evidence and evidence not in clause and clause not in evidence):
            continue
        before = clause[:clause.index(value)]
        if re.search(r'(?:不|没|未|不是|并非|不再|没有|一点也不|从未).{0,3}$', before):
            return ''
        if re.search(r'如果|假如|假设|要是|会不会|是不是|能不能', clause):
            return ''
        if len(clause.strip()) <= 160:
            return clause.strip()
    return ''


def validated_attribute(predicate: str, value: str, excerpt: str) -> bool:
    patterns = {
        '工作城市': r'我(?:现在|如今)?(?:正在)?在(.{1,20}?)工作',
        '工作地点': r'我(?:现在|如今)?(?:正在)?在(.{1,20}?)工作',
        '居住城市': r'我(?:现在|如今)?(?:居)?住在(.{1,20})',
        '居住地': r'我(?:现在|如今)?(?:居)?住在(.{1,20})',
    }
    if predicate not in patterns:
        return False
    match = re.match(patterns[predicate], excerpt)
    return bool(match and match.group(1).strip('了 ') == value)
