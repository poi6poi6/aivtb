'use strict';
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
class Element{
 constructor(tag='div'){this.tag=tag;this.children=[];this.textContent='';this.value='';this.disabled=false;}
 append(...children){this.children.push(...children);}
 replaceChildren(...children){this.children=children;}
 querySelector(){return new Element('button');}
 set innerHTML(_){throw Error('Untrusted content must never be assigned to innerHTML');}
}
const nodes=new Map(),el=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
const policy={mode:'proposal',allowed_paths:[],max_attempts:2,daily_limit:3,min_remaining_percent:20};
const upgrade={policy:{...policy},issues:[],active:null,events:[],release:null};
const state={jobs:[],self_upgrade:upgrade};
const calls=[];let actionFailure=null,actionResult={},now=10000;
const ctx=vm.createContext({document:{querySelector:el,createElement:t=>new Element(t)},window:{confirm(){throw Error('No unrelated approval flow should be opened');}},console,setTimeout(){},Date:class extends Date{static now(){return now;}},fetch:async(url,options)=>{
 const payload=options?.body?JSON.parse(options.body):null;calls.push({url,payload});
 if(url==='/tasks-status')return {ok:true,json:async()=>state};
 if(payload.action==='self_upgrade_status')return {ok:true,json:async()=>upgrade};
 if(payload.action==='review_code')return {ok:true,json:async()=>({changes:[{path:'brain/static/face.js',operation:'modify'}],diff:'+test',commands:[],syntax_errors:[],can_apply:true,tests_passed:true,token:'review'})};
 if(actionFailure)return {ok:false,json:async()=>({error:actionFailure})};
 if(payload.action==='self_upgrade_configure'&&!actionResult.error)upgrade.policy=payload.policy;
 return {ok:true,json:async()=>actionResult};
}});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../brain/static/tasks.js'),'utf8'),ctx);
const refresh=()=>vm.runInContext('refresh()',ctx);
const submit=id=>el(id).onsubmit({preventDefault(){},target:new Element('form')});
const textTree=e=>[e.textContent,...e.children.map(textTree)].join(' ');
const findButton=(e,label)=>e.children.flatMap(c=>c.tag==='button'&&c.textContent===label?[c]:findButton(c,label));
const countAction=name=>calls.filter(c=>c.payload?.action===name).length;
(async()=>{
 await new Promise(setImmediate);
 // Passive refresh has no side effects, and the default grants no code paths.
 assert.equal(el('#self-upgrade-mode').value,'proposal');
 assert.equal(el('#self-upgrade-paths').value,'');
 assert.match(el('#self-upgrade-stage').textContent,/不会自动开发或升级/);
 assert(!calls.some(c=>c.payload));
 await refresh();await refresh();assert(!calls.some(c=>c.payload));
 const html=fs.readFileSync(path.join(__dirname,'../brain/static/tasks.html'),'utf8');
 assert.match(html,/<option value="proposal" selected>/);
 assert.match(html,/id="self-upgrade-paths"[^>]*><\/textarea>/);
 assert.match(html,/不购买额度，也不切换付费 API/);

 // Missing scope or an absolute/parent path cannot enable automatic development.
 el('#self-upgrade-mode').value='auto';
 await submit('#self-upgrade-configure');assert.equal(countAction('self_upgrade_configure'),0);
 assert.match(el('#self-upgrade-notice').textContent,/允许修改/);
 for(const bad of ['../brain','C:\\project','/','brain/../../tools','brain/*']){
  el('#self-upgrade-paths').value=bad;await submit('#self-upgrade-configure');
  assert.equal(countAction('self_upgrade_configure'),0);
 }
 el('#self-upgrade-paths').value='brain/static/avatar.js\nbrain/static/avatar.js\ndocs/avatar.md';
 el('#self-upgrade-attempts').value='4';await submit('#self-upgrade-configure');assert.equal(countAction('self_upgrade_configure'),0);
 el('#self-upgrade-attempts').value='2';await submit('#self-upgrade-configure');
 const config=calls.find(c=>c.payload?.action==='self_upgrade_configure').payload;
 assert.deepEqual(config.policy.allowed_paths,['brain/static/avatar.js','docs/avatar.md']);
 assert.equal(config.policy.mode,'auto');assert.equal(config.policy.min_remaining_percent,20);

 // Unsubmitted settings survive status updates. Badge still reflects saved policy.
 el('#self-upgrade-paths').value='docs/draft.md';el('#self-upgrade-configure').oninput();
 await refresh();assert.equal(el('#self-upgrade-paths').value,'docs/draft.md');
 assert.match(el('#self-upgrade-mode-label').textContent,/指定范围/);

 // Manual goal is recorded, not described as deployed. Repeated poll does not resubmit.
 el('#self-upgrade-prompt').value='Fix visible playback counts';await submit('#self-upgrade-submit');
 assert.equal(countAction('self_upgrade_submit'),1);assert.match(el('#self-upgrade-notice').textContent,/已记录/);
 assert.equal(el('#self-upgrade-prompt').value,'');await refresh();assert.equal(countAction('self_upgrade_submit'),1);
 await el('#self-upgrade-scan').onclick();assert.equal(countAction('self_upgrade_scan'),1);
 await el('#self-upgrade-stop').onclick();assert.equal(countAction('self_upgrade_stop'),1);assert.equal(countAction('cancel'),0);
 assert(!/升级成功|已完成升级/.test(el('#self-upgrade-notice').textContent));

 // Titles, event details and IDs remain text, including error responses.
 upgrade.issues=[{id:'i',title:'<img onerror=alert(1)>',source:'<script>',count:2,status:'review',job_id:'job"><img',detail:'<svg onload=alert(1)>'}];
 upgrade.events=[{at:1700000000,detail:'<script>alert(1)</script>'}];
 upgrade.active={issue_id:'i',job_id:'job',stage:'validating'};await refresh();
 assert.match(el('#self-upgrade-stage').textContent,/正在独立验收/);
 assert.equal(findButton(el('#self-upgrade-issues'),'运行独立验收').length,0);
 upgrade.active=null;await refresh();
 const issue=el('#self-upgrade-issues').children[0];assert.match(textTree(issue),/<img onerror=alert\(1\)>/);
 assert.match(textTree(el('#self-upgrade-events')),/<script>alert\(1\)<\/script>/);
 const link=issue.children.find(c=>c.tag==='div').children.find(c=>c.tag==='a');
 assert.equal(link.href,'#job-job%22%3E%3Cimg');
 assert.equal(findButton(issue,'升级到此版本').length,0);
 await findButton(issue,'运行独立验收')[0].onclick();assert.equal(countAction('self_upgrade_validate'),1);
 assert.match(el('#self-upgrade-notice').textContent,/已提交独立验收/);
 upgrade.issues[0].status='validated';await refresh();
 await findButton(el('#self-upgrade-issues'),'升级到此版本')[0].onclick();assert.equal(countAction('self_upgrade_deploy'),1);
 assert.match(el('#self-upgrade-notice').textContent,/已提交升级请求/);
 upgrade.release={state:'recovery_blocked',detail:'文件变化，无法恢复'};await refresh();
 assert.match(el('#self-upgrade-release').textContent,/恢复受阻.*文件变化/);
 assert(!el('#self-upgrade-release').textContent.includes('已恢复旧版本'));
 upgrade.paused=true;await refresh();assert.match(el('#self-upgrade-stage').textContent,/已停止/);
 assert.equal(findButton(el('#self-upgrade-issues'),'升级到此版本').length,0);
 upgrade.paused=false;await refresh();

 // Failed writes preserve drafts and don't report success, including a 200 error envelope.
 actionFailure='额度不足';el('#self-upgrade-prompt').value='keep my goal';await submit('#self-upgrade-submit');
 assert.equal(el('#self-upgrade-notice').textContent,'额度不足');assert.equal(el('#self-upgrade-prompt').value,'keep my goal');
 assert.equal(el('#self-upgrade-submit').disabled,false);
 actionFailure=null;actionResult={ok:false,detail:'当前忙碌，未接受升级'};
 await findButton(el('#self-upgrade-issues'),'升级到此版本')[0].onclick();
 assert.equal(el('#self-upgrade-notice').textContent,'当前忙碌，未接受升级');

 // A self-upgrade candidate cannot escape its independent gate via ordinary controls.
 state.jobs=[{id:'upgrade-job',kind:'code',state:'review',prompt:'reviewed improvement',self_upgrade_id:'issue1'}];
 await refresh();const jobCard=el('#jobs').children[0];
 assert(!textTree(jobCard).includes('补充或修改这项任务'));
 await findButton(jobCard,'查看代码改动与测试')[0].onclick();
 assert.equal(findButton(jobCard,'确认应用这些文件').length,0);
 assert.match(textTree(jobCard),/不能从普通任务入口应用或修订/);
 state.jobs[0].state='verified';await refresh();
 assert.equal(findButton(el('#jobs'),'回退到应用前').length,0);

 // An old server without an embedded status receives only a throttled read action.
 delete state.self_upgrade;now+=5001;await refresh();const reads=countAction('self_upgrade_status');
 await refresh();await refresh();assert.equal(countAction('self_upgrade_status'),reads);
 now+=5001;await refresh();assert.equal(countAction('self_upgrade_status'),reads+1);
 assert.equal(countAction('self_upgrade_scan'),1);
 console.log('PASS: self-upgrade defaults, scope and budget validation, inert rendering, exact action routing, draft retention, truthful failures and read-only polling');
})().catch(e=>{console.error(e);process.exitCode=1;});
