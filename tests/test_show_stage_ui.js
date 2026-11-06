const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
class Element{
 constructor(){this.textContent='';this.classes=new Set();this.classList={add:x=>this.classes.add(x),remove:x=>this.classes.delete(x)};}
 set innerHTML(_){throw Error('Stage output must remain literal text');}
}
const elements=new Map(),timers=[],requests=[],root=new Element();
const element=s=>{if(!elements.has(s))elements.set(s,new Element());return elements.get(s);};
let online=true,state={active:true,theme:'庭院',title:'<img src=x onerror=alert(1)>',stage_label:'准备草稿',status:'正在准备草稿',next_label:'看画讨论',pending_count:2,updated_at:1,fresh:true,note:'private note',prompt:'private prompt',detail:'private tools'};
const context=vm.createContext({URLSearchParams,AbortSignal,location:{search:'?clean=1'},setTimeout:fn=>timers.push(fn),
 document:{querySelector:element,documentElement:root},fetch:async(url,options)=>{requests.push({url,options});if(!online)throw Error('offline');return{ok:true,json:async()=>({...state})};}
});
vm.runInContext(fs.readFileSync(path.join(__dirname,'../brain/static/show-stage.js'),'utf8'),context);
(async()=>{
 await new Promise(setImmediate);
 assert(root.classes.has('clean'));
 assert.equal(element('#title').textContent,state.title);
 assert(element('#pending').textContent.includes('2 项'));
 assert(![...elements.values()].some(e=>e.textContent.includes('private')));
 assert.equal(requests[0].url,'/show-stage-status');assert.equal(requests[0].options.method,undefined);
 state={...state,status:'作品进度暂未确认',pending_count:null,fresh:false};await timers.shift()();
 assert(element('#stage-card').classes.has('stale'));
 assert(element('#connection').textContent.includes('过期'));
 assert(element('#pending').textContent.includes('待确认'));
 online=false;await timers.shift()();assert(element('#connection').textContent.includes('连接中断'));
 assert.equal(element('#status').textContent,'作品进度暂未确认');
 online=true;state={...state,active:false,title:'',theme:'',fresh:true,stage_label:'栏目休息中',status:'等待主播开启直播节目单'};await timers.shift()();
 assert.equal(element('#title').textContent,'栏目休息中');assert.equal(element('#pending').textContent,'');
 assert(!element('#stage-card').classes.has('stale'));
 console.log('PASS: read-only OBS stage, clean mode, literal text, no private fields, stale progress and disconnect');
})().catch(e=>{console.error(e);process.exitCode=1;});
