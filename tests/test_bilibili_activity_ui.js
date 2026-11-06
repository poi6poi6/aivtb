'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {ActivityBridge, ActivityPage, safeURL} = require('../brain/static/bilibili-activity.js');
const BASE = '/api/bilibili-activity';
const response = (data, status=200) => ({ok:status>=200&&status<300,status,json:async()=>data});
const deferred = () => {let resolve; const promise=new Promise(done=>{resolve=done;});return {promise,resolve};};
const flush = () => new Promise(setImmediate);

class Element {
  constructor(tag='div') {this.tag=tag;this.children=[];this.dataset={};this.value='';this.checked=false;this.disabled=false;this.hidden=false;this.open=false;this.parent=null;this.textContent='';}
  append(...children) {for(const child of children){child.remove();child.parent=this;this.children.push(child);}}
  insertBefore(child, before) {child.remove();child.parent=this;const index=this.children.indexOf(before);this.children.splice(index<0?this.children.length:index,0,child);}
  replaceChildren(...children) {for(const child of [...this.children])child.remove();this.append(...children);}
  remove() {if(this.parent){this.parent.children=this.parent.children.filter(child=>child!==this);this.parent=null;}}
  focus() {this.focused=true;}
  set innerHTML(_) {throw new Error('Untrusted text must not become HTML');}
}
const tree = node => [node.textContent,...node.children.map(tree)].join(' ');
function fakeWindow(onPost=()=>{}) {
  const listeners=new Map(), posts=[];
  const win={location:{origin:'http://127.0.0.1:8080'},
    addEventListener(type, callback){if(!listeners.has(type))listeners.set(type,new Set());listeners.get(type).add(callback);},
    removeEventListener(type, callback){listeners.get(type)?.delete(callback);},
    postMessage(message,origin){posts.push({message,origin});onPost(message);},
  };
  const emit=(message,extra={})=>{for(const listener of listeners.get('message')||[])listener({data:message,source:win,origin:win.location.origin,...extra});};
  const reply=(id,result={},ok=true)=>emit({source:'ai-vtuber-bilibili-activity-extension',type:'result',request_id:id,ok,...(ok?{result}:{error:result})});
  const pagehide=()=>{for(const callback of [...(listeners.get('pagehide')||[])])callback();};
  return {win,posts,emit,reply,pagehide,listeners};
}
const initial = () => ({enabled:false,auto_publish:false,running:false,status:'idle',error:'',
  settings:{interval_minutes:60,max_items:3,daily_posts:1,expected_uid:'123'},
  browser:{connected:true,account:{uid:'123',name:'测试账号'}},harness:{ready:true,detail:''},notes:[],drafts:[],history:[]});
function pageFixture({state=initial(), action}={}) {
  const elements=new Map(), el=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  const browser=fakeWindow(), calls=[];
  const fetcher=async(url,options)=>{
    const body=options.body?JSON.parse(options.body):null;calls.push({url,body,options});
    if(url===BASE)return response(state);
    if(action)return action(body, state);
    if(body.action==='settings'){
      const {action:_,...settings}=body;
      state.settings=settings;state.enabled=settings.enabled;state.auto_publish=settings.auto_publish;
    }
    return response({ok:true,state});
  };
  const page=new ActivityPage({documentRef:{getElementById:el,createElement:tag=>new Element(tag)},windowRef:browser.win,fetcher});
  return {page,el,state,calls,browser};
}
const postActions = f => f.calls.filter(call=>call.url===BASE+'/action');

