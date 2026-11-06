"""Current host time, expressed in the configured local UTC+8 time zone."""
import re
from datetime import datetime, timedelta, timezone

LOCAL_ZONE = timezone(timedelta(hours=8), 'Asia/Hong_Kong')


def local_now():
    return datetime.now(LOCAL_ZONE)


def clock_prompt():
    now = local_now()
    return (f'本轮系统实际时间：{now:%Y年%m月%d日 %H:%M:%S}，星期{"一二三四五六日"[now.weekday()]}，'
            '时区 Asia/Hong_Kong（UTC+8）。今天、明天以此为准，历史消息日期不是当前日期。'
            '这是主机所在地时间，不代表观众的位置。')


def factual_reply(text):
    clean = re.sub(r'[\s？?。！!，,]', '', text)
    if re.fullmatch(r'(请问|告诉我)?(现在|今天|今日)(是)?(几号|几月几号|什么日期|哪一天|星期几|周几|几月几日|日期)(了|呀|啊|呢|吗)?', clean):
        now = local_now()
        return f'按北京时间，现在是{now.year}年{now.month}月{now.day}日，星期{"一二三四五六日"[now.weekday()]}。'
    if re.fullmatch(r'(请问)?(现在|此刻)(是)?(几点|几点钟|什么时间)(了|呀|啊|呢)?', clean):
        now = local_now()
        return f'现在是北京时间{now.hour}点{now.minute:02d}分。'
    if re.fullmatch(r'(今天|明天|后天)?(的)?天气(怎么样|如何|呢|好吗)?', clean):
        return '你想查哪个城市的天气呀？告诉我城市，我再帮你查。'
    return None
