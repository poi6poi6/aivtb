"""One typed, local-owner tool directory over existing runtime adapters."""
import inspect
from .events import Event

class ToolRegistry:
    def __init__(self): self.tools={}
    def add(self,name,description,parameters,handler,available=lambda:True,receipt='completed',
            unavailable_reason='工具当前不可用，请启动对应服务'):
        self.tools[name]={'name':name,'description':description,'parameters':parameters,
                          'handler':handler,'available':available,'receipt':receipt,
                          'unavailable_reason':unavailable_reason}
    def catalog(self):
        return [{'name':t['name'],'description':t['description'],'parameters':{
                    'type':'object','properties':{k:{p:v for p,v in s.items() if p!='required'} for k,s in t['parameters'].items()},
                    'required':[k for k,s in t['parameters'].items() if s.get('required')],'additionalProperties':False},
                 'available':bool(t['available']()),'permission':'local_owner','result_state':t['receipt']}
                for t in self.tools.values()]
    async def call(self,name,arguments,owner=False):
        if not owner: raise ValueError('工具操作需要本机主播授权')
        if name not in self.tools: raise ValueError('未注册的工具')
        t=self.tools[name]
        if not t['available'](): raise ValueError(t['unavailable_reason'])
        if not isinstance(arguments,dict) or set(arguments)-set(t['parameters']):
            raise ValueError('工具参数格式错误或含未知字段')
        for key,spec in t['parameters'].items():
            if spec.get('required') and key not in arguments: raise ValueError('缺少参数：'+key)
            if key in arguments:
                value=arguments[key]
                if not isinstance(value,str) or len(value)>4000 or spec.get('enum') and value not in spec['enum']:
                    raise ValueError('非法参数：'+key)
        value=t['handler'](**arguments)
        if inspect.isawaitable(value): value=await value
        return {'ok':True,'tool':name,'state':t['receipt'],'result':value}


def build_registry(ui,live=False):
    r=ToolRegistry(); manager=ui.agent_tasks
    string={'type':'string','required':True}
    if manager:
        from .agent_tasks import KINDS
        r.add('task.submit','提交绘画、代码、图片或查询任务',{'kind':{**string,'enum':sorted(KINDS)},'prompt':string,
              'resource_profile':{'type':'string','enum':['standard','local_gpu']},
              'execution_mode':{'type':'string','enum':['auto','direct','omp']}},
              lambda kind,prompt,resource_profile='standard',execution_mode='auto':manager.submit(prompt,kind,owner=True,resource_profile=resource_profile,execution_mode=execution_mode),receipt='queued')
        r.add('task.status','读取本机任务的真实状态',{'id':string},
              lambda id:manager.fetch_job(id) if getattr(manager,'remote',False) else manager.get(id))
        r.add('task.amend','预览修订影响；确认后带回 token 才会停止旧任务',{'id':string,'instruction':string,'token':{'type':'string'}},
              lambda id,instruction,token=None:manager.amend(id,instruction,token=token),receipt='amend_result')
    r.add('minecraft.observe','读取已连接 Minecraft 的实际状态',{},lambda:dict(ui.minecraft.state),
          lambda:live and bool(ui.minecraft.state.get('connected')))
    r.add('minecraft.command','提交游戏指令，完成以游戏实际回执为准',
          {'action':{**string,'enum':sorted(ui.minecraft.ACTIONS)},'target':{'type':'string'}},
          lambda action,target='':ui.minecraft.command(action,target),lambda:live,receipt='accepted')
    r.add('music.request','申请 RVC 翻唱或 Suno 半自动原创；不会自动生成或播放',
          {'mode':{**string,'enum':['cover','original']},'query':string},
          lambda mode,query:ui.music_requests.submit(mode,query,'主播'),lambda:live,receipt='intake_response')
    r.add('music.status','读取音乐制作任务',{},lambda:ui.music_requests.items,lambda:live)
    async def play(name):
        event=Event('companion','chat','点歌 '+name,user='你')
        event.owner_control=True
        await ui.event_queue.put(event)
        return '已提交本地曲库请求；是否匹配和播放，以后续回执为准'
    r.add('music.play_local','请求播放已导入的本地歌曲',{'name':string},play,lambda:live,receipt='accepted')
    browser_required = '此工具不直接搜索B站；请在控制台连接浏览器点歌助手后查询，或使用 bilibili.play 提交点歌请求。'
    def search_bilibili_unavailable(query):
        # Retain the old name for integrations, but never reach the rejected
        # anonymous API or return a successful/completed search receipt.
        raise ValueError(browser_required)
    r.add('bilibili.search','兼容入口，当前不可用；请通过控制台浏览器点歌助手查询',{'query':string},
          search_bilibili_unavailable,lambda:False,receipt='unavailable',unavailable_reason=browser_required)
    async def play_bilibili(query):
        event=Event('companion','chat','B站点歌 '+query,user='你')
        event.owner_control=True
        await ui.event_queue.put(event)
        return '已提交B站点歌请求；需控制台连接浏览器助手读取官方页面，确认版本并等介绍语音完成后再播放。关键词只有能确认合适的最高播放量版本才自动选择，否则保留手选；以实际回执为准'
    r.add('bilibili.play','提交到控制台浏览器点歌助手；读取官方页面并介绍后播放BV或关键词对应版本',{'query':string},
          play_bilibili,lambda:live,receipt='accepted')
    return r
