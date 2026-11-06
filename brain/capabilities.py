"""Deterministic boundaries for actions the runtime cannot execute."""
import re
import unicodedata

from .songs import parse_song_command

CAPABILITY_PROMPT = (
    '音乐制作：“唱歌 歌名”请求由本地人声分离加RVC任务处理，依赖角色模型和同名原曲；“原创 主题”会准备歌词和风格，由主播在Suno网页手动生成、下载并确认导入。没有自动提交Suno。实际进度以任务面板为准，没有播放回执时不能声称已经演唱。'
    '实际音乐工具：聊天中的“点歌 歌名”“播放歌名”由角色先回应，再查询已导入的本地曲库，唯一匹配后报出歌名，等介绍语音实际播完才播放；点击曲库播放和切换下一首也会先介绍。没有查询结果时不能断言曲库没有这首歌。'
    '聊天中的“B站点歌 歌名”、BV号或B站官方视频链接会转给已连接的本机控制台；角色先回应，再查询真实视频信息。关键词按播放量搜索，自动选择符合后台规则、且在可比较结果中播放量最高的视频并由角色说出真实视频名和已验证的选择理由，保留卡片供用户换版本。播放量门槛仅供后台筛选，角色不得口播具体门槛，也不说已达到门槛；未满足自动筛选条件时说“这次还没找到合适的版本，你可以换个关键词，或者从结果里挑一个。”缺少播放量时如实说明无法确认并请用户选择。BV号和视频链接验证后由角色介绍，介绍语音实际播完才打开官方播放器；未播完会提示手动继续，浏览器也可能要求用户点击后才有声音。'
    'B站查询请求不代表查询成功，播放器加载也不代表已经开始播放；必须以实际结果和播放回执为准。'
    '网页搜索只会返回系统提供的文字资料，不等于打开了网页或获得可播放的歌单。'
    '尚未接入网易云和QQ音乐的搜索播放；不能把B站、本地曲库或文字搜索说成这些平台的播放结果。公开测试模式不操作正式音乐资源。'
    '没有执行结果时，不能说“我看到了几个结果”“我点开了”“已经播放”，也不能虚构搜索列表及排序。'
    '皮套动作标签是待执行请求，不是动作成功回执。历史回复中对执行的自述同样不作为成功证据。'
    '收到未接入的操作请求，简短说明当前不能执行，并给出本地曲库等实际可用的选择。'
)


def unexecuted_media_reply(text):
    """Last resort only for Persona; never use this to gate real playback.

    A question card or another direct Persona caller can bypass music intake.
    Recognizing a command does not provide a catalog or playback receipt.
    """
    command = parse_song_command(text)
    if command is None:
        return None
    if command['mode'] in {'local', 'bilibili'}:
        return '这条点歌还没有执行，请在聊天输入框或点歌面板发起。'
    return '这条播放操作还没有执行，请在聊天输入框或点歌面板操作。'


def unsupported_media_reply(text):
    clean = re.sub(r'\s+', '', unicodedata.normalize('NFKC', text)).casefold()
    command = parse_song_command(text)
    if command and command['mode'] == 'bilibili':
        return None  # The ingestion/tool route executes this request, not the LLM.
    if re.search(r'(网易云|qq音乐)', clean) and re.search(r'(搜索|搜一下|搜|播放|点歌|点开|打开|放一首|放首|放歌|来一首)', clean):
        return '这个平台暂时没有接入，可以使用控制台的“B站点歌”，或用“点歌 歌名”播放已导入的本地歌曲。'
    if re.search(r'(b站|哔哩哔哩|bilibili)', clean) and re.search(r'(搜索|搜一下|搜|播放|点歌|点开|打开|放一首|放首|放歌|来一首)', clean):
        return '可以发送“B站点歌 歌名”、BV号或B站视频链接，由本机控制台查询。关键词会优先选择本次结果里播放量最高的合适版本，角色介绍后交给官方播放器；也可从真实结果中换版本，BV号或视频链接播放指定视频；实际是否播放以播放器回执为准。'
    if re.fullmatch(r'(那就|那|请|帮我)?(播放|放|点开|打开|选)(第)?[一二三四五六七八九十\d]+(个|首|条)(结果|视频|歌曲)?(吧|呀|啊|就好)?[。！？!?]*', clean):
        return '我这里没有经过实际查询的可播放结果列表，不能按这个序号操作。请给出本地曲库里的歌名；如果我刚才说已经搜到，那是我说错了。'
    if re.fullmatch(r'(点歌|点个歌|唱首歌|唱一首歌|放首歌)(歌)?(吧|呀|啊)?[。！？!?]*', clean):
        return '可以发送“点歌 歌名”查询本地曲库，或发送“B站点歌 歌名”、BV号或B站视频链接来查询B站。'
    return None
