const assert=require('node:assert/strict');
const fs=require('node:fs');
const {TrendingTopicsPage,sourceURL}=require('../brain/static/trending-topics.js');
class Element {
  constructor(tag='div'){this.tag=tag;this.children=[];this.value='';this.textContent='';this.checked=false;this.disabled=false;this.dataset={};this.listeners={};}
  set innerHTML(_){throw Error('No dynamic HTML');}
  appendChild(child){this.children.push(child);return child;}
  replaceChildren(...children){this.children=children;}
  addEventListener(name,fn){this.listeners[name]=fn;}
}
const html=fs.readFileSync(require.resolve('../brain/static/companion.html'),'utf8');
const ids=[...html.matchAll(/\bid="([^"]+)"/g)].map(m=>m[1]);
assert.equal(new Set(ids).size,ids.length,'unique IDs across all companion features');
const descend=node=>[node,...node.children.flatMap(descend)];
const now=Date.parse('2026-10-29T06:00:00Z')/1000;
const state=()=>({settings:{enabled:true,interval_hours:6,categories:['anime','meme','finance']},state:'ready',message:'已读取近期资料',last_success_at:now-100,next_refresh_at:now+21000,stale:false,
  topics:[{id:'anime1',title:'<img src=x onerror=bad()>新番',category:'anime',summary:'<script>只是文字</script>',talking_point:'今天的角色设计',published_at:'2026-10-29T01:00:00Z',expires_at:now+5000,sources:[{title:'官方更新',url:'https://anime.example.com/news/1'},{title:'不能打开',url:'javascript:alert(1)'}]}]});
