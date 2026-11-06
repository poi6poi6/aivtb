const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
let now=0, online=true, calls=[];
let state={root:'aaaaaaaaaaaa',latest:{id:'aaaaaaaaaaaa',state:'succeeded'},drawing:{job_id:'aaaaaaaaaaaa',version:'v1',stage:'sketch'},
 playback:{session:'s1',revision:0,speed:1,position:0,playing:false}};
let serverAnchor=0;
const clone=x=>JSON.parse(JSON.stringify(x));
function current(){const s=clone(state);if(s.playback.playing)s.playback.position=Math.min(2000,s.playback.position+now-serverAnchor);return s;}
class FakePlayer{
 constructor(){this.time=0;this.duration=2000;}
 reset(){this.time=0;}
 seek(t){this.time=Math.max(0,Math.min(t,this.duration));}
 state(){return{done:this.time>=this.duration,progress:this.time/this.duration,index:0,total:1,phase:'轮廓'};}
}
function page(clean=false){
 const elements=new Map(),frames=new Map(),timers=new Map();let id=0;
 function element(s){if(!elements.has(s))elements.set(s,{disabled:true,value:'',hidden:false});return elements.get(s);}
 const context=vm.createContext({URLSearchParams,location:{search:'?job=aaaaaaaaaaaa'+(clean?'&clean=1':'')},DrawingPlayer:FakePlayer,
  document:{querySelector:element,documentElement:{classList:{add(){},remove(){}}},addEventListener(){}},window:{addEventListener(){}},
  performance:{now:()=>now},requestAnimationFrame:f=>{frames.set(++id,f);return id;},cancelAnimationFrame:i=>frames.delete(i),
  setTimeout:(f,ms)=>{timers.set(++id,f);return id;},clearTimeout:i=>timers.delete(i),console,
  fetch:async(url,options={})=>{
   if(!online)throw Error('offline');
   if(url.startsWith('/drawing-data/'))return{ok:true,json:async()=>({title:'test'})};
   if(url.startsWith('/drawing-state/'))return{ok:true,json:async()=>current()};
   const data=JSON.parse(options.body);calls.push(data);
   if(data.action==='drawing_playback'){
    assert.equal(data.revision,state.playback.revision);
    state=current();serverAnchor=now;state.playback.revision++;
    if(data.operation==='play')state.playback.playing=true;
    if(data.operation==='pause')state.playback.playing=false;
    if(data.operation==='restart'){state.playback.position=0;state.playback.playing=false;}
    if(data.operation==='speed')state.playback.speed=data.speed;
    return{ok:true,json:async()=>clone(state)};
   }
   if(data.action==='finish_drawing'||data.action==='amend')return{ok:true,json:async()=>({token:'preview-token',detail:'保留草稿并生成新版本'})};
   throw Error('unexpected request');
  }});
 vm.runInContext(fs.readFileSync(require('node:path').join(__dirname,'../brain/static/drawing.js'),'utf8'),context);
 return{context,element,frames,timers};
}
(async()=>{
 const main=page(),live=page(true);await new Promise(setImmediate);
 assert.equal(main.element('#play').disabled,false);
 assert.equal(live.element('#revision-panel').hidden,true);
 await main.element('#play').onclick();assert.equal(main.frames.size,1);
 now=700;await live.context.poll();
 assert.equal(live.element('#progress').value,.35,'broadcast follows host timeline');
 await main.element('#play').onclick();assert.equal(main.frames.size,0);
 await live.context.poll();assert.equal(live.frames.size,0);
 now=1000;await live.context.poll();assert.equal(live.element('#progress').value,.35,'pause is shared');
 await main.element('#speed').onchange({target:{value:'2'}});
 assert.equal(state.playback.speed,2,'speed change must not be overwritten by stale snapshot');
 await main.element('#restart').onclick();assert.equal(main.element('#progress').value,0);
 await main.element('#play').onclick();assert.equal(main.frames.size,1);
 await main.element('#play').onclick();assert.equal(main.frames.size,0);
 main.element('#instruction').value='眼睛小一点';await main.element('#finish').onclick();
 assert.equal(main.element('#confirm-change').hidden,false);
 assert.equal(calls.at(-1).action,'finish_drawing');assert(!calls.at(-1).token);
 await main.element('#confirm-change').onclick();assert.equal(calls.at(-1).token,'preview-token');
 online=false;await main.context.poll();assert.equal(main.element('#play').disabled,true);
 assert.equal(main.frames.size,0);assert(main.element('#status').textContent.includes('断开'));
 online=true;await main.context.poll();assert.equal(main.element('#play').disabled,false);
 state.latest.id='bbbbbbbbbbbb';state.drawing={job_id:'bbbbbbbbbbbb',version:'v2',stage:'final'};
 state.playback={session:'s2',revision:0,speed:1,position:0,playing:false};
 await main.context.poll();await live.context.poll();
 assert.equal(main.element('#finish').hidden,true);assert.equal(live.element('#progress').value,0);
 console.log('PASS: shared drawing timeline, pause/restart/speed, staged revision preview, reconnect and final version sync');
})().catch(e=>{console.error(e);process.exitCode=1;});
