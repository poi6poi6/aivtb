const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
class Element{
 constructor(tag='div'){this.tag=tag;this.children=[];this.textContent='';this.value='';}
 append(...items){this.children.push(...items);}
 replaceChildren(...items){this.children=items;}
 querySelector(){return new Element('button');}
}
const elements=new Map();
const el=s=>{if(!elements.has(s))elements.set(s,new Element());return elements.get(s);};
let calls=[];
const state={subscription:{auth:'chatgpt',astra:true,rateLimits:{primary:{usedPercent:23}}},jobs:[
 {id:'safe',state:'succeeded',prompt:'<img src=x onerror=alert(1)>',detail:'done',artifacts:['garden.png']},
 {id:'pending',state:'waiting_approval',prompt:'test',detail:'waiting',approvals:[{id:'approval',method:'command',details:{command:'echo test'}}]}]};
const ctx=vm.createContext({document:{querySelector:el,createElement:t=>new Element(t)},window:{confirm:()=>true},
 fetch:async(url,options)=>{calls.push({url,options});return {ok:true,json:async()=>url==='/tasks-status'?state:options?.body&&JSON.parse(options.body).action==='review_code'?{changes:[{operation:'add',path:'tools/new.py'}],diff:'+example',commands:[],syntax_errors:[],tests_passed:true,can_apply:true,token:'reviewed-version'}:{id:'new'}};},setTimeout(){},console});
vm.runInContext(fs.readFileSync(require('node:path').join(__dirname,'../brain/static/tasks.js'),'utf8'),ctx);
(async()=>{
 await new Promise(setImmediate);
 assert.match(el('#account').textContent,/23%/);
 assert.equal(el('#jobs').children.length,2);
 const card=el('#jobs').children[1];
 assert(card.children.some(e=>e.tag==='h2'&&e.textContent==='<img src=x onerror=alert(1)>'));
 assert(card.children.some(e=>e.tag==='img'&&e.src==='/task-artifact/safe/garden.png'));
 await el('#stop').onclick();
 assert(calls.some(c=>c.options?.body&&JSON.parse(c.options.body).action==='cancel'));
 const approval=el('#jobs').children[0].children.find(e=>e.tag==='section');
 await approval.children.find(e=>e.tag==='button'&&e.textContent==='拒绝').onclick();
 const answer=calls.find(c=>c.options?.body&&JSON.parse(c.options.body).action==='answer');
 assert.equal(JSON.parse(answer.options.body).accept,false);
 state.jobs.push({id:'code',kind:'code',state:'review',prompt:'add tool',detail:'review'});
 await vm.runInContext('refresh()',ctx);
 const code=el('#jobs').children[0];
 assert(!code.children.some(e=>e.textContent==='确认应用这些文件'));
 await code.children.find(e=>e.tag==='button'&&e.textContent==='查看代码改动与测试').onclick();
 const section=code.children.find(e=>e.tag==='section');
 await section.children.find(e=>e.tag==='button'&&e.textContent==='确认应用这些文件').onclick();
 assert(calls.some(c=>c.options?.body&&JSON.parse(c.options.body).action==='apply_code'&&JSON.parse(c.options.body).token==='reviewed-version'));
 state.jobs.push({id:'applied',kind:'code',state:'applied',prompt:'test',detail:'waiting',verification_check:{detail:'连接超时，文件已保留'}});
 state.jobs.push({id:'gpu',kind:'image',state:'queued',prompt:'test',queue_position:1,wait_reason:'语音保留显卡'});
 await vm.runInContext('refresh()',ctx);
 const queued=el('#jobs').children[0],applied=el('#jobs').children[1];
 assert(queued.children.some(e=>(e.textContent||'').includes('排队位置：1')));
 assert(queued.children.some(e=>(e.textContent||'').includes('语音保留显卡')));
 await applied.children.find(e=>e.tag==='button'&&e.textContent==='检查主服务版本').onclick();
 assert(calls.some(c=>c.options?.body&&JSON.parse(c.options.body).action==='verify_code'));
 assert(!calls.some(c=>c.options?.body&&JSON.parse(c.options.body).action==='rollback_code'));
 el('#resource-profile').value='local_gpu';el('#kind').value='image';el('#prompt').value='test';
 await el('#submit').onsubmit({preventDefault(){},target:new Element('form')});
 assert(calls.some(c=>c.options?.body&&JSON.parse(c.options.body).resource_profile==='local_gpu'));
 state.jobs.push({id:'progress',state:'running',prompt:'task',progress:{stage:'testing',label:'运行测试命令',last_event_at:1700000000,active:[{key:'one'},{key:'two'}],history:[
  {stage:'searching',label:'检索资料',event:'completed',at:1699999900},
  {stage:'testing',label:'运行测试命令',event:'started',at:1699999950},
  {stage:'testing',label:'运行测试命令',event:'failed',exit_code:2,error:'执行或连接超时',at:1700000000}
 ]}});
 await vm.runInContext('refresh()',ctx);
 const progress=el('#jobs').children[0].children.find(e=>e.tag==='details'&&e.children.some(c=>c.textContent==='执行进度 · 运行测试命令'));
 assert(progress.open);
 assert(progress.children.some(e=>e.textContent.startsWith('最近有效进展：')&&!e.textContent.includes('暂无')));
 assert(progress.children.some(e=>e.textContent==='执行器报告 2 个步骤尚未结束'));
 const timeline=progress.children.find(e=>e.tag==='ol');
 assert.equal(timeline.children.length,3);
 assert.match(timeline.children[2].textContent,/步骤未完成 · 退出码 2 · 执行或连接超时/);
 assert(!timeline.children.some(e=>/\d+%|预计|测试通过/.test(e.textContent)));
 state.jobs[state.jobs.length-1].state='cancelled';state.jobs[state.jobs.length-1].wait_reason='旧资源等待';state.jobs[state.jobs.length-1].progress.label='任务已停止';
 await vm.runInContext('refresh()',ctx);
 const stopped=el('#jobs').children[0].children.find(e=>e.tag==='details'&&e.children.some(c=>c.textContent==='执行进度 · 任务已停止'));
 assert.equal(stopped.open,false);
 assert(!el('#jobs').children[0].children.some(e=>(e.textContent||'').includes('旧资源等待')));
 console.log('PASS: task results render, text stays inert, image URLs resolve, stop and rejection are dispatched');
})().catch(e=>{console.error(e);process.exitCode=1;});