test('automatic browsing and publication default off; polling preserves unsaved settings', async t=>{
  const f=pageFixture();t.after(()=>f.page.stop());
  await f.page.refresh();await f.page.refresh();
  assert.equal(f.el('enabled').checked,false);assert.equal(f.el('auto-publish').checked,false);
  assert.equal(postActions(f).length,0);
  f.el('expected-uid').value='456';f.el('settings-form').oninput();
  await f.page.refresh();assert.equal(f.el('expected-uid').value,'456');
  assert.match(f.el('settings-hint').textContent,/未保存/);
  f.el('auto-publish').checked=true;f.el('expected-uid').value='';await f.page.save();
  assert.equal(postActions(f).length,0);assert.match(f.el('notice').textContent,/UID/);
  f.el('expected-uid').value='456';f.el('daily-posts').value='0';await f.page.save();
  assert.equal(postActions(f).length,0);
  f.el('daily-posts').value='2';f.el('interval-minutes').value='9';await f.page.save();
  assert.equal(postActions(f).length,0);
  f.el('interval-minutes').value='30';await f.page.save();
  assert.deepEqual(postActions(f)[0].body,{action:'settings',enabled:false,auto_publish:true,expected_uid:'456',interval_minutes:30,max_items:3,daily_posts:2,
    allow_likes:false,allow_comments:false,daily_interactions:3,active_start_hour:0,active_end_hour:24});
  assert.equal(f.page.dirty,false);assert.match(f.el('saved-mode').textContent,/自动浏览关闭.*自动发布开启/);
  const html=fs.readFileSync(path.join(__dirname,'../brain/static/bilibili-activity.html'),'utf8');
  assert(!/<input[^>]*type="checkbox"[^>]*checked/.test(html));
  assert.match(html,/id="notice"[^>]*aria-live="polite"/);
  assert.match(html,/for="expected-uid"/);
});

test('edits made while saving survive its response, and rejected settings keep their draft', async t=>{
  const pending=deferred();const f=pageFixture({action:()=>pending.promise});t.after(()=>f.page.stop());
  await f.page.refresh();f.el('interval-minutes').value='40';f.el('settings-form').oninput();
  const saving=f.page.save();await flush();
  f.el('interval-minutes').value='50';f.el('settings-form').oninput();
  f.state.settings.interval_minutes=40;pending.resolve(response({ok:true,state:f.state}));await saving;
  assert.equal(f.el('interval-minutes').value,'50');assert.equal(f.page.dirty,true);
  assert.match(f.el('settings-hint').textContent,/新修改/);
  const denied=pageFixture({action:()=>response({ok:false,error:'账号不一致，未保存'})});t.after(()=>denied.page.stop());
  await denied.page.refresh();denied.el('expected-uid').value='999';denied.el('settings-form').oninput();
  await denied.page.save();assert.equal(denied.el('expected-uid').value,'999');
  assert.equal(denied.page.dirty,true);assert.match(denied.el('notice').textContent,/未保存/);
  assert.equal(denied.el('notice').dataset.error,'true');
});

test('manual run and publish work with automatic switches off; unknown results offer verification only', async t=>{
  const f=pageFixture();t.after(()=>f.page.stop());
  f.state.drafts=[{id:'one',state:'draft',text:'一条动态',source_urls:[]},
    {id:'uncertain',state:'unknown',text:'也许已经发布',source_urls:[]}];
  await f.page.refresh();assert.equal(f.el('run').disabled,false);
  await f.el('run').onclick();await f.el('read-account').onclick();
  const item=f.page.cards.get('one');assert.equal(item.buttons.find(b=>b.dataset.action==='publish').disabled,false);
  await item.buttons.find(b=>b.dataset.action==='publish').onclick();
  assert.equal(postActions(f).find(c=>c.body.action==='publish').body.draft_id,'one');
  assert(!/发布成功|已发布/.test(f.el('notice').textContent));
  const unknown=f.page.cards.get('uncertain');
  assert(!unknown.buttons.some(b=>b.dataset.action==='publish'));
  await unknown.buttons.find(b=>b.dataset.action==='verify').onclick();
  assert.deepEqual(postActions(f).map(c=>c.body.action),['run','account','publish','verify']);
  assert.match(tree(unknown.node),/不丢弃这条待核验/);
  assert.deepEqual(unknown.buttons.map(button=>button.dataset.action),['verify']);
  f.state.browser.account.uid='999';await f.page.refresh();
  assert.equal(f.page.cards.get('one').buttons.find(b=>b.dataset.action==='publish').disabled,true);
  assert.match(f.el('account-match').textContent,/账号不一致/);
});

