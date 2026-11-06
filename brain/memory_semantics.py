"""Small, local semantic vocabulary for recall, never for inventing facts.

Aliases and weak topic bridges only rank already visible, active memories.
Original wording, negation, dates and evidence remain in the memory record.
No model download, remote embedding call or additional memory store is needed.
"""
from __future__ import annotations

from functools import lru_cache


ALIASES = {
    'coffee': ('咖啡', '拿铁', '美式', '卡布奇诺', '浓缩咖啡', 'coffee', 'latte'),
    'tea': ('茶', '乌龙', '红茶', '绿茶', '抹茶'),
    'drink': ('饮料', '饮品', '喝点', '喝什么', '喝些什么', '喝的'),
    'sleep': ('睡觉', '睡眠', '睡不着', '失眠', '入睡', '熬夜', '休息不好'),
    'evening': ('晚上', '今晚', '睡前', '夜里', '夜晚'),
    'rest': ('放松', '歇一会', '休息', '解压', '缓一缓'),
    'stress': ('压力', '紧张', '焦虑', '担心', '忐忑'),
    'sadness': ('难过', '伤心', '失落', '低落', '委屈', '不开心'),
    'food': ('吃什么', '吃点', '好吃的', '饭菜', '食物', '晚饭', '午饭'),
    'sweet': ('甜食', '甜点', '蛋糕', '布丁', '饼干', '巧克力', '冰淇淋'),
    'fruit': ('水果', '苹果', '香蕉', '草莓', '葡萄', '橙子', '西瓜'),
    'spicy': ('辣椒', '辣的', '辣味', '吃辣', '麻辣'),
    'exercise': ('运动', '锻炼', '健身', '跑步', '慢跑', '散步'),
    'study': ('学习', '复习', '功课', '作业', '备考', '考试'),
    'work': ('工作', '上班', '加班', '职场', '同事'),
    'music': ('音乐', '听歌', '歌曲', '唱歌', '乐曲', '歌单'),
    'drawing': ('画画', '绘画', '手绘', '插画', '素描'),
    'plants': ('养花', '种花', '花卉', '园艺', '花园', '绿植'),
    'clothes': ('衣服', '衣物', '穿搭', '着装', '裙子', '外套'),
    'pets': ('宠物', '小猫', '猫咪', '小狗', '狗狗', '毛孩子'),
    'games': ('游戏', '打游戏', '我的世界', 'minecraft', '原神'),
}
BRIDGES = {
    'coffee': {'drink': .7}, 'tea': {'drink': .7},
    'sweet': {'food': .6}, 'fruit': {'food': .6}, 'spicy': {'food': .6},
    'evening': {'sleep': .35}, 'sleep': {'rest': .35},
}


@lru_cache(maxsize=2048)
def concepts(text: str) -> frozenset[str]:
    clean = text.lower()
    return frozenset(key for key, aliases in ALIASES.items() if any(a in clean for a in aliases))


def recall_similarity(query: str, memory: str) -> float:
    """A bounded relevance signal; a matching topic is not a matching claim."""
    def expand(words):
        weights = dict.fromkeys(words, 1.0)
        for word in words:
            for related, weight in BRIDGES.get(word, {}).items():
                weights[related] = max(weights.get(related, 0), weight)
        return weights
    q, m = expand(concepts(query)), expand(concepts(memory))
    if not q or not m:
        return 0.0
    return max((min(q[key], m[key]) for key in q.keys() & m.keys()), default=0.0)
