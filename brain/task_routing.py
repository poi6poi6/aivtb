"""Host-owned resolution and authorization for semantic task intents."""
import time
from types import SimpleNamespace


class TaskRouting:
    def bump_intent_generation(self, key):
        self.intent_generation.pop(key, None)
        self.intent_generation[key] = time.monotonic_ns()
        while len(self.intent_generation) > 256:
            self.intent_generation.pop(next(iter(self.intent_generation)))

    def clear_intent_prompt(self, key, *, invalidate=True):
        pending = self.intent_prompts.pop(key, None)
        if pending is None:
            return
        self.bump_intent_generation(key)
        if invalidate and pending['mode'] == 'confirm':
            try:
                job = self.get(pending['job_id'])
                if job.get('amend_preview', {}).get('token') == pending['token']:
                    job.pop('amend_preview', None)
                    self.save(job)
            except StopIteration:
                pass

    def store_intent_prompt(self, key, pending):
        for expired in [k for k,v in self.intent_prompts.items() if v['expires'] <= time.time()]:
            self.clear_intent_prompt(expired)
        self.clear_intent_prompt(key)
        self.bump_intent_generation(key)
        self.intent_prompts[key] = pending
        while len(self.intent_prompts) > 128:
            self.clear_intent_prompt(next(iter(self.intent_prompts)))

    def own_intent_jobs(self, event):
        key = self.requester(event)
        return [j for j in self.jobs if j.get('requester') == key and not j.get('superseded_by')][-6:]

    def remember_intent_preview(self, event, job, preview):
        self.store_intent_prompt(self.requester(event), {
            'mode':'confirm', 'job_id':job['id'], 'instruction':preview['instruction'],
            'token':preview['token'], 'expires':time.time()+120})

    def ask_intent_target(self, event, decision, candidates):
        choices = [{'id':j['id'], 'revision':j.get('revision'), 'kind':j['kind'],
                    'prompt':j.get('prompt', '')[:28]} for j in candidates]
        if not choices:
            event.agent_reply = '没有找到与你关联的任务；请提供自己的任务编号或重新说明要做什么。'
            return
        self.store_intent_prompt(self.requester(event), {
            'mode':'choose', 'action':decision.action, 'instruction':event.text,
            'choices':choices, 'expires':time.time()+120})
        event.agent_reply = '你指的是哪一项？' + '；'.join(
            f"{index+1}，{j['prompt']}（{j['id'][-4:]}）" for index,j in enumerate(choices)) + '。回复序号或编号即可。'

    async def followup_intent_prompt(self, event):
        key = self.requester(event)
        pending = self.intent_prompts.get(key)
        if not pending:
            return False
        if pending['expires'] < time.time():
            self.clear_intent_prompt(key)
            return False
        text = event.text.strip().rstrip('。！!')
        if text in {'算了', '不用了', '不改了'}:
            self.clear_intent_prompt(key)
            event.agent_reply = '已撤销这次待确认的操作，原任务没有因此改动。'
            return True
        if pending['mode'] == 'confirm':
            if text not in {'确认', '确认修改任务', '好', '好的', '就这么改', '好就这么改', '确认继续'}:
                return False
            self.clear_intent_prompt(key, invalidate=False)
            try:
                job = await self.amend(pending['job_id'], pending['instruction'], requester=key, token=pending['token'])
                self.bind(event, job)
            except (ValueError, StopIteration) as exc:
                event.agent_reply = str(exc) or '原任务已不存在，请重新说明。'
            return True
        choices = pending['choices']
        ordinal = {'第一个':1, '第二个':2, '第三个':3, '第四个':4, '第五个':5, '第六个':6}
        number = ordinal.get(text, int(text) if text.isdigit() and len(text)==1 else 0)
        selected = choices[number-1] if 1 <= number <= len(choices) else None
        if selected is None:
            matches = [j for j in choices if text in {j['id'],j['id'][-4:]}]
            selected = matches[0] if len(matches)==1 else None
        if selected is None:
            return False
        self.clear_intent_prompt(key)
        jobs = [j for j in self.own_intent_jobs(event) if j['id']==selected['id']]
        if not jobs or jobs[0].get('revision') != selected['revision']:
            event.agent_reply = '这项任务的状态已变化，请重新说明要操作的任务。'
            return True
        await self.apply_task_intent(event, SimpleNamespace(action=pending['action'],
            target_id=selected['id'], kind=selected['kind'], ambiguous=False),
            instruction=pending['instruction'])
        return True

    async def resolve_task_intent(self, event):
        if not getattr(event, 'task_intent_pending', False):
            return
        event.task_intent_pending = False
        jobs = self.own_intent_jobs(event)
        key = self.requester(event)
        generation = self.intent_generation.get(key, 0)
        snapshot = {j['id']:j.get('revision') for j in jobs}
        previews = {j['id']:j.get('amend_preview',{}).get('token') for j in jobs}
        decision = await self.intent_classifier.classify(event.text, jobs)
        if self.closing:
            return
        if decision.action == 'chat':
            return
        if generation != self.intent_generation.get(key, 0):
            event.agent_reply = '已有更新的任务确认内容，这次较早的识别结果没有执行。请以最新预览为准。'
            return
        if decision.action == 'undecided':
            event.agent_reply = '这次没有可靠识别出要执行的操作，还没有更改任务。请补充要做什么，或提供任务编号。'
            return
        current = self.own_intent_jobs(event)
        # A pending question must never bind to a replacement revision after the request.
        eligible = [j for j in current if j.get('revision')==snapshot.get(j['id']) and
                    j.get('amend_preview',{}).get('token')==previews.get(j['id'])]
        if decision.target_id and not any(j['id']==decision.target_id for j in eligible):
            event.agent_reply = '关联任务的状态已变化，还没有执行这次操作。请重新确认任务。'
            return
        if decision.ambiguous or (decision.action != 'submit' and not decision.target_id):
            self.clear_intent_prompt(key)
            if decision.action == 'submit':
                event.agent_reply = '还不确定你想让我执行哪种任务。请补充是画图、查询、开发代码，还是桌面操作。'
                return
            ids = set(getattr(decision, 'candidates', ()))
            self.ask_intent_target(event, decision, [j for j in eligible if j['id'] in ids and
                                  (not decision.kind or j['kind']==decision.kind)])
            return
        self.clear_intent_prompt(key)
        await self.apply_task_intent(event, decision)

    async def apply_task_intent(self, event, decision, *, instruction=None):
        instruction = event.text if instruction is None else instruction
        key = self.requester(event)
        try:
            if decision.action == 'submit':
                job = self.submit(instruction, decision.kind, owner=bool(getattr(event,'owner_control',False)),
                    requester=key, user=event.user or '观众', origin={'source':event.source,
                    'turn_id':getattr(event,'ui_id',None),'created':event.ts})
                self.bind(event, job, notice=True)
                return
            jobs = [j for j in self.own_intent_jobs(event) if j['id']==decision.target_id]
            if not jobs:
                raise ValueError('只能操作与你关联的任务；这次没有执行。')
            job = jobs[0]
            if decision.action == 'query':
                self.bind(event, job)
            elif decision.action == 'cancel':
                await self.cancel(job['id'])
                self.bind(event, job)
            elif decision.action in {'amend','resume'}:
                if decision.action == 'resume' and job['state'] in {'pending','queued','running','waiting_approval'}:
                    self.bind(event, job)
                    return
                preview = await self.amend(job['id'], instruction, requester=key)
                self.remember_intent_preview(event, job, preview)
                event.agent_reply = preview['detail'] + '回复“确认”后创建修订。'
            else:
                raise ValueError('没有可执行的任务操作，请重新说明。')
        except (ValueError, StopIteration) as exc:
            event.agent_reply = str(exc) or '任务已不存在。'
