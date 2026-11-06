"""Task references shared by conversational acknowledgements and background receipts."""
import re
import time
from .events import Event

LABELS = {'pending':'等待主播批准', 'queued':'排队等待执行', 'running':'正在执行',
          'waiting_approval':'等待本机批准具体操作', 'succeeded':'已生成产物，可以查看',
          'review':'开发结果待审核，尚未应用', 'applied':'文件已应用，尚未确认重启和功能生效',
          'failed':'执行失败，未完成', 'blocked':'暂时无法执行，未完成', 'cancelled':'已取消',
          'verified':'已确认服务加载新源码并通过健康检查', 'rolled_back':'已回退文件，等待重启服务'}
KINDS = {'drawing':'逐笔绘画', 'image':'图片', 'code':'代码', 'research':'查询', 'desktop':'桌面操作'}
NOTICE_STATES = {'succeeded', 'review', 'applied', 'failed', 'blocked', 'verified', 'rolled_back'}


class TaskCoordination:
    def context_for(self, event):
        jobs = [j for j in self.jobs if j.get('requester') == self.requester(event)][-3:]
        lines=[]
        registry=getattr(self,'tool_registry',None)
        if registry:
            lines.append('当前工具目录（由本机主播通过工具入口提交；本轮聊天未自动调用，不能声称已执行）：'
                         + '；'.join(t['name']+'：'+('可用' if t['available'] else '未就绪') for t in registry.catalog()))
        if jobs:
            lines.append('后台任务当前真实状态（优先于历史聊天中的旧进度；产物完成不等于播放、应用或生效）：\n'
                         + '\n'.join(self.describe(j) for j in jobs))
        return '\n'.join(lines)

    @staticmethod
    def requester(event):
        if getattr(event, 'owner_control', False):
            return 'owner'
        return event.source + ':' + (event.viewer_key or event.user or 'anonymous')

    def bind(self, event, job, *, notice=False):
        event.task_id = job['id']
        event.task_notice = notice
        event.task_revision = job.get('revision', 0)
        if notice:
            event.forced = False
        event.agent_reply = self.describe(job)

    def describe(self, job):
        name = str(job.get('user') or '你')[:32]
        text = f"{name}，你的{KINDS.get(job['kind'], '后台')}任务（{job['id'][-4:]}）{LABELS.get(job['state'], '状态待确认')}。"
        if job['kind'] == 'drawing' and job['state'] == 'succeeded' and job.get('drawing_stage') == 'sketch':
            text = f"{name}，绘画草稿（{job['id'][-4:]}）已准备，可以在画布查看或修改；确认草稿后再继续上色和细节。"
        progress = job.get('progress', {})
        if job['state'] == 'running' and progress.get('label'):
            text += '当前记录：' + progress['label'] + '。'
        elif job['state'] == 'queued' and job.get('wait_reason'):
            text += '等待原因：' + job['wait_reason'] + '。'
        return text

    def notify_state(self, job, retry=False):
        if self.event_queue is None or not job.get('origin') or (job['state'] not in NOTICE_STATES and not retry):
            return
        if job.get('superseded_by') or job['state'] == 'cancelled':
            return
        if (job.get('delivery') or {}).get('revision') != job.get('revision', 0):
            job['delivery'] = {'state':'pending','revision':job.get('revision',0),'failures':0}
            self.save(job)
        event = Event('system', 'task_notice', '', user=job.get('user'))
        self.bind(event, job, notice=True)
        event.text = event.agent_reply
        self.event_queue.put_nowait(event)

    def refresh_receipt(self, event):
        if not event.task_id:
            return True
        try:
            job = self.get(event.task_id)
        except StopIteration:
            return False
        revision = job.get('revision', 0)
        if event.task_notice and (job['state'] == 'cancelled' or job.get('superseded_by') or time.time() - event.ts > 300
                or job.get('announced_revision') == revision):
            return False
        event.task_revision = revision
        event.agent_reply = self.describe(job)
        if event.kind == 'task_notice':
            event.text = event.agent_reply
        return True

    def receipt_valid(self, event):
        if not event.task_id:
            return True
        try:
            job = self.get(event.task_id)
        except StopIteration:
            return False
        return (event.task_revision == job.get('revision', 0) and
                (not event.task_notice or (job['state'] != 'cancelled' and not job.get('superseded_by') and time.time() - event.ts <= 300
                  and job.get('announced_revision') != event.task_revision)))

    def speech_valid(self, item):
        ref = item.event
        if not ref.get('task_id'):
            return True
        event = Event('system', 'task_notice', '', ts=ref.get('task_ts', item.created_at),
                      task_id=ref['task_id'], task_revision=ref.get('task_revision', -1),
                      task_notice=False)
        if not self.receipt_valid(event):
            return False
        job = self.get(event.task_id)
        return (not ref.get('task_notice') or (job['state'] != 'cancelled' and not job.get('superseded_by') and time.time() - event.ts <= 300
            and (job.get('announced_revision') != event.task_revision or job.get('announced_audio') == item.id)))

    def speech_dispatched(self, item):
        if item.event.get('task_id') and item.event.get('task_notice'):
            job = self.get(item.event['task_id'])
            job['announced_revision'] = item.event['task_revision']
            job['announced_audio'] = item.id
            delivery = job.get('delivery') or {}
            if delivery.get('revision') != item.event['task_revision']:
                delivery = {'revision':item.event['task_revision'],'failures':0}
            delivery.update(state='dispatched',audio_id=item.id)
            job['delivery'] = delivery
            self.save(job)

    def speech_state(self, item, state, reason=''):
        if not item.event.get('task_notice') or not item.event.get('task_id'):
            return
        job = self.get(item.event['task_id'])
        if job.get('revision',0) != item.event.get('task_revision'):
            return
        self.delivery_state(job, state, reason)

    def synthesis_failed(self, event, reason):
        if event.task_notice and self.receipt_valid(event):
            self.delivery_state(self.get(event.task_id), 'failed', reason)

    def delivery_state(self, job, state, reason):
        delivery = job.get('delivery') or {'revision':job.get('revision',0),'failures':0}
        if delivery.get('revision') != job.get('revision',0):
            delivery = {'revision':job.get('revision',0),'failures':0}
        delivery.update(state=state, reason=reason, updated=time.time())
        if state == 'failed':
            delivery.pop('retry_at',None)
            delivery['failures'] = delivery.get('failures',0) + 1
            if delivery['failures'] <= self.cfg.notification_retries and not job.get('superseded_by') and job['state'] != 'cancelled':
                delivery['retry_at'] = time.time() + 5
        elif state in {'played','cancelled'}:
            delivery.pop('retry_at',None)
        job['delivery'] = delivery
        self.save(job)

    def notification_tick(self):
        for job in self.jobs:
            delivery = job.get('delivery') or {}
            if not delivery.get('retry_at') or delivery['retry_at'] > time.time():
                continue
            delivery.pop('retry_at',None)
            if delivery.get('revision') == job.get('revision',0):
                job.pop('announced_revision',None)
                job.pop('announced_audio',None)
                self.notify_state(job, retry=True)
            self.save(job)

    async def task_followup(self, event):
        text = event.text.strip().rstrip('？?。!！')
        amendment = re.fullmatch(r'(?:修改|补充)(?:刚才|刚刚|之前)(?:那个|的)?(?:任务)?[：:,，\s]+(.+)', text, re.S)
        confirm_amend = text == '确认修改任务'
        finish_drawing = re.fullmatch(r'(?:草稿(?:可以|没问题)[，,\s]*)?(?:继续上色|开始上色|给草稿上色)(?:[，,:：\s]+(.+))?', text)
        cancel = bool(re.fullmatch(r'(?:取消|停止)(?:刚才|刚刚|之前)(?:那个|的)?(?:任务)?', text))
        query = bool(re.fullmatch(r'(?:(?:刚才|刚刚|之前)(?:那个|的)?(?:任务)?[，, ]*)?(?:做好了吗|做完了吗|完成了吗|进度怎么样|任务进度|任务状态)', text))
        explicit = re.fullmatch(r'(查询|取消|停止)任务\s*([0-9a-f]{12})', text, re.I)
        if not (cancel or query or explicit or amendment or confirm_amend or finish_drawing):
            return False
        key = self.requester(event)
        jobs = [j for j in self.jobs if j.get('requester') == key]
        if finish_drawing:
            jobs = [j for j in jobs if j['kind'] == 'drawing' and j.get('drawing_stage') == 'sketch'
                    and j['state'] == 'succeeded' and not j.get('superseded_by')]
        if explicit:
            cancel = explicit[1] != '查询'
            jobs = [j for j in self.jobs if j['id'] == explicit[2].lower()
                    and (key == 'owner' or j.get('requester') == key)]
        if not jobs:
            event.agent_reply = '没有找到与你关联的任务；可以提供你自己的任务编号。'
            return True
        job = jobs[-1]
        if finish_drawing:
            try:
                preview = await self.finish_drawing(job['id'], finish_drawing[1] or '', requester=key)
                self.remember_intent_preview(event, job, preview)
                event.agent_reply = preview['detail'] + ' 如需执行，请在两分钟内发送“确认修改任务”。'
            except ValueError as exc:
                event.agent_reply = str(exc)
            return True
        if amendment or confirm_amend:
            try:
                if amendment:
                    preview = await self.amend(job['id'], amendment[1], requester=key)
                    self.remember_intent_preview(event, job, preview)
                    event.agent_reply = preview['detail'] + '保留产物：' + ('、'.join(preview['artifacts']) or '暂无') + ' 如需执行，请在两分钟内发送“确认修改任务”。'
                    return True
                preview = job.get('amend_preview', {})
                if not preview:
                    raise ValueError('没有待确认的修改，请先补充任务要求')
                job = await self.amend(job['id'], preview['impact']['instruction'], requester=key, token=preview['token'])
            except ValueError as exc:
                event.agent_reply = str(exc)
                return True
        if cancel:
            await self.cancel(job['id'])
        self.bind(event, job)
        return True
