'use strict';
const $ = s => document.querySelector(s);
const labels = {pending:'等待主播确认',queued:'排队中',running:'执行中',waiting_approval:'等待操作审批',succeeded:'已有执行结果',review:'改动待审核',applied:'文件已应用 · 待重启验证',failed:'未完成',cancelled:'已取消',blocked:'暂不可执行'};
async function action(data){
  const r=await fetch('/tasks-action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  const result=await r.json();if(!r.ok)throw Error(result.error||'请求失败');return result;
}
function node(tag,text){const e=document.createElement(tag);e.textContent=text;return e;}
function button(text,run){const e=node('button',text);e.onclick=async()=>{e.disabled=true;try{await run();$('#notice').textContent='已更新';await refresh();}catch(err){$('#notice').textContent=err.message;}finally{e.disabled=false;}};return e;}
function progressTime(value){const date=new Date(Number(value)*1000);return value&&Number.isFinite(date.getTime())?date.toLocaleString('zh-CN',{hour12:false}):'暂无执行回执';}
function taskProgress(job){
  const p=job.progress;if(!p||!Array.isArray(p.history))return null;
  const box=node('details','');box.open=['running','waiting_approval'].includes(job.state);
  box.append(node('summary','执行进度 · '+(p.label||'等待执行回执')),
    node('p','最近有效进展：'+progressTime(p.last_event_at)));
  if(p.active?.length>1)box.append(node('p',`执行器报告 ${p.active.length} 个步骤尚未结束`));
  const timeline=node('ol','');
  for(const entry of p.history.slice(-24)){
    const event={started:'已开始',completed:'已结束',failed:'步骤未完成',state:'状态回执',resumed:'已恢复'}[entry.event]||'执行回执';
    const code=Number.isInteger(entry.exit_code)?` · 退出码 ${entry.exit_code}`:'';
    timeline.append(node('li',`${progressTime(entry.at)} · ${entry.label} · ${event}${code}${entry.error?' · '+entry.error:''}`));
  }
  box.append(timeline,node('small','工具步骤结束不代表任务完成；整体结果以上方任务状态为准。'));
  return box;
}
let signature='';
const amendments=new Map();
Object.assign(labels,{verified:'新版本服务健康检查通过',rolled_back:'文件已回退 · 待重启'});
const upgradeModes={proposal:'仅记录改进方案',develop:'自动开发，等待应用',auto:'在指定范围内自动升级'};
const upgradeModeHelp={proposal:'收集问题和改进目标，不自动调用 Astra 或修改代码。',develop:'在允许范围内调用 Astra 修改副本并独立验收，等待你决定是否升级。',auto:'在允许范围内开发并独立验收；主服务空闲且升级条件满足后自动升级，失败时尝试恢复旧版本。'};
const upgradeStages={idle:'等待改进任务',proposal:'已记录，等待开启开发',proposed:'已记录，等待开启开发',queued:'开发任务排队中',running:'正在开发副本',developing:'正在开发副本',review:'改动待独立验收',validating:'正在独立验收',validated:'独立验收已通过',ready:'等待升级',waiting_idle:'等待主服务空闲',deploying:'正在切换版本',checking:'正在检查新版本',verified:'新版本服务检查通过',succeeded:'本次处理已完成',completed:'本次处理已完成',failed:'本次处理未完成',blocked:'等待条件满足',cancelled:'已停止',stopped:'已停止',rolling_back:'正在恢复旧版本',rolled_back:'已恢复旧版本',rollback_failed:'恢复旧版本未完成',quota_blocked:'套餐额度不足，已暂停执行'};
const releaseStages={idle:'尚无升级任务',queued:'升级已排队',prepared:'准备升级',stopping:'正在停止旧版本',stopped:'旧版本已停止',applying:'正在应用已验收的改动',starting:'正在启动新版本',verifying:'正在检查新版本',verified:'新版本服务检查通过',rolling_back:'正在恢复旧文件',restarting_previous:'正在启动旧版本',rolled_back:'已恢复旧版本',cancelled:'升级已取消',failed:'升级未完成',recovery_blocked:'旧版本恢复受阻，需要处理'};
upgradeStages.retry='等待限次重试';
upgradeStages.preparing_release='正在核对升级条件';
let upgradePolicyLoaded=false,upgradePolicyDirty=false,upgradeSignature='',upgradeLastRead=0,upgradeReading=false;
function upgradeNotice(message){$('#self-upgrade-notice').textContent=message;}
function upgradePhase(value){return upgradeStages[value]||String(value||'等待处理');}
function upgradeLink(jobId){const link=node('a','查看开发任务与完整改动');link.href='#job-'+encodeURIComponent(jobId);return link;}
function upgradePolicyFromForm(){
 const mode=$('#self-upgrade-mode').value;
 if(!Object.hasOwn(upgradeModes,mode))throw Error('请选择有效的运行方式');
 const paths=[...new Set($('#self-upgrade-paths').value.split(/\r?\n/).map(p=>p.trim().replace(/\\/g,'/')).filter(Boolean))];
 if(paths.some(p=>p==='.'||p.startsWith('/')||p.split('/').includes('..')||/[:*?\x00]/.test(p)))throw Error('请填写项目内的相对文件或目录，不能使用绝对路径、上级目录或通配符');
 if(mode!=='proposal'&&!paths.length)throw Error('自动开发或升级前，请先填写允许修改的文件或目录');
 const bounded=(selector,min,max)=>{const n=Number($(selector).value);if(!Number.isInteger(n)||n<min||n>max)throw Error('请按标注范围填写执行次数和额度保留比例');return n;};
 return {mode,allowed_paths:paths,max_attempts:bounded('#self-upgrade-attempts',1,3),daily_limit:bounded('#self-upgrade-daily',1,10),min_remaining_percent:bounded('#self-upgrade-remaining',5,50)};
}
function renderSelfUpgrade(s){
 if(!s||!s.policy||typeof s.policy!=='object')return false;
 const policy=s.policy;
 $('#self-upgrade-mode-label').textContent=(s.paused?'已停止 · ':'')+(upgradeModes[policy.mode]||'状态未确认');
 const remaining=s.remaining_percent;
 $('#self-upgrade-quota').textContent=typeof remaining==='number'&&Number.isFinite(remaining)?`套餐可用窗口的最低剩余：${Math.max(0,Math.min(100,remaining))}% · 自举保留线：${policy.min_remaining_percent}%${remaining<=policy.min_remaining_percent?' · 暂不开始开发':''}`:'套餐剩余额度尚未确认；确认前不会开始自举开发。';
 if(!upgradePolicyLoaded||!upgradePolicyDirty){
  $('#self-upgrade-mode').value=Object.hasOwn(upgradeModes,policy.mode)?policy.mode:'proposal';
  $('#self-upgrade-paths').value=Array.isArray(policy.allowed_paths)?policy.allowed_paths.join('\n'):'';
  for(const [selector,key,fallback] of [['#self-upgrade-attempts','max_attempts',2],['#self-upgrade-daily','daily_limit',3],['#self-upgrade-remaining','min_remaining_percent',20]])$(selector).value=String(policy[key]??fallback);
  $('#self-upgrade-mode-help').textContent=upgradeModeHelp[$('#self-upgrade-mode').value];upgradePolicyLoaded=true;
 }
 const active=s.active;
 $('#self-upgrade-stage').textContent=s.paused?'自举已停止。检查问题记录后，保存运行设置可重新启用。':active?`当前阶段：${upgradePhase(active.stage)}${active.job_id?' · 任务 '+active.job_id:''}`:(policy.mode==='proposal'?'仅记录问题；不会自动开发或升级。':'当前没有正在处理的改进任务。');
 const release=s.release;
 const releaseState=release&&(release.stage||release.status||release.state);
 $('#self-upgrade-release').textContent=release?`最近升级：${releaseStages[releaseState]||upgradePhase(releaseState)}${release.detail?' · '+release.detail:''}`:'尚无升级记录。';
 const sig=JSON.stringify({issues:s.issues,events:s.events,paused:s.paused,active:s.active});if(sig===upgradeSignature)return true;upgradeSignature=sig;
 const issues=Array.isArray(s.issues)?s.issues:[];
 $('#self-upgrade-issues').replaceChildren(...(issues.length?issues.slice().reverse().map(issue=>{
  const card=node('article','');card.append(node('h3',issue.title||'待确认问题'),node('p',`${upgradePhase(issue.status)} · ${['manual','owner'].includes(issue.source)?'手动提交':issue.source==='runtime'?'运行检查':issue.source||'系统检查'} · 记录 ${issue.count||1} 次`));
  if(issue.detail)card.append(node('p',issue.detail));
  if(issue.job_id){
   const actions=node('div','');actions.className='row';actions.append(upgradeLink(issue.job_id));
   if(!s.paused&&!active&&['review','validated','blocked'].includes(issue.status))actions.append(upgradeButton('运行独立验收',()=>({action:'self_upgrade_validate',id:issue.job_id}),'已提交独立验收，请查看最新阶段。'));
   if(!s.paused&&!active&&issue.status==='validated')actions.append(upgradeButton('升级到此版本',()=>({action:'self_upgrade_deploy',id:issue.job_id}),'已提交升级请求，请查看服务切换和检查结果。'));
   card.append(actions);
  }return card;
 }):[node('p','还没有记录改进问题。可以提交目标，或检查当前运行状态。')]));
 $('#self-upgrade-events').replaceChildren(...(Array.isArray(s.events)?s.events.slice(-20).reverse().map(event=>node('li',`${progressTime(event.at)} · ${event.detail||upgradePhase(event.stage)}`)):[]));
 return true;
}
async function readSelfUpgrade(force=false){
 if(upgradeReading||(!force&&Date.now()-upgradeLastRead<5000))return;
 upgradeReading=true;upgradeLastRead=Date.now();
 try{const s=await action({action:'self_upgrade_status'});if(!renderSelfUpgrade(s))$('#self-upgrade-stage').textContent='当前服务尚未提供自举状态。';}
 catch(err){$('#self-upgrade-stage').textContent='自举状态暂不可用：'+err.message;}
 finally{upgradeReading=false;}
}
async function upgradeAction(data,success){
 const result=await action(data);
 if(result?.ok===false||result?.success===false||result?.error)throw Error(result.error||result.detail||'操作未完成');
 upgradeNotice(result?.detail||success);
 await readSelfUpgrade(true);await refresh();return result;
}
function upgradeButton(text,payload,success){const b=node('button',text);b.type='button';b.onclick=async()=>{b.disabled=true;try{await upgradeAction(payload(),success);}catch(err){upgradeNotice(err.message);}finally{b.disabled=false;}};return b;}
$('#self-upgrade-configure').oninput=()=>{upgradePolicyDirty=true;$('#self-upgrade-mode-help').textContent=upgradeModeHelp[$('#self-upgrade-mode').value]||'';};
$('#self-upgrade-configure').onchange=$('#self-upgrade-configure').oninput;
$('#self-upgrade-configure').onsubmit=async e=>{
 e.preventDefault();const b=e.target.querySelector('button');b.disabled=true;
 try{const policy=upgradePolicyFromForm(),result=await action({action:'self_upgrade_configure',policy});if(result?.ok===false||result?.success===false||result?.error)throw Error(result.error||result.detail||'设置未保存');upgradePolicyDirty=false;upgradeNotice(result?.detail||'运行设置已保存；后续执行以实际阶段为准。');await readSelfUpgrade(true);await refresh();}
 catch(err){upgradeNotice(err.message);}finally{b.disabled=false;}
};
$('#self-upgrade-submit').onsubmit=async e=>{
 e.preventDefault();const b=e.target.querySelector('button');b.disabled=true;
 try{const prompt=$('#self-upgrade-prompt').value.trim();if(!prompt||prompt.length>1800)throw Error('请填写 1 到 1800 字的改进目标');await upgradeAction({action:'self_upgrade_submit',prompt},'改进目标已记录，将按当前运行方式处理。');$('#self-upgrade-prompt').value='';}
 catch(err){upgradeNotice(err.message);}finally{b.disabled=false;}
};
$('#self-upgrade-scan').onclick=async()=>{const b=$('#self-upgrade-scan');b.disabled=true;try{await upgradeAction({action:'self_upgrade_scan'},'检查已完成，请查看记录的问题。');}catch(err){upgradeNotice(err.message);}finally{b.disabled=false;}};
$('#self-upgrade-stop').onclick=async()=>{const b=$('#self-upgrade-stop');b.disabled=true;try{await upgradeAction({action:'self_upgrade_stop'},'自举停止请求已提交，后续自动执行已撤销；请查看当前阶段。');}catch(err){upgradeNotice(err.message);}finally{b.disabled=false;}};
async function refresh(){
 try{
  const r=await fetch('/tasks-status',{cache:'no-store'});if(!r.ok)throw Error('任务服务不可用');const s=await r.json();
  if($('#release-stale'))$('#release-stale').hidden=!s.foreground?.stale;
  const a=s.subscription||{};const limit=a.rateLimits?.primary;
  $('#account').textContent=a.auth==='chatgpt'?`套餐已登录 · Astra ${a.astra?'可用':'不可用'}${limit?` · 当前窗口已用 ${limit.usedPercent}%`:''} · 无 API 回退`:'尚未检查套餐连接；执行前会自动检查。';
  if(a.deepseek)$('#account').textContent+=' · '+a.deepseek.detail;
  if(renderSelfUpgrade(s.self_upgrade))upgradeLastRead=Date.now();else await readSelfUpgrade();
  const next=JSON.stringify(s.jobs);if(next===signature||document.activeElement?.matches?.('#jobs textarea'))return;signature=next;
  $('#jobs').replaceChildren(...(s.jobs||[]).slice().reverse().map(j=>{
    const card=document.createElement('article');card.id='job-'+encodeURIComponent(j.id);const row=node('div','');row.className='row';
    const badge=node('span',labels[j.state]||j.state);badge.className='badge';row.append(badge,node('small',j.id));card.append(row,node('h2',j.prompt),node('p',j.detail));
    if(j.origin)card.append(node('small',`请求人：${j.user||'观众'} · 来源：${j.origin.source} · 原消息：${j.origin.turn_id??'未编号'}`));
    if(j.execution)card.append(node('small','执行方式：'+(j.execution.route==='omp'?'OMP 兼容调度 → Astra':'宿主直接调度 Astra')));
    if(j.parent_id)card.append(node('p','修订自任务：'+j.parent_id));
    if(j.superseded_by)card.append(node('p','后续任务：'+j.superseded_by));
    if(j.delivery)card.append(node('p','通知状态：'+({pending:'待播报',dispatched:'等待客户端开始',playing:'播放中',played:'客户端确认播完',failed:'播放失败',cancelled:'已停止'}[j.delivery.state]||j.delivery.state)+(j.delivery.retry_at?' · 等待有限重试':'')));
    if(j.queue_position)card.append(node('p',`排队位置：${j.queue_position} · 无资源冲突的任务可先执行`));
    if(j.wait_reason&&['queued','running','waiting_approval'].includes(j.state))card.append(node('p','等待原因：'+j.wait_reason));
    const progress=taskProgress(j);if(progress)card.append(progress);
    if(j.verification_check)card.append(node('p','最近版本检查：'+(j.verification_check.detail||({checking:'正在检查',verified:'服务版本检查通过'}[j.verification_check.status]||'待确认'))));
    if(j.budget)card.append(node('small',`任务预算：模型请求 ${j.budget.model_calls||0} 次 · 工具操作 ${j.budget.tool_calls||0} 次`));
    if(!j.superseded_by&&!j.self_upgrade_id){
      const amend=node('details',''),input=node('textarea','');input.maxLength=1200;input.placeholder='补充具体修改要求；执行中的旧任务会停止，随后创建关联修订。';
      input.value=amendments.get(j.id)||'';input.oninput=()=>amendments.set(j.id,input.value);
      amend.append(node('summary','补充或修改这项任务'),input,button('预览并创建后续修订',async()=>{
        const instruction=input.value,p=await action({action:'amend',id:j.id,instruction});
        if(!window.confirm(p.detail+'\n保留产物：'+(p.artifacts.join('、')||'暂无')+'\n修改要求：'+instruction+'\n确认创建修订？'))return;
        await action({action:'amend',id:j.id,instruction,token:p.token});amendments.delete(j.id);
      }));card.append(amend);
    }
    if(j.summary)card.append(node('p',j.summary));if(j.verification){const d=document.createElement('details');d.append(node('summary','执行端验证记录'),node('pre',j.verification));card.append(d);}
    if(j.kind==='drawing'&&j.state==='succeeded'){const link=node('a',j.drawing_stage==='sketch'?'✎ 查看草稿、修改或继续上色':'✎ 打开画布，看她一笔一笔画');link.href='/drawing?job='+encodeURIComponent(j.id);link.target='_blank';link.rel='noopener';card.append(link);}
    if(j.kind==='code'&&j.state==='applied')card.append(button('检查主服务版本',()=>action({action:'verify_code',id:j.id})));
    if(j.kind==='code'&&j.state==='verified'){
      const form=node('details','');form.append(node('summary','记录功能验收（人工实测）'));
      const fields=['实际操作步骤','预期结果','实际观察结果'].map(label=>{const input=node('textarea','');input.placeholder=label;input.maxLength=2000;form.append(input);return input;});
      for(const passed of [true,false])form.append(button(passed?'记录此项通过':'记录此项未通过',()=>action({action:'accept_code',id:j.id,scenario:fields[0].value,expected:fields[1].value,observed:fields[2].value,passed})));
      card.append(form);
    }
    for(const check of j.acceptance||[])card.append(node('p',`人工验收${check.passed?'通过':'未通过'}：${check.scenario}\n预期：${check.expected}\n实际：${check.observed}\n记录对应服务：${check.boot_id}（历史记录，不代表当前版本）`));
    if(j.kind==='code'&&!j.self_upgrade_id&&['applied','verified'].includes(j.state))card.append(button('回退到应用前',async()=>{if(window.confirm('恢复这项任务修改前的文件？存在后续修改时会拒绝覆盖。回退后仍需重启。'))await action({action:'rollback_code',id:j.id});}));
    if(j.kind==='code'&&j.state==='review'){
      const review=node('section','');
      card.append(button('查看代码改动与测试',async()=>{
        const r=await action({action:'review_code',id:j.id});review.replaceChildren();
        review.append(node('p',`改动 ${r.changes.length} 个文件 · ${r.tests_passed?'测试命令已通过':'未取得全部测试通过的记录'} · 尚未应用`));
        if(r.version_valid===false)review.append(node('p','测试版本缺失或项目已变化，请创建修订并重新测试。'));
        review.append(node('pre',r.changes.map(c=>c.operation+' '+c.path).join('\n')));
        const diff=node('details','');diff.append(node('summary','展开逐行差异预览'),node('pre',r.diff||'没有源码改动'));review.append(diff);
        const tests=node('details','');tests.append(node('summary','实际命令、退出码和输出'),node('pre',JSON.stringify(r.commands,null,2)));review.append(tests);
        if(r.syntax_errors.length)review.append(node('pre',r.syntax_errors.join('\n')));
        if(j.self_upgrade_id)review.append(node('p','这是自举候选，请在上方“系统自我改进”中独立验收和升级；不能从普通任务入口应用或修订。'));
        else if(r.can_apply)review.append(button('确认应用这些文件',async()=>{
          if(!window.confirm('应用当前展示的代码改动？会检查原文件是否发生变化并保留原始副本。服务不会自动重启。'))return;
          await action({action:'apply_code',id:j.id,token:r.token});
        }));
        else review.append(node('p','检查未通过，暂不能应用。请新建修复任务。'));
      }),review);
    }
    if(j.state==='pending')card.append(button('批准此任务',()=>action({action:'approve',id:j.id})));
    if(['pending','queued','running','waiting_approval'].includes(j.state))card.append(button('取消',()=>action({action:'cancel',id:j.id})));
    for(const a of j.approvals||[]){const box=node('section','执行器请求：'+a.method);box.append(node('pre',JSON.stringify(a.details,null,2)),button('批准本次操作',()=>action({action:'answer',id:a.id,accept:true})),button('拒绝',()=>action({action:'answer',id:a.id,accept:false})));card.append(box);}
    for(const name of j.artifacts||[]){const url='/task-artifact/'+encodeURIComponent(j.id)+'/'+name.split(/[\\/]/).map(encodeURIComponent).join('/');const link=node('a','打开产物：'+name);link.href=url;link.target='_blank';link.rel='noopener';const p=node('p','');p.append(link);card.append(p);if(/\.(png|jpg|jpeg|webp)$/i.test(name)){const img=document.createElement('img');img.src=url;img.alt=name;card.append(img);}}
    return card;
  }));
 }catch(e){$('#notice').textContent=e.message;}
}
$('#submit').onsubmit=async e=>{e.preventDefault();const b=e.target.querySelector('button');b.disabled=true;try{const j=await action({action:'submit',kind:$('#kind').value,resource_profile:$('#resource-profile').value||'standard',execution_mode:$('#execution-mode')?.value||'auto',prompt:$('#prompt').value});$('#notice').textContent='任务 '+j.id+' 已加入队列';$('#prompt').value='';await refresh();}catch(err){$('#notice').textContent=err.message;}finally{b.disabled=false;}};
$('#check').onclick=async()=>{try{$('#notice').textContent='正在检查…';await action({action:'check'});await refresh();$('#notice').textContent='检查完成';}catch(e){$('#notice').textContent=e.message;}};
$('#stop').onclick=async()=>{try{await action({action:'cancel'});await refresh();$('#notice').textContent='后台任务已停止';}catch(e){$('#notice').textContent=e.message;}};
if($('#release-stale'))$('#release-stale').onclick=async()=>{try{await action({action:'release_stale'});await refresh();$('#notice').textContent='已释放失联会话的资源保留';}catch(e){$('#notice').textContent=e.message;}};
let catalog=[];
$('#tools-check').onclick=async()=>{try{catalog=await action({action:'tools'});$('#tool-name').replaceChildren(...catalog.map(t=>{const o=node('option',t.name+(t.available?'':'（不可用）'));o.value=t.name;o.disabled=!t.available;return o;}));$('#tool-schema').textContent=JSON.stringify(catalog,null,2);}catch(e){$('#tool-result').textContent=e.message;}};
$('#tool-form').onsubmit=async e=>{e.preventDefault();const b=e.target.querySelector('button');b.disabled=true;try{$('#tool-result').textContent=JSON.stringify(await action({action:'call_tool',name:$('#tool-name').value,arguments:JSON.parse($('#tool-args').value)}),null,2);await refresh();}catch(err){$('#tool-result').textContent=err.message;}finally{b.disabled=false;}};
async function poll(){await refresh();setTimeout(poll,1500);}poll();