const response=(value,ok=true)=>({ok,json:async()=>value});
const deferred=()=>{let resolve;return{promise:new Promise(r=>{resolve=r;}),get resolve(){return resolve;}};};
function fixture(fetcher){
  const nodes=Object.fromEntries(ids.map(id=>[id,new Element()])),listeners={},timers=new Map();let timerId=0;
  const document={getElementById:id=>nodes[id],createElement:tag=>new Element(tag)};
  const window={addEventListener:(name,fn)=>listeners[name]=fn,setTimeout:(fn,ms)=>{const id=++timerId;timers.set(id,{fn,ms});return id;},clearTimeout:id=>timers.delete(id)};
  const calls=[];let current=state(),failure=false;
  const page=new TrendingTopicsPage({document,window,clock:()=>now,fetcher:async(path,options)=>{calls.push({path,options});return fetcher?fetcher(path,options):response(failure?{error:'暂未更新，输入保留'}:current,!failure);}});
  return{page,nodes,calls,timers,listeners,set(value){current=value;},fail(value){failure=value;}};
}
const event={preventDefault(){}};
(async()=>{
  for(const value of ['javascript:alert(1)','http://site.com','https://user:pass@site.com','https://site.com:8080','https://localhost','https://127.0.0.1','https://0x7f000001','https://[::1]','https://foo.local','/local','https://foo.internal'])assert.equal(sourceURL(value),null,value);
  assert.equal(sourceURL('https://anime.example.com/news/1'),'https://anime.example.com/news/1');
  const f=fixture();await f.page.refresh();
  assert.equal(f.nodes['trends-interval'].value,6);assert(f.nodes['trends-anime'].checked);
  assert.equal(f.calls.length,1);assert.equal(f.calls[0].path,'/trends-status');assert.equal(f.calls[0].options.credentials,'same-origin');
  const rendered=descend(f.nodes['trends-list']);assert(rendered.some(node=>node.textContent==='<script>只是文字</script>'));
  const links=rendered.filter(node=>node.tag==='a');assert.equal(links.length,1);assert.equal(links[0].rel,'noopener noreferrer');
  assert.match(f.nodes['trends-times'].textContent,/香港时间/);
  assert.equal([...f.timers.values()][0].ms,20000);
  console.log('PASS: public source links, inert untrusted text, status-only polling and timezone');

  f.nodes['trends-interval'].value='12';f.nodes['trends-finance'].checked=false;f.nodes['trends-form'].listeners.input();
  await f.page.refresh();assert.equal(f.nodes['trends-interval'].value,'12');assert.equal(f.nodes['trends-finance'].checked,false);
  f.fail(true);await f.nodes['trends-form'].onsubmit(event);assert.equal(f.nodes['trends-interval'].value,'12');assert(f.page.dirty);assert.match(f.nodes['trends-notice'].textContent,/输入保留/);
  f.fail(false);f.set({...state(),settings:{enabled:true,interval_hours:12,categories:['anime','meme']}});await f.page.save();
  const saved=f.calls.filter(call=>call.path==='/trends-settings').at(-1);assert.deepEqual(JSON.parse(saved.options.body),{enabled:true,interval_hours:12,categories:['anime','meme']});assert.equal(saved.options.method,'POST');assert(!f.page.dirty);
  console.log('PASS: polling preserves edited settings; failed saves retain drafts and exact successful payload');

  for(const key of ['anime','meme','finance'])f.nodes['trends-'+key].checked=false;
  let count=f.calls.length;await f.page.save();assert.equal(f.calls.length,count);assert.match(f.nodes['trends-notice'].textContent,/至少/);
  f.nodes['trends-anime'].checked=true;
  for(const interval of ['0','25','2.5','not-a-number']){f.nodes['trends-interval'].value=interval;await f.page.save();assert.equal(f.calls.length,count);}
  console.log('PASS: invalid intervals and empty category selections never reach server');

  for(const snapshot of [{...state(),state:'stale',stale:true},{...state(),last_success_at:now-90000},{...state(),topics:[{...state().topics[0],expires_at:now-1}]}]){f.page.render(snapshot);assert.equal(descend(f.nodes['trends-list']).filter(n=>n.tag==='article').length,0);}
  f.page.render({...state(),state:'failed'});assert.match(f.nodes['trends-list'].children[0].textContent,/缓存.*不是本次刚查到/);assert.equal(descend(f.nodes['trends-list']).filter(n=>n.tag==='article').length,1);
  f.page.render({...state(),settings:{...state().settings,enabled:false},state:'disabled'});assert(f.nodes['trends-refresh'].disabled);assert.equal(descend(f.nodes['trends-list']).filter(n=>n.tag==='article').length,0);
  console.log('PASS: expired data is hidden; failures identify surviving cache and disabled mode does not expose active topics');

  f.page.render(state());f.set({...state(),state:'refreshing'});await f.page.update();assert.equal(f.calls.at(-1).path,'/trends-refresh');assert.equal(f.calls.at(-1).options.body,'{}');assert(f.nodes['trends-refresh'].disabled);assert.equal([...f.timers.values()][0].ms,5000);
  count=f.calls.length;await f.page.update();assert.equal(f.calls.length,count,'no double refresh submission');
  f.set({...state(),state:'ready'});await f.page.refresh();assert(!f.nodes['trends-refresh'].disabled);assert.match(f.nodes['trends-status'].textContent,/近期资料/);
  console.log('PASS: explicit refresh requests only a background job; running status disables repeats and resumes polling');

  f.set({...state(),state:'empty',message:'本轮已完成检索，暂未找到符合范围的新话题。',topics:[],stale:false});
  await f.page.refresh();assert.match(f.nodes['trends-status'].textContent,/已完成检索/);assert.match(f.nodes['trends-list'].children[0].textContent,/这次已经查过/);
  assert.doesNotMatch(f.nodes['trends-list'].children[0].textContent,/过期|失败/);
  console.log('PASS: successful empty searches are not presented as failures or stale data');

  const old=deferred();let getCount=0;
  const race=fixture((path)=>path==='/trends-status'&&getCount++===0?old.promise:Promise.resolve(response({...state(),settings:{...state().settings,interval_hours:10}})));
  const pending=race.page.refresh();race.nodes['trends-enabled'].checked=true;race.nodes['trends-anime'].checked=true;race.nodes['trends-interval'].value='10';await race.page.save();
  old.resolve(response(state()));await pending;assert.equal(race.nodes['trends-interval'].value,10,'late pre-save GET must not revert saved settings');
  console.log('PASS: out-of-order status responses cannot overwrite a newer saved state');

  const slow=deferred();const closing=fixture(()=>slow.promise);const late=closing.page.refresh();closing.listeners.pagehide();slow.resolve(response(state()));await late;
  assert.equal(closing.nodes['trends-list'].children.length,0);assert.equal(closing.timers.size,0);assert(closing.calls[0].options.signal.aborted);
  f.page.stop();race.page.stop();
  console.log('PASS: closing the page aborts pending reads, stops polling and ignores late responses');
})().catch(error=>{console.error(error);process.exitCode=1;});