test('draft detail nodes survive polls and failures never claim success', async t=>{
  const f=pageFixture({action:()=>response({error:'本机权限不足'},403)});t.after(()=>f.page.stop());
  f.state.drafts=[{id:'a',state:'draft',text:'<img onerror=alert(1)>',source_urls:['javascript:alert(1)','https://www.bilibili.com/video/BV1B7411m7LV']}];
  f.state.notes=[{id:'n',title:'<script>alert(1)</script>',summary:'<svg>',reaction:'一点看法',url:'file:///secret',evidence_kind:'metadata'}];
  await f.page.refresh();const card=f.page.cards.get('a');card.details.open=true;
  await f.page.refresh();assert.equal(f.page.cards.get('a'),card);assert.equal(card.details.open,true);
  f.state.history.push({at:1700000000,action:'internal-tool-name',detail:'<unsafe>'});await f.page.refresh();
  assert.equal(f.page.cards.get('a'),card);assert.match(tree(card.node),/<img onerror/);
  assert(!tree(card.node).includes('javascript:'));
  assert.match(tree(f.el('notes')),/<script>/);assert(!tree(f.el('notes')).includes('file:///'));
  assert(!tree(f.el('history')).includes('internal-tool-name'));
  await card.buttons.find(b=>b.dataset.action==='publish').onclick();
  assert.equal(f.el('notice').textContent,'本机权限不足');assert.equal(f.el('notice').dataset.error,'true');
  f.state.drafts[0].state='unknown';await f.page.refresh();
  assert.equal(f.page.cards.get('a').details.open,true);
  for(const url of ['javascript:alert(1)','https://bilibili.com.evil.test','https://evil.test','http://www.bilibili.com','https://u:p@www.bilibili.com','https://www.bilibili.com:444/'])assert.equal(safeURL(url),null);
  assert.equal(safeURL('https://t.bilibili.com/123'),'https://t.bilibili.com/123');
});

test('stop remains available during another request and stale responses cannot overwrite it', async t=>{
  const pending=deferred();const f=pageFixture({action:(body,state)=>{
    if(body.action==='run')return pending.promise;
    if(body.action==='stop'){state.status='stopped';return response({ok:true,state});}
  }});t.after(()=>f.page.stop());await f.page.refresh();
  const running=f.el('run').onclick();await flush();assert.equal(f.el('stop').disabled,false);
  await f.el('stop').onclick();assert.match(f.el('notice').textContent,/停止请求/);
  pending.resolve(response({ok:true,state:{...f.state,status:'running',running:true}}));await running;
  assert.equal(f.el('activity-status').textContent,'已停止');assert.match(f.el('notice').textContent,/停止请求/);
});

test('status reads continue during browser work but an older read cannot overwrite its final receipt', async t=>{
  const action=deferred(), read=deferred();const f=pageFixture({action:()=>action.promise});t.after(()=>f.page.stop());
  await f.page.refresh();const original=f.page.fetcher;
  const work=f.page.action('account');await flush();
  f.page.fetcher=(url,options)=>url===BASE?read.promise:original(url,options);
  const refreshing=f.page.refresh();await flush();assert.equal(f.page.refreshing,true);
  action.resolve(response({ok:true,state:{...f.state,status:'账号读取已完成',browser:{connected:true,account:{uid:'123',name:'已确认账号'}}}}));
  await work;assert.equal(f.el('activity-status').textContent,'账号读取已完成');
  read.resolve(response({...f.state,status:'等待浏览器读取',browser:{connected:true,account:{}}}));await refreshing;
  assert.equal(f.el('activity-status').textContent,'账号读取已完成');assert.match(f.el('account-status').textContent,/已确认账号/);
});

