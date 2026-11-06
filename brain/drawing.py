"""Validated, executable pen paths. No scripts, external URLs or hidden raster image."""
import json
import math
import re

DRAWING_PROMPT = '''这是网页逐笔绘画任务，不是位图生成。创建 drawing.json，使用以下格式：
{"version":1,"width":1000,"height":750,"background":"#fffaf0","title":"画作标题",
"strokes":[{"phase":"轮廓","color":"#263d31","width":3,"duration":500,
"points":[[100,100,0.7],[110,105,1],[120,110,0.6]]}]}
每个 points 点为 [x,y,pressure]；pressure 为 0.1~1。duration 是本笔播放毫秒，范围80~10000。
每笔是一条连续折线，至少两个点。用足够密集的采样点画出平滑曲线。
按构图、轮廓、上色、细节组织 phase，建议40~160笔，总时长30~120秒。
颜色仅用六位十六进制；线宽0.5~80；坐标在画布内。禁止脚本、SVG、图片URL、成图描线。
可用 Python 计算贝塞尔采样或有结构的形状曲线，并保存 JSON；设计必须贴合用户主题。
先构思明确的主体与构图，再生成路径，检查坐标和数据。将 drawing.json 列为产物。
这是笔画规划，尚未在观众画布上播放；不要声称现场绘制已经完成。'''


def validate_drawing(data):
    def number(v, low, high):
        return type(v) in (int, float) and math.isfinite(v) and low <= v <= high
    def color(v):
        return isinstance(v, str) and re.fullmatch(r'#[0-9a-fA-F]{6}', v)
    if not isinstance(data, dict) or data.get('version') != 1:
        raise ValueError('绘画数据版本不正确')
    w, h = data.get('width'), data.get('height')
    if type(w) is not int or type(h) is not int or not 256 <= w <= 2048 or not 256 <= h <= 2048:
        raise ValueError('画布尺寸须为 256～2048')
    if not color(data.get('background')) or not isinstance(data.get('title'), str) or len(data['title']) > 100:
        raise ValueError('画作标题或底色不合法')
    strokes = data.get('strokes')
    if not isinstance(strokes, list) or not 1 <= len(strokes) <= 500:
        raise ValueError('笔画数须为 1～500')
    count, duration, clean = 0, 0, []
    for s in strokes:
        if not isinstance(s, dict) or not color(s.get('color')) or not number(s.get('width'), .5, 80) or not number(s.get('duration'), 80, 10000):
            raise ValueError('笔画颜色、宽度或时长不合法')
        phase, points = s.get('phase'), s.get('points')
        if not isinstance(phase, str) or len(phase) > 40 or not isinstance(points, list) or not 2 <= len(points) <= 1000:
            raise ValueError('笔画阶段或点数不合法')
        for p in points:
            if not isinstance(p, list) or len(p) != 3 or not number(p[0], 0, w) or not number(p[1], 0, h) or not number(p[2], .1, 1):
                raise ValueError('笔画坐标或笔压不合法')
        count += len(points); duration += s['duration']
        clean.append({k: s[k] for k in ('phase', 'color', 'width', 'duration', 'points')})
    if count > 50000 or duration > 600000:
        raise ValueError('绘画超过点数或十分钟播放上限')
    return {'version': 1, 'width': w, 'height': h, 'title': data['title'],
            'background': data['background'], 'strokes': clean}


def load_drawing(path):
    if path.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('绘画数据超过 4MB')
    return validate_drawing(json.loads(path.read_text('utf-8')))
