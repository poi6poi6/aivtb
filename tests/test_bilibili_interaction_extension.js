'use strict';
// Offline DOM and worker fixtures only. No browser account or network is used.
const test=require('node:test');
const assert=require('node:assert/strict');
const a=require('../tools/bilibili-browser-extension/activity.js');
const interaction=require('../tools/bilibili-browser-extension/activity-interaction.js');
const VIDEO='https://www.bilibili.com/video/BV1B7411m7LV';
const payload=(extra={})=>({expected_uid:'12345',url:VIDEO,action:'comment',text:'这段养护经验很有帮助。',
  interaction_id:'11111111-1111-4111-8111-111111111111',...extra});

class Element {
  constructor(tag='div',attrs={},text='',children=[]) {
    this.tagName=tag.toUpperCase();this.attrs=attrs;this.value=text;this.children=[];
    this.hidden=false;this.clicks=0;this.events=[];this.nodeType=1;this.append(...children);
  }
  append(...children){for(const child of children){child.parentNode=this;this.children.push(child);}}
  get className(){return this.attrs.class||'';}
  get classList(){return {contains:value=>this.className.split(/\s+/).includes(value)};}
  get innerText(){return this.value+this.children.map(child=>child.innerText).join('');}
  get textContent(){return this.innerText;}
  set textContent(value){this.value=value;this.children=[];}
  get href(){return this.attrs.href?new URL(this.attrs.href,'https://www.bilibili.com').href:'';}
  getAttribute(name){return this.attrs[name]??null;}
  getClientRects(){return this.hidden||this.parentNode&&!this.parentNode.getClientRects().length?[]:[{}];}
  focus(){this.focused=true;}
  dispatchEvent(event){this.events.push(event);return true;}
  click(){this.clicks++;this.onClick?.();}
  matches(selector){return selector.split(',').some(part=>{
    const clauses=part.trim().split(/\s+(?![^\[]*\])/);let node=this;
    if(!simple(node,clauses.pop()))return false;
    while(clauses.length){const clause=clauses.pop();node=node.parentNode;
      while(node&&!simple(node,clause))node=node.parentNode;if(!node)return false;}
    return true;
  });}
  querySelectorAll(selector){const out=[];const walk=node=>{for(const child of node.children){
    if(child.matches(selector))out.push(child);walk(child);
  }};walk(this);return out;}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null;}
}
function simple(node,selector){
  const attributes=[...selector.matchAll(/\[([\w-]+)(\*=|=)?(?:"([^"]*)")?\]/g)];
  selector=selector.replace(/\[[^\]]+\]/g,'');
  const tag=selector.match(/^[A-Za-z][A-Za-z0-9-]*/)?.[0];
  if(tag&&node.tagName!==tag.toUpperCase())return false;
  for(const [,name] of selector.matchAll(/\.([\w-]+)/g))if(!node.classList.contains(name))return false;
  for(const [,name] of selector.matchAll(/#([\w-]+)/g))if(node.getAttribute('id')!==name)return false;
  return attributes.every(([,key,op,value])=>{
    const actual=node.getAttribute(key);return actual!==null&&(!op||op==='='&&actual===value||op==='*='&&actual.includes(value));
  });
}
const el=(cls,text='',children=[],attrs={})=>new Element('div',{class:cls,...attrs},text,children);
const link=(uid,cls='user-name',text='作者')=>new Element('a',{class:cls,href:`https://space.bilibili.com/${uid}`},text);
const member=(uid='12345')=>el('header-avatar-wrap','',[link(uid,'header-entry-mini')]);
const document=(children=[],uid='12345')=>new Element('document',{},'',[member(uid),...children]);
function windowFor(url=VIDEO){return {location:{href:url},InputEvent:class{
  constructor(type,options){this.type=type;Object.assign(this,options);}
}};}
function composer({text='',uid='12345',attachments=false}={}){
  const editor=new Element('textarea',{class:'reply-box-textarea'},text),button=el('reply-box-send','发布');
  const box=el('reply-box','',[editor,button,...(attachments?[el('image-preview')]:[])]);
  return {doc:document([box],uid),editor,button,box,win:windowFor()};
}
function comment({uid='12345',text=payload().text,id='908070',time='刚刚',extra=[],author=true}={}){
  const card=el('root-reply-container','',[
    ...(author?[link(uid)]:[]),el('reply-content',text),el('reply-time',time),...extra],{'data-rpid':id});
  return card;
}
function workerFixture({before={prepared:true},proof={status:'completed'},persistFailure=false,cancelAfterPersist=false}={}){
  const calls=[],job={command:{action:'interact',request_id:'22222222-2222-4222-8222-222222222222',payload:payload()}};
  const worker={ledger:{},now:()=>100000,
    hash:async value=>'hash:'+JSON.stringify(value),
    requireCurrent(current){if(current.cancelled)throw 'cancelled';},
    async open(current,url){this.requireCurrent(current);calls.push({op:'open',url});},
    async run(current,operation,p){this.requireCurrent(current);calls.push({op:operation,payload:p});
      if(operation==='interaction-prepare')return before;
      return operation==='interaction-proof'?proof:{submitted:true};
    },
    api:{storage:{local:{async set(value){calls.push({op:'persist',value:structuredClone(value)});
      if(persistFailure)throw new Error('disk unavailable');if(cancelAfterPersist)job.cancelled=true;
    }}}},
  };
  return {worker,job,calls};
}

test('like preparation reports existing state and commit never toggles an existing like off',()=>{
  const button=el('video-like'),doc=document([el('video-toolbar-left','',[button])]);
  const p=payload({action:'like',text:''}),win=windowFor();
  assert.deepEqual(interaction.prepare(doc,win,p,a),{already:false});
  button.onClick=()=>{button.attrs['aria-pressed']='true';};
  interaction.click(doc,win,p,a);assert.equal(button.clicks,1);
  assert.equal(interaction.prepare(doc,win,p,a).already,true);
  interaction.click(doc,win,p,a);assert.equal(button.clicks,1);
  assert.deepEqual(interaction.receipt(doc,VIDEO,p,a),{status:'completed',uid:'12345',url:VIDEO,action:'like',liked:true});
  button.attrs['aria-pressed']='false';assert.deepEqual(interaction.receipt(doc,VIDEO,p,a),{status:'unknown'});
});

test('interaction rechecks identity and current video before preparing, committing or verifying',()=>{
  const p=payload(),f=composer();
  for(const fn of [()=>interaction.prepare(f.doc,windowFor(VIDEO.replace('BV1B','BV2B')),p,a),
    ()=>interaction.click(f.doc,windowFor(VIDEO.replace('BV1B','BV2B')),p,a),
    ()=>interaction.receipt(f.doc,VIDEO.replace('BV1B','BV2B'),p,a)])assert.throws(fn,e=>e==='invalid_request');
  for(const fn of [()=>interaction.prepare(composer({uid:'999'}).doc,f.win,p,a),
    ()=>interaction.click(composer({uid:'999'}).doc,f.win,p,a),
    ()=>interaction.receipt(composer({uid:'999'}).doc,VIDEO,p,a)])assert.throws(fn,e=>e==='account_mismatch');
  const empty=new Element('document');assert.throws(()=>interaction.prepare(empty,f.win,p,a),e=>e==='login_required');
  assert.equal(f.button.clicks,0);
});

test('comments preserve an existing user draft and reject attachment or ambiguous composer controls',()=>{
  const p=payload();
  const busy=composer({text:'用户还没发出的内容'});assert.throws(()=>interaction.prepare(busy.doc,busy.win,p,a),e=>e==='unsafe_composer');
  assert.equal(busy.editor.value,'用户还没发出的内容');assert.equal(busy.button.clicks,0);
  const attached=composer({attachments:true});assert.throws(()=>interaction.prepare(attached.doc,attached.win,p,a),e=>e==='unsafe_composer');
  const duplicate=composer();duplicate.box.append(el('reply-box-send','发送'));
  assert.throws(()=>interaction.prepare(duplicate.doc,duplicate.win,p,a),e=>e==='unsupported_page');
});

test('comment preparation emits input but commit refuses changed text and disabled buttons',()=>{
  const f=composer(),p=payload();assert.deepEqual(interaction.prepare(f.doc,f.win,p,a),{prepared:true});
  assert.equal(f.editor.value,p.text);assert.equal(f.editor.events.length,1);assert.equal(f.editor.events[0].type,'input');
  assert.equal(f.button.clicks,0,'prepare cannot submit');
  f.editor.value+='改动';assert.throws(()=>interaction.click(f.doc,f.win,p,a),e=>e==='unsafe_composer');
  f.editor.value=p.text;f.button.attrs['aria-disabled']='true';
  assert.throws(()=>interaction.click(f.doc,f.win,p,a),e=>e==='unsafe_composer');
  f.button.attrs['aria-disabled']='false';interaction.click(f.doc,f.win,p,a);assert.equal(f.button.clicks,1);
});

test('comment hosts expose controls and receipt evidence from their own shadow root',()=>{
  const p=payload({_since:1000,_now:2000}),win=windowFor();
  const editor=new Element('textarea'),button=new Element('button',{},'发布');
  const box=new Element('bili-comment-box');
  box.shadowRoot=new Element('shadow-root',{},'',[editor,button]);
  const doc=document([box]);
  assert.deepEqual(interaction.roots(box),[box,box.shadowRoot]);
  assert.deepEqual(interaction.commentControl(doc,a),{editor,button});
  assert.deepEqual(interaction.prepare(doc,win,p,a),{prepared:true});
  assert.equal(editor.value,p.text);assert.equal(button.clicks,0);
  interaction.click(doc,win,p,a);assert.equal(button.clicks,1);
  const card=new Element('bili-comment-thread-renderer',{'data-rpid':'908070'});
  card.shadowRoot=new Element('shadow-root',{},'',[link('12345'),el('reply-content',p.text),el('reply-time','刚刚')]);
  doc.append(card);
  assert.equal(interaction.receipt(doc,VIDEO,p,a).comment_id,'908070');
});

test('existing matching comment blocks submission without rewriting the composer',()=>{
  const f=composer(),p=payload();f.doc.append(comment());
  assert.throws(()=>interaction.prepare(f.doc,f.win,p,a),e=>e==='existing_text');assert.equal(f.editor.value,'');
  f.editor.value=p.text;assert.throws(()=>interaction.click(f.doc,f.win,p,a),e=>e==='existing_text');assert.equal(f.button.clicks,0);
});

test('fresh comment receipt requires exact author, body, comment id and timestamp evidence',()=>{
  const p=payload({_since:1000,_now:2000});
  assert.deepEqual(interaction.receipt(document([comment()]),VIDEO,p,a),{status:'completed',uid:'12345',url:VIDEO,
    action:'comment',text:p.text,comment_id:'908070'});
  for(const options of [{uid:'999'},{text:p.text+'多一句'},{id:'not-a-comment'},{time:'昨天'},{time:'2小时前'}]){
    assert.deepEqual(interaction.receipt(document([comment(options)]),VIDEO,p,a),{status:'unknown'});
  }
  assert.deepEqual(interaction.receipt(document([comment()]),VIDEO,payload(),a),{status:'unknown'},'no attempt timestamp is not proof');
});

test('known comment id permits later verification but still requires matching author and content',()=>{
  const p=payload({comment_id:'908070'});
  assert.equal(interaction.receipt(document([comment({time:'昨天'})]),VIDEO,p,a).status,'completed');
  for(const options of [{id:'908071'},{uid:'999'},{text:'其他正文'}]){
    assert.equal(interaction.receipt(document([comment(options)]),VIDEO,p,a).status,'unknown');
  }
});

test('multiple possible comments, mixed author evidence and nested reply bodies cannot satisfy a receipt',()=>{
  const p=payload({_since:1000,_now:2000});
  const ambiguous=document([comment(),comment({id:'908071'})]);
  assert.equal(interaction.receipt(ambiguous,VIDEO,p,a).status,'unknown');
  assert.equal(interaction.receipt(document([comment({extra:[link('999')]})]),VIDEO,p,a).status,'unknown');
  assert.equal(interaction.receipt(document([comment({extra:[el('reply-content','嵌套回复') ]})]),VIDEO,p,a).status,'unknown');
});

test('a profile link inside the comment body cannot substitute for actual author evidence',()=>{
  const p=payload({_since:1000,_now:2000});
  const card=comment({author:false});card.querySelector('.reply-content').append(link('12345','',''));
  assert.equal(interaction.receipt(document([card]),VIDEO,p,a).status,'unknown');
});

test('fallback comment links must belong to the current official video before proving an id',()=>{
  const p=payload({_since:1000,_now:2000});
  const withLink=href=>document([comment({id:'',extra:[new Element('a',{href},'定位评论')]})]);
  for(const href of ['https://evil.invalid/video?rpid=908070',VIDEO.replace('BV1B','BV2B')+'?rpid=908070',
    'https://www.bilibili.com/opus/123456?rpid=908070']){
    assert.equal(interaction.receipt(withLink(href),VIDEO,p,a).status,'unknown');
  }
  assert.equal(interaction.receipt(withLink(VIDEO+'?rpid=908070'),VIDEO,p,a).comment_id,'908070');
});

test('worker persists a durable interaction barrier before any commit and preserves submitted identity',async()=>{
  const f=workerFixture();const result=await interaction.execute(f.worker,f.job,a);
  assert.deepEqual(f.calls.map(x=>x.op),['open','interaction-prepare','persist','interaction-commit','interaction-proof']);
  assert.equal(result.result.status,'completed');assert.equal(f.job.attempted,true);assert.equal(f.job.commitSent,true);
  const item=f.calls.find(x=>x.op==='persist').value.activityPublishLedger['interaction:'+f.job.command.payload.interaction_id];
  assert.equal(item.request_id,f.job.command.request_id);assert.equal(item.attempted_at,100000);
  assert.equal(f.calls.at(-1).payload._since,100000);
});

test('worker does not click an already-liked control and repeated interaction ids never execute twice',async()=>{
  const f=workerFixture({before:{already:true},proof:{status:'completed',liked:true}});
  f.job.command.payload=payload({action:'like',text:''});
  assert.equal((await interaction.execute(f.worker,f.job,a)).result.status,'completed');
  assert(!f.calls.some(x=>x.op==='interaction-commit'));
  const count=f.calls.length;
  const duplicate={command:{...f.job.command,request_id:'33333333-3333-4333-8333-333333333333'}};
  assert.deepEqual(await interaction.execute(f.worker,duplicate,a),{ok:true,result:{status:'unknown'}});
  assert.equal(f.calls.length,count);
});

test('a changed proposal under the same id or a reused request id fails before opening a tab',async()=>{
  const f=workerFixture();await interaction.execute(f.worker,f.job,a);const count=f.calls.length;
  const changed={command:{...f.job.command,payload:{...f.job.command.payload,text:'另一条评论'}}};
  await assert.rejects(interaction.execute(f.worker,changed,a),e=>e==='draft_conflict');assert.equal(f.calls.length,count);
  const reused={command:{...f.job.command,payload:{...f.job.command.payload,interaction_id:'44444444-4444-4444-8444-444444444444'}}};
  await assert.rejects(interaction.execute(f.worker,reused,a),e=>e==='draft_conflict');assert.equal(f.calls.length,count);
});

test('cancelled or undurable jobs cannot click, including cancellation immediately after persistence',async()=>{
  const cancelled=workerFixture();cancelled.job.cancelled=true;
  await assert.rejects(interaction.execute(cancelled.worker,cancelled.job,a),e=>e==='cancelled');assert.equal(cancelled.calls.length,0);
  const disk=workerFixture({persistFailure:true});
  await assert.rejects(interaction.execute(disk.worker,disk.job,a),/disk unavailable/);
  assert(!disk.calls.some(x=>x.op==='interaction-commit'));assert.equal(disk.job.commitSent,undefined);
  const race=workerFixture({cancelAfterPersist:true});
  await assert.rejects(interaction.execute(race.worker,race.job,a),e=>e==='cancelled');
  assert(!race.calls.some(x=>x.op==='interaction-commit'));assert.equal(race.job.commitSent,undefined);
  assert(race.worker.ledger['interaction:'+race.job.command.payload.interaction_id]);
});

test('verification never prepares or commits and missing history cannot trigger a fresh interaction',async()=>{
  const f=workerFixture();f.job.command.action='verify_interaction';
  assert.deepEqual(await interaction.execute(f.worker,f.job,a),{ok:true,result:{status:'unknown'}});assert.equal(f.calls.length,0);
  f.job.command.payload.comment_id='908070';await interaction.execute(f.worker,f.job,a);
  assert.deepEqual(f.calls.map(x=>x.op),['open','interaction-proof']);
  const known=workerFixture();await interaction.execute(known.worker,known.job,a);known.calls.length=0;
  known.job.command.action='verify_interaction';await interaction.execute(known.worker,known.job,a);
  assert.deepEqual(known.calls.map(x=>x.op),['open','interaction-proof']);
});