function bridgeFixture({onPost, fetcher, commandTimeoutMs=1000, intervalMs=1000}={}) {
  const browser=fakeWindow(message=>onPost?.(message,browser));
  const calls=[], statuses=[];
  const bridge=new ActivityBridge({windowRef:browser.win,commandTimeoutMs,intervalMs,requestTimeoutMs:1000,onStatus:s=>statuses.push(s),
    fetcher:async(url,options)=>{const body=JSON.parse(options.body);calls.push({url,options,body});return fetcher?fetcher(body,calls):response({command:null});}});
  return {...browser,bridge,calls,statuses};
}

test('bridge claims serially and rejects foreign windows, origins, ids and malformed replies', async t=>{
  const f=bridgeFixture({fetcher:body=>response(body.action==='claim'?{command:{id:'c1',action:'read',payload:{url:'https://www.bilibili.com'}}}:{ok:true})});
  t.after(()=>f.bridge.stop());const work=f.bridge.step();await flush();
  await f.bridge.step();assert.equal(f.calls.length,1);assert.equal(f.posts.length,1);
  assert.deepEqual(f.posts[0],{origin:f.win.location.origin,message:{source:'ai-vtuber-bilibili-activity',type:'request',request_id:'c1',action:'read',payload:{url:'https://www.bilibili.com'}}});
  const packet={source:'ai-vtuber-bilibili-activity-extension',type:'result',request_id:'c1',ok:true,result:{title:'页面标题'}};
  f.emit(packet,{origin:'https://evil.test'});f.emit(packet,{source:{}});f.emit({...packet,request_id:'other'});
  f.emit({...packet,ok:'true'});f.emit({...packet,result:'not an object'});await flush();assert.equal(f.calls.length,1);
  f.emit(packet);await work;
  assert.deepEqual(f.calls[1].body,{action:'result',id:'c1',ok:true,result:{title:'页面标题'}});
  assert(f.calls.every(c=>c.options.credentials==='same-origin'&&c.options.headers['Content-Type']==='application/json'));
  f.emit(packet);await flush();assert.equal(f.calls.length,2);
});

test('failed receipt uploads retry only the receipt; repeated command ids never republish', async t=>{
  let receipts=0;
  const f=bridgeFixture({onPost:(message,browser)=>browser.reply(message.request_id,{status:'unknown'}),fetcher:body=>{
    if(body.action==='claim')return response({command:{id:'publish-1',action:'publish',payload:{text:'原文'}}});
    if(++receipts===1)throw new Error('network gone');
    return response({ok:true});
  }});t.after(()=>f.bridge.stop());
  await f.bridge.step();assert.equal(f.posts.length,1);assert(f.bridge.receipt);
  await f.bridge.step();assert.equal(f.posts.length,1);assert.equal(f.bridge.receipt,null);
  assert.deepEqual(f.calls.slice(0,3).map(c=>c.body.action),['claim','result','result']);
  await f.bridge.step();assert.equal(f.posts.length,1,'a duplicated claimed command only resends its original receipt');
  assert.equal(receipts,3);assert(f.statuses.some(s=>s.includes('不会重新执行发布')));
});

test('a receipt already consumed by the server releases the bridge for the next command', async t=>{
  let claims=0, receipts=0;
  const f=bridgeFixture({onPost:(message,browser)=>browser.reply(message.request_id,{uid:'123'}),fetcher:body=>{
    if(body.action==='claim')return response({command:{id:'account-'+(++claims),action:'account',payload:{}}});
    if(++receipts===1)throw new Error('response lost after acceptance');
    return response({ok:receipts!==2});
  }});t.after(()=>f.bridge.stop());
  await f.bridge.step();assert(f.bridge.receipt);
  await f.bridge.step();assert.equal(f.bridge.receipt,null);
  await f.bridge.step();assert.equal(f.posts.length,2);assert.equal(claims,2);
  assert(f.statuses.some(message=>message.includes('已处理或过期')));
});

