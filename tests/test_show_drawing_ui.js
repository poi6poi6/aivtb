const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs'),path=require('node:path');
async function page(ok){
 const elements=new Map(),calls=[],paint=[];
 const element=s=>{if(!elements.has(s))elements.set(s,{hidden:true});return elements.get(s);};
 const context=vm.createContext({URLSearchParams,AbortSignal,Date,location:{search:'?show=closed-show&card=saved-card'},
  document:{querySelector:element},DrawingPlayer:class{constructor(canvas,data){paint.push(data);this.duration=80;}seek(time){paint.push(time);}},
  fetch:async(url,options)=>{calls.push({url,options});return{ok,json:async()=>ok?{document:{title:'<img src=x onerror=alert(1)>'},snapshot:{stage:'sketch'},theme:'第一场',closed:1000,name:''}:{error:'历史作品文件已变化，无法展示收播时版本'}};}
 });
 vm.runInContext(fs.readFileSync(path.join(__dirname,'../brain/static/show-drawing.js'),'utf8'),context);await new Promise(setImmediate);
 return{element,calls,paint};
}
(async()=>{
 const ready=await page(true);
 assert.equal(ready.calls[0].url,'/shows-drawing-data/closed-show/saved-card');
 assert.equal(ready.calls[0].options.method,undefined);
 assert.equal(ready.paint[1],80);
 assert.equal(ready.element('#paper').hidden,false);
 assert.equal(ready.element('#title').textContent,'<img src=x onerror=alert(1)>');
 assert(ready.element('#status').textContent.includes('后续修订不会替换'));
 const changed=await page(false);
 assert.equal(changed.paint.length,0);
 assert.equal(changed.element('#paper').hidden,true);
 assert(changed.element('#status').textContent.includes('文件已变化'));
 console.log('PASS: frozen show artwork GET, final static rendering, literal title, changed artifact rejection');
})().catch(e=>{console.error(e);process.exitCode=1;});