test('publication timeout stays unknown and a late helper result cannot restart or overwrite it', async t=>{
  const f=bridgeFixture({commandTimeoutMs:5,fetcher:body=>response(body.action==='claim'?{command:{id:'late',action:'publish',payload:{}}}:{ok:true})});
  t.after(()=>f.bridge.stop());await f.bridge.step();
  const receipt=f.calls.find(c=>c.body.action==='result').body;
  assert.equal(receipt.ok,true);assert.equal(receipt.result.status,'unknown');assert.match(receipt.result.detail,/先核验/);
  f.reply('late',{status:'published',url:'https://t.bilibili.com/123'});await flush();assert.equal(f.calls.length,2);
});

test('host cancellation reaches the extension for the exact active request and ignores late results', async t=>{
  const id='11111111-1111-4111-8111-111111111111';
  const f=bridgeFixture({intervalMs:2,fetcher:body=>response(body.active_id
    ? {command:null,cancelled:[body.active_id]} : {command:{id,action:'publish',payload:{}}})});
  t.after(()=>f.bridge.stop());await f.bridge.step();
  assert.deepEqual(f.posts.map(item=>item.message.type),['request','cancel']);
  assert.deepEqual(f.posts[1],{origin:f.win.location.origin,message:{source:'ai-vtuber-bilibili-activity',type:'cancel',request_id:id}});
  assert.equal(f.calls[1].body.active_id,id);
  assert(!f.calls.some(item=>item.body.action==='result'),'cancelled host work is not overwritten with a browser result');
  f.reply(id,{status:'published'});await flush();assert.equal(f.bridge.pending,null);
  assert.equal(f.calls.length,2);
});

test('transient heartbeat failures and another request cancellation never cancel an active publication', async t=>{
  const polled=deferred();let controls=0;
  const f=bridgeFixture({intervalMs:2,fetcher:body=>{
    if(body.active_id){
      if(++controls===1)throw new Error('brief connection loss');
      polled.resolve();return response({command:null,cancelled:['other-request']});
    }
    return response(body.action==='claim'?{command:{id:'still-publishing',action:'publish',payload:{}}}:{ok:true});
  }});t.after(()=>f.bridge.stop());
  const work=f.bridge.step();await polled.promise;
  assert.equal(f.posts.length,1);assert.equal(f.bridge.pending.id,'still-publishing');
  f.reply('still-publishing',{status:'published',url:'https://t.bilibili.com/12345'});await work;
  assert.equal(f.posts.length,1,'network failure cannot become an extension cancel');
  assert.equal(f.calls.find(item=>item.body.action==='result').body.result.status,'published');
});

test('missing helper and unsupported commands are explicit failures, and pagehide ends the bridge', async t=>{
  for(const action of ['read','unsupported_action']){
    const f=bridgeFixture({commandTimeoutMs:5,fetcher:body=>response(body.action==='claim'?{command:{id:action,action,payload:{}}}:{ok:true})});
    t.after(()=>f.bridge.stop());await f.bridge.step();
    assert.equal(f.calls.find(c=>c.body.action==='result').body.ok,false);
    assert.equal(f.posts.length,action==='read'?1:0);
  }
  const f=bridgeFixture({fetcher:body=>response({command:{id:'unload',action:'publish',payload:{}}})});t.after(()=>f.bridge.stop());
  const work=f.bridge.step();await flush();f.pagehide();await work;
  f.reply('unload',{status:'published'});await f.bridge.step();assert.equal(f.calls.length,1);
  assert.equal(f.bridge.pending,null);assert.equal(f.listeners.get('message').size,0);
});

test('older settings gain disabled interaction defaults and edits never enable actions before save', async t=>{
  const f=pageFixture();t.after(()=>f.page.stop());
  f.state.interactions=[{id:'like-one',action:'like',state:'draft',url:'https://www.bilibili.com/video/BV1B7411m7LV'}];
  await f.page.refresh();
  assert.equal(f.el('allow-likes').checked,false);assert.equal(f.el('allow-comments').checked,false);
  assert.equal(f.el('active-start-hour').value,'0');assert.equal(f.el('active-end-hour').value,'24');
  assert.equal(f.el('daily-interactions').value,'3');assert.equal(postActions(f).length,0);
  const button=f.page.interactionCards.get('like-one').buttons[0];assert.equal(button.disabled,true);
  f.el('allow-likes').checked=true;f.el('settings-form').oninput();await f.page.refresh();
  assert.equal(f.el('allow-likes').checked,true);assert.equal(button.disabled,true);
  assert.match(f.el('interaction-policy').textContent,/尚未开启/);
  await f.page.save();assert.equal(button.disabled,false);
  assert.equal(postActions(f).length,1);assert.equal(postActions(f)[0].body.action,'settings');
  assert.match(f.el('interaction-policy').textContent,/每轮结束可自动执行一条/);
  const html=fs.readFileSync(path.join(__dirname,'../brain/static/bilibili-activity.html'),'utf8');
  assert.match(html,/浏览器助手 1\.2\.0/);assert.match(html,/重新加载扩展/);
  assert.match(html,/允许自动评论/);assert.match(html,/手动执行也受开关、账号与每日额度约束/);
});

test('interaction submissions carry only proposal ids and unknown results cannot be resent', async t=>{
  const f=pageFixture();t.after(()=>f.page.stop());
  Object.assign(f.state.settings,{allow_likes:true,allow_comments:false,daily_interactions:3});
  f.state.interactions=[
    {id:'like-one',action:'like',state:'draft',url:'https://www.bilibili.com/video/BV1B7411m7LV'},
    {id:'comment-one',action:'comment',state:'draft',text:'一条评论'},
    {id:'unknown-one',action:'comment',state:'unknown',text:'可能已经发送'},
    {id:'future-one',action:'follow',state:'draft'},
  ];
  await f.page.refresh();const card=f.page.interactionCards.get('like-one');
  assert.equal(card.buttons[0].disabled,false);
  assert.equal(f.page.interactionCards.get('comment-one').buttons[0].disabled,true);
  assert.equal(f.page.interactionCards.get('future-one').buttons.length,0);
  await f.page.refresh();assert.equal(f.page.interactionCards.get('like-one'),card);
  await card.buttons[0].onclick();
  assert.deepEqual(postActions(f)[0].body,{action:'interact',interaction_id:'like-one'});
  assert.match(f.el('notice').textContent,/请求已提交/);assert.doesNotMatch(f.el('notice').textContent,/已确认完成/);
  const unknown=f.page.interactionCards.get('unknown-one');
  assert.deepEqual(unknown.buttons.map(b=>b.dataset.action),['verify_interaction']);
  assert.equal(unknown.buttons[0].disabled,false,'verification remains available when comment switch is off');
  await unknown.buttons[0].onclick();
  assert.deepEqual(postActions(f)[1].body,{action:'verify_interaction',interaction_id:'unknown-one'});
  assert.match(tree(unknown.node),/不重复执行/);
  f.state.browser.account.uid='456';await f.page.refresh();assert.equal(card.buttons[0].disabled,true);
  f.state.browser.account.uid='123';f.state.settings.daily_interactions=0;await f.page.refresh();
  assert.equal(card.buttons[0].disabled,true);assert.match(f.el('interaction-policy').textContent,/额度为 0/);
});

test('diary text and sources stay inert and truncated captions are never called complete', async t=>{
  const f=pageFixture();t.after(()=>f.page.stop());
  f.state.diaries=[{id:'entry',text:'<img onerror=alert(1)> 今天读到花园养护。',at:1700000000,
    source_urls:['javascript:alert(1)','https://www.bilibili.com/video/BV1B7411m7LV']}];
  f.state.notes=[
    {id:'full',title:'字幕轨',evidence_kind:'full_subtitles',subtitle_truncated:true,video_part:1,video_part_count:3},
    {id:'excerpt',title:'字幕节选',evidence_kind:'subtitle_excerpt'},
    {id:'visible',title:'可见字幕',evidence_kind:'visible_subtitles'},
  ];
  await f.page.refresh();const rendered=f.el('diaries').children[0];
  assert.match(tree(rendered),/<img onerror/);assert.doesNotMatch(tree(rendered),/javascript:/);
  assert.match(tree(rendered),/https:\/\/www\.bilibili\.com\/video\//);
  assert.equal(f.el('diary-count').textContent,'1 篇记录');assert.equal(postActions(f).length,0);
  await f.page.refresh();assert.equal(f.el('diaries').children[0],rendered);
  assert.match(tree(f.el('notes')),/字幕轨节选/);assert.match(tree(f.el('notes')),/页面当时显示的字幕/);
  assert.doesNotMatch(tree(f.el('notes')),/已取得的完整字幕轨/);
  f.state.notes[0].subtitle_truncated=false;await f.page.refresh();
  assert.match(tree(f.el('notes')),/已取得的完整字幕轨，未观看视频画面/);
  assert.match(tree(f.el('notes')),/分P范围：P1 \/ 共 3P；字幕仅限此分P/);
});

test('routine fields validate ranges, retain overnight schedules and require an account for enabled interaction', async t=>{
  const f=pageFixture();t.after(()=>f.page.stop());await f.page.refresh();
  f.el('active-start-hour').value='24';await f.page.save();assert.equal(postActions(f).length,0);
  assert.match(f.el('notice').textContent,/开始时间/);
  f.el('active-start-hour').value='20';f.el('active-end-hour').value='25';await f.page.save();
  assert.equal(postActions(f).length,0);assert.match(f.el('notice').textContent,/结束时间/);
  f.el('active-end-hour').value='8';f.el('daily-interactions').value='11';await f.page.save();
  assert.equal(postActions(f).length,0);assert.match(f.el('notice').textContent,/互动上限/);
  f.el('daily-interactions').value='2';f.el('allow-comments').checked=true;f.el('expected-uid').value='';
  await f.page.save();assert.equal(postActions(f).length,0);assert.match(f.el('notice').textContent,/UID/);
  f.el('expected-uid').value='123';await f.page.save();
  assert.equal(postActions(f)[0].body.active_start_hour,20);assert.equal(postActions(f)[0].body.active_end_hour,8);
  assert.equal(postActions(f)[0].body.allow_comments,true);assert.equal(postActions(f)[0].body.daily_interactions,2);
});

test('interaction bridge timeouts stay unknown, repeated commands only resend receipts, and verification is supported', async t=>{
  let command={id:'interaction-once',action:'interact',payload:{interaction_id:'one'}};
  const f=bridgeFixture({commandTimeoutMs:5,fetcher:body=>response(body.action==='claim'?{command}:{ok:true})});
  t.after(()=>f.bridge.stop());await f.bridge.step();
  const result=f.calls.find(c=>c.body.action==='result').body;
  assert.equal(result.ok,true);assert.equal(result.result.status,'unknown');assert.match(result.result.detail,/互动结果可能未知/);
  await f.bridge.step();assert.equal(f.posts.length,1);
  command={id:'verify-only',action:'verify_interaction',payload:{interaction_id:'one'}};
  const verification=f.bridge.step();await flush();
  assert.equal(f.posts[1].message.action,'verify_interaction');
  f.reply('verify-only',{status:'completed'});await verification;
  assert.equal(f.calls.at(-1).body.result.status,'completed');
});
